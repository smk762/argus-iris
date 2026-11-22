"""Admission (``admission: enforce``): spill, eviction plans, the queue's order and the head's reservation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argus_cortex.gpu_wire import GpuConstraints, LeaseRelease, LeaseRequest

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.leases import LeaseBook
from argus_iris.broker.state import Broker
from argus_iris.models import GpuInfo, NodeLedger, NodeState, Resident, ResidentSource, ServiceState

BASE = {
    "nodes": {"dragon": {"agent_url": "http://dragon:9110"}, "jarrah": {"agent_url": "http://jarrah:9110"}},
    "services": [
        {"id": "ollama-dragon", "node": "dragon", "adapter": "ollama", "base_url": "http://dragon:11434"},
        {"id": "ollama-jarrah", "node": "jarrah", "adapter": "ollama", "base_url": "http://jarrah:11434"},
        {"id": "xtts", "node": "dragon", "adapter": "argus-http", "base_url": "http://dragon:9150/r/xtts"},
    ],
    "admission": "enforce",
    "admission_min_idle_s": 60,
}


def config(**over) -> BrokerConfig:
    return BrokerConfig.model_validate(BASE | over)


def res(node: str, service: str, vram: int, *, model=None, idle=600.0, busy=False, source=None, **detail) -> Resident:
    rid = f"{node}/{service}" + (f"/{model}" if model else "")
    src = source or (ResidentSource.OLLAMA if model else ResidentSource.ARGUS_HTTP)
    return Resident(
        id=rid, node=node, service=service, source=src, model=model, vram_mb=vram, idle_s=idle, busy=busy, detail=detail
    )


def node(name: str, admissible: int, residents=(), total: int = 8192) -> NodeState:
    return NodeState(
        name=name,
        agent_url="",
        reachable=True,
        gpu=GpuInfo(index=0, name="g", uuid=name, total_mb=total, free_mb=admissible + 512, used_mb=0),
        ledger=NodeLedger(
            total_mb=total,
            free_mb=admissible + 512,
            reserve_mb=0,
            headroom_mb=512,
            resident_mb=0,
            unattributed_mb=0,
            admissible_mb=admissible,
        ),
        services=[
            ServiceState(id=s.id, node=name, adapter=s.adapter, base_url="", up=True)
            for s in config().services_on(name)
        ],
        residents=list(residents),
    )


class World:
    def __init__(self, cfg: BrokerConfig | None = None) -> None:
        self.nodes = {"dragon": node("dragon", 4000), "jarrah": node("jarrah", 6000)}
        self.now = datetime(2026, 11, 16, 12, tzinfo=UTC)
        self.evicted: list[tuple[str, list[str]]] = []
        self.book = LeaseBook(
            cfg or config(),
            lambda: self.nodes,
            clock=lambda: self.now,
            evict=lambda lid, ids: self.evicted.append((lid, ids)),
        )

    def ask(self, **kw):
        kw.setdefault("tenant", "agent-composer")
        kw.setdefault("wait_s", 20)
        return self.book.request(LeaseRequest(**kw))


def test_fits_where_it_is_wanted():
    w = World()
    rec = w.ask(target="ollama:qwen3:8b", vram_mb=3000)
    assert (rec.state, rec.node, rec.fits) == ("granted", "dragon", True)


def test_spills_to_the_next_node_when_the_first_is_full():
    w = World()
    w.nodes["dragon"] = node("dragon", 1000)
    rec = w.ask(target="ollama:qwen3:8b", vram_mb=5000)
    assert (rec.state, rec.node, rec.base_url) == ("granted", "jarrah", "http://jarrah:11434")


def test_granted_leases_count_against_the_node():
    w = World()
    w.nodes["jarrah"] = node("jarrah", 1000)
    first = w.ask(target="ollama:a", vram_mb=3000)
    second = w.ask(target="ollama:b", vram_mb=3000)
    assert (first.state, first.node) == ("granted", "dragon")
    assert (second.state, second.reason) == ("queued", "waiting for 2000 MB on dragon: nothing idle to evict")


def test_evicts_idle_residents_least_recently_used_first_then_grants():
    w = World()
    w.nodes["dragon"] = node(
        "dragon",
        1000,
        [
            res("dragon", "xtts", 2400, idle=300),
            res("dragon", "ollama-dragon", 3000, model="old:7b", idle=900),
            res("dragon", "ollama-dragon", 500, model="nomic-embed-text:latest", idle=1200),
        ],
    )
    w.nodes["jarrah"] = node("jarrah", 0)
    rec = w.ask(target="ollama:qwen3:8b", vram_mb=4000)
    assert rec.state == "queued" and rec.node == "dragon"
    # 3000 short: the two idlest (1200 s, 900 s) cover it; old:7b alone does, so nomic-embed-text is spared too.
    assert w.evicted == [(rec.lease_id, ["dragon/ollama-dragon/old:7b"])]
    assert rec.reason == "evicting ollama-dragon/old:7b on dragon for 3000 MB"
    w.book.tick()
    assert len(w.evicted) == 1  # not planned twice while it runs

    # After the plan: old:7b's 3000 MB freed, nomic-embed-text still resident, exactly the 4000 MB asked for.
    w.nodes["dragon"] = node(
        "dragon",
        4000,
        [
            res("dragon", "xtts", 2400, idle=300),
            res("dragon", "ollama-dragon", 500, model="nomic-embed-text:latest", idle=1200),
        ],
    )
    w.book.evictions_done(rec.lease_id, failed=[])
    assert (rec.state, rec.node) == ("granted", "dragon")
    assert w.book.evictions[("dragon", "confirmed")] == 1


def test_without_an_evictor_a_lease_waits_instead_of_sticking_in_evicting():
    w = World()
    w.book._evict = None  # LeaseBook allows it: nothing would ever call evictions_done
    w.nodes["dragon"] = node("dragon", 1000, [res("dragon", "ollama-dragon", 3000, model="old:7b", idle=900)])
    w.nodes["jarrah"] = node("jarrah", 0)
    rec = w.ask(target="ollama:qwen3:8b", vram_mb=4000)
    assert (rec.state, rec.reason) == ("queued", "waiting for 3000 MB on dragon: eviction is off")
    assert w.book._evicting == {}

    w.nodes["dragon"] = node("dragon", 4500)  # the memory frees by itself
    w.book.tick()
    assert (rec.state, rec.node) == ("granted", "dragon")


@pytest.mark.parametrize(
    "spared",
    [
        res("dragon", "xtts", 6000, busy=True, idle=None),
        res("dragon", "xtts", 6000, idle=30),  # idle less than admission_min_idle_s
        res("dragon", "frigate", 6000, source=ResidentSource.PROCESS),  # no adapter
        res("dragon", "xtts", 6000, model_state="unloaded"),  # holds only a context
        res("dragon", "ollama-dragon", 6000, model="hf.co/inclusionAI/SingGuard-4b-GGUF:Q4_K_M"),  # pinned
        res("dragon", "ollama-dragon", 6000, model="qwen3:8b"),  # the model being asked for
    ],
)
def test_what_admission_never_evicts(spared):
    w = World(config(pinned=["dragon/ollama-dragon/hf.co/inclusionAI/SingGuard-*"]))
    w.nodes = {"dragon": node("dragon", 0, [spared]), "jarrah": node("jarrah", 0)}
    rec = w.ask(target="ollama:qwen3:8b", vram_mb=4000)
    assert rec.state == "queued" and w.evicted == []


def test_a_resident_under_a_live_lease_is_not_evicted():
    w = World()
    w.nodes["jarrah"] = node("jarrah", 0)
    w.nodes["dragon"] = node("dragon", 6000)
    held = w.ask(target="ollama:guard", vram_mb=1000)
    assert held.state == "granted"
    w.nodes["dragon"] = node("dragon", 0, [res("dragon", "ollama-dragon", 5000, model="guard:latest")])
    rec = w.ask(target="ollama:other", vram_mb=4000)
    assert rec.state == "queued" and w.evicted == []


def test_a_resident_of_a_higher_class_is_not_evicted_for_a_lower_request():
    w = World()
    w.nodes["jarrah"] = node("jarrah", 0)
    rt = w.ask(target="ollama:a2f", vram_mb=100, priority="realtime")
    w.book.release(rt.lease_id, LeaseRelease())
    w.nodes["dragon"] = node("dragon", 0, [res("dragon", "ollama-dragon", 6000, model="a2f:latest")])
    batch = w.ask(target="ollama:other", vram_mb=4000, priority="batch")
    assert batch.state == "queued" and w.evicted == []
    w.now += timedelta(seconds=301)  # same class: only once it has settled
    realtime = w.ask(target="ollama:other2", vram_mb=4000, priority="realtime")
    assert w.evicted == [(realtime.lease_id, ["dragon/ollama-dragon/a2f:latest"])]


def test_a_failed_eviction_is_not_retried_at_once():
    w = World()
    w.nodes = {"dragon": node("dragon", 0, [res("dragon", "xtts", 6000)]), "jarrah": node("jarrah", 0)}
    rec = w.ask(target="ollama:x", vram_mb=4000)
    w.book.evictions_done(rec.lease_id, failed=["dragon/xtts"])
    assert len(w.evicted) == 1 and rec.reason.startswith("waiting for")
    assert w.book.evictions[("dragon", "failed")] == 1
    w.now += timedelta(seconds=61)
    w.book.get(rec.lease_id)
    w.book.tick()
    assert len(w.evicted) == 2


def test_what_could_never_fit_is_refused():
    w = World()
    rec = w.ask(target="ollama:huge", vram_mb=9000)
    assert (rec.state, rec.reason) == ("refused", "needs 9000 MB; the most any candidate offers is 7680 MB")


def test_the_queue_serves_by_class_and_the_head_keeps_its_reservation():
    w = World()
    w.nodes = {"dragon": node("dragon", 0), "jarrah": node("jarrah", 0)}
    pin = GpuConstraints(node="dragon")
    batch = w.ask(target="ollama:b", vram_mb=3000, priority="batch", constraints=pin)
    w.now += timedelta(seconds=1)
    inter = w.ask(target="ollama:i", vram_mb=3000, priority="interactive", constraints=pin)
    small = w.ask(target="ollama:s", vram_mb=500, priority="background", constraints=pin)
    assert [w.book.position(r) for r in (inter, batch, small)] == [0, 1, 2]

    w.nodes["dragon"] = node("dragon", 3500)
    w.book.tick()
    # The interactive head takes 3000; the batch lease then waits, and the 500 MB background lease may not take the
    # last 500 MB from under it: it fits only beside the batch lease's reservation, which is all of it.
    assert (inter.state, batch.state, small.state) == ("granted", "queued", "queued")


def test_waiting_raises_a_lease_a_class_per_aging_period():
    w = World()
    w.nodes = {"dragon": node("dragon", 0), "jarrah": node("jarrah", 0)}
    old = w.ask(target="ollama:b", vram_mb=3000, priority="batch")
    w.now += timedelta(seconds=301)
    new = w.ask(target="ollama:i", vram_mb=3000, priority="interactive")
    assert [w.book.position(r) for r in (old, new)] == [0, 1]


def test_an_unmanaged_node_fails_open():
    w = World()
    w.nodes["dragon"] = NodeState(name="dragon", agent_url="", reachable=False)  # agent down: no ledger
    assert w.ask(target="ollama:x", vram_mb=7000).state == "granted"


def test_observe_mode_still_grants_everything():
    w = World(config(admission="observe"))
    w.nodes = {"dragon": node("dragon", 0), "jarrah": node("jarrah", 0)}
    rec = w.ask(target="ollama:x", vram_mb=7000)
    assert (rec.state, rec.fits, rec.reason) == ("granted", False, "observe-only: granted without admission")


# --- through the broker: a real eviction (adapter call, NVML confirm) frees the room -------------------------------


async def test_broker_evicts_and_then_grants():
    from test_evict import CONFIG as EVICT_CONFIG
    from test_evict import World as Dragon

    dragon = Dragon()
    dragon.vram.update({"ollama": 0, "whisper": 0, "xtts": 6000})
    dragon.ollama_models = []
    cfg = BrokerConfig.model_validate(
        EVICT_CONFIG | {"admission": "enforce", "admission_min_idle_s": 0, "evict_confirm_s": 2}
    )
    broker = Broker(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(dragon.handler)))
    await broker.poll_once()
    rec = broker.leases.request(LeaseRequest(tenant="t", target="ollama:qwen3:8b", vram_mb=3000, wait_s=5))
    assert rec.state == "queued" and "evicting xtts" in rec.reason
    got = await broker.leases.wait(rec.lease_id, 5)
    assert (got.state, got.node) == ("granted", "dragon")
    assert dragon.calls == ["POST http://dragon:9150/r/xtts/admin/evict"]
    await asyncio.sleep(0)


# --- thrash, efficiency, fairness --------------------------------------------------------------------------------


def lease_on(w: World, model: str, priority: str) -> None:
    """A lease of *priority* used *model* on dragon (so the resident takes that class), and ended."""
    rec = w.ask(target=f"ollama:{model}", vram_mb=1, priority=priority, constraints=GpuConstraints(node="dragon"))
    w.book.release(rec.lease_id, LeaseRelease())


def test_same_class_residents_are_evicted_only_once_settled():
    w = World()
    w.nodes["dragon"] = node("dragon", 9999)
    lease_on(w, "chat:a", "interactive")
    w.nodes = {
        "dragon": node("dragon", 0, [res("dragon", "ollama-dragon", 6000, model="chat:a", idle=120)]),
        "jarrah": node("jarrah", 0),
    }
    rec = w.ask(target="ollama:chat:b", vram_mb=4000, priority="interactive")
    assert rec.state == "queued" and w.evicted == []  # idle 120 s < 300 s: it might be used again in a moment
    w.nodes["dragon"] = node("dragon", 0, [res("dragon", "ollama-dragon", 6000, model="chat:a", idle=400)])
    w.book.tick()
    assert w.evicted == []  # idle long enough, but loaded (granted) under 300 s ago
    w.now += timedelta(seconds=301)
    w.book.get(rec.lease_id)
    w.book.tick()
    assert w.evicted == [(rec.lease_id, ["dragon/ollama-dragon/chat:a"])]


def test_lower_classes_go_before_the_idler_same_class_resident():
    w = World()
    w.nodes["dragon"] = node("dragon", 9999)
    lease_on(w, "chat:a", "interactive")
    lease_on(w, "bulk:b", "batch")
    w.now += timedelta(seconds=301)
    w.nodes = {
        "dragon": node(
            "dragon",
            0,
            [
                res("dragon", "ollama-dragon", 5000, model="chat:a", idle=3000),
                res("dragon", "ollama-dragon", 5000, model="bulk:b", idle=400),
            ],
        ),
        "jarrah": node("jarrah", 0),
    }
    rec = w.ask(target="ollama:new", vram_mb=4000, priority="interactive")
    assert w.evicted == [(rec.lease_id, ["dragon/ollama-dragon/bulk:b"])]


def test_the_cheapest_plan_across_nodes_wins():
    w = World()
    w.nodes = {
        "dragon": node(
            "dragon",
            0,
            [res("dragon", "ollama-dragon", 3000, model="x:1"), res("dragon", "ollama-dragon", 3000, model="x:2")],
        ),
        "jarrah": node("jarrah", 0, [res("jarrah", "ollama-jarrah", 4500, model="y:1")]),
    }
    rec = w.ask(target="ollama:new", vram_mb=4000)
    # dragon needs both (6000 MB evicted), jarrah one (4500 MB): jarrah, though dragon comes first in config.
    assert w.evicted == [(rec.lease_id, ["jarrah/ollama-jarrah/y:1"])] and rec.node == "jarrah"


def test_within_a_class_the_tenant_holding_fewest_grants_goes_first():
    w = World()
    w.nodes = {"dragon": node("dragon", 2000), "jarrah": node("jarrah", 0)}
    hog = [w.ask(tenant="hog", target="ollama:h", vram_mb=1000) for _ in range(2)]
    assert [h.state for h in hog] == ["granted", "granted"]
    first = w.ask(tenant="hog", target="ollama:h", vram_mb=1000)
    w.now += timedelta(seconds=1)
    other = w.ask(tenant="quiet", target="ollama:q", vram_mb=1000)
    assert [w.book.position(r) for r in (other, first)] == [0, 1]  # later, but holding nothing
    w.book.release(hog[0].lease_id, LeaseRelease())
    assert (other.state, first.state) == ("granted", "queued")


# --- stalls: heartbeating, but no GPU work ----------------------------------------------------------------------


def _hold(w: World, holder, seconds: float, *, waiter=None, beat=None) -> None:
    """Advance *seconds*: the holder's keepalive beats and the waiter keeps polling, as live clients would."""
    from argus_cortex.gpu_wire import Heartbeat

    left = seconds
    while left > 0:
        step = min(20, left)  # all of it: a remainder under one beat still moves the clock
        left -= step
        w.now += timedelta(seconds=step)
        w.book.heartbeat(holder.lease_id, beat or Heartbeat())
        if waiter is not None and waiter.state == "queued":
            w.ask(target=waiter.target, vram_mb=waiter.vram_mb, request_id=waiter.request_id)  # a poll
        w.book.tick()


