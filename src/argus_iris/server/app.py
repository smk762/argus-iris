"""The broker's FastAPI app: node state, residents, leases, an SSE change stream and Prometheus metrics.

Serve with ``argus-iris serve`` or ``uvicorn --factory argus_iris.server.app:create_app``. The lease routes are
DESIGN.md's "API (v1)" with argus-cortex's wire models; they grant without admission for now (observe-only, see
:mod:`argus_iris.broker.leases`). Every write passes the shared WriteGuard. The lease routes take
``Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN`` when the broker has that token set, and are open when it does not;
manual actions (warm) are disabled until it is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from argus_cortex.gpu_wire import (
    Heartbeat,
    HeartbeatReply,
    LeaseRelease,
    LeaseRequest,
    LeaseStatus,
    TenantManifest,
)
from argus_cortex.server import WriteGuard, cross_site_refuse
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

import argus_iris
from argus_iris.broker.actions import ActionError, EvictMode, EvictResult, WarmResult, evict_resident, warm_service
from argus_iris.broker.config import BrokerConfig, load_config
from argus_iris.broker.leases import LeaseConflict, LeaseRecord
from argus_iris.broker.policy import Policy, load_policy
from argus_iris.broker.state import Broker
from argus_iris.models import NodeState, Resident

log = structlog.get_logger("argus_iris.server")

SSE_KEEPALIVE_S = 15.0
MAX_WAIT_S = 60.0  # longest a GET /leases/{id} is held; the client asks for 20 s
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class _BrokerCollector:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def collect(self):
        up = GaugeMetricFamily("iris_agent_up", "1 when the node's agent answered the last poll", labels=["node"])
        total = GaugeMetricFamily("iris_node_vram_total_mb", "Total VRAM", labels=["node"])
        free = GaugeMetricFamily("iris_node_vram_free_mb", "Free VRAM (NVML)", labels=["node"])
        admissible = GaugeMetricFamily(
            "iris_node_vram_admissible_mb", "VRAM a new lease could get without evicting", labels=["node"]
        )
        unattributed = GaugeMetricFamily(
            "iris_node_vram_unattributed_mb", "Used VRAM no resident accounts for", labels=["node"]
        )
        ram = GaugeMetricFamily("iris_node_host_ram_available_mb", "Host MemAvailable", labels=["node"])
        rlabels = ["node", "resident", "service", "source", "model"]
        rvram = GaugeMetricFamily("iris_resident_vram_mb", "VRAM held by a resident", labels=rlabels)
        ridle = GaugeMetricFamily(
            "iris_resident_idle_seconds", "Seconds since the resident was last busy", labels=rlabels
        )
        svc_up = GaugeMetricFamily(
            "iris_service_up", "1 when the service's adapter read succeeded", labels=["node", "service"]
        )
        queue = GaugeMetricFamily("iris_service_queue", "Service queue depth", labels=["node", "service", "state"])
        for state in self.broker.nodes.values():
            up.add_metric([state.name], 1.0 if state.reachable else 0.0)
            if state.ledger:
                total.add_metric([state.name], state.ledger.total_mb)
                free.add_metric([state.name], state.ledger.free_mb)
                admissible.add_metric([state.name], state.ledger.admissible_mb)
                unattributed.add_metric([state.name], state.ledger.unattributed_mb)
            if state.host:
                ram.add_metric([state.name], state.host.available_mb)
            for r in state.residents:
                labels = [r.node, r.id, r.service, r.source.value, r.model or ""]
                rvram.add_metric(labels, r.vram_mb)
                if r.idle_s is not None:
                    ridle.add_metric(labels, r.idle_s)
            for s in state.services:
                svc_up.add_metric([s.node, s.id], 1.0 if s.up else 0.0)
                if s.queue_running is not None:
                    queue.add_metric([s.node, s.id, "running"], s.queue_running)
                if s.queue_pending is not None:
                    queue.add_metric([s.node, s.id, "pending"], s.queue_pending)
        llabels = ["node", "tenant", "kind", "state"]
        leases = GaugeMetricFamily("iris_leases", "Leases queued or granted now", labels=llabels)
        lease_mb = GaugeMetricFamily(
            "iris_lease_vram_mb", "VRAM declared by leases queued or granted now", labels=llabels
        )
        unfit = GaugeMetricFamily(
            "iris_leases_over_admissible",
            "Granted leases that admission would not have granted (observe-only)",
            labels=["node", "tenant", "kind"],
        )
        count: dict[tuple[str, ...], int] = {}
        mb: dict[tuple[str, ...], int] = {}
        over: dict[tuple[str, ...], int] = {}
        for rec in self.broker.leases.active():
            key = (rec.node or "", rec.tenant, rec.kind, rec.state)
            count[key] = count.get(key, 0) + 1
            mb[key] = mb.get(key, 0) + rec.vram_mb
            if rec.fits is False:
                over[key[:3]] = over.get(key[:3], 0) + 1
        for key, n in count.items():
            leases.add_metric(list(key), n)
            lease_mb.add_metric(list(key), mb[key])
        for key, n in over.items():
            unfit.add_metric(list(key), n)
        ended = CounterMetricFamily(
            "iris_leases_ended",
            "Leases ended, by how: ok, failed, oom, cancelled, expired, refused",
            labels=["tenant", "kind", "how"],
        )
        for (tenant, kind, how), n in self.broker.leases.ended.items():
            ended.add_metric([tenant, kind, how], n)
        evictions = CounterMetricFamily(
            "iris_admission_evictions",
            "Residents admission evicted, by whether NVML confirmed it",
            labels=["node", "outcome"],
        )
        for (node, outcome), n in self.broker.leases.evictions.items():
            evictions.add_metric([node, outcome], n)
        residency = CounterMetricFamily(
            "iris_residency_actions",
            "Policy actions taken: pin, extend, evict, restart",
            labels=["node", "action", "outcome"],
        )
        for (node, action, outcome), n in self.broker.residency.actions.items():
            residency.add_metric([node, action, outcome], n)
        tenants = GaugeMetricFamily("iris_tenants", "Tenants that registered a manifest")
        tenants.add_metric([], len(self.broker.leases.tenants))
        yield from (up, total, free, admissible, unattributed, ram, rvram, ridle, svc_up, queue)
        yield from (leases, lease_mb, unfit, ended, evictions, residency, tenants)


def create_app(
    config: BrokerConfig | None = None, *, broker: Broker | None = None, poll: bool = True, policy: Policy | None = None
) -> FastAPI:
    if broker is None:
        broker = Broker(config or load_config(), policy=policy if policy is not None else load_policy())
    config = broker.config
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_BrokerCollector(broker))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(broker.run(), name="iris-broker-poll") if poll else None
        log.info("broker_started", nodes=sorted(config.nodes), services=[s.id for s in config.services])
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await broker.aclose()

    app = FastAPI(title="argus-iris", version=argus_iris.__version__, lifespan=lifespan)
    app.add_middleware(WriteGuard, refuse=cross_site_refuse(config.trusted_origins))
    app.state.broker = broker

    @app.get("/health")
    async def health() -> dict:
        agents = {name: s.reachable for name, s in broker.nodes.items()}
        return {
            "status": "ok" if all(agents.values()) else "degraded",
            "version": argus_iris.__version__,
            "mode": "observe-only" if config.admission == "observe" else "admission",
            "polls": broker.polls,
            "leases": {"active": len(broker.leases.active()), "tenants": len(broker.leases.tenants)},
            "agents": agents,
        }

    @app.get("/nodes", response_model=list[NodeState])
    async def nodes() -> list[NodeState]:
        return list(broker.nodes.values())

    @app.get("/nodes/{name}", response_model=NodeState)
    async def node(name: str) -> NodeState:
        if name not in broker.nodes:
            raise HTTPException(404, f"no node {name!r}")
        return broker.nodes[name]

    @app.get("/residents", response_model=list[Resident])
    async def residents(node: str | None = None) -> list[Resident]:
        return [r for r in broker.residents() if node is None or r.node == node]

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async with broker.subscribe() as queue:
                # Current state first, so a fresh page needs no separate fetch.
                for state in list(broker.nodes.values()):
                    yield f"event: node\ndata: {state.model_dump_json()}\n\n"
                for rec in broker.leases.active():
                    yield f"event: lease\ndata: {rec.model_dump_json()}\n\n"
                while not await request.is_disconnected():
                    try:
                        kind, item = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {kind}\ndata: {item.model_dump_json()}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    def check_caller(authorization: str | None, *, required: bool) -> None:
        token = os.environ.get(config.caller_token_env, "")
        if not token:
            if required:
                raise HTTPException(403, f"manual actions are disabled: {config.caller_token_env} is not set")
            return
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), token.encode()):
            raise HTTPException(401, "bad or missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    @app.post("/services/{service_id}/warm", response_model=WarmResult)
    async def warm(
        service_id: str, force: bool = False, authorization: str | None = Header(default=None)
    ) -> WarmResult:
        """Bring a service back: start its container through the agent if it is down, then call its warm_path.
        Refused with 409 while the node cannot admit what the service takes warm, unless ``force``."""
        check_caller(authorization, required=True)
        try:
            return await warm_service(broker, service_id, force=force)
        except ActionError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/residents/{resident_id:path}/evict", response_model=EvictResult)
    async def evict(
        resident_id: str, mode: EvictMode = "auto", authorization: str | None = Header(default=None)
    ) -> EvictResult:
        """Free a resident's VRAM through its adapter: ``evict`` moves it to the CPU where the service can, ``unload``
        drops it, ``auto`` prefers evict. Refused with 409 while it is busy. Waits to see the memory come back."""
        check_caller(authorization, required=True)
        try:
            return await evict_resident(broker, resident_id, mode=mode, reason="manual")
        except ActionError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    # --- leases (DESIGN.md "API (v1)"). Busy is never a 4xx: cortex's client raises on any 4xx but 404. ----------

    leases = broker.leases

    def status_of(rec: LeaseRecord) -> LeaseStatus:
        return rec.status(leases.position(rec))

    @app.post("/tenants")
    async def register_tenant(manifest: TenantManifest, authorization: str | None = Header(default=None)) -> dict:
        check_caller(authorization, required=False)
        changed = leases.register(manifest)
        return {"tenant": manifest.tenant, "kinds": [k.kind for k in manifest.kinds], "changed": changed}

    @app.get("/tenants", response_model=list[TenantManifest])
    async def tenants() -> list[TenantManifest]:
        return list(leases.tenants.values())

    @app.post("/leases", response_model=LeaseStatus)
    async def request_lease(req: LeaseRequest, authorization: str | None = Header(default=None)) -> LeaseStatus:
        check_caller(authorization, required=False)
        try:
            rec = leases.request(req)
        except LeaseConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if rec.state == "queued" and req.wait_s > 0:
            rec = await leases.wait(rec.lease_id, min(req.wait_s, MAX_WAIT_S)) or rec
        return status_of(rec)

    @app.get("/leases", response_model=list[LeaseRecord])
    async def list_leases(node: str | None = None, tenant: str | None = None, all: bool = False) -> list[LeaseRecord]:
        """Leases queued or granted now; ``all=true`` adds the recently ended ones."""
        recs = leases.leases.values() if all else leases.active()
        return [r for r in recs if (node is None or r.node == node) and (tenant is None or r.tenant == tenant)]

    @app.get("/queue", response_model=list[LeaseRecord])
    async def queue() -> list[LeaseRecord]:
        return sorted((r for r in leases.active() if r.state == "queued"), key=lambda r: r.created_at)

    @app.get("/leases/{lease_id}", response_model=LeaseStatus)
    async def get_lease(lease_id: str, wait_s: float = Query(0.0, ge=0)) -> LeaseStatus:
        rec = await leases.wait(lease_id, min(wait_s, MAX_WAIT_S))
        if rec is None:
            raise HTTPException(404, f"no lease {lease_id!r}")
        return status_of(rec)

    @app.post("/leases/{lease_id}/heartbeat", response_model=HeartbeatReply)
    async def heartbeat(
        lease_id: str, body: Heartbeat | None = None, authorization: str | None = Header(default=None)
    ) -> HeartbeatReply:
        check_caller(authorization, required=False)
        reply = leases.heartbeat(lease_id, body or Heartbeat())
        if reply is None:
            raise HTTPException(404, f"no live lease {lease_id!r}: re-attach with resume")
        return reply

    @app.post("/leases/{lease_id}/release", response_model=LeaseStatus)
    async def release(
        lease_id: str, body: LeaseRelease | None = None, authorization: str | None = Header(default=None)
    ) -> LeaseStatus:
        check_caller(authorization, required=False)
        rec = leases.release(lease_id, body or LeaseRelease())
        if rec is None:
            raise HTTPException(404, f"no lease {lease_id!r}")
        return status_of(rec)

    @app.get("/policy")
    async def show_policy() -> dict:
        return json.loads(broker.policy.model_dump_json(by_alias=True))

    @app.get("/config")
    async def show_config() -> dict:
        return json.loads(config.model_dump_json())

    # The dashboard: static files shipped in the package, a thin client over the routes above. Same origin, so its
    # warm POST passes the WriteGuard without a trusted_origins entry; the bearer token is still required.
    if UI_DIR.is_dir():

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    return app
