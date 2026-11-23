from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from argus_cortex.service import ServiceHealth
from fastapi.testclient import TestClient
from pydantic import ValidationError

import argus_iris
from argus_iris.broker.config import BrokerConfig, load_config
from argus_iris.broker.state import Broker
from argus_iris.models import GpuInfo, GpuProcess, NodeSnapshot
from argus_iris.server.app import create_app

T0 = datetime(2026, 11, 15, tzinfo=UTC)
GIB = 1024**3

CONFIG = {
    "nodes": {
        "morpheus": {"agent_url": "http://morpheus:9110/", "reserve_mb": 3500},
        "dragon": {"agent_url": "http://dragon:9110"},
    },
    "services": [
        {
            "id": "comfyui",
            "node": "morpheus",
            "adapter": "comfyui",
            "base_url": "http://morpheus:8188",
            "container": "voluptas_comfyui",
        },
        {
            "id": "lens",
            "node": "morpheus",
            "adapter": "argus-http",
            "base_url": "http://morpheus:8101",
            "container": "lens",
        },
        {
            "id": "ollama",
            "node": "dragon",
            "adapter": "ollama",
            "base_url": "http://dragon:11434",
            "container": "ollama",
        },
    ],
}


def proc(pid: int, vram: int, container: str | None, idle: float = 0.0) -> GpuProcess:
    return GpuProcess(pid=pid, gpu=0, vram_mb=vram, container=container, service=container, first_seen=T0, idle_s=idle)


def snapshot(node: str, total: int, free: int, procs: list[GpuProcess]) -> dict:
    return NodeSnapshot(
        node=node,
        taken_at=T0,
        agent_version="test",
        gpus=[GpuInfo(index=0, name="gpu", uuid="GPU-x", total_mb=total, free_mb=free, used_mb=total - free)],
        processes=procs,
    ).model_dump(mode="json")


def network(*, dragon_agent_up: bool = True, comfy_queue: int = 0, tags: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://morpheus:9110/snapshot":
            return httpx.Response(
                200,
                json=snapshot(
                    "morpheus",
                    24576,
                    13000,
                    [
                        proc(1, 7352, "voluptas_comfyui", 400),
                        proc(2, 2254, "lens", 30),
                        proc(3, 500, "a2f", 5),
                        proc(4, 470, None),
                    ],
                ),
            )
        if url == "http://dragon:9110/snapshot":
            if not dragon_agent_up:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json=snapshot("dragon", 8188, 900, [proc(10, 6400, "ollama", 12)]))
        if url == "http://morpheus:8188/system_stats":
            return httpx.Response(
                200, json={"system": {"comfyui_version": "0.19.3"}, "devices": [{"vram_total": 24 * GIB}]}
            )
        if url == "http://morpheus:8188/queue":
            return httpx.Response(200, json={"queue_running": [["x"]] * comfy_queue, "queue_pending": []})
        if url == "http://morpheus:8101/health":
            return httpx.Response(200, json={"status": "ok", "vram_status": {"loaded": True}})
        if url == "http://dragon:11434/api/tags" and tags:
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"model": "nomic-embed-text:latest"}]})
        if url == "http://dragon:11434/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "hf.co/inclusionAI/SingGuard-4b-GGUF:Q4_K_M",
                            "size": 6 * GIB,
                            "size_vram": 6 * GIB,
                            "expires_at": "2026-11-15T19:00:00.123456789Z",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_poll_composes_residents_and_ledger() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network())
    await broker.poll_once()
    m = broker.nodes["morpheus"]
    assert m.reachable
    by_id = {r.id: r for r in m.residents}
    assert set(by_id) == {"morpheus/comfyui", "morpheus/lens", "morpheus/a2f", "morpheus/pid-4"}
    assert by_id["morpheus/comfyui"].vram_mb == 7352 and by_id["morpheus/comfyui"].idle_s == 400
    assert by_id["morpheus/lens"].detail["vram_status"] == {"loaded": True}
    assert by_id["morpheus/a2f"].source == "process"  # observed: no service configured for it

    ledger = m.ledger
    assert ledger is not None
    assert ledger.resident_mb == 7352 + 2254 + 500 + 470
    assert ledger.unattributed_mb == (24576 - 13000) - ledger.resident_mb
    # managed (comfyui + lens) + reserve + headroom, capped by NVML free - headroom
    assert ledger.admissible_mb == min(13000 - 512, 24576 - 3500 - 512 - (7352 + 2254))

    d = broker.nodes["dragon"]
    (guard,) = d.residents
    assert guard.model == "hf.co/inclusionAI/SingGuard-4b-GGUF:Q4_K_M"
    assert guard.vram_mb == 6144 and guard.idle_s == 12
    assert guard.expires_at == datetime(2026, 11, 15, 19, 0, 0, 123456, tzinfo=UTC)
    # NVML free binds here; the inventory term counts the runner process (6400), larger than the model's share (6144).
    assert d.ledger is not None and d.ledger.admissible_mb == min(900 - 512, 8188 - 512 - 6400)