def _stall_world(**over) -> tuple[World, object]:
    w = World(config(lease_stall_s=600, **over))
    w.nodes = {"dragon": node("dragon", 4000, [res("dragon", "ollama-dragon", 300, model="qwen3:8b")])}
    holder = w.ask(target="ollama:qwen3:8b", vram_mb=4000, request_id="holder")
    assert holder.state == "granted"
    return w, holder


def test_a_heartbeating_lease_with_no_gpu_work_is_marked_stalled():
    w, holder = _stall_world()
    _hold(w, holder, 580)
    assert holder.stalled_at is None
    _hold(w, holder, 40)
    assert holder.state == "granted" and holder.stalled_at is not None
    assert not holder.yield_requested  # nothing waits for the node: flag only


def test_gpu_work_or_reported_progress_keeps_a_lease_from_stalling():
    w, holder = _stall_world()
    _hold(w, holder, 500)
    w.nodes["dragon"].residents[0].busy = True
    w.book.tick()
    w.nodes["dragon"].residents[0].busy = False
    _hold(w, holder, 500)
    assert holder.stalled_at is None  # the busy tick reset the clock
    from types import SimpleNamespace

    for i in range(3):  # a CPU-bound phase that reports progress is working too
        _hold(w, holder, 300, beat=SimpleNamespace(peak_vram_mb=None, progress=i / 3, stage=None))
    assert holder.stalled_at is None


