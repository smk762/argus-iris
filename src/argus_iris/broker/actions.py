"""Lifecycle actions: warm a service, evict a resident. The dashboard and operators ask for them by hand; admission and
idle-kill (later rollout steps) call :func:`evict_resident` themselves.

Warming a service means making it answer again: if its health check fails and it has a container, the node's agent
starts that container (subject to the agent's own allowlist), then iris waits for the health check to pass. If it then
has a ``warm_path`` (argus-http), iris calls that to load its model onto the GPU. This is what brings ComfyUI back
after voluptas's old idle stop left it down.

Evicting a resident frees its VRAM through its adapter (DESIGN.md, "Adapters"): Ollama is sent ``keep_alive: 0`` for
the model, ComfyUI ``POST /free`` (all its models: it is one resident), and an argus-http service its ``evict_path``
(GPU to CPU, kept warm) or ``unload_path``. An external service has no such route: one configured with
``evict_by: stop`` is freed by having its node's agent stop its container, and every other is refused. A busy
resident is refused with 409, never interrupted. iris then re-polls the node until the memory is back by the agent's
NVML view, and reports what the resident still holds: an unloaded
service often keeps its CUDA context (modernbert, about 94 MB) or allocator cache (Miso, about 2 GB), which only a
container restart returns.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Literal, TypeVar

import httpx
import structlog
from argus_cortex.gpu_wire import ResidentActionReply, ResidentBusyReply
from pydantic import BaseModel, ValidationError

from argus_iris.broker.config import ServiceConfig
from argus_iris.broker.residency import RESIDUAL_SLACK_MB, on_gpu
from argus_iris.broker.state import Broker
from argus_iris.models import NodeState, Resident, ResidentSource

log = structlog.get_logger("argus_iris.actions")

WARM_POLL_S = 2.0

_M = TypeVar("_M", ResidentActionReply, ResidentBusyReply)


class ActionError(RuntimeError):
    """The action could not be done. ``status`` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class WarmResult(BaseModel):
    service: str
    node: str
    started_container: bool = False  # the agent started it
    warmed: bool = False  # warm_path was called
    model_state: str | None = None  # from the wake reply (ResidentActionReply), when the service sends one
    up: bool
    waited_s: float = 0.0
    detail: str | None = None


async def _start_container(broker: Broker, svc: ServiceConfig) -> None:
    await container_action(broker, svc, "start")


async def container_action(broker: Broker, svc: ServiceConfig, action: Literal["start", "stop", "restart"]) -> None:
    """Ask the node's agent to start, stop or restart *svc*'s container (the agent's own allowlist still applies)."""
    node = broker.config.nodes[svc.node]
    token = os.environ.get(broker.config.agent_token_env, "")
    if not token:
        raise ActionError(
            f"cannot {action} {svc.container}: {broker.config.agent_token_env} is not set on the broker", 503
        )
    try:
        resp = await broker.client.post(
            f"{node.agent_url}/containers/{svc.container}/{action}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60.0),
        )
    except httpx.HTTPError as exc:
        raise ActionError(f"agent on {svc.node} unreachable: {exc}") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:200]
        raise ActionError(f"agent on {svc.node} refused to {action} {svc.container}: HTTP {resp.status_code} {detail}")


def _reply(resp: httpx.Response, model: type[_M]) -> _M | None:
    """The reply as the resident contract's model, or None when the service does not follow it yet."""
    try:
        return model.model_validate(resp.json())
    except (ValueError, ValidationError):
        return None


def _warm_room(broker: Broker, svc: ServiceConfig) -> None:
    """Refuse a warm the node has no room for: a model loading into a full card fails part way (an OOM that leaves the
    service holding memory in an error state) and can take a running job down with it."""
    state = broker.nodes.get(svc.node)
    if state is None or state.ledger is None:
        return
    base = f"{svc.node}/{svc.id}"
    seen = [mb for rid, mb in broker.peaks.items() if rid == base or rid.startswith(base + "/")]
    want = svc.warm_mb if svc.warm_mb is not None else max(seen, default=0)
    if not want:
        return
    ledger = state.ledger
    held = sum(r.vram_mb for r in state.residents if r.service == svc.id)
    need = want - held
    if need > ledger.admissible_mb:
        raise ActionError(
            f"{svc.node} has {ledger.admissible_mb} MB admissible and warming {svc.id} needs about {need} MB more "
            f"({want} MB warm, {held} MB held): free room first, or pass force=true",
            409,
        )


