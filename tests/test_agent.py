from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from argus_iris.agent.app import Agent, AgentSettings, create_app
from argus_iris.agent.docker import DockerClient
from argus_iris.agent.nvml import RawProcess, Sample
from argus_iris.models import GpuInfo

COMFY_ID = "a" * 64
COMFY_SCOPE = f"/system.slice/docker-{COMFY_ID}.scope"
TOKEN = "s3cret"


class FakeDocker:
    """Docker Engine API over httpx.MockTransport; records calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if request.method == "GET" and path in (f"/containers/{COMFY_ID}/json", "/containers/voluptas_comfyui/json"):
            return httpx.Response(
                200,
                json={
                    "Id": COMFY_ID,
                    "Name": "/voluptas_comfyui",
                    "Config": {"Labels": {"com.docker.compose.service": "comfyui"}},
                    "State": {"Running": True, "Pid": 100},
                },
            )
        if request.method == "POST" and path == "/containers/voluptas_comfyui/start":
            return httpx.Response(304)
        if request.method == "POST" and path.startswith("/containers/voluptas_comfyui/"):
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "no such container"})


@pytest.fixture
def host(tmp_path: Path) -> tuple[Path, Path]:
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    for pid, line in ((100, f"0::{COMFY_SCOPE}"), (200, "0::/user.slice/user-1000.slice/session-1.scope")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cgroup").write_text(line + "\n")
    (proc / "meminfo").write_text("MemTotal: 31722496 kB\nMemAvailable: 18841600 kB\n")
    (cgroup / COMFY_SCOPE.lstrip("/")).mkdir(parents=True)
    (cgroup / COMFY_SCOPE.lstrip("/") / "memory.stat").write_text(f"anon {6 * 1024**3}\n")
    return proc, cgroup


def make_agent(host: tuple[Path, Path], docker: FakeDocker, *, token: str | None = TOKEN) -> Agent:
    proc, cgroup = host
    settings = AgentSettings(
        node="morpheus",
        token=token,
        containers=frozenset({"voluptas_comfyui"}),
        proc_root=proc,
        cgroup_root=cgroup,
    )
    sample = Sample(
        gpus=[
            GpuInfo(index=0, name="RTX 3090", uuid="GPU-x", total_mb=24576, free_mb=14000, used_mb=10576, util_pct=50)
        ],
        processes=[RawProcess(0, 100, 7352), RawProcess(0, 200, 300)],
        proc_util={0: {100: 60}},
    )
    return Agent(
        settings,
        sample=lambda: sample,
        docker=DockerClient(transport=httpx.MockTransport(docker.handler)),
        clock=lambda: 1000.0,
    )


async def test_poll_resolves_containers_and_memory(host) -> None:
    docker = FakeDocker()
    agent = make_agent(host, docker)
    snap = await agent.poll_once()
    comfy, desktop = sorted(snap.processes, key=lambda p: p.pid)
    assert (comfy.container, comfy.service, comfy.vram_mb, comfy.sm_util_pct) == (
        "voluptas_comfyui",
        "comfyui",
        7352,
        60,
    )
    assert desktop.container is None and desktop.container_id is None
    assert [(c.container, c.anon_mb) for c in snap.containers] == [("voluptas_comfyui", 6144)]
    assert snap.host is not None and snap.host.available_mb == 18400

    # A second poll does not inspect the container again (one lookup per new container ID).
    await agent.poll_once()
    assert docker.calls.count(f"GET /containers/{COMFY_ID}/json") == 1


async def test_nvml_failure_keeps_idle_tracking(host) -> None:
    agent = make_agent(host, FakeDocker())
    await agent.poll_once()
    agent._sample = lambda: Sample(error="NVMLError: GPU is lost")
    snap = await agent.poll_once()
    assert snap.nvml_error and snap.processes == []
    assert len(agent.tracker._records) == 2


def test_snapshot_health_and_metrics(host) -> None:
    agent = make_agent(host, FakeDocker())
    with TestClient(create_app(agent=agent, poll=False)) as client:
        assert client.get("/snapshot").status_code == 503
        client.portal.call(agent.poll_once)
        body = client.get("/snapshot").json()
        assert body["node"] == "morpheus" and len(body["processes"]) == 2
        assert client.get("/health").json()["status"] == "ok"
        metrics = client.get("/metrics").text
        assert 'iris_agent_process_vram_mb{container="voluptas_comfyui"' in metrics
        assert 'iris_agent_container_anon_mb{container="voluptas_comfyui",node="morpheus"} 6144.0' in metrics


def test_container_actions_are_token_and_allowlist_gated(host) -> None:
    docker = FakeDocker()
    with TestClient(create_app(agent=make_agent(host, docker), poll=False)) as client:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/containers/voluptas_comfyui/restart").status_code == 401
        assert (
            client.post("/containers/voluptas_comfyui/restart", headers={"Authorization": "Bearer no"}).status_code
            == 401
        )
        assert client.post("/containers/postgres/stop", headers=auth).status_code == 403
        assert client.post("/containers/voluptas_comfyui/explode", headers=auth).status_code == 422

        r = client.post("/containers/voluptas_comfyui/restart", headers=auth)
        assert r.status_code == 200 and r.json() == {
            "container": "voluptas_comfyui",
            "action": "restart",
            "ok": True,
            "detail": "done",
        }
        assert client.post("/containers/voluptas_comfyui/start", headers=auth).json()["detail"] == "unchanged"
    assert "POST /containers/postgres/stop" not in docker.calls


def test_actions_disabled_without_token(host) -> None:
    with TestClient(create_app(agent=make_agent(host, FakeDocker(), token=None), poll=False)) as client:
        r = client.post("/containers/voluptas_comfyui/start", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 403
        assert "IRIS_AGENT_TOKEN" in r.json()["detail"]
