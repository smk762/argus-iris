"""The broker's FastAPI app (observe-only): node state, residents, an SSE change stream and Prometheus metrics.

Serve with ``argus-iris serve`` or ``uvicorn --factory argus_iris.server.app:create_app``. No route changes state yet;
the shared WriteGuard is installed anyway so the lease and eviction routes of later steps are covered from the start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import structlog
from argus_cortex.server import WriteGuard, cross_site_refuse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

import argus_iris
from argus_iris.broker.config import BrokerConfig, load_config
from argus_iris.broker.state import Broker
from argus_iris.models import NodeState, Resident

log = structlog.get_logger("argus_iris.server")

SSE_KEEPALIVE_S = 15.0


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
        yield from (up, total, free, admissible, unattributed, ram, rvram, ridle, svc_up, queue)


def create_app(config: BrokerConfig | None = None, *, broker: Broker | None = None, poll: bool = True) -> FastAPI:
    if broker is None:
        broker = Broker(config or load_config())
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
            "mode": "observe-only",
            "polls": broker.polls,
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
                while not await request.is_disconnected():
                    try:
                        state = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: node\ndata: {state.model_dump_json()}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/config")
    async def show_config() -> dict:
        return json.loads(config.model_dump_json())

    return app