async def warm_service(
    broker: Broker, service_id: str, *, poll_s: float | None = None, force: bool = False
) -> WarmResult:
    poll_s = WARM_POLL_S if poll_s is None else poll_s
    svc = next((s for s in broker.config.services if s.id == service_id), None)
    if svc is None:
        raise ActionError(f"no service {service_id!r}", 404)
    if svc.adapter == "ollama":
        raise ActionError("an Ollama model is warmed by a lease naming it, not per service", 422)
    if not force:
        _warm_room(broker, svc)
    adapter = broker.adapter(svc.id)
    result = WarmResult(service=svc.id, node=svc.node, up=False)

    reading = await adapter.read(broker.client)
    if not reading.up:
        if not svc.container:
            raise ActionError(f"{svc.id} is down ({reading.error}) and has no container to start", 503)
        await _start_container(broker, svc)
        result.started_container = True
        log.info("warm_started_container", service=svc.id, node=svc.node, container=svc.container)
        started = time.monotonic()
        deadline = started + broker.config.warm_timeout_s
        while not reading.up and time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            reading = await adapter.read(broker.client)
        result.waited_s = round(time.monotonic() - started, 1)
        if not reading.up:
            result.detail = f"started, but not answering after {broker.config.warm_timeout_s:g}s: {reading.error}"
            return result
    result.up = True

    if svc.adapter == "argus-http" and svc.warm_path:
        try:
            resp = await broker.client.post(
                f"{svc.base_url}{svc.warm_path}", headers=svc.headers(), timeout=httpx.Timeout(300.0)
            )
        except httpx.HTTPError as exc:
            raise ActionError(f"{svc.id} {svc.warm_path} failed: {exc}") from exc
        if resp.status_code == 409:
            busy = _reply(resp, ResidentBusyReply)
            running = f"{busy.active} running" if busy else resp.text[:200]
            raise ActionError(f"{svc.id} is busy ({running}): nothing was done; retry after the work finishes", 409)
        if resp.status_code >= 400:
            raise ActionError(f"{svc.id} {svc.warm_path}: HTTP {resp.status_code} {resp.text[:200]}")
        result.warmed = True
        reply = _reply(resp, ResidentActionReply)
        if reply is not None:
            result.model_state = reply.model_state
        else:
            log.warning("resident_off_contract", service=svc.id, route=svc.warm_path, body=resp.text[:200])
    broker.stopped.pop(svc.id, None)  # up again: nothing left for residency to bring back
    if not result.started_container and not result.warmed:
        result.detail = "already up"
    log.info("warm_done", **result.model_dump())
    return result


# --- evict ---------------------------------------------------------------------------------------------------------

EvictMode = Literal["auto", "unload", "evict"]
EVICT_POLL_S = 1.0


class EvictResult(BaseModel):
    resident: str
    node: str
    service: str
    model: str | None = None
    # evict: GPU to CPU, kept warm in host RAM; unload: gone from both; stop: the agent stopped the container
    action: Literal["unload", "evict", "stop"]
    route: str  # what was called on the service (for a stop, the agent's container route)
    before_mb: int
    after_mb: int = 0  # what the resident holds now; 0 when it is gone
    residual_mb: int = 0  # still held although the model left the GPU: a CUDA context or allocator cache
    confirmed: bool = False  # the agent saw the memory come back (or the service reported the model off the GPU)
    model_state: str | None = None
    waited_s: float = 0.0
    reason: str | None = None  # who asked and why (manual, admission, idle-kill)
    detail: str | None = None


