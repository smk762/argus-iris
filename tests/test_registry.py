"""Self-registration: tenant manifests' residents become services, announced agents become nodes (registry.py)."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argus_cortex.gpu_wire import TenantManifest
from fastapi.testclient import TestClient

from argus_iris.broker.config import BrokerConfig, RegisteredService
from argus_iris.broker.registry import RegistrationError, Registry
from argus_iris.broker.state import Broker
from argus_iris.models import ContainerEntry, GpuInfo, NodeSnapshot, PortMap
from argus_iris.server.app import create_app

T0 = datetime(2026, 11, 23, tzinfo=UTC)
CALLER, AGENT = "caller-token", "agent-token"

CONFIG = {
    "nodes": {
        "morpheus": {"agent_url": "http://192.168.1.39:9110"},
        "dragon": {"agent_url": "http://192.168.1.198:9110"},
    },
    "services": [
        {
            "id": "echo-xtts",
            "node": "morpheus",
            "adapter": "argus-http",
            "base_url": "http://192.168.1.39:8141",
            "container": "argus-echo-xtts",
        }
    ],
}

ECHO = TenantManifest.model_validate(
    {
        "tenant": "argus-echo",
        "residents": [
            {
                "name": "xtts",
                "adapter": "argus-http",
                "container": "argus-echo-xtts",
                "vram_mb": 2000,
                "unload_path": "/admin/unload",
                "evict_path": "/admin/evict",
                "warm_path": "/admin/wake",
                "admin_auth": True,
            },
            {"name": "whisper", "adapter": "argus-http", "container": "argus-echo-whisper", "vram_mb": 3000},
        ],
    }
)


def entry(name: str, *ports: tuple[int, int | None], service: str | None = None, **kw) -> ContainerEntry:
    return ContainerEntry(
        id=kw.pop("id", name.encode().hex().ljust(64, "0")[:64]),
        name=name,
        service=service,
        running=kw.pop("running", True),
        ports=[PortMap(private=p, public=q) for p, q in ports],
        **kw,
    )


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def registry(tmp_path: Path | None = None, **overrides) -> tuple[Registry, Clock]:
    clock = Clock()
    config = BrokerConfig.model_validate({**CONFIG, **overrides})
    return Registry(config, state_dir=tmp_path, clock=clock), clock


def test_resident_lands_where_an_agent_reports_its_container() -> None:
    reg, _ = registry()
    reg.register(ECHO, token="echo-admin")
    reg.observe("dragon", [entry("argus-echo-xtts", (8141, 8141)), entry("argus-echo-whisper", (8144, 18144))])
    reg.observe("morpheus", [entry("argus-echo-xtts", (8141, 8141))])
    assert reg.reconcile()

    by_id = {s.id: s for s in reg.services}
    assert set(by_id) == {"xtts", "whisper"}
    xtts, whisper = by_id["xtts"], by_id["whisper"]
    assert (xtts.node, xtts.base_url, xtts.warm_mb, xtts.tenant) == (
        "dragon",
        "http://192.168.1.198:8141",
        2000,
        "argus-echo",
    )
    assert whisper.base_url == "http://192.168.1.198:18144"  # the published port, not the container's
    # The admin token goes only where the manifest says the routes need it.
    assert xtts.headers() == {"Authorization": "Bearer echo-admin"} and whisper.headers() == {}
    # morpheus's echo-xtts is already a configured service reading that container: config wins.
    covered = [p for p in reg.placements if p.node == "morpheus"]
    assert covered and covered[0].covered_by == "echo-xtts" and covered[0].service is None
    # Registered services join the list the lease book and residency read, after the configured ones.
    assert [s.id for s in reg.config.services] == ["echo-xtts", "xtts", "whisper"]
    assert not reg.reconcile()  # nothing new: no change


def test_the_callers_host_is_never_used() -> None:
    manifest = TenantManifest.model_validate(
        {
            "tenant": "argus-lens",
            "residents": [
                {
                    "name": "lens-captioner",
                    "adapter": "argus-http",
                    "base_url": "https://evil.example:8100/prefix",
                    "container": "argus-lens",
                    "admin_auth": True,
                }
            ],
        }
    )
    reg, _ = registry()
    reg.register(manifest, token="t")
    # Matched by compose service when the container name carries a project prefix.
    reg.observe("morpheus", [entry("argus-suite-argus-lens-1", (8100, 8100), (9000, 9001), service="argus-lens")])
    reg.reconcile()
    (svc,) = reg.services
    assert (
        svc.base_url == "https://192.168.1.39:8100/prefix"
    )  # scheme, port and path from the manifest; host the node's
    assert svc.container == "argus-suite-argus-lens-1"


def test_ports_that_cannot_be_resolved_say_why() -> None:
    reg, _ = registry()
    reg.register(ECHO)
    reg.observe(
        "dragon", [entry("argus-echo-xtts", (8141, 8141), (9000, 9001)), entry("argus-echo-whisper", (8144, None))]
    )
    reg.reconcile()
    assert reg.services == []
    reasons = {p.resident: p.reason for p in reg.placements}
    assert "several ports (8141, 9000)" in reasons["xtts"]
    assert reasons["whisper"] == "argus-echo-whisper publishes no port"

    reg2, _ = registry()
    reg2.register(ECHO)
    reg2.reconcile()
    assert {p.reason for p in reg2.placements} == {
        "no agent reports container 'argus-echo-xtts'",
        "no agent reports container 'argus-echo-whisper'",
    }


def test_a_resident_on_several_nodes_gets_one_service_each() -> None:
    reg, _ = registry(services=[])
    reg.register(ECHO)
    reg.observe("dragon", [entry("argus-echo-xtts", (8141, 8141))])
    reg.observe("morpheus", [entry("argus-echo-xtts", (8141, 8141))])
    reg.reconcile()
    by_id = {s.id: s for s in reg.services}
    assert set(by_id) == {"xtts-morpheus", "xtts-dragon"}
    assert all(s.answers("xtts") for s in by_id.values())  # the lease target still reaches both


def test_registered_ids_never_take_a_configured_one() -> None:
    reg, _ = registry(
        services=[
            {"id": "xtts", "node": "morpheus", "adapter": "argus-http", "base_url": "http://x:1", "container": "c"}
        ]
    )
    reg.register(ECHO)
    reg.observe("dragon", [entry("argus-echo-xtts", (8141, 8141))])
    reg.reconcile()
    (svc,) = reg.services
    assert svc.id == "xtts-dragon" and svc.answers("xtts")


def test_lapsed_registration_keeps_services_until_the_container_goes() -> None:
    reg, clock = registry(services=[])
    reg.register(ECHO)
    reg.observe("dragon", [entry("argus-echo-xtts", (8141, 8141), running=False)])  # stopped: still warmable
    reg.reconcile()
    clock.now += timedelta(seconds=reg.config.registration_ttl_s + 1)
    reg.reconcile()
    assert [s.id for s in reg.services] == ["xtts"]
    reg.observe("dragon", None)  # an agent that could not say removes nothing
    reg.reconcile()
    assert [s.id for s in reg.services] == ["xtts"]
    reg.observe("dragon", [])  # the container was removed
    assert reg.reconcile()
    assert reg.services == [] and reg.config.services == []


def test_registrations_survive_a_restart_in_a_private_file(tmp_path: Path) -> None:
    reg, _ = registry(tmp_path)
    reg.register(ECHO, token="echo-admin", container_id="ab" * 32)
    reg.announce("jarrah", "http://192.168.1.109:9110")
    path = tmp_path / "registrations.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # it holds admin tokens

    again, _ = registry(tmp_path)
    again.load()
    assert again.tenants["argus-echo"].manifest == ECHO and again.tenants["argus-echo"].token_for() == "echo-admin"
    assert again.config.nodes["jarrah"].agent_url == "http://192.168.1.109:9110"


def test_announced_nodes_are_fenced() -> None:
    reg, _ = registry()
    assert reg.announce("morpheus", "http://10.0.0.1:9110") == "configured"
    assert reg.config.nodes["morpheus"].agent_url == "http://192.168.1.39:9110"  # config wins
    assert reg.announce("jarrah", "http://192.168.1.109:9110/") == "added"
    assert reg.announce("jarrah", "http://192.168.1.109:9110") == "renewed"
    for name, url in [("Bad Name", "http://x:1"), ("ok", "file:///etc"), ("ok", "http://u:p@x:1"), ("ok", "x:1")]:
        with pytest.raises(RegistrationError):
            reg.announce(name, url)
    off, _ = registry(register_nodes=False)
    with pytest.raises(RegistrationError, match="register_nodes"):
        off.announce("jarrah", "http://192.168.1.109:9110")


# ── through the broker and its routes ────────────────────────────────────────────────────────────────────────────


def agents(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    inventories = {
        "http://192.168.1.198:9110/snapshot": ("dragon", [entry("argus-echo-xtts", (8141, 8141), managed=True)]),
        "http://192.168.1.39:9110/snapshot": ("morpheus", []),
        "http://192.168.1.109:9110/snapshot": ("jarrah", []),
    }
    if url in inventories:
        node, inv = inventories[url]
        return httpx.Response(
            200,
            json=NodeSnapshot(
                node=node,
                taken_at=T0,
                agent_version="test",
                gpus=[GpuInfo(index=0, name="gpu", uuid="GPU-x", total_mb=8188, free_mb=6000, used_mb=2188)],
                processes=[],
                inventory=inv,
            ).model_dump(mode="json"),
        )
    if url == "http://192.168.1.198:8141/health":
        assert request.headers["authorization"] == "Bearer echo-admin"  # like a configured token_env, on every call
        return httpx.Response(200, json={"loaded": True, "model_state": "gpu", "busy": False, "idle_s": 3})
    return httpx.Response(404)


@pytest.fixture
def api(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", CALLER)
    monkeypatch.setenv("IRIS_AGENT_TOKEN", AGENT)
    monkeypatch.setenv("IRIS_STATE_DIR", str(tmp_path))
    broker = Broker(
        BrokerConfig.model_validate(CONFIG), client=httpx.AsyncClient(transport=httpx.MockTransport(agents))
    )
    with TestClient(create_app(broker=broker, poll=False)) as client:
        yield client, broker


async def test_register_then_poll_reads_the_new_service(api) -> None:
    client, broker = api
    auth = {"Authorization": f"Bearer {CALLER}"}
    r = client.post(
        "/tenants",
        json=ECHO.model_dump(mode="json"),
        headers={**auth, "X-Argus-Admin-Token": "echo-admin", "X-Argus-Container-Id": "not-an-id"},
    )
    assert r.status_code == 200 and r.json()["changed"]
    assert list(broker.registry.tenants["argus-echo"].instances) == [""]  # a malformed id is ignored, not trusted

    await broker.poll_once()
    services = {s["id"]: s for s in client.get("/services").json()}
    assert services["echo-xtts"]["origin"] == "config"
    xtts = services["xtts"]
    assert xtts["origin"] == "tenant" and xtts["node"] == "dragon" and xtts["managed"] is True
    assert xtts["admin_token"] is True and "token" not in str(client.get("/registry").json()).replace("admin_token", "")
    dragon = next(n for n in client.get("/nodes").json() if n["name"] == "dragon")
    assert any(s["id"] == "xtts" and s["up"] for s in dragon["services"])
    # /config stays the file as loaded.
    assert [s["id"] for s in client.get("/config").json()["services"]] == ["echo-xtts"]

    # A client's sign-off (every shutdown) only lapses it: the container is still there, so the service stays.
    r = client.delete("/tenants/argus-echo", headers=auth)
    assert r.status_code == 200 and r.json()["lapsed"]
    assert "xtts" in {s["id"] for s in client.get("/services").json()}
    assert client.get("/registry").json()["tenants"][0]["lapsed"]
    # The dashboard's Forget purges it.
    assert client.delete("/tenants/argus-echo?purge=true", headers=auth).json()["forgotten"]
    assert "xtts" not in {s["id"] for s in client.get("/services").json()}
    assert "argus-echo" not in broker.leases.tenants
    assert client.delete("/tenants/argus-echo", headers=auth).status_code == 404


def test_nodes_announce_with_the_agent_token(api) -> None:
    client, broker = api
    body = {"name": "jarrah", "agent_url": "http://192.168.1.109:9110"}
    assert client.post("/nodes", json=body).status_code == 401
    assert client.post("/nodes", json=body, headers={"Authorization": f"Bearer {CALLER}"}).status_code == 401
    r = client.post("/nodes", json=body, headers={"Authorization": f"Bearer {AGENT}"})
    assert r.status_code == 200 and r.json()["status"] == "added"
    assert "jarrah" in broker.nodes and "jarrah" in broker.config.nodes
    origins = {n["name"]: n["origin"] for n in client.get("/registry").json()["nodes"]}
    assert origins == {"morpheus": "config", "dragon": "config", "jarrah": "agent"}
    # No agent_url: the address the announcement came from.
    r = client.post("/nodes", json={"name": "spare", "port": 9111}, headers={"Authorization": f"Bearer {AGENT}"})
    assert r.json()["agent_url"] == "http://testclient:9111"

    auth = {"Authorization": f"Bearer {CALLER}"}
    assert client.delete("/nodes/dragon", headers=auth).status_code == 409  # configured
    assert client.delete("/nodes/jarrah", headers=auth).status_code == 200
    assert "jarrah" not in broker.nodes and "jarrah" not in broker.config.nodes


def test_operator_tokens_never_reach_a_registered_host(monkeypatch) -> None:
    monkeypatch.setenv("SECRET", "operator")
    svc = RegisteredService(
        id="x", node="dragon", adapter="argus-http", base_url="http://h:1", token_env="SECRET", tenant="t", resident="x"
    )
    assert svc.headers() == {}


def test_several_processes_register_one_tenant() -> None:
    """argus-echo: each engine is its own process and container, registering the same tenant."""
    xtts_id, whisper_id = "a" * 64, "b" * 64
    reg, clock = registry(services=[])
    assert reg.register(ECHO, token="xtts-token", container_id=xtts_id)
    assert reg.register(ECHO, token="whisper-token", container_id=whisper_id)  # a new instance is a change
    assert not reg.register(ECHO, token="xtts-token", container_id=xtts_id)  # a renewal is not
    reg.observe(
        "dragon",
        [entry("argus-echo-xtts", (8141, 8141), id=xtts_id), entry("argus-echo-whisper", (8144, 8144), id=whisper_id)],
    )
    reg.reconcile()
    by_id = {s.id: s for s in reg.services}
    # Each resident gets the token of the process in its own container, whichever renewed last.
    assert by_id["xtts"].headers() == {"Authorization": "Bearer xtts-token"}
    whisper = ECHO.residents[1]
    assert not whisper.admin_auth and by_id["whisper"].headers() == {}

    # One engine signing off lapses only itself; the tenant lapses once every instance has.
    reg.forget("argus-echo", container_id=xtts_id)
    assert not reg.lapsed(reg.tenants["argus-echo"])
    reg.forget("argus-echo", container_id=whisper_id)
    assert reg.lapsed(reg.tenants["argus-echo"])
    assert [s.id for s in reg.services] == ["xtts", "whisper"]  # containers still there: kept


def test_registration_file_keeps_instances(tmp_path: Path) -> None:
    reg, _ = registry(tmp_path)
    reg.register(ECHO, token="t1", container_id="a" * 64)
    reg.register(ECHO, token="t2", container_id="b" * 64)
    again, _ = registry(tmp_path)
    again.load()
    inst = again.tenants["argus-echo"].instances
    assert {k: v.token for k, v in inst.items()} == {"a" * 64: "t1", "b" * 64: "t2"}
