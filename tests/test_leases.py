from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argus_cortex.gpu_broker import BrokerCoordinator, BrokerError
from argus_cortex.gpu_wire import GpuConstraints, Heartbeat, LeaseRelease, LeaseRequest, TenantManifest
from fastapi.testclient import TestClient

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.leases import LeaseBook
from argus_iris.broker.state import Broker
from argus_iris.models import GpuInfo, NodeLedger, NodeState, Resident, ResidentSource, ServiceState
from argus_iris.server.app import create_app

CONFIG = BrokerConfig.model_validate(
    {
        "nodes": {
            "morpheus": {"agent_url": "http://morpheus:9110"},
            "dragon": {"agent_url": "http://dragon:9110"},
            "jarrah": {"agent_url": "http://jarrah:9110"},
        },
        "services": [
            {"id": "comfyui", "node": "morpheus", "adapter": "comfyui", "base_url": "http://morpheus:8188"},
            {"id": "ollama-dragon", "node": "dragon", "adapter": "ollama", "base_url": "http://dragon:11434"},
            {"id": "ollama-jarrah", "node": "jarrah", "adapter": "ollama", "base_url": "http://jarrah:11434"},
        ],
    }
)

MANIFEST = TenantManifest(
    tenant="agent-composer",
    node="dragon",
    kinds=[
        {"kind": "chat", "target": "ollama:qwen3:8b", "vram_mb": 6000, "model_mb": 5200},
        {"kind": "guard", "target": "ollama:singguard", "vram_mb": 6000, "constraints": {"node": "dragon"}},
        {"kind": "train", "vram_mb": 20000, "priority": "background", "preemptible": True},
    ],
)


