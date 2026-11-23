"""The service directory (#48): containers labelled argus.peer, their URLs, health, lookup and Prometheus SD."""

from __future__ import annotations

import httpx
import pytest
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
    full = read_health(
        200,
        {
            "service": "argus-retina",
            "version": "0.3.0",
            "status": "degraded",
            "wire": {"infer_version": "1.0", "bad": 3},
            "capabilities": ["nudity"],
            "checks": {"model": {"ok": False, "detail": "loading"}, "junk": "x"},
            "token": "never kept",
        },
    )
    assert full.contract and full.status == "degraded" and full.version == "0.3.0"
    assert full.wire == {"infer_version": "1.0"} and list(full.checks) == ["model"]
    assert "never kept" not in full.model_dump_json()
    assert read_health(200, {"ok": True}).model_dump(include={"status", "contract"}) == {
        "status": "ok",
        "contract": False,
    }
    assert read_health(503, None).status == "starting"
    assert read_health(500, "boom").status == "down"
    assert read_health(None, None, error="ConnectError").status == "down"


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
    assert d.lookup("ocr").urls == []  # stopped
    assert d.lookup("a2f").urls == []  # loopback only
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