def test_a_stall_unflags_when_work_shows_up():
    w, holder = _stall_world()
    _hold(w, holder, 620)
    assert holder.stalled_at is not None
    w.nodes["dragon"].residents[0].idle_s = 5
    w.book.tick()
    assert holder.stalled_at is None and holder.active_at == w.now


def test_a_stalled_lease_blocking_the_queue_is_asked_to_yield():
    from argus_cortex.gpu_wire import Heartbeat

    w, holder = _stall_world()
    waiter = w.ask(target="ollama:other", vram_mb=3000, request_id="waiter", constraints=GpuConstraints(node="dragon"))
    assert waiter.state == "queued"
    _hold(w, holder, 620, waiter=waiter)
    reply = w.book.heartbeat(holder.lease_id, Heartbeat())
    assert reply.state == "granted" and reply.yield_requested and "stalled" in reply.reason
    _hold(w, holder, 3600, waiter=waiter)
    assert holder.state == "granted"  # without lease_stall_reclaim it is only asked


def test_reclaim_ends_a_stalled_blocker_and_its_resume_stays_refused():
    w, holder = _stall_world(lease_stall_reclaim=True)
    waiter = w.ask(target="ollama:other", vram_mb=3000, request_id="waiter", constraints=GpuConstraints(node="dragon"))
    _hold(w, holder, 620, waiter=waiter)
    assert holder.state == "granted" and holder.yield_requested
    _hold(w, holder, 300, waiter=waiter)
    assert (holder.state, waiter.state) == ("refused", "granted")
    assert holder.reason.startswith("reclaimed") and w.book.ended[("agent-composer", "default", "reclaimed")] == 1
    # The keepalive's next beat sees the refusal (not a 404), and a resume does not re-grant it.
    from argus_cortex.gpu_wire import Heartbeat

    assert w.book.heartbeat(holder.lease_id, Heartbeat()).state == "refused"
    again = w.book.request(LeaseRequest(tenant="agent-composer", target="ollama:qwen3:8b", resume=holder.lease_id))
    assert again.state == "refused"


