from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from argus_iris.agent.app import Agent, AgentSettings, create_app
from argus_iris.agent.docker import DockerClient, DockerError
from argus_iris.agent.nvml import RawProcess, Sample
from argus_iris.models import ContainerAction, GpuInfo

COMFY_ID = "a" * 64
COMFY_SCOPE = f"/system.slice/docker-{COMFY_ID}.scope"
TOKEN = "s3cret"


class FakeDocker:
    """Docker Engine API over httpx.MockTransport; records calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.listing = [
            {
                "Id": COMFY_ID,
                "Names": ["/voluptas_comfyui"],
                "State": "running",
                "Labels": {
                    "com.docker.compose.service": "comfyui",
                    "com.docker.compose.project": "voluptas",
                    "argus.peer": "comfyui",
                    "traefik.http.routers.x.middlewares": "basic-auth secret",
                },
                "Ports": [
                    {"PrivatePort": 8188, "PublicPort": 8188, "Type": "tcp", "IP": "0.0.0.0"},
                    {"PrivatePort": 8188, "PublicPort": 8188, "Type": "tcp", "IP": "::"},
                ],
                "HostConfig": {"NetworkMode": "voluptas_default"},
            },
            {
                "Id": "e" * 64,
                "Names": ["/echo"],
                "State": "exited",
                "Labels": {"argus.iris.manage": "true"},
                "Ports": [{"PrivatePort": 8141, "Type": "tcp"}],
                "HostConfig": {"NetworkMode": "host"},
            },
        ]

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
        if request.method == "GET" and path == "/containers/json":
            return httpx.Response(200, json=self.listing)
        if request.method == "GET" and path == "/containers/echo/json":
            return httpx.Response(
                200,
                json={
                    "Id": "e" * 64,
                    "Name": "/echo",
                    "Config": {"Labels": {"argus.iris.manage": "true"}},
                    "State": {"Running": True, "Pid": 300},
                },
            )
        if request.method == "POST" and path.startswith("/containers/echo/"):
            return httpx.Response(204)
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


@pytest.mark.parametrize("ref", ["x/../../images/prune?", "x/../../containers/create?", "a#b", "../etc", "", "-x"])
async def test_docker_client_rejects_refs_outside_name_grammar(ref) -> None:
    docker = FakeDocker()
    client = DockerClient(transport=httpx.MockTransport(docker.handler))
    with pytest.raises(DockerError, match="not a container name"):
        await client.inspect(ref)
    with pytest.raises(DockerError, match="not a container name"):
        await client.act(ref, ContainerAction.STOP)
    assert docker.calls == []  # nothing reached the socket


async def test_docker_error_clears_once_docker_recovers(host) -> None:
    docker = FakeDocker()
    agent = make_agent(host, docker)
    # "sidecar" is allow-listed but not on the GPU, so every poll inspects it in the allowlist loop.
    agent.settings = dataclasses.replace(agent.settings, containers=frozenset({"voluptas_comfyui", "sidecar"}))
    await agent.poll_once()  # caches the GPU container's ID
    assert agent.docker_error is None

    down = True
    agent.docker = DockerClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500) if down else docker.handler(r))
    )
    await agent.poll_once()
    assert agent.docker_error and "HTTP 500" in agent.docker_error

    down = False
    await agent.poll_once()
    assert agent.docker_error is None


async def test_inventory_lists_every_container_with_ports_and_management(host) -> None:
    agent = make_agent(host, FakeDocker())
    snap = await agent.poll_once()
    inv = {e.name: e for e in snap.inventory}
    comfy, echo = inv["voluptas_comfyui"], inv["echo"]
    assert comfy.running and comfy.service == "comfyui" and comfy.project == "voluptas"
    assert [(p.private, p.public) for p in comfy.ports] == [(8188, 8188)]  # IPv4 and IPv6 bindings fold into one
    assert comfy.ports[0].ips == ["0.0.0.0", "::"]
    assert comfy.managed  # allow-listed
    assert comfy.labels == {"argus.peer": "comfyui"}  # argus.* only: other labels never leave the host
    assert not echo.running and echo.host_network and echo.ports[0].public is None
    assert not echo.managed  # labelled, but this agent has not opted in to the label


def test_manage_label_is_opt_in_per_agent(host) -> None:
    docker = FakeDocker()
    agent = make_agent(host, docker)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(agent=agent, poll=False)) as client:
        assert client.post("/containers/echo/restart", headers=auth).status_code == 403
    assert "GET /containers/echo/json" not in docker.calls  # no label lookup unless opted in
    agent = make_agent(host, docker)  # the first app closed its Docker client on the way out
    agent.settings = dataclasses.replace(agent.settings, manage_label="argus.iris.manage")
    with TestClient(create_app(agent=agent, poll=False)) as client:
        assert client.post("/containers/echo/restart", headers=auth).status_code == 200
        # The label does not open containers that lack it.
        assert client.post("/containers/postgres/stop", headers=auth).status_code == 403
    assert "POST /containers/postgres/stop" not in docker.calls


@pytest.mark.parametrize(
    ("raw", "want"), [("", None), ("0", None), ("1", "argus.iris.manage"), ("my.label", "my.label")]
)
def test_manage_label_setting(raw, want) -> None:
    from argus_iris.agent.app import _label_setting

    assert _label_setting(raw) == want


async def test_announce_posts_node_with_agent_token(host) -> None:
    agent = make_agent(host, FakeDocker())
    agent.settings = dataclasses.replace(agent.settings, broker_url="http://broker:8120", advertise_url=None)
    seen: list[httpx.Request] = []

    def broker(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "added"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker)) as client:
        assert await agent.announce(client)
    req = seen[0]
    assert req.url == "http://broker:8120/nodes" and req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == {"name": "morpheus", "agent_url": None, "port": 9110, "gpu": 0}
