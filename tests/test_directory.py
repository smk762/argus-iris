"""The service directory (#48): containers labelled argus.peer, their URLs, health, lookup and Prometheus SD."""

from __future__ import annotations

import httpx
import pytest
import structlog
from argus_cortex.service import DirectoryEntry, PeerLookup
from fastapi.testclient import TestClient

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.directory import Directory, read_health
from argus_iris.broker.state import Broker
from argus_iris.models import ContainerEntry, PortMap
from argus_iris.server.app import create_app

CALLER = "caller-token"
CONFIG = {
    "nodes": {
        "morpheus": {"agent_url": "http://192.168.1.39:9110"},
        "dragon": {"agent_url": "http://192.168.1.198:9110"},
    },
}


def c(name: str, *ports: tuple[int, int | None], labels: dict | None = None, running: bool = True, ips=None):
    return ContainerEntry(
        id=name.encode().hex().ljust(64, "0")[:64],
        name=name,
        running=running,
        ports=[PortMap(private=p, public=q, ips=ips or []) for p, q in ports],
        labels=labels or {},
    )


INVENTORIES = {
    "morpheus": [
        c("argus-retina-nudenet-1", (8080, 8130), labels={"argus.peer": "nudenet", "argus.service": "argus-retina"}),
        c("argus-retina-singguard-1", (8080, 8134), labels={"argus.peer": "singguard"}),
        c("unlabelled", (5432, 5432)),
        c("argus-mask-a2f-1", (8110, 8110), labels={"argus.peer": "a2f"}, ips=["127.0.0.1"]),
        c(
            "lens",
            (8100, 8100),
            (9000, 9000),
            labels={"argus.peer": "lens", "argus.port": "8100", "argus.health": "healthz"},
        ),
    ],
    "dragon": [
        c("argus-retina-nudenet-1", (8080, 8130), labels={"argus.peer": "nudenet"}),
        c("stopped", (8080, 8150), labels={"argus.peer": "ocr"}, running=False),
    ],
}


def directory(**overrides) -> Directory:
    config = BrokerConfig.model_validate({**CONFIG, **overrides})
    d = Directory(config, httpx.AsyncClient())
    d.rebuild(INVENTORIES)
    return d


def test_labelled_containers_become_entries_with_agent_host_urls() -> None:
    d = directory()
    rows = {(e.node, e.peer): e for e in d.entries}
    assert set(rows) == {
        ("morpheus", "nudenet"),
        ("morpheus", "singguard"),
        ("morpheus", "a2f"),
        ("morpheus", "lens"),
        ("dragon", "nudenet"),
        ("dragon", "ocr"),
    }  # the unlabelled container is not in the directory
    assert rows["morpheus", "nudenet"].url == "http://192.168.1.39:8130"  # the published port, the agent's host
    assert rows["morpheus", "nudenet"].service == "argus-retina"
    a2f = rows["morpheus", "a2f"]
    assert a2f.url is None and "127.0.0.1 only" in a2f.reason  # reported, never served
    lens = rows["morpheus", "lens"]
    assert lens.url == "http://192.168.1.39:8100" and lens.health_path == "/healthz"  # argus.port picks; path fixed up
    assert not rows["dragon", "ocr"].running


def test_config_peers_win_and_are_validated() -> None:
    d = directory(peers={"nudenet": "http://10.0.0.5:8130/"})
    assert [e.url for e in d.lookup("nudenet").urls][0] == "http://10.0.0.5:8130"
    with pytest.raises(ValueError, match="http"):
        BrokerConfig.model_validate({**CONFIG, "peers": {"x": "ftp://h"}})
    with pytest.raises(ValueError, match="credentials"):
        BrokerConfig.model_validate({**CONFIG, "peers": {"x": "http://u:p@h"}})