def test_reclaim_counts_from_the_yield_request_not_the_stall():
    w, holder = _stall_world(lease_stall_reclaim=True)
    _hold(w, holder, 3600)  # stalled long ago, nobody waiting: flagged only
    assert holder.stalled_at is not None and not holder.yield_requested
    waiter = w.ask(target="ollama:other", vram_mb=3000, request_id="waiter", constraints=GpuConstraints(node="dragon"))
    _hold(w, holder, 20, waiter=waiter)
    assert holder.state == "granted" and holder.yield_requested  # asked first, not reclaimed on the same tick
    _hold(w, holder, 300, waiter=waiter)
    assert holder.state == "refused"


# --- a tenant's own memory (#36) ---------------------------------------------------------------------------------


def curator_world(own_mb: int, **cfg) -> tuple[World, Resident]:
    """dragon full, mostly with curator's own process (its ONNX Runtime arena), the rest an idle xtts at its floor."""
    from argus_cortex.gpu_wire import TenantManifest

    w = World(config(**cfg))
    proc = res("dragon", "studio-rec-curator", own_mb, source=ResidentSource.PROCESS, container="studio-rec-curator")
    proc.tenant = "argus-curator"
    w.nodes["dragon"] = node("dragon", 0, [proc, res("dragon", "xtts", 140, idle=900, model_state="unloaded")])
    w.book.register(TenantManifest(tenant="argus-curator", node="dragon"))
    return w, proc