async def test_busy_comfyui_is_never_idle() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network(comfy_queue=1))
    await broker.poll_once()
    comfy = next(r for r in broker.nodes["morpheus"].residents if r.service == "comfyui")
    assert comfy.busy and comfy.idle_s is None


async def test_ollama_reports_its_pulled_models() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network())
    await broker.poll_once()
    (svc,) = [s for s in broker.nodes["dragon"].services if s.adapter == "ollama"]
    assert (svc.up, svc.available) == (True, ["nomic-embed-text:latest", "qwen3:8b"])

    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network(tags=False))
    await broker.poll_once()
    (svc,) = [s for s in broker.nodes["dragon"].services if s.adapter == "ollama"]
    assert (svc.up, svc.available) == (True, None)  # /api/tags failing is "unknown", not "has nothing"


async def test_unreachable_agent_keeps_ollama_residents() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network(dragon_agent_up=False))
    await broker.poll_once()
    d = broker.nodes["dragon"]
    assert not d.reachable and "ConnectError" in (d.error or "")
    assert d.ledger is None
    assert [r.model for r in d.residents] == ["hf.co/inclusionAI/SingGuard-4b-GGUF:Q4_K_M"]


async def test_events_published_only_on_change() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network())
    async with broker.subscribe() as queue:
        await broker.poll_once()
        assert queue.qsize() == 2
        await broker.poll_once()  # nothing changed but timestamps
        assert queue.qsize() == 2


def test_http_api() -> None:
    broker = Broker(BrokerConfig.model_validate(CONFIG), client=network())
    with TestClient(create_app(broker=broker, poll=False)) as client:
        client.portal.call(broker.poll_once)
        health = client.get("/health").json()
        assert health["agents"] == {"morpheus": True, "dragon": True} and health["mode"] == "observe-only"
        # Every field /health had before argus-cortex's ServiceHealth, with the same meaning.
        assert health["status"] == "ok" and health["version"] == argus_iris.__version__
        assert {"polls", "leases", "pins_missing", "crowded_out", "registered"} <= set(health)
        assert health["leases"] == {"active": 0, "tenants": 0}
        assert set(health["registered"]) == {"tenants", "services", "nodes"}
        # And it is the suite's contract: who it is, the wire versions it speaks, its dependency checks.
        contract = ServiceHealth.model_validate(health)
        assert contract.service == "argus-iris" and contract.checks["agents"].ok
        assert set(contract.wire) == {"lease_version", "tenant_version", "resident_version", "directory_version"}
        assert client.get("/health", params={"checks": "0"}).json()["checks"] == {}
        # An unreachable agent is degraded on the very next read (the check is not cached), and still a 200.
        broker.nodes["dragon"].reachable = False
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "degraded" and r.json()["agents"]["dragon"] is False
        assert r.json()["checks"]["agents"]["detail"] == "1 of 2 unreachable"
        broker.nodes["dragon"].reachable = True
        assert client.get("/health").json()["status"] == "ok"
        assert {n["name"] for n in client.get("/nodes").json()} == {"morpheus", "dragon"}
        assert client.get("/nodes/jarrah").status_code == 404
        assert [r["node"] for r in client.get("/residents", params={"node": "dragon"}).json()] == ["dragon"]
        metrics = client.get("/metrics").text
        assert 'iris_agent_up{node="morpheus"} 1.0' in metrics
        assert 'iris_resident_vram_mb{model="",node="morpheus",resident="morpheus/comfyui"' in metrics
        # The write guard is installed: a cross-site write is refused before any route.
        r = client.post("/leases", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


def test_config_validation(tmp_path: Path) -> None:
    bad = {**CONFIG, "services": [*CONFIG["services"], {**CONFIG["services"][0], "node": "jarrah", "id": "x"}]}
    with pytest.raises(ValidationError, match="unknown nodes"):
        BrokerConfig.model_validate(bad)
    dupe = {**CONFIG, "services": [CONFIG["services"][0], CONFIG["services"][0]]}
    with pytest.raises(ValidationError, match="duplicate service ids"):
        BrokerConfig.model_validate(dupe)

    path = tmp_path / "iris.yaml"
    path.write_text("nodes:\n  jarrah: { agent_url: 'http://jarrah:9110/' }\n")
    cfg = load_config(path)
    assert cfg.nodes["jarrah"].agent_url == "http://jarrah:9110"
    assert cfg.nodes["jarrah"].headroom_mb == 512


def test_example_config_loads() -> None:
    load_config(Path(__file__).parent.parent / "deploy" / "iris.example.yaml")


def test_parse_health_reads_the_resident_contract() -> None:
    from argus_iris.broker.adapters.http import parse_health

    # ResidentHealth (argus-cortex 0.7): voluptas image-gen-diffusers, agent-composer's sidecars, extras kept
    r = parse_health(
        {"status": "ok", "model_state": "gpu", "busy": False, "idle_s": 3.5, "gpu": {"total_mb": 24576, "loaded": True}}
    )
    assert (r.busy, r.idle_s, r.detail) == (False, 3.5, {"model_state": "gpu", "status": "ok"})
    r = parse_health({"model_state": "gpu", "busy": True, "idle_s": None})
    assert (r.busy, r.idle_s) == (True, None) and "off_contract" not in r.detail


def test_parse_health_reads_shapes_off_the_contract() -> None:
    from argus_iris.broker.adapters.http import parse_health

    # voluptas Miso (smk762/voluptas#26): busy is an object while working, null when idle. Busy must not read as idle.
    miso = {"status": "ok", "model_state": "gpu", "idle_s": None, "gpu": {"allocated_mb": 8.1, "reserved_mb": 2004.0}}
    r = parse_health({**miso, "busy": {"request_id": "r1", "endpoint": "stream", "busy_s": 3.2}})
    assert (r.busy, r.idle_s, r.detail["model_state"]) == (True, None, "gpu")
    assert "busy" in r.detail["off_contract"]
    r = parse_health({**miso, "busy": None, "idle_s": 40})
    assert (r.busy, r.idle_s) == (False, 40.0)
    # argus-lens: the state in a gpu block
    r = parse_health(
        {"status": "ok", "gpu": {"loaded": True, "model_state": "cpu", "idle_s": 120, "free_vram_mb": None}}
    )
    assert (r.busy, r.idle_s) == (False, 120.0)
    assert {k: r.detail[k] for k in ("loaded", "model_state", "status")} == {
        "loaded": True,
        "model_state": "cpu",
        "status": "ok",
    }
    assert parse_health({"gpu": {"loaded": False, "idle_s": None}}).idle_s is None
    r = parse_health("plain text")
    assert r.up and r.detail["off_contract"]


async def test_off_contract_is_logged_once_per_change(monkeypatch) -> None:
    from argus_iris.broker.adapters import http as http_adapter
    from argus_iris.broker.config import ServiceConfig

    bodies = iter([{"busy": None}, {"busy": None}, {"model_state": "unloaded", "busy": False}])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(bodies))))
    logged: list[str] = []
    monkeypatch.setattr(http_adapter.log, "warning", lambda event, **kw: logged.append(event))
    monkeypatch.setattr(http_adapter.log, "info", lambda event, **kw: logged.append(event))
    adapter = http_adapter.ArgusHttpAdapter(
        ServiceConfig(id="miso", node="morpheus", adapter="argus-http", base_url="http://morpheus:8034")
    )
    for _ in range(3):
        await adapter.read(client)
    assert logged == ["resident_off_contract", "resident_on_contract"]