def test_read_health_takes_the_contract_and_tolerates_anything_else() -> None:
    # A body that is argus-cortex's ServiceHealth is read through that model.
    full = read_health(
        200,
        {
            "service_version": "1.0",
            "service": "argus-retina",
            "version": "0.3.0",
            "status": "degraded",
            "started_at": "2026-11-23T00:00:00Z",
            "wire": {"infer_version": "1.0"},
            "capabilities": ["nudity"],
            "checks": {"model": {"ok": False, "detail": "loading http://u:p@h/x", "latency_ms": 3.5}},
            "token": "never kept",
        },
    )
    assert full.contract and full.status == "degraded" and full.version == "0.3.0"
    assert full.wire == {"infer_version": "1.0"} and full.capabilities == ("nudity",)
    assert full.checks["model"].latency_ms == 3.5 and "u:p" not in full.checks["model"].detail
    assert "never kept" not in full.model_dump_json()
    # Nearly one (no started_at, a wire value that is not a string): read field by field, and not called a contract.
    near = read_health(
        200,
        {
            "service": "argus-retina",
            "version": "0.3.0",
            "status": "degraded",
            "wire": {"infer_version": "1.0", "bad": 3},
            "capabilities": ["nudity", 7],
            "checks": {"model": {"ok": False, "detail": "loading"}, "junk": "x"},
            "token": "never kept",
        },
    )
    assert not near.contract and near.status == "degraded" and near.version == "0.3.0"
    assert near.wire == {"infer_version": "1.0"} and list(near.checks) == ["model"] and near.capabilities == ("nudity",)
    assert "never kept" not in near.model_dump_json()
    # Another major of the contract is not this contract.
    assert not read_health(200, {**full.model_dump(mode="json"), "service_version": "2.0"}).contract
    assert read_health(200, {"ok": True}).model_dump(include={"status", "contract"}) == {
        "status": "ok",
        "contract": False,
    }
    assert read_health(503, None).status == "starting"
    assert read_health(500, "boom").status == "down"
    assert read_health(None, None, error="ConnectError").status == "down"
    # What is kept from one body is bounded.
    many = read_health(200, {"capabilities": [f"c{i}" for i in range(100)], "wire": {f"k{i}": "1" for i in range(100)}})
    assert len(many.capabilities) == 32 and len(many.wire) == 32


def test_a_container_with_a_refused_label_is_skipped_and_logged_once() -> None:
    bad = {
        "morpheus": [
            *INVENTORIES["morpheus"],
            c("shouty", (8080, 8200), labels={"argus.peer": "NudeNet"}),
            c("spaced", (8080, 8201), labels={"argus.peer": "ok-name", "argus.health": "/health check"}),
            c("slashed", (8080, 8202), labels={"argus.peer": "../etc"}),
        ],
        "dragon": INVENTORIES["dragon"],
    }
    d = directory()
    with structlog.testing.capture_logs() as logs:
        d.rebuild(bad)
        d.rebuild(bad)  # the next poll: said once, not on every rebuild
    assert len(d.entries) == 6 and {e.container for e in d.entries}.isdisjoint({"shouty", "spaced", "slashed"})
    refused = [entry for entry in logs if entry["event"] == "directory_label_refused"]
    assert {(entry["container"], tuple(entry["fields"])) for entry in refused} == {
        ("shouty", ("peer",)),
        ("spaced", ("health_path",)),
        ("slashed", ("peer",)),
    }
    assert len(refused) == 3
    # A name no peer can have finds nothing, and does not raise.
    assert d.lookup("NudeNet") is None and d.lookup("../etc") is None
    assert all(g["labels"]["argus_peer"] != "NudeNet" for g in d.service_discovery())


def test_a_configured_peer_needs_a_peer_name() -> None:
    with pytest.raises(ValueError, match="not a peer name"):
        BrokerConfig.model_validate({**CONFIG, "peers": {"Nude_Net": "http://10.0.0.5:8130"}})


