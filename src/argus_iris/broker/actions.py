"""Manual lifecycle actions the dashboard or an operator can ask for. The only one so far is warm.

Warming a service means making it answer again: if its health check fails and it has a container, the node's agent
starts that container (subject to the agent's own allowlist), then iris waits for the health check to pass. If it then
has a ``warm_path`` (argus-http), iris calls that to load its model onto the GPU. This is what brings ComfyUI back
after voluptas's old idle stop left it down.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TypeVar

import httpx
import structlog
from argus_cortex.gpu_wire import ResidentActionReply, ResidentBusyReply
from pydantic import BaseModel, ValidationError

from argus_iris.broker.config import ServiceConfig
from argus_iris.broker.state import Broker

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
    node = broker.config.nodes[svc.node]
    token = os.environ.get(broker.config.agent_token_env, "")
    if not token:
        raise ActionError(
            f"cannot start {svc.container}: {broker.config.agent_token_env} is not set on the broker", 503
        )
    try:
        resp = await broker.client.post(
            f"{node.agent_url}/containers/{svc.container}/start",
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
        raise ActionError(f"agent on {svc.node} refused to start {svc.container}: HTTP {resp.status_code} {detail}")


def _reply(resp: httpx.Response, model: type[_M]) -> _M | None:
    """The reply as the resident contract's model, or None when the service does not follow it yet."""
    try:
        return model.model_validate(resp.json())
    except (ValueError, ValidationError):
        return None


async def warm_service(broker: Broker, service_id: str, *, poll_s: float | None = None) -> WarmResult:
    poll_s = WARM_POLL_S if poll_s is None else poll_s
    svc = next((s for s in broker.config.services if s.id == service_id), None)
    if svc is None:
        raise ActionError(f"no service {service_id!r}", 404)
    if svc.adapter == "ollama":
        raise ActionError("an Ollama model is warmed by a lease naming it, not per service", 422)
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
    if not result.started_container and not result.warmed:
        result.detail = "already up"
    log.info("warm_done", **result.model_dump())
    return result