async def test_service_idle_and_token(monkeypatch) -> None:
    monkeypatch.setenv("RAG_CHAT_TOKEN", "k3y")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/snapshot"):
            return httpx.Response(200, json=snapshot("dragon", 8188, 4000, [proc(7, 1300, "xtts", idle=2)]))
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"gpu": {"loaded": True, "model_state": "gpu", "idle_s": 900}})

    cfg = BrokerConfig.model_validate(
        {
            "nodes": {"dragon": {"agent_url": "http://dragon:9110"}},
            "services": [
                {
                    "id": "xtts",
                    "node": "dragon",
                    "adapter": "argus-http",
                    "base_url": "http://dragon:9150/api/runtime/residents/xtts",
                    "container": "xtts",
                    "token_env": "RAG_CHAT_TOKEN",
                }
            ],
        }
    )
    broker = Broker(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await broker.poll_once()
    (xtts,) = broker.nodes["dragon"].residents
    assert seen == ["Bearer k3y"]
    # The service's own idle time (900 s since its last request) beats the agent's (VRAM moved 2 s ago).
    assert xtts.idle_s == 900 and xtts.vram_mb == 1300


def test_config_rejects_unknown_keys() -> None:
    svc = {**CONFIG["services"][0], "evict_paht": "/admin/unload"}
    with pytest.raises(ValidationError, match="evict_paht"):
        BrokerConfig.model_validate({**CONFIG, "services": [svc]})


@pytest.mark.parametrize("path", ["admin/unload", "http://x/admin/unload", "/admin/un load"])
def test_route_paths_must_be_absolute(path: str) -> None:
    svc = {**CONFIG["services"][1], "unload_path": path}
    with pytest.raises(ValidationError):
        BrokerConfig.model_validate({**CONFIG, "services": [svc]})