def node(name: str, *, admissible: int = 4000, services: dict[str, bool] | None = None, models=()) -> NodeState:
    svc = {s.id: True for s in CONFIG.services_on(name)} | (services or {})
    return NodeState(
        name=name,
        agent_url=f"http://{name}:9110",
        reachable=True,
        gpu=GpuInfo(
            index=0, name="gpu", uuid=name, total_mb=8192, free_mb=4000, used_mb=4192, compute_capability="8.9"
        ),
        ledger=NodeLedger(
            total_mb=8192,
            free_mb=4000,
            reserve_mb=0,
            headroom_mb=512,
            resident_mb=4192,
            unattributed_mb=0,
            admissible_mb=admissible,
        ),
        services=[ServiceState(id=i, node=name, adapter="ollama", base_url="", up=up) for i, up in svc.items()],
        residents=[
            Resident(id=f"{name}/{s}/{m}", node=name, service=s, source=ResidentSource.OLLAMA, model=m, vram_mb=5000)
            for s, m in models
        ],
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 16, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


@pytest.fixture
def world():
    nodes = {n: node(n) for n in CONFIG.nodes}
    clock = Clock()
    book = LeaseBook(CONFIG, lambda: nodes, clock=clock)
    return book, nodes, clock


def ask(book: LeaseBook, **kw) -> object:
    return book.request(LeaseRequest(tenant=kw.pop("tenant", "agent-composer"), **kw))


# --- placement ---------------------------------------------------------------------------------------------------


def test_ollama_goes_to_the_first_service_in_config_order(world):
    book, _, _ = world
    rec = ask(book, target="ollama:qwen3:8b", vram_mb=1000)
    assert (rec.state, rec.node, rec.base_url, rec.device) == ("granted", "dragon", "http://dragon:11434", 0)


def test_a_model_resident_elsewhere_draws_the_lease_there(world):
    book, nodes, _ = world
    nodes["jarrah"] = node("jarrah", models=[("ollama-jarrah", "nomic-embed-text:latest")])
    rec = ask(book, target="ollama:nomic-embed-text", vram_mb=1000)
    assert rec.node == "jarrah"  # untagged name matches Ollama's ":latest"


def test_a_down_service_is_skipped_and_constraints_are_honoured(world):
    book, nodes, _ = world
    nodes["dragon"] = node("dragon", services={"ollama-dragon": False})
    assert ask(book, target="ollama:qwen3:8b").node == "jarrah"
    pinned = ask(book, target="ollama:qwen3:8b", constraints=GpuConstraints(node="dragon"))
    assert pinned.node == "dragon"  # the only candidate, so it is placed there even while down


def test_full_node_is_still_granted_but_marked(world):
    book, nodes, _ = world
    nodes["dragon"] = node("dragon", admissible=1000)
    rec = ask(book, target="ollama:qwen3:8b", vram_mb=3000)
    assert (rec.state, rec.node, rec.fits, rec.admissible_mb) == ("granted", "dragon", False, 1000)
    assert "observe-only" in rec.reason


def test_fit_counts_granted_leases_and_resident_models(world):
    book, nodes, _ = world
    nodes["dragon"] = node("dragon", admissible=4000, models=[("ollama-dragon", "qwen3:8b")])
    first = ask(book, target="ollama:qwen3:8b", vram_mb=6000, model_mb=5200)
    assert first.fits is True  # loaded already: only the 800 MB working set is new
    big = ask(book, target="ollama:other", vram_mb=3500)
    assert (big.fits, big.admissible_mb) == (False, 3200)  # 4000 less the first lease's 800


def test_unknown_target_is_granted_without_a_node(world):
    book, _, _ = world
    rec = ask(book, target="nosuch:model")
    assert (rec.state, rec.node, rec.base_url) == ("granted", None, None)
    assert "no configured service" in rec.reason


def test_in_process_uses_the_tenant_node(world):
    book, _, _ = world
    book.register(MANIFEST)
    assert ask(book, kind="train").node == "dragon"
    assert ask(book, tenant="other", constraints=GpuConstraints(node="jarrah")).node == "jarrah"
    assert ask(book, tenant="other", constraints=GpuConstraints(min_cc=9.0)).node is None


# --- manifest defaults -------------------------------------------------------------------------------------------


def test_manifest_fills_what_the_request_leaves_unset(world):
    book, _, _ = world
    book.register(MANIFEST)
    rec = ask(book, kind="train")
    assert (rec.vram_mb, rec.priority, rec.preemptible) == (20000, "background", True)
    assert ask(book, kind="chat").target == "ollama:qwen3:8b"  # the kind's target when the request names none
    chat = ask(book, kind="chat", target="ollama:qwen3:8b")
    assert (chat.vram_mb, chat.model_mb, chat.working_mb) == (6000, 5200, 800)
    own = ask(book, kind="chat", target="ollama:qwen3:8b", vram_mb=100, priority="batch")
    assert (own.vram_mb, own.model_mb, own.priority) == (100, None, "batch")
    # The kind's node constraint holds even though the request sends none, and dragon's Ollama is down.
    book._nodes()["dragon"] = node("dragon", services={"ollama-dragon": False})
    assert ask(book, kind="guard", target="ollama:singguard").node == "dragon"


def test_register_says_whether_the_manifest_changed(world):
    book, _, _ = world
    assert book.register(MANIFEST) is True
    assert book.register(MANIFEST) is False


# --- liveness ----------------------------------------------------------------------------------------------------


def test_a_retried_post_gets_the_same_lease(world):
    book, _, _ = world
    a = ask(book, request_id="r1")
    b = ask(book, request_id="r1")
    c = ask(book, request_id="r2")
    assert a.lease_id == b.lease_id != c.lease_id
    assert ask(book, tenant="someone-else", request_id="r1").lease_id != a.lease_id


def test_heartbeats_keep_a_lease_and_silence_expires_it(world):
    book, _, clock = world
    rec = ask(book, ttl_s=60)
    clock.advance(20)
    reply = book.heartbeat(rec.lease_id, Heartbeat(peak_vram_mb=900))
    assert (reply.state, reply.ttl_s, rec.peak_vram_mb) == ("granted", 60, 900)
    clock.advance(59)
    book.tick()
    assert rec.state == "granted"
    clock.advance(2)
    book.tick()
    assert (rec.state, rec.reason) == ("expired", "no heartbeat within the TTL")
    assert book.heartbeat(rec.lease_id, Heartbeat()) is None  # 404: the client re-attaches
    assert book.ended[("agent-composer", "default", "expired")] == 1


def test_a_grant_never_heartbeaten_is_reclaimed_early(world):
    book, _, clock = world
    rec = ask(book, ttl_s=300)
    clock.advance(149)
    book.tick()
    assert rec.state == "granted"  # half the TTL, since a client beats every ttl/3
    clock.advance(2)
    book.tick()
    assert (rec.state, rec.reason) == ("expired", "no heartbeat since the grant")


def test_resume_re_attaches_under_the_same_id(world):
    book, _, clock = world
    rec = ask(book, target="ollama:qwen3:8b", vram_mb=1000)
    clock.advance(100)
    book.tick()
    assert rec.state == "expired"
    back = ask(book, target="ollama:qwen3:8b", vram_mb=1000, resume=rec.lease_id)
    assert (back.lease_id, back.state, back.resumed, back.node) == (rec.lease_id, "granted", True, "dragon")
    # A broker that never knew it (restart) does the same.
    fresh = ask(book, resume="lost-in-a-restart")
    assert (fresh.lease_id, fresh.state, fresh.heartbeats) == ("lost-in-a-restart", "granted", 1)
    # And resuming a live lease is just a heartbeat.
    assert ask(book, resume=fresh.lease_id) is fresh and fresh.heartbeats == 2


def test_resume_refuses_another_tenants_lease(world):
    book, _, clock = world
    granted = ask(book, target="ollama:qwen3:8b", vram_mb=1000)
    with pytest.raises(ValueError, match="another tenant"):  # LeaseConflict, a caller error
        ask(book, tenant="intruder", resume=granted.lease_id)
    assert granted.heartbeats == 0  # not kept alive by someone else's beat

    queued = ask(book, target="ollama:qwen3:8b", vram_mb=1000, not_before=clock.now + timedelta(hours=1))
    assert queued.state == "queued"
    with pytest.raises(ValueError, match="another tenant"):  # LeaseConflict, a caller error
        ask(book, tenant="intruder", resume=queued.lease_id)
    assert book.leases[queued.lease_id] is queued and queued.tenant == "agent-composer"


def test_release_records_the_outcome(world):
    book, _, _ = world
    rec = ask(book)
    book.release(rec.lease_id, LeaseRelease(outcome="oom", peak_vram_mb=7000, detail="CUDA out of memory"))
    assert (rec.state, rec.outcome, rec.peak_vram_mb) == ("released", "oom", 7000)
    book.release(rec.lease_id, LeaseRelease())  # idempotent
    assert rec.outcome == "oom" and book.ended[("agent-composer", "default", "oom")] == 1
    assert book.release("nope", LeaseRelease()) is None


def test_not_before_queues_until_then(world):
    book, _, clock = world
    rec = ask(book, not_before=clock.now + timedelta(seconds=10), wait_s=20)
    other = ask(book, not_before=clock.now + timedelta(seconds=10), wait_s=20)
    assert (rec.state, book.position(rec), book.position(other)) == ("queued", 0, 1)
    clock.advance(11)
    book.get(rec.lease_id), book.get(other.lease_id)  # polled
    book.tick()
    assert rec.state == "granted"


def test_unpolled_queue_entries_are_dropped_and_deadlines_refuse(world):
    book, _, clock = world
    later = clock.now + timedelta(hours=1)
    rec = ask(book, not_before=later, wait_s=20)
    clock.advance(41)
    book.tick()
    assert (rec.state, rec.reason) == ("expired", "nobody polled the queued request")
    late = ask(book, deadline=clock.now - timedelta(seconds=1))
    assert (late.state, late.reason) == ("refused", "deadline already passed")
    queued = ask(book, not_before=later, deadline=clock.now + timedelta(seconds=5))
    clock.advance(6)
    book.tick()
    assert queued.state == "refused"


def test_cancel_a_queued_request(world):
    book, _, clock = world
    rec = ask(book, not_before=clock.now + timedelta(hours=1))
    book.release(rec.lease_id, LeaseRelease(outcome="cancelled"))
    assert (rec.state, rec.reason, rec.outcome) == ("released", "cancelled while queued", "cancelled")


def test_finished_leases_are_forgotten_after_a_while(world):
    book, _, clock = world
    rec = ask(book, request_id="r1")
    book.release(rec.lease_id, LeaseRelease())
    clock.advance(601)
    book.tick()
    assert rec.lease_id not in book.leases
    assert ask(book, request_id="r1").lease_id != rec.lease_id


# --- over HTTP, with argus-cortex's own client --------------------------------------------------------------------


def broker_with(nodes: dict[str, NodeState]) -> Broker:
    broker = Broker(CONFIG, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    broker.nodes.update(nodes)
    return broker


def coordinator(app, **kw) -> BrokerCoordinator:
    return BrokerCoordinator("http://iris", tenant="agent-composer", async_transport=httpx.ASGITransport(app=app), **kw)


async def test_cortex_client_takes_and_releases_a_lease(monkeypatch):
    monkeypatch.delenv("ARGUS_GPU_BROKER_TOKEN", raising=False)  # the client sends none
    broker = broker_with({n: node(n) for n in CONFIG.nodes})
    app = create_app(broker=broker, poll=False)
    coord = coordinator(app, manifest=MANIFEST)
    async with coord.alease(caller="rag-chat", min_vram_mb=0, kind="chat", target="ollama:qwen3:8b") as lease:
        assert (lease.via, lease.node, lease.base_url) == ("broker", "dragon", "http://dragon:11434")
        rec = broker.leases.leases[lease.lease_id]
        assert (rec.state, rec.vram_mb, rec.caller) == ("granted", 6000, "rag-chat")
        lease.peak_vram_mb = 5900
    assert (rec.state, rec.outcome, rec.peak_vram_mb) == ("released", "ok", 5900)
    assert "agent-composer" in broker.leases.tenants

    with pytest.raises(ValueError):
        async with coord.alease(caller="rag-chat", min_vram_mb=100, target="ollama:qwen3:8b") as lease:
            failed = broker.leases.leases[lease.lease_id]
            raise ValueError("boom")
    assert (failed.outcome, failed.detail) == ("failed", "ValueError: boom")


async def test_a_refusal_reads_back_over_http():
    broker = broker_with({n: node(n) for n in CONFIG.nodes})
    app = create_app(broker=broker, poll=False)
    rec = broker.leases.request(LeaseRequest(tenant="t", deadline=datetime(2020, 1, 1, tzinfo=UTC)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://iris") as client:
        body = (await client.get(f"/leases/{rec.lease_id}")).json()
    assert (body["state"], body["reason"]) == ("refused", "deadline already passed")


def test_routes_and_auth(monkeypatch):
    monkeypatch.delenv("ARGUS_GPU_BROKER_TOKEN", raising=False)  # the open case must not depend on the shell
    broker = broker_with({n: node(n) for n in CONFIG.nodes})
    client = TestClient(create_app(broker=broker, poll=False))
    body = {"tenant": "agent-composer", "target": "ollama:qwen3:8b", "vram_mb": 100}

    # No token configured: the lease routes are open (the token is optional, DESIGN.md "API").
    status = client.post("/leases", json=body).json()
    assert (status["state"], status["node"], status["lease_version"]) == ("granted", "dragon", "1.0")
    lid = status["lease_id"]
    assert client.get(f"/leases/{lid}").json()["state"] == "granted"
    assert client.post(f"/leases/{lid}/heartbeat", json={}).json()["state"] == "granted"
    assert [r["lease_id"] for r in client.get("/leases").json()] == [lid]
    assert client.get("/health").json()["leases"] == {"active": 1, "tenants": 0}
    assert "iris_leases{" in client.get("/metrics").text
    assert client.post(f"/leases/{lid}/release", json={"outcome": "ok"}).json()["state"] == "released"
    assert client.get("/leases").json() == []
    assert len(client.get("/leases", params={"all": True}).json()) == 1
    assert client.get("/leases/nope").status_code == 404
    assert client.post("/leases/nope/heartbeat", json={}).status_code == 404
    assert client.post("/leases/nope/release", json={}).status_code == 404
    assert client.post("/tenants", json=MANIFEST.model_dump(mode="json")).json()["changed"] is True
    assert [t["tenant"] for t in client.get("/tenants").json()] == ["agent-composer"]

    # With a token set, a wrong one is a 401: a caller bug cortex's client surfaces instead of falling back.
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "tok")
    assert client.post("/leases", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/leases", json=body).status_code == 401  # and so is none at all
    ok = client.post("/leases", json=body, headers={"Authorization": "Bearer tok"})
    assert ok.status_code == 200
    # Only the tenant is bad, so the 422 is the slug check and not a missing field.
    bad = client.post("/leases", json={**body, "tenant": "Bad Slug"}, headers={"Authorization": "Bearer tok"})
    assert bad.status_code == 422
    assert [e["loc"][-1] for e in bad.json()["detail"]] == ["tenant"]

    # Resuming another tenant's lease is refused, not re-attached.
    theirs = ok.json()["lease_id"]
    hijack = {**body, "tenant": "someone-else", "resume": theirs}
    assert client.post("/leases", json=hijack, headers={"Authorization": "Bearer tok"}).status_code == 409


async def test_cortex_client_raises_on_a_bad_token(monkeypatch):
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "tok")
    app = create_app(broker=broker_with({}), poll=False)
    with pytest.raises(BrokerError):
        async with coordinator(app, token="wrong").alease(caller="x", min_vram_mb=0):
            pass


async def test_long_poll_returns_when_the_lease_is_granted():
    broker = broker_with({n: node(n) for n in CONFIG.nodes})
    clock = Clock()
    broker.leases._clock = clock
    rec = broker.leases.request(LeaseRequest(tenant="t", not_before=clock.now + timedelta(seconds=5)))

    async def later():
        await asyncio.sleep(0.05)
        clock.advance(6)
        broker.leases.tick()

    task = asyncio.create_task(later())
    got = await broker.leases.wait(rec.lease_id, 5)
    await task
    assert got.state == "granted"