def find_resident(broker: Broker, resident_id: str) -> Resident | None:
    return next((r for r in broker.residents() if r.id == resident_id), None)


def _plan(
    svc: ServiceConfig, resident: Resident, mode: EvictMode
) -> tuple[Literal["unload", "evict", "stop"], str, dict | None]:
    """(action, route, JSON body) for evicting *resident* of *svc*."""
    if resident.source == ResidentSource.EXTERNAL:
        if svc.evict_by != "stop":
            raise ActionError(f"{svc.id} is an external service: iris cannot free it through an adapter", 422)
        if mode == "evict":
            raise ActionError(f"{svc.id} has no CPU stage: evicting it stops its container", 422)
        return "stop", f"/containers/{svc.container}/stop", None
    if resident.source == ResidentSource.OLLAMA:
        if mode == "evict":
            raise ActionError("Ollama has no CPU stage: use unload", 422)
        if not resident.model:
            raise ActionError(f"{resident.id}: no model name to unload", 422)
        return "unload", "/api/generate", {"model": resident.model, "keep_alive": 0}
    if resident.source == ResidentSource.COMFYUI:
        if mode == "evict":
            raise ActionError("ComfyUI has no CPU stage: use unload", 422)
        return "unload", "/free", {"unload_models": True, "free_memory": True}
    # argus-http
    if mode in ("auto", "evict") and svc.evict_path:
        return "evict", svc.evict_path, None
    if mode == "evict":
        raise ActionError(f"{svc.id} has no evict_path (GPU to CPU); use unload", 422)
    if not svc.unload_path:
        raise ActionError(f"{svc.id} has no unload_path", 422)
    return "unload", svc.unload_path, None


def _freed(before: NodeState, now: NodeState, resident: Resident) -> bool:
    """NVML confirms it: the GPU's free memory rose by at least half of what the resident held. A service's own word
    (Ollama dropping the model from /api/ps, a model_state of unloaded) is not enough: its runner may still hold it."""
    if not (now.reachable and before.gpu and now.gpu):
        return False
    return now.gpu.free_mb - before.gpu.free_mb >= resident.vram_mb / 2


