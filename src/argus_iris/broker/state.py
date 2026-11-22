"""The broker's view of every node: agent snapshots and adapter readings composed into residents and a ledger.

Observe-only: nothing here admits, evicts or warms. Leases are kept in :class:`~argus_iris.broker.leases.LeaseBook`,
which the broker ticks every second and whose changes go out on the same subscriber queues as node changes. :func:`compose_node` is pure so the accounting can be tested
without a network.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
from collections.abc import AsyncIterator

import httpx
import structlog

from argus_iris.broker.adapters import ServiceReading, build_adapter
from argus_iris.broker.config import BrokerConfig, NodeConfig, ServiceConfig
from argus_iris.broker.leases import LeaseBook, LeaseRecord
from argus_iris.broker.policy import Policy
from argus_iris.broker.residency import Residency
from argus_iris.models import (
    GpuProcess,
    NodeLedger,
    NodeSnapshot,
    NodeState,
    Resident,
    ResidentSource,
    ServiceState,
    utcnow,
)

log = structlog.get_logger("argus_iris.broker")

TICK_S = 1.0
Event = tuple[str, NodeState | LeaseRecord]  # ("node", state) or ("lease", record), as the SSE stream names them


def _min_idle(procs: list[GpuProcess]) -> float | None:
    return min((p.idle_s for p in procs), default=None)


def _service_residents(
    node: str, svc: ServiceConfig, reading: ServiceReading, procs: list[GpuProcess]
) -> list[Resident]:
    base_id = f"{node}/{svc.id}"
    proc_mb = sum(p.vram_mb for p in procs)
    if reading.busy:
        idle = None
    elif reading.idle_s is not None:
        idle = reading.idle_s  # the service knows when it last served a request
    else:
        idle = _min_idle(procs)
    if svc.adapter == "ollama" and reading.models:
        return [
            Resident(
                id=f"{base_id}/{m.name}",
                node=node,
                service=svc.id,
                source=ResidentSource.OLLAMA,
                model=m.name,
                vram_mb=m.vram_mb,
                idle_s=idle,  # per process: Ollama does not say which model ran last
                expires_at=m.expires_at,
                detail=m.detail,
            )
            for m in reading.models
        ]
    if not procs:
        return []  # nothing on the GPU (or no container configured to match it by)
    return [
        Resident(
            id=base_id,
            node=node,
            service=svc.id,
            source=ResidentSource(svc.adapter),
            vram_mb=proc_mb,
            idle_s=idle,
            busy=reading.busy,
            detail={"pids": sorted(p.pid for p in procs), **reading.detail},
        )
    ]


def compose_node(
    name: str,
    cfg: NodeConfig,
    services: list[tuple[ServiceConfig, ServiceReading]],
    snapshot: NodeSnapshot | None,
    *,
    agent_error: str | None = None,
    last_seen=None,
) -> NodeState:
    state = NodeState(
        name=name,
        agent_url=cfg.agent_url,
        reachable=snapshot is not None,
        last_seen=last_seen,
        error=agent_error or (snapshot.nvml_error if snapshot else None),
    )
    state.services = [
        ServiceState(
            id=svc.id,
            node=name,
            adapter=svc.adapter,
            base_url=svc.base_url,
            up=r.up,
            error=r.error,
            queue_running=r.queue_running,
            queue_pending=r.queue_pending,
            available=r.available,
        )
        for svc, r in services
    ]
    if snapshot is None:
        # No agent: Ollama still says what it holds, so keep those; everything else needs the agent's numbers.
        for svc, r in services:
            if svc.adapter == "ollama":
                state.residents.extend(_service_residents(name, svc, r, []))
        return state

    state.host = snapshot.host
    state.gpu = next((g for g in snapshot.gpus if g.index == cfg.gpu), None)
    procs = [p for p in snapshot.processes if p.gpu == cfg.gpu]

    anon = {c.container: c.anon_mb for c in snapshot.containers}
    claimed: set[int] = set()
    managed_mb = 0
    for svc, reading in services:
        mine = [p for p in procs if svc.container and p.container == svc.container]
        claimed.update(p.pid for p in mine)
        residents = _service_residents(name, svc, reading, mine)
        if svc.container in anon:
            for r in residents:
                r.host_ram_mb = anon[svc.container]
        if svc.adapter == "ollama" and reading.models:
            # The runner processes can hold more than the models' share (CUDA context, KV cache): count the larger.
            managed_mb += max(sum(r.vram_mb for r in residents), sum(p.vram_mb for p in mine))
        else:
            managed_mb += sum(r.vram_mb for r in residents)
        state.residents.extend(residents)

    # GPU processes no configured service claims: observed tenants (Immich, the desktop, strays).
    groups: dict[str, list[GpuProcess]] = {}
    for p in procs:
        if p.pid not in claimed:
            groups.setdefault(p.service or p.container or f"pid-{p.pid}", []).append(p)
    for key, group in sorted(groups.items()):
        state.residents.append(
            Resident(
                id=f"{name}/{key}",
                node=name,
                service=key,
                source=ResidentSource.PROCESS,
                vram_mb=sum(p.vram_mb for p in group),
                idle_s=_min_idle(group),
                detail={"pids": sorted(p.pid for p in group), "container": group[0].container},
            )
        )

    if state.gpu:
        g = state.gpu
        resident_mb = sum(r.vram_mb for r in state.residents)
        state.ledger = NodeLedger(
            total_mb=g.total_mb,
            free_mb=g.free_mb,
            reserve_mb=cfg.reserve_mb,
            headroom_mb=cfg.headroom_mb,
            resident_mb=resident_mb,
            unattributed_mb=max(0, g.used_mb - resident_mb),
            # The design's inequality (managed residents + reserve + headroom <= total), and NVML free as well,
            # which catches whatever iris does not know about.
            admissible_mb=max(
                0, min(g.free_mb - cfg.headroom_mb, g.total_mb - cfg.reserve_mb - cfg.headroom_mb - managed_mb)
            ),
        )
    return state


def room_for_services(
    state: NodeState,
    cfg: NodeConfig,
    services: list[ServiceConfig],
    held: dict[str, Resident],
    peaks: dict[str, int],
    pinned: set[str],
) -> list[ServiceState]:
    """Mark each service on *state* that cannot fit beside what admission can never free (#40); return those.

    *held* maps each such resident (pinned, a dropped pin awaiting restore included; a process iris has no adapter
    for; what an unloaded service keeps) to it as it holds the most, and *pinned* names the pinned ones. A service's
    need is its ``warm_mb``, else the most iris has seen it hold; an Ollama service has none unless set, since what it
    takes depends on the model. What the service holds itself counts toward its need, not against its room. Pure.
    """
    if state.ledger is None:
        return []
    led = state.ledger
    base = led.total_mb - cfg.reserve_mb - cfg.headroom_mb
    led.pinned_mb = sum(r.vram_mb for rid, r in held.items() if rid in pinned)
    led.held_mb = sum(r.vram_mb for r in held.values())
    led.room_mb = max(0, base - led.held_mb)
    by_id = {s.id: s for s in services}
    crowded: list[ServiceState] = []
    for st in state.services:
        svc = by_id.get(st.id)
        if svc is None:
            continue
        if svc.warm_mb is not None:
            st.need_mb = svc.warm_mb
        elif svc.adapter != "ollama":
            st.need_mb = peaks.get(f"{state.name}/{svc.id}")
        if not st.need_mb:
            continue
        st.room_mb = max(0, base - sum(r.vram_mb for r in held.values() if r.service != svc.id))
        st.crowded_out = st.need_mb > st.room_mb
        if st.crowded_out:
            crowded.append(st)
    return crowded


def _volatile_free(state: NodeState) -> dict:
    """A node's state without the fields that change every poll, to tell real changes from ticks."""
    data = state.model_dump(mode="json", exclude={"last_seen"})
    for r in data["residents"]:
        r.pop("idle_s", None)
        if r.get("policy"):
            r["policy"] = {k: v for k, v in r["policy"].items() if k not in ("idle_s", "due_in_s")}
    if data.get("host"):
        data["host"].pop("available_mb", None)
    if data.get("gpu"):
        data["gpu"].pop("util_pct", None)
    return data


class Broker:
    def __init__(
        self, config: BrokerConfig, *, client: httpx.AsyncClient | None = None, policy: Policy | None = None
    ) -> None:
        self.config = config
        self.policy = policy or Policy()
        # What a confirmed eviction left each resident holding. Until it grows past that (a model loaded again) the
        # resident counts as evicted: ComfyUI keeps a few hundred MB after /free and says nothing about its models.
        self.residuals: dict[str, int] = {}
        # The most VRAM each resident has been seen holding, so a manual warm can tell whether it would fit.
        self.peaks: dict[str, int] = {}
        # External services admission stopped for room (evict_by: stop) -> the lease it was for. Residency warms each
        # again once no lease is active on its node and the node can admit it; a manual or idle-kill stop is not here.
        self.stopped: dict[str, str] = {}
        self.residency = Residency(self, self.policy)
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.agent_timeout_s))
        self._adapters = {s.id: build_adapter(s) for s in config.services}
        self.nodes: dict[str, NodeState] = {
            name: NodeState(name=name, agent_url=cfg.agent_url, reachable=False) for name, cfg in config.nodes.items()
        }
        self._last_seen: dict[str, object] = {}
        self._crowded: dict[str, set[str]] = {}  # node -> services flagged crowded out
        self._fingerprints: dict[str, dict] = {}
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self.polls = 0
        self.leases = LeaseBook(
            config,
            lambda: self.nodes,
            on_change=lambda rec: self._publish(("lease", rec.model_copy())),
            evict=self._start_eviction,
            pinned=self.residency.protected,
            policy=self.policy,
        )
        self._tasks: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch_snapshot(self, cfg: NodeConfig) -> tuple[NodeSnapshot | None, str | None]:
        try:
            resp = await self._client.get(f"{cfg.agent_url}/snapshot")
            resp.raise_for_status()
            return NodeSnapshot.model_validate(resp.json()), None
        except Exception as exc:  # noqa: BLE001 - HTTP, JSON and validation failures all mean "agent unusable"
            return None, f"{type(exc).__name__}: {exc}"[:300]

    async def _poll_node(self, name: str, cfg: NodeConfig) -> NodeState:
        services = self.config.services_on(name)
        snap_result, *readings = await asyncio.gather(
            self._fetch_snapshot(cfg), *(self._adapters[s.id].read(self._client) for s in services)
        )
        snapshot, error = snap_result
        if snapshot is not None:
            self._last_seen[name] = utcnow()
        return compose_node(
            name,
            cfg,
            list(zip(services, readings, strict=True)),
            snapshot,
            agent_error=error,
            last_seen=self._last_seen.get(name),
        )

    def _apply(self, name: str, state: NodeState) -> None:
        for r in state.residents:
            r.tenant = self.leases.tenant_of(r)
        self.residency.annotate(state)
        for r in state.residents:
            if r.vram_mb > self.peaks.get(r.id, 0):
                self.peaks[r.id] = r.vram_mb
            r.peak_mb = self.peaks.get(r.id)
        self._check_room(name, state)
        was = self.nodes.get(name)
        if was and was.reachable != state.reachable:
            log.info("agent_reachability", node=name, reachable=state.reachable, error=state.error)
        self.nodes[name] = state
        fingerprint = _volatile_free(state)
        if self._fingerprints.get(name) != fingerprint:
            self._fingerprints[name] = fingerprint
            self._publish(("node", state))

    def _held_on(self, state: NodeState) -> tuple[dict[str, Resident], set[str]]:
        """What admission can never free on *state*'s node, each resident as it holds the most, and which are pins.

        Pinned residents, and a pin that dropped out and residency will load again; a process with no adapter and no
        tenant (Frigate: a tenant's own process comes and goes with its work); an external service iris may not stop;
        and the context an unloaded, CPU-parked or failed service keeps, which only a restart returns.
        """
        held: dict[str, Resident] = {}
        pinned: set[str] = set()
        stoppable = {s.id for s in self.config.services_on(state.name) if s.evict_by}
        for r in state.residents:
            if self.residency.pinned(r) or any(fnmatch.fnmatchcase(r.id, pat) for pat in self.config.pinned):
                pinned.add(r.id)
            elif not (
                (r.source == ResidentSource.PROCESS and r.tenant is None)
                or (r.source == ResidentSource.EXTERNAL and r.service not in stoppable)
                or r.detail.get("model_state") in ("unloaded", "cpu", "error")
                or self.residency.evicted(r)
            ):
                continue
            held[r.id] = r
        for rid, memo in self.residency.pins.items():
            if memo.resident.node != state.name or not self.residency.pinned(memo.resident):
                continue
            pinned.add(rid)
            if rid not in held or memo.resident.vram_mb > held[rid].vram_mb:
                held[rid] = memo.resident
        return held, pinned

    def _check_room(self, name: str, state: NodeState) -> None:
        """Flag services that cannot fit beside what the node can never free (#40), logging when that changes."""
        if not state.reachable:
            return  # no ledger this poll: keep the last verdict rather than flap
        held, pinned = self._held_on(state)
        crowded = room_for_services(
            state, self.config.nodes[name], self.config.services_on(name), held, self.peaks, pinned
        )
        now = {st.id: st for st in crowded}
        was = self._crowded.get(name, set())
        for sid in now.keys() - was:
            st = now[sid]
            biggest = sorted(held.values(), key=lambda r: -r.vram_mb)[:4]
            log.warning(
                "service_crowded_out",
                node=name,
                service=sid,
                need_mb=st.need_mb,
                room_mb=st.room_mb,
                held_by={r.id: r.vram_mb for r in biggest if r.service != sid},
            )
        for sid in was - now.keys():
            log.info("service_crowded_out_cleared", node=name, service=sid)
        self._crowded[name] = set(now)

    def crowded(self) -> list[dict]:
        """Services that cannot fit beside what their node can never free (#40), for /health."""
        return [
            {"node": name, "service": st.id, "need_mb": st.need_mb, "room_mb": st.room_mb}
            for name, state in self.nodes.items()
            for st in state.services
            if st.crowded_out
        ]

    async def poll_once(self) -> None:
        names = list(self.config.nodes)
        states = await asyncio.gather(*(self._poll_node(n, self.config.nodes[n]) for n in names))
        for name, state in zip(names, states, strict=True):
            self._apply(name, state)
        self.polls += 1

    async def refresh(self, name: str) -> NodeState:
        """Poll one node now (after an action, to see its effect) and publish the change."""
        state = await self._poll_node(name, self.config.nodes[name])
        self._apply(name, state)
        return state

    async def run(self) -> None:
        await asyncio.gather(self._poll_loop(), self._tick_loop(), self._residency_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - keep polling; the failure is logged
                log.exception("broker_poll_failed")
            await asyncio.sleep(self.config.poll_s)

    async def _residency_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.residency_s)
            try:
                await self.residency.step()
            except Exception:  # noqa: BLE001 - a failed step must not stop residency for good
                log.exception("residency_step_failed")

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.leases.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not stop expiry for good
                log.exception("lease_tick_failed")

    def _start_eviction(self, lease_id: str, resident_ids: list[str]) -> None:
        task = asyncio.get_running_loop().create_task(self._evict_for(lease_id, resident_ids), name=f"evict-{lease_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _evict_for(self, lease_id: str, resident_ids: list[str]) -> None:
        """Admission's eviction plan, one resident at a time; then let the lease book serve its queue again."""
        from argus_iris.broker.actions import ActionError, evict_resident  # noqa: PLC0415 - actions imports this module

        failed: list[str] = []
        try:
            for rid in resident_ids:
                rec = self.leases.leases.get(lease_id)
                if rec is None or rec.state != "queued":
                    break  # released, expired or refused meanwhile: evicting more would free memory for nobody
                try:
                    result = await evict_resident(self, rid, reason=f"admission: lease {lease_id}")
                except ActionError as exc:
                    log.warning("admission_evict_failed", lease=lease_id, resident=rid, error=str(exc))
                    failed.append(rid)
                else:
                    if not result.confirmed:
                        failed.append(rid)
                    elif result.action == "stop":
                        self.stopped[result.service] = lease_id  # residency starts it again once the room is free
        finally:
            self.leases.evictions_done(lease_id, failed)

    def _publish(self, event: Event) -> None:
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def adapter(self, service_id: str):
        return self._adapters[service_id]

    def residents(self) -> list[Resident]:
        return [r for state in self.nodes.values() for r in state.residents]
