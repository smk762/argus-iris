"""Evicting residents: each adapter's route, refusing while busy, and confirming the memory came back."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from argus_iris.broker.actions import ActionError, evict_resident
from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.state import Broker
from argus_iris.models import ContainerMemory, GpuInfo, GpuProcess, NodeSnapshot
from argus_iris.server.app import create_app

T0 = datetime(2026, 11, 16, tzinfo=UTC)
MIB = 1024**2
SINGGUARD = "hf.co/inclusionAI/SingGuard-4b-GGUF:Q4_K_M"

CONFIG = {
    "nodes": {"dragon": {"agent_url": "http://dragon:9110"}, "morpheus": {"agent_url": "http://morpheus:9110"}},
    "services": [
        {
            "id": "ollama",
            "node": "dragon",
            "adapter": "ollama",
            "base_url": "http://dragon:11434",
            "container": "ollama",
        },
        {
            "id": "xtts",
            "node": "dragon",
            "adapter": "argus-http",
            "base_url": "http://dragon:9150/r/xtts",
            "container": "xtts",
            "evict_path": "/admin/evict",
        },
        {
            "id": "whisper",
            "node": "dragon",
            "adapter": "argus-http",
            "base_url": "http://dragon:9150/r/whisper",
            "container": "whisper",
        },
        {
            "id": "comfyui",
            "node": "morpheus",
            "adapter": "comfyui",
            "base_url": "http://morpheus:8188",
            "container": "voluptas_comfyui",
        },
    ],
    "evict_confirm_s": 0.2,
}


class World:
    """dragon with Ollama (SingGuard), xtts and whisper on the GPU; morpheus with ComfyUI. Evicts change what the
    agent then reports, as the real services do."""

    def __init__(self) -> None:
        self.vram = {"ollama": 4000, "xtts": 2400, "whisper": 1500, "voluptas_comfyui": 6000, "frigate": 150}
        self.state = {"xtts": "gpu", "whisper": "gpu"}
        self.ollama_models = [SINGGUARD]
        self.expires: dict[str, str] = {}  # model -> RFC 3339 expiry, as /api/ps reports it
        self.context: dict[str, int] = {}  # model -> context_length it was loaded with, as /api/ps reports it
        self.comfy_queue = 0
        self.busy: dict[str, int] = {}
        self.busy_on_post: dict[str, int] = {}  # work that arrives between iris's health read and its POST
        self.residual = {"whisper": 94}  # CUDA context left behind after an unload
        self.loads_on_post: dict[str, int] = {}  # a request reloads the model (MB) just as iris's call lands
        self.frees_memory = True
        self.anon: dict[str, int] = {}  # container -> cgroup anon MB, as the agent reports it
        self.calls: list[str] = []

    def _snap(self, node: str) -> dict:
        names = ["ollama", "xtts", "whisper", "frigate"] if node == "dragon" else ["voluptas_comfyui"]
        procs = [
            GpuProcess(pid=i + 1, gpu=0, vram_mb=self.vram[n], container=n, service=n, first_seen=T0, idle_s=60)
            for i, n in enumerate(names)
            if self.vram[n]
        ]
        used = sum(p.vram_mb for p in procs)
        return NodeSnapshot(
            node=node,
            taken_at=T0,
            agent_version="t",
            gpus=[GpuInfo(index=0, name="g", uuid=node, total_mb=8192, free_mb=8192 - used, used_mb=used)],
            processes=procs,
            containers=[ContainerMemory(container=n, anon_mb=mb) for n, mb in self.anon.items() if n in names],
        ).model_dump(mode="json")

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        if method == "POST":
            self.calls.append(f"POST {url} {request.content.decode() or ''}".strip())
        if ":9110/containers/" in url and method == "POST":  # the agent: start or restart a container
            name, action = url.split("/")[-2:]
            if action == "restart" and name in self.vram:
                self.vram[name] = 0  # the CUDA context goes with the process
            return httpx.Response(200, json={"container": name, "action": action, "ok": True})
        if url.endswith(":9110/snapshot"):
            return httpx.Response(200, json=self._snap("dragon" if "dragon" in url else "morpheus"))
        if url == "http://dragon:11434/api/ps":
            models = [
                {
                    "name": m,
                    "size": 4000 * MIB,
                    "size_vram": 4000 * MIB,
                    "expires_at": self.expires.get(m),
                    **({"context_length": self.context[m]} if m in self.context else {}),
                }
                for m in self.ollama_models
            ]
            return httpx.Response(200, json={"models": models})
        if url == "http://dragon:11434/api/generate":
            body = json.loads(request.content)
            if body["keep_alive"] != 0:  # -1 pins; "900s" extends
                self.expires[body["model"]] = "2318-01-01T00:00:00Z" if body["keep_alive"] == -1 else "extended"
                return httpx.Response(200, json={"done": True})
            self.ollama_models.remove(body["model"])
            self.vram["ollama"] = 300 if self.frees_memory else self.vram["ollama"]
            return httpx.Response(200, json={"done": True, "done_reason": "unload"})
        for name in ("xtts", "whisper"):
            base = f"http://dragon:9150/r/{name}"
            if url == f"{base}/health":
                return httpx.Response(
                    200, json={"model_state": self.state[name], "busy": name in self.busy, "idle_s": 60.0}
                )
            if method == "POST" and url.startswith(base):
                active = self.busy.get(name) or self.busy_on_post.get(name)
                if active:
                    return httpx.Response(409, json={"status": "busy", "active": active})
                to = "cpu" if url.endswith("/admin/evict") else "unloaded"
                self.state[name] = to
                self.vram[name] = self.residual.get(name, 0)
                if name in self.loads_on_post:  # the reply says what the call did; /health then says gpu
                    self.state[name], self.vram[name] = "gpu", self.loads_on_post.pop(name)
                return httpx.Response(200, json={"status": "ok", "model_state": to, "unloaded": True})
        if url == "http://morpheus:8188/system_stats":
            return httpx.Response(200, json={"devices": []})
        if url == "http://morpheus:8188/queue":
            return httpx.Response(200, json={"queue_running": [1] * self.comfy_queue, "queue_pending": []})
        if url == "http://morpheus:8188/free":
            self.vram["voluptas_comfyui"] = 400
            return httpx.Response(200)
        raise httpx.ConnectError(f"refused {url}")


@pytest.fixture
async def setup():
    world = World()
    broker = Broker(
        BrokerConfig.model_validate(CONFIG), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    )
    await broker.poll_once()
    return world, broker


async def test_ollama_model_is_unloaded_by_keep_alive_zero(setup):
    world, broker = setup
    result = await evict_resident(broker, f"dragon/ollama/{SINGGUARD}", poll_s=0)
    assert world.calls == [f'POST http://dragon:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":0}}']
    assert (result.action, result.before_mb, result.after_mb, result.confirmed) == ("unload", 4000, 0, True)
    assert not any(r.model == SINGGUARD for r in broker.residents())  # the node was re-polled and published


async def test_argus_http_prefers_evict_to_cpu(setup):
    world, broker = setup
    result = await evict_resident(broker, "dragon/xtts", poll_s=0)
    assert world.calls == ["POST http://dragon:9150/r/xtts/admin/evict"]
    assert (result.action, result.model_state, result.confirmed, result.after_mb) == ("evict", "cpu", True, 0)
    unload = await evict_resident(broker, "dragon/whisper", mode="auto", poll_s=0)  # no evict_path: unload
    assert (unload.route, unload.model_state, unload.residual_mb) == ("/admin/unload", "unloaded", 94)
    assert "restart frees it" in unload.detail


async def test_busy_is_refused_and_nothing_is_done(setup):
    world, broker = setup
    world.busy["xtts"] = 2
    await broker.poll_once()
    with pytest.raises(ActionError) as err:
        await evict_resident(broker, "dragon/xtts")
    assert err.value.status == 409 and world.calls == []  # known busy from /health: not even asked

    world.busy.clear()
    world.busy_on_post["xtts"] = 1  # work arrives after iris read /health: the service answers 409 itself
    with pytest.raises(ActionError) as err:
        await evict_resident(broker, "dragon/xtts")
    assert err.value.status == 409 and "1 running" in str(err.value)

    world.comfy_queue = 1
    await broker.poll_once()
    with pytest.raises(ActionError) as err:
        await evict_resident(broker, "morpheus/comfyui")
    assert err.value.status == 409


async def test_comfyui_frees_everything(setup):
    world, broker = setup
    result = await evict_resident(broker, "morpheus/comfyui", poll_s=0)
    assert world.calls == ['POST http://morpheus:8188/free {"unload_models":true,"free_memory":true}']
    assert (result.before_mb, result.after_mb, result.residual_mb, result.confirmed) == (6000, 400, 400, True)


async def test_unconfirmed_when_the_memory_stays(setup):
    world, broker = setup
    world.frees_memory = False
    world.ollama_models.append("qwen3:8b")
    await broker.poll_once()
    # Ollama dropped the model from /api/ps but its runner still holds the memory.
    result = await evict_resident(broker, f"dragon/ollama/{SINGGUARD}", poll_s=0.05)
    assert result.confirmed is False and "not seen back" in result.detail


@pytest.mark.parametrize(
    ("rid", "mode", "status"),
    [
        ("dragon/frigate", "auto", 422),  # observed process: no adapter
        ("dragon/nope", "auto", 404),
        (f"dragon/ollama/{SINGGUARD}", "evict", 422),  # Ollama has no CPU stage
        ("dragon/whisper", "evict", 422),  # no evict_path
    ],
)
async def test_refusals(setup, rid, mode, status):
    world, broker = setup
    with pytest.raises(ActionError) as err:
        await evict_resident(broker, rid, mode=mode)
    assert err.value.status == status and world.calls == []


def test_route_needs_the_token_and_takes_ids_with_slashes(monkeypatch):
    monkeypatch.delenv("ARGUS_GPU_BROKER_TOKEN", raising=False)  # "no token configured" must not depend on the shell
    world = World()
    broker = Broker(
        BrokerConfig.model_validate(CONFIG), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    )
    client = TestClient(create_app(broker=broker, poll=False))
    path = f"/residents/dragon/ollama/{SINGGUARD}/evict"
    assert client.post(path).status_code == 403  # no token configured: manual actions are off
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "tok")
    assert client.post(path).status_code == 401
    asyncio.run(broker.poll_once())  # fill the node view, then evict over HTTP
    body = client.post(path, headers={"Authorization": "Bearer tok"}).json()
    assert (body["resident"], body["action"], body["reason"]) == (f"dragon/ollama/{SINGGUARD}", "unload", "manual")