async def evict_resident(
    broker: Broker,
    resident_id: str,
    *,
    mode: EvictMode = "auto",
    reason: str | None = None,
    confirm_s: float | None = None,
    poll_s: float | None = None,
) -> EvictResult:
    """Free *resident_id*'s VRAM through its adapter and wait (up to *confirm_s*) to see it come back."""
    known = find_resident(broker, resident_id)
    if known is None:
        raise ActionError(f"no resident {resident_id!r}", 404)
    baseline = await broker.refresh(known.node)  # a fresh NVML figure to confirm against, and a fresh busy flag
    resident = next((r for r in baseline.residents if r.id == resident_id), None)
    if resident is None:
        raise ActionError(f"{resident_id} is no longer resident", 404)
    if resident.source == ResidentSource.PROCESS:
        raise ActionError(f"{resident_id} is an observed process: iris has no adapter to evict it", 422)
    svc = next((s for s in broker.config.services if s.id == resident.service), None)
    if svc is None:  # pragma: no cover - a non-process resident always comes from a configured service
        raise ActionError(f"no service {resident.service!r}", 404)
    if resident.busy:
        raise ActionError(f"{resident_id} is busy: nothing was done; retry after the work finishes", 409)
    action, route, body = _plan(svc, resident, mode)

    resp: httpx.Response | None = None
    if action == "stop":
        # Its clients fall back elsewhere (evict_by's premise); iris cannot see its work in flight, so a stop only
        # follows the agent's idle reading: admission and idle-kill both wait for idle_s first.
        await container_action(broker, svc, "stop")
    else:
        try:
            resp = await broker.client.post(
                f"{svc.base_url}{route}", json=body, headers=svc.headers(), timeout=httpx.Timeout(120.0)
            )
        except httpx.HTTPError as exc:
            raise ActionError(f"{svc.id} {route} failed: {exc}") from exc
        if resp.status_code == 409:
            busy = _reply(resp, ResidentBusyReply)
            running = f"{busy.active} running" if busy else resp.text[:200]
            raise ActionError(f"{svc.id} is busy ({running}): nothing was done; retry after the work finishes", 409)
        if resp.status_code >= 400:
            raise ActionError(f"{svc.id} {route}: HTTP {resp.status_code} {resp.text[:200]}")

    result = EvictResult(
        resident=resident.id,
        node=resident.node,
        service=svc.id,
        model=resident.model,
        action=action,
        route=route,
        before_mb=resident.vram_mb,
        reason=reason,
    )
    if resp is not None and svc.adapter == "argus-http":
        reply = _reply(resp, ResidentActionReply)
        if reply is not None:
            result.model_state = reply.model_state
        else:
            log.warning("resident_off_contract", service=svc.id, route=route, body=resp.text[:200])

    confirm_s = broker.config.evict_confirm_s if confirm_s is None else confirm_s
    poll_s = EVICT_POLL_S if poll_s is None else poll_s
    started = time.monotonic()
    while True:
        state = await broker.refresh(resident.node)
        now = next((r for r in state.residents if r.id == resident.id), None)
        if _freed(baseline, state, resident) or time.monotonic() - started >= confirm_s:
            break
        await asyncio.sleep(poll_s)
    result.waited_s = round(time.monotonic() - started, 1)
    result.after_mb = now.vram_mb if now else 0
    result.confirmed = _freed(baseline, state, resident)
    if now is not None and result.model_state is None:
        result.model_state = now.detail.get("model_state")
    # The service took the call: what it holds now is its floor until it loads something again, confirmed or not.
    # Without this, ComfyUI at a few hundred MB after /free (which frees nothing more) would be evicted every minute.
    # Not for an Ollama model still listed unconfirmed, though: it is still loaded (a slow or refused unload), and
    # recording its full size would make it count as evicted, so idle-kill would never try it again.
    # Nor for anything that grew during the wait or says it is on the GPU: a request loaded the model meanwhile
    # (dragon/xtts, 2026-11-21: 146 MB on the CPU, unload sent, 1970 MB back on the GPU, written off as its floor).
    loaded = now is not None and (now.vram_mb > result.before_mb + RESIDUAL_SLACK_MB or on_gpu(now))
    if not loaded and (result.confirmed or now is None or resident.source != ResidentSource.OLLAMA):
        broker.residuals[resident.id] = result.after_mb
    broker.residency.annotate(broker.nodes[resident.node])  # the policy view now knows it is evicted
    if result.confirmed:
        result.residual_mb = result.after_mb
        if result.residual_mb:
            result.detail = f"{result.residual_mb} MB still held (CUDA context or allocator cache): a restart frees it"
    else:
        result.detail = f"memory not seen back after {confirm_s:g}s ({result.after_mb} of {result.before_mb} MB held)"
    log.info("resident_evicted", **result.model_dump())
    return result


async def set_keep_alive(broker: Broker, resident: Resident, keep_alive: str | int) -> None:
    """Set a loaded Ollama model's keep_alive (``-1`` pins it). Ollama resets the model's timer to this.

    The request carries the context the model is loaded with: Ollama reloads a model for a request whose ``num_ctx``
    differs, so a bare call reloaded a model its caller had loaded with a larger context (SingGuard at 16384 on
    dragon), and the caller's next request reloaded it back, about once a minute while it was pinned.
    """
    svc = next(s for s in broker.config.services if s.id == resident.service)
    body: dict = {"model": resident.model, "keep_alive": keep_alive}
    if ctx := resident.detail.get("context_length"):
        body["options"] = {"num_ctx": ctx}
    try:
        resp = await broker.client.post(
            f"{svc.base_url}/api/generate",
            json=body,
            headers=svc.headers(),
            timeout=httpx.Timeout(60.0),
        )
    except httpx.HTTPError as exc:
        raise ActionError(f"{svc.id} keep_alive for {resident.model} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ActionError(f"{svc.id} keep_alive for {resident.model}: HTTP {resp.status_code} {resp.text[:200]}")