def test_a_tenants_own_process_memory_counts_toward_its_in_process_lease():
    w, _ = curator_world(6000)
    rec = w.ask(tenant="argus-curator", vram_mb=512)
    assert (rec.state, rec.node, rec.fits) == ("granted", "dragon", True)
    assert w.evicted == []


def test_the_credit_is_shared_by_the_tenants_leases_and_not_given_to_others():
    w, _ = curator_world(1000)
    first = w.ask(tenant="argus-curator", vram_mb=800)
    second = w.ask(tenant="argus-curator", vram_mb=800)  # only 200 MB of the 1000 left to credit
    other = w.ask(tenant="argus-darkroom", vram_mb=512)
    assert first.state == "granted"
    assert (second.state, second.reason) == ("queued", "waiting for 600 MB on dragon: nothing idle to evict")
    assert other.node == "jarrah"  # no credit on dragon for another tenant: it goes where there is room


def test_only_in_process_leases_get_the_credit():
    w, _ = curator_world(6000)
    w.nodes["jarrah"] = node("jarrah", 0)
    rec = w.ask(tenant="argus-curator", target="ollama:qwen3:8b", vram_mb=512)
    assert rec.state == "queued"  # the work runs in Ollama, not in curator's process: its memory is no help


def test_a_tenants_own_residents_are_never_evicted_for_its_lease():
    w = World()
    mine = res("dragon", "xtts", 3000, idle=900)
    mine.tenant = "agent-composer"
    w.nodes["dragon"] = node("dragon", 500, [mine])
    w.nodes["jarrah"] = node("jarrah", 0)
    rec = w.ask(tenant="agent-composer", target="ollama:qwen3:8b", vram_mb=2000)
    assert rec.state == "queued" and w.evicted == []
    other = w.ask(tenant="argus-darkroom", target="ollama:qwen3:8b", vram_mb=2000)
    assert w.evicted == [(other.lease_id, ["dragon/xtts"])]


