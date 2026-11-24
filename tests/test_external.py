"""External services (#29): Immich's ML server read for up/down, its VRAM attributed through its container, freed
only by stopping it (``evict_by: stop``), stopped by admission only under an enforced rule, and warmed again."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from argus_cortex.gpu_wire import LeaseRequest

from argus_iris.broker import actions
from argus_iris.broker.actions import ActionError, evict_resident
from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.leases import LeaseBook
from argus_iris.broker.policy import Policy
from argus_iris.broker.state import Broker
from argus_iris.models import GpuInfo, GpuProcess, NodeLedger, NodeSnapshot, NodeState, Resident, ResidentSource

T0 = datetime(2026, 11, 20, tzinfo=UTC)
IMMICH = {
    "id": "immich-ml",
    "node": "rtx4060",
    "adapter": "external",
    "base_url": "http://rtx4060:3003",
    "health_path": "/ping",
    "container": "immich_machine_learning",
}
ENFORCED = Policy.model_validate({"residents": [{"match": {"resident": "rtx4060/immich-ml"}, "enforce": True}]})


def cfg(**svc) -> BrokerConfig:
    return BrokerConfig.model_validate(
        {
            "nodes": {"rtx4060": {"agent_url": "http://rtx4060:9110"}},
            "services": [
                IMMICH | svc,
                {"id": "ollama", "node": "rtx4060", "adapter": "ollama", "base_url": "http://rtx4060:11434"},
            ],
            "admission": "enforce",
            "evict_confirm_s": 0.2,
        }
    )


class World:
    """rtx4060 running immich_machine_learning (380 MB: CUDA context and the CLIP text model). A stop takes the
    process off the GPU and /ping down with it; a start brings both back."""

    def __init__(self) -> None:
        self.running = True
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            self.calls.append(url)
        if url.startswith("http://rtx4060:9110/containers/"):
            name, action = url.split("/")[-2:]
            self.running = action != "stop"
            return httpx.Response(200, json={"container": name, "action": action, "ok": True})
        if url == "http://rtx4060:9110/snapshot":
            procs = [
                GpuProcess(
                    pid=7,
                    gpu=0,
                    vram_mb=380,
                    container="immich_machine_learning",
                    service="immich-machine-learning",
                    first_seen=T0,
                    idle_s=900,
                )
            ]
            procs = procs if self.running else []
            used = sum(p.vram_mb for p in procs)
            snap = NodeSnapshot(
                node="rtx4060",
                taken_at=T0,
                agent_version="t",
                gpus=[GpuInfo(index=0, name="g", uuid="d", total_mb=8192, free_mb=8192 - used, used_mb=used)],
                processes=procs,
            )
            return httpx.Response(200, json=snap.model_dump(mode="json"))
        if url == "http://rtx4060:3003/ping" and self.running:
            return httpx.Response(200, text="pong")
        raise httpx.ConnectError(f"refused {url}")


async def broker_for(config: BrokerConfig, policy: Policy | None = None) -> tuple[World, Broker]:
    world = World()
    broker = Broker(config, client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)), policy=policy)
    await broker.poll_once()
    return world, broker


def test_evict_by_needs_an_external_service_with_a_container():
    with pytest.raises(ValueError, match="evict_by needs adapter: external and a container"):
        cfg(evict_by="stop", container=None)
    with pytest.raises(ValueError, match="evict_by needs"):
        cfg(evict_by="stop", adapter="argus-http")


async def test_health_and_vram_are_attributed_to_the_service():
    world, broker = await broker_for(cfg())
    state = broker.nodes["rtx4060"]
    assert state.services[0].up is True
    (r,) = state.residents
    assert (r.id, r.source, r.vram_mb, r.idle_s) == ("rtx4060/immich-ml", ResidentSource.EXTERNAL, 380, 900)
    assert state.ledger.unattributed_mb == 0

    world.running = False
    await broker.poll_once()
    assert broker.nodes["rtx4060"].services[0].up is False
    assert broker.nodes["rtx4060"].residents == []


async def test_evict_is_refused_without_evict_by():
    world, broker = await broker_for(cfg())
    with pytest.raises(ActionError, match="cannot free it through an adapter") as err:
        await evict_resident(broker, "rtx4060/immich-ml")
    assert err.value.status == 422 and world.calls == []


async def test_evict_by_stop_stops_the_container_and_confirms_on_nvml(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "t")
    world, broker = await broker_for(cfg(evict_by="stop"))
    with pytest.raises(ActionError) as err:
        await evict_resident(broker, "rtx4060/immich-ml", mode="evict")  # no CPU stage
    assert err.value.status == 422

    result = await evict_resident(broker, "rtx4060/immich-ml", poll_s=0.01)
    assert world.calls == ["http://rtx4060:9110/containers/immich_machine_learning/stop"]
    assert (result.action, result.confirmed, result.before_mb, result.after_mb) == ("stop", True, 380, 0)
    assert broker.stopped == {}  # a manual stop is not warmed again by residency


async def test_residency_view_says_what_admission_would_do():
    r = Resident(
        id="rtx4060/immich-ml", node="rtx4060", service="immich-ml", source="external", vram_mb=380, idle_s=900
    )

    async def view(policy: Policy, **svc):
        _, broker = await broker_for(cfg(**svc), policy)
        return broker.residency.view(r, T0)

    report = Policy.model_validate({"residents": [{"match": {"resident": "rtx4060/immich-ml"}}]})
    assert (await view(report, evict_by="stop")).note == "would stop for a lease"
    assert (await view(ENFORCED, evict_by="stop")).note == "stopped when a lease needs the room"
    killing = Policy.model_validate({"residents": [{"match": {"resident": "rtx4060/immich-ml"}, "idle_kill": 60}]})
    assert (await view(killing)).note == "due, but iris has no adapter for it"
    assert (await view(killing, evict_by="stop")).next == "evict"


# --- admission -----------------------------------------------------------------------------------------------------


def rtx4060(admissible: int, residents: list[Resident]) -> NodeState:
    ledger = NodeLedger(
        total_mb=8192,
        free_mb=admissible + 512,
        reserve_mb=0,
        headroom_mb=512,
        resident_mb=0,
        unattributed_mb=0,
        admissible_mb=admissible,
    )
    gpu = GpuInfo(index=0, name="g", uuid="d", total_mb=8192, free_mb=admissible + 512, used_mb=0)
    return NodeState(name="rtx4060", agent_url="", reachable=True, gpu=gpu, ledger=ledger, residents=residents)


def book_for(config: BrokerConfig, policy: Policy) -> tuple[LeaseBook, list]:
    immich = Resident(
        id="rtx4060/immich-ml", node="rtx4060", service="immich-ml", source="external", vram_mb=1500, idle_s=900
    )
    evicted: list = []
    nodes = {"rtx4060": rtx4060(1000, [immich])}
    book = LeaseBook(config, lambda: nodes, clock=lambda: T0, evict=lambda lid, ids: evicted.append(ids), policy=policy)
    return book, evicted


@pytest.mark.parametrize(
    ("svc", "policy", "stops"),
    [
        ({"evict_by": "stop"}, ENFORCED, True),
        ({"evict_by": "stop"}, Policy(), False),  # observe first: no enforced rule, no stop
        ({}, ENFORCED, False),  # no evict_by: iris cannot free it
    ],
)
def test_admission_stops_it_only_when_allowed(svc, policy, stops):
    book, evicted = book_for(cfg(**svc), policy)
    rec = book.request(LeaseRequest(tenant="t", target="ollama:qwen3:8b", vram_mb=2000, wait_s=20))
    assert rec.state == "queued"
    assert evicted == ([["rtx4060/immich-ml"]] if stops else [])


# --- warmed again ----------------------------------------------------------------------------------------------------


async def test_admission_stop_is_warmed_again_once_no_lease_is_active(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "t")
    monkeypatch.setattr(actions, "WARM_POLL_S", 0.01)
    world, broker = await broker_for(cfg(evict_by="stop"), ENFORCED)

    monkeypatch.setattr(broker.leases, "evictions_done", lambda *a: None)
    broker.leases.leases["L1"] = type("Queued", (), {"state": "queued"})()  # _evict_for checks it is still waiting
    await broker._evict_for("L1", ["rtx4060/immich-ml"])
    assert broker.stopped == {"immich-ml": "L1"} and not world.running

    monkeypatch.setattr(broker.leases, "active", lambda node=None: ["a lease"])
    await broker.residency._rewarm(T0)
    assert not world.running  # a lease is still on rtx4060

    monkeypatch.setattr(broker.leases, "active", lambda node=None: [])
    broker.residency._cooldown.clear()
    await broker.residency._rewarm(T0)
    assert world.running and broker.stopped == {}
    assert world.calls[-1] == "http://rtx4060:9110/containers/immich_machine_learning/start"
    assert broker.residency.actions[("rtx4060", "rewarm", "ok")] == 1
