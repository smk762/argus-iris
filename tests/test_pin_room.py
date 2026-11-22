"""#40: one lease target served on several nodes, and services a node's pins leave no room for."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argus_cortex.gpu_wire import LeaseRequest

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.leases import LeaseBook
from argus_iris.broker.state import Broker, room_for_services
from argus_iris.models import GpuInfo, NodeLedger, NodeState, Resident, ResidentSource, ServiceState

CONFIG = BrokerConfig.model_validate(
    {
        "nodes": {
            "morpheus": {"agent_url": "http://morpheus:9110", "reserve_mb": 3500},
            "dragon": {"agent_url": "http://dragon:9110"},
        },
        "services": [
            {"id": "ollama-dragon", "node": "dragon", "adapter": "ollama", "base_url": "http://dragon:11434"},
            {"id": "xtts", "node": "dragon", "adapter": "argus-http", "base_url": "http://dragon:9150/xtts"},
            {"id": "tts", "node": "dragon", "adapter": "argus-http", "base_url": "http://dragon:9150/tts"},
            {
                "id": "echo-xtts",
                "node": "morpheus",
                "adapter": "argus-http",
                "base_url": "http://morpheus:8141",
                "serves": ["xtts"],
                "warm_mb": 2000,
            },
        ],
        "pinned": ["dragon/ollama-dragon/singguard*"],
    }
)


def node(name: str, *, admissible: int = 4000, residents=(), up: dict[str, bool] | None = None) -> NodeState:
    total = 8192 if name == "dragon" else 24576
    reserve = CONFIG.nodes[name].reserve_mb
    return NodeState(
        name=name,
        agent_url=f"http://{name}:9110",
        reachable=True,
        gpu=GpuInfo(index=0, name="gpu", uuid=name, total_mb=total, free_mb=admissible, used_mb=total - admissible),
        ledger=NodeLedger(
            total_mb=total,
            free_mb=admissible,
            reserve_mb=reserve,
            headroom_mb=512,
            resident_mb=0,
            unattributed_mb=0,
            admissible_mb=admissible,
        ),
        services=[
            ServiceState(id=s.id, node=name, adapter=s.adapter, base_url=s.base_url, up=(up or {}).get(s.id, True))
            for s in CONFIG.services_on(name)
        ],
        residents=list(residents),
    )


def singguard(vram: int = 5300) -> Resident:
    return Resident(
        id="dragon/ollama-dragon/singguard:4b",
        node="dragon",
        service="ollama-dragon",
        source=ResidentSource.OLLAMA,
        model="singguard:4b",
        vram_mb=vram,
    )


def argus(node_name: str, service: str, vram: int) -> Resident:
    return Resident(
        id=f"{node_name}/{service}", node=node_name, service=service, source=ResidentSource.ARGUS_HTTP, vram_mb=vram
    )


class Clock:
    now = datetime(2026, 11, 22, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


# --- serves: one target, a service on each node -----------------------------------------------------------------


def test_serves_adds_a_target_name() -> None:
    svc = next(s for s in CONFIG.services if s.id == "echo-xtts")
    assert svc.answers("xtts") and svc.answers("echo-xtts") and svc.answers("argus-http")
    assert not svc.answers("tts") and not svc.answers(None)


def test_an_xtts_lease_goes_to_dragon_first_and_to_morpheus_when_dragon_is_down() -> None:
    nodes = {n: node(n) for n in CONFIG.nodes}
    book = LeaseBook(CONFIG, lambda: nodes, clock=Clock())
    rec = book.request(LeaseRequest(tenant="agent-composer", target="xtts", vram_mb=2000))
    assert (rec.state, rec.node, rec.base_url) == ("granted", "dragon", "http://dragon:9150/xtts")

    nodes["dragon"] = node("dragon", up={"xtts": False})
    rec = book.request(LeaseRequest(tenant="agent-composer", target="xtts", vram_mb=2000))
    assert (rec.node, rec.base_url) == ("morpheus", "http://morpheus:8141")


def test_under_enforce_an_xtts_lease_spills_to_morpheus_when_dragon_has_no_room() -> None:
    config = CONFIG.model_copy(update={"admission": "enforce"})
    nodes = {"dragon": node("dragon", admissible=400, residents=[singguard()]), "morpheus": node("morpheus")}
    book = LeaseBook(config, lambda: nodes, clock=Clock())
    rec = book.request(LeaseRequest(tenant="agent-composer", target="xtts", vram_mb=2000))
    assert (rec.state, rec.node, rec.base_url) == ("granted", "morpheus", "http://morpheus:8141")


# --- services nothing can make room for ------------------------------------------------------------------------


def frigate(vram: int = 600) -> Resident:
    return Resident(id="dragon/frigate", node="dragon", service="frigate", source=ResidentSource.PROCESS, vram_mb=vram)


def unloaded(service: str, vram: int) -> Resident:
    r = argus("dragon", service, vram)
    r.detail["model_state"] = "unloaded"
    return r


def test_room_for_services_flags_a_service_that_cannot_fit_beside_what_is_held() -> None:
    held = [singguard(), frigate(), unloaded("tts", 1350)]
    state = node("dragon", residents=[*held, argus("dragon", "xtts", 300)])
    crowded = room_for_services(
        state,
        CONFIG.nodes["dragon"],
        CONFIG.services_on("dragon"),
        {r.id: r for r in held},
        {"dragon/xtts": 2000, "dragon/tts": 1800},
        {singguard().id},
    )
    base = 8192 - 512
    assert [s.id for s in crowded] == ["xtts", "tts"]
    assert (state.ledger.pinned_mb, state.ledger.held_mb, state.ledger.room_mb) == (5300, 7250, base - 7250)
    by_id = {s.id: s for s in state.services}
    assert (by_id["xtts"].need_mb, by_id["xtts"].room_mb) == (2000, base - 7250)
    assert by_id["tts"].room_mb == base - 5900  # its own residue is part of its need, not in its way
    assert by_id["ollama-dragon"].need_mb is None and not by_id["ollama-dragon"].crowded_out  # depends on the model


def test_a_service_that_fits_is_not_flagged_and_unseen_ones_are_not_judged() -> None:
    state = node("dragon", residents=[singguard()])
    crowded = room_for_services(
        state,
        CONFIG.nodes["dragon"],
        CONFIG.services_on("dragon"),
        {singguard().id: singguard()},
        {"dragon/xtts": 2000},
        {singguard().id},
    )
    assert crowded == []
    by_id = {s.id: s for s in state.services}
    assert by_id["xtts"].room_mb == 8192 - 512 - 5300
    assert by_id["tts"].need_mb is None and by_id["tts"].room_mb is None


def test_the_broker_flags_crowded_services_in_health_and_clears_them() -> None:
    broker = Broker(CONFIG)
    broker.peaks["dragon/xtts"] = 2000
    loaded_xtts = argus("dragon", "xtts", 300)
    broker._apply("dragon", node("dragon", residents=[singguard(), frigate(300), unloaded("tts", 1350), loaded_xtts]))
    assert broker.crowded() == [{"node": "dragon", "service": "xtts", "need_mb": 2000, "room_mb": 8192 - 512 - 6950}]
    # tts restarted: its residue is gone, and xtts fits again
    broker._apply("dragon", node("dragon", residents=[singguard(), frigate(300), loaded_xtts]))
    assert broker.crowded() == []
    assert broker._crowded["dragon"] == set()


def test_a_tenants_own_process_and_an_evictable_resident_are_not_held() -> None:
    broker = Broker(
        BrokerConfig.model_validate(CONFIG.model_dump() | {"tenants": {"curator": {"containers": ["cur"]}}})
    )
    mine = Resident(
        id="dragon/cur",
        node="dragon",
        service="cur",
        source=ResidentSource.PROCESS,
        vram_mb=3000,
        detail={"container": "cur"},
    )
    broker._apply("dragon", node("dragon", residents=[mine, argus("dragon", "tts", 1800)]))
    assert broker.nodes["dragon"].ledger.held_mb == 0


def test_a_dropped_pin_still_holds_its_room() -> None:
    from argus_iris.broker.policy import Policy

    policy = Policy.model_validate(
        {"residents": [{"match": {"node": "dragon", "ollama": "singguard*"}, "keepalive": "always"}]}
    )
    broker = Broker(CONFIG.model_copy(update={"pinned": []}), policy=policy)
    broker.peaks["dragon/xtts"] = 2600
    broker._apply("dragon", node("dragon", residents=[singguard()]))
    assert broker.nodes["dragon"].ledger.pinned_mb == 5300
    broker._apply("dragon", node("dragon"))  # SingGuard dropped out: residency will load it again
    assert broker.nodes["dragon"].ledger.pinned_mb == 5300
    assert [c["service"] for c in broker.crowded()] == ["xtts"]