def test_residents_are_linked_by_configured_container_or_by_compose_service():
    w = World(config(tenants={"argus-curator": {"containers": ["studio-rec-*"]}}))
    by_pattern = res("dragon", "studio-rec-curator", 100, source=ResidentSource.PROCESS, container="studio-rec-curator")
    by_service = res("dragon", "argus-lens", 100, source=ResidentSource.PROCESS, container="argus-suite-argus-lens-1")
    stranger = res("dragon", "frigate", 100, source=ResidentSource.PROCESS, container="frigate")
    assert w.book.tenant_of(by_pattern) == "argus-curator"
    assert w.book.tenant_of(by_service) is None  # no tenant argus-lens known yet
    w.ask(tenant="argus-lens", vram_mb=100)
    assert w.book.tenant_of(by_service) == "argus-lens"
    assert w.book.tenant_of(stranger) is None


async def test_the_broker_labels_residents_with_their_tenant():
    from test_evict import CONFIG as EVICT_CONFIG
    from test_evict import World as Dragon

    dragon = Dragon()
    cfg = BrokerConfig.model_validate(EVICT_CONFIG | {"tenants": {"agent-composer": {"containers": ["xtts"]}}})
    broker = Broker(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(dragon.handler)))
    await broker.poll_once()
    tenants = {r.id: r.tenant for r in broker.residents()}
    assert tenants["dragon/xtts"] == "agent-composer"
    assert tenants["dragon/whisper"] is None