async def test_probe_and_lookup_order() -> None:
    def svc(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.168.1.39":
            return httpx.Response(503, json={"status": "starting"})
        return httpx.Response(200, json={"service": "argus-retina", "version": "0.3.0", "status": "ok"})

    d = directory()
    d._client = httpx.AsyncClient(transport=httpx.MockTransport(svc))
    await d.probe()
    found = d.lookup("nudenet")
    # dragon's is healthy, morpheus's still starting: healthiest first, whatever the node order.
    assert [e.node for e in found.urls] == ["dragon", "morpheus"]
    assert found.urls[0].health.version == "0.3.0"
    assert d.lookup("ocr").urls == ()  # stopped
    assert d.lookup("a2f").urls == ()  # loopback only
    # Health survives a rebuild (the next poll's inventories) as long as the entry does.
    d.rebuild(INVENTORIES)
    assert next(e for e in d.entries if e.node == "dragon" and e.peer == "nudenet").health.status == "ok"


def test_service_discovery_targets() -> None:
    groups = {g["labels"]["argus_peer"] + "@" + g["labels"].get("node", ""): g for g in directory().service_discovery()}
    assert set(groups) == {"nudenet@morpheus", "singguard@morpheus", "lens@morpheus", "nudenet@dragon"}
    g = groups["lens@morpheus"]
    assert g["targets"] == ["192.168.1.39:8100"] and g["labels"]["argus_health_path"] == "/healthz"


def test_routes(monkeypatch) -> None:
    monkeypatch.delenv("ARGUS_GPU_BROKER_TOKEN", raising=False)
    broker = Broker(BrokerConfig.model_validate(CONFIG))
    broker.directory.rebuild(INVENTORIES)
    with TestClient(create_app(broker=broker, poll=False)) as client:
        assert len(client.get("/directory").json()) == 6
        assert len(client.get("/directory/sd").json()) == 4
        # Lookup decides where work goes: refused while the broker has no caller token, then token-gated.
        assert client.get("/directory/nudenet").status_code == 403
        monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", CALLER)
        assert client.get("/directory/nudenet").status_code == 401
        auth = {"Authorization": f"Bearer {CALLER}"}
        r = client.get("/directory/nudenet", headers=auth)
        assert r.status_code == 200 and [u["node"] for u in r.json()["urls"]] == ["morpheus", "dragon"]
        assert client.get("/directory/nobody", headers=auth).status_code == 404
        assert client.get("/directory/Not_A_Name", headers=auth).status_code == 404  # not found, never a 500


ENTRY_FIELDS = {
    "peer",
    "origin",
    "service",
    "node",
    "container",
    "running",
    "url",
    "reason",
    "health_path",
    "health",
}
HEALTH_FIELDS = {
    "checked_at",
    "status",
    "http_status",
    "contract",
    "service",
    "version",
    "wire",
    "capabilities",
    "checks",
    "error",
}


async def test_the_wire_is_cortexs_models_with_the_fields_iris_served_before(monkeypatch) -> None:
    """#50: /directory and /directory/{peer} serve argus-cortex's models. The JSON keeps every field of iris's own
    models (which these replaced), and the lookup reply gains directory_version."""
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", CALLER)

    def svc(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service_version": "1.0",
                "service": "argus-retina",
                "version": "0.3.0",
                "status": "ok",
                "started_at": "2026-11-23T00:00:00Z",
                "capabilities": ["nudity"],
                "checks": {"model": {"ok": True}},
            },
        )

    broker = Broker(BrokerConfig.model_validate(CONFIG))
    broker.directory.rebuild(INVENTORIES)
    broker.directory._client = httpx.AsyncClient(transport=httpx.MockTransport(svc))
    await broker.directory.probe()
    with TestClient(create_app(broker=broker, poll=False)) as client:
        reply = client.get("/directory/nudenet", headers={"Authorization": f"Bearer {CALLER}"}).json()
        listing = client.get("/directory").json()
    assert set(reply) == {"directory_version", "peer", "urls"} and reply["directory_version"] == "1.0"
    for entry in (*reply["urls"], *listing):
        assert set(entry) == ENTRY_FIELDS
        DirectoryEntry.model_validate(entry)
    first = reply["urls"][0]
    assert (
        first["origin"] == "label" and first["url"] == "http://192.168.1.39:8130" and first["health_path"] == "/health"
    )
    assert set(first["health"]) == HEALTH_FIELDS
    assert first["health"]["capabilities"] == ["nudity"] and first["health"]["contract"] is True  # a JSON array still
    assert first["health"]["checks"]["model"]["ok"] is True
    # What cortex's client does with it: the first entry for the peer with an absolute http(s) URL.
    parsed = PeerLookup.model_validate(reply)
    assert parsed.peer == "nudenet" and parsed.urls[0].url == "http://192.168.1.39:8130"
    # A stopped entry has no health; an unlabelled service still gets its name from what its health says.
    rows = {(e["node"], e["peer"]): e for e in listing}
    assert rows["dragon", "ocr"]["health"] is None
    assert rows["morpheus", "singguard"]["service"] == "argus-retina"


async def test_only_due_entries_are_probed() -> None:
    from datetime import timedelta

    calls: list[str] = []

    def svc(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"})

    d = directory()
    now = [d._clock()]
    d._clock = lambda: now[0]
    d._client = httpx.AsyncClient(transport=httpx.MockTransport(svc))
    await d.probe()
    first = len(calls)
    assert first == 4  # every reachable, running entry, at once
    await d.probe()
    assert len(calls) == first  # none due yet
    now[0] += timedelta(seconds=d.config.directory_health_s)
    await d.probe()
    assert len(calls) == 2 * first


def test_the_brokers_own_health_reads_as_the_contract() -> None:
    """iris labels itself argus.peer: iris, so its directory reads its own /health."""
    with TestClient(create_app(broker=Broker(BrokerConfig.model_validate(CONFIG)), poll=False)) as client:
        r = client.get("/health")
    own = read_health(r.status_code, r.json())
    assert own.contract and own.service == "argus-iris" and own.wire["directory_version"] == "1.0"
    assert own.status == "degraded" and not own.checks["agents"].ok  # no agent has been polled yet