# --- the broker's peak for a client that cannot see its own VRAM (#39) -------------------------------------------


def release(w: World, rec, peak=None):
    from argus_cortex.gpu_wire import LeaseRelease

    return w.book.release(rec.lease_id, LeaseRelease(outcome="ok", peak_vram_mb=peak))


def test_a_release_without_a_peak_records_the_tenants_process_vram():
    w, _ = curator_world(1500)
    rec = w.ask(tenant="argus-curator", vram_mb=512)
    done = release(w, rec)
    assert (done.peak_vram_mb, done.peak_source) == (1500, "broker")


def test_a_client_peak_wins_over_the_broker_figure():
    w, _ = curator_world(1500)
    rec = w.ask(tenant="argus-curator", vram_mb=512)
    w.book.tick()  # the broker has seen 1500 by now
    assert (rec.peak_vram_mb, rec.peak_source) == (1500, "broker")
    done = release(w, rec, peak=900)
    assert (done.peak_vram_mb, done.peak_source) == (900, "client")


def test_the_broker_peak_is_the_most_seen_over_the_lease_not_the_last():
    w, proc = curator_world(1500)
    rec = w.ask(tenant="argus-curator", vram_mb=512)
    w.book.tick()
    proc.vram_mb = 3200  # the model loaded
    w.book.tick()
    proc.vram_mb = 700  # and its arena shrank again before release
    w.book.tick()
    assert release(w, rec).peak_vram_mb == 3200


def test_a_client_heartbeat_peak_stops_the_broker_filling_in():
    from argus_cortex.gpu_wire import Heartbeat

    w, proc = curator_world(1500)
    rec = w.ask(tenant="argus-curator", vram_mb=512)
    w.book.heartbeat(rec.lease_id, Heartbeat(peak_vram_mb=1100))
    proc.vram_mb = 5000
    w.book.tick()
    done = release(w, rec)
    assert (done.peak_vram_mb, done.peak_source) == (1100, "client")


def test_no_linked_process_and_service_leases_stay_null():
    w = World()
    rec = w.ask(tenant="argus-darkroom", vram_mb=512)  # in-process, but no process of its own on the node
    w.book.tick()
    assert (release(w, rec).peak_vram_mb, rec.peak_source) == (None, None)
    w2, _ = curator_world(6000)
    w2.nodes["dragon"].ledger.admissible_mb = 8000
    svc = w2.ask(tenant="argus-curator", target="ollama:qwen3:8b", vram_mb=512)
    w2.book.tick()
    assert release(w2, svc).peak_vram_mb is None  # the work ran in Ollama, not in curator's process


def test_concurrent_leases_of_one_tenant_each_show_the_process_total():
    w, _ = curator_world(3000)
    a = w.ask(tenant="argus-curator", vram_mb=512)
    b = w.ask(tenant="argus-curator", vram_mb=512)
    w.book.tick()
    assert (a.peak_vram_mb, b.peak_vram_mb) == (3000, 3000)  # the whole, not a share: said so in peak_source's doc
