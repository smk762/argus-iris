from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from argus_iris.broker.actions import ActionError, warm_service
from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.state import Broker
from argus_iris.server.app import create_app

CONFIG = {
    "nodes": {"rtx3090": {"agent_url": "http://rtx3090:9110"}},
    "services": [
        {
            "id": "comfyui",
            "node": "rtx3090",
            "adapter": "comfyui",
            "base_url": "http://rtx3090:8188",
            "container": "imagegen_comfyui",
        },
        {
            "id": "diffusers",
            "node": "rtx3090",
            "adapter": "argus-http",
            "base_url": "http://rtx3090:8011",
            "container": "imagegen_diffusers",
            "warm_path": "/admin/wake",
        },
        {"id": "nocontainer", "node": "rtx3090", "adapter": "argus-http", "base_url": "http://rtx3090:9999"},
        {"id": "ollama", "node": "rtx3090", "adapter": "ollama", "base_url": "http://rtx3090:11434"},
    ],
    "warm_timeout_s": 1,
}


class World:
    """ComfyUI and diffusers that are down until the agent starts their container."""

    def __init__(
        self, *, comfy_up: bool = False, comes_back: bool = True, wake: tuple[int, dict] | None = None
    ) -> None:
        self.up = {"imagegen_comfyui": comfy_up, "imagegen_diffusers": False}
        self.comes_back = comes_back
        self.wake = wake or (200, {"status": "ready", "model_state": "gpu", "unloaded": None})
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        self.calls.append(f"{method} {url} {request.headers.get('authorization', '')}".strip())
        if url.startswith("http://rtx3090:9110/containers/") and method == "POST":
            name = url.split("/")[4]
            if self.comes_back:
                self.up[name] = True
            return httpx.Response(200, json={"container": name, "action": "start", "ok": True, "detail": "done"})
        if url.startswith("http://rtx3090:8188/"):
            if not self.up["imagegen_comfyui"]:
                raise httpx.ConnectError("refused")
            if url.endswith("/queue"):
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            return httpx.Response(200, json={"devices": []})
        if url.startswith("http://rtx3090:8011/"):
            if not self.up["imagegen_diffusers"]:
                raise httpx.ConnectError("refused")
            if method == "POST" and url.endswith("/admin/wake"):
                return httpx.Response(self.wake[0], json=self.wake[1])
            return httpx.Response(200, json={"model_state": "unloaded"})
        raise httpx.ConnectError("refused")


def broker_for(world: World) -> Broker:
    return Broker(
        BrokerConfig.model_validate(CONFIG), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    )


@pytest.fixture(autouse=True)
def agent_token(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "agent-tok")


async def test_warm_starts_a_stopped_comfyui() -> None:
    world = World()
    result = await warm_service(broker_for(world), "comfyui", poll_s=0)
    assert result.started_container and result.up and not result.warmed
    assert "POST http://rtx3090:9110/containers/imagegen_comfyui/start Bearer agent-tok" in world.calls


async def test_warm_leaves_a_running_service_alone() -> None:
    world = World(comfy_up=True)
    result = await warm_service(broker_for(world), "comfyui", poll_s=0)
    assert result.up and not result.started_container and result.detail == "already up"
    assert not any("/containers/" in c for c in world.calls)


async def test_warm_starts_then_calls_warm_path() -> None:
    world = World()
    result = await warm_service(broker_for(world), "diffusers", poll_s=0)
    assert result.started_container and result.warmed and result.up
    assert any(c.startswith("POST http://rtx3090:8011/admin/wake") for c in world.calls)


async def test_warm_reports_a_service_that_never_answers() -> None:
    result = await warm_service(broker_for(World(comes_back=False)), "comfyui", poll_s=0.05)
    assert result.started_container and not result.up
    assert "not answering after 1s" in (result.detail or "")


@pytest.mark.parametrize(
    ("service", "status"),
    [("nope", 404), ("ollama", 422), ("nocontainer", 503)],
)
async def test_warm_refusals(service: str, status: int) -> None:
    with pytest.raises(ActionError) as exc:
        await warm_service(broker_for(World()), service, poll_s=0)
    assert exc.value.status == status


async def test_warm_needs_the_agent_token(monkeypatch) -> None:
    monkeypatch.delenv("IRIS_AGENT_TOKEN")
    with pytest.raises(ActionError, match="IRIS_AGENT_TOKEN") as exc:
        await warm_service(broker_for(World()), "comfyui", poll_s=0)
    assert exc.value.status == 503


def test_warm_route_auth(monkeypatch) -> None:
    monkeypatch.delenv(
        "ARGUS_GPU_BROKER_TOKEN", raising=False
    )  # the "no token configured" 403 must not depend on the shell
    world = World()
    with TestClient(create_app(broker=broker_for(world), poll=False)) as client:
        r = client.post("/services/comfyui/warm")
        assert r.status_code == 403 and "ARGUS_GPU_BROKER_TOKEN" in r.json()["detail"]

        monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "caller-tok")
        assert client.post("/services/comfyui/warm", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/services/nope/warm", headers={"Authorization": "Bearer caller-tok"}).status_code == 404

        monkeypatch.setattr("argus_iris.broker.actions.WARM_POLL_S", 0)
        r = client.post("/services/comfyui/warm", headers={"Authorization": "Bearer caller-tok"})
        assert r.status_code == 200 and r.json()["started_container"] is True
        # The shared write guard still refuses a cross-site browser write, token or not.
        r = client.post(
            "/services/comfyui/warm", headers={"Authorization": "Bearer caller-tok", "Origin": "https://evil.example"}
        )
        assert r.status_code == 403


async def test_warm_reads_the_wake_reply() -> None:
    result = await warm_service(broker_for(World()), "diffusers", poll_s=0)
    assert result.warmed and result.model_state == "gpu"
    # A reply off the contract still counts as warmed; the state is just unknown.
    result = await warm_service(broker_for(World(wake=(200, {"status": "ok"}))), "diffusers", poll_s=0)
    assert result.warmed and result.model_state is None


async def test_warm_while_busy_is_a_409_that_did_nothing() -> None:
    world = World(wake=(409, {"status": "busy", "active": 2}))
    with pytest.raises(ActionError, match=r"busy \(2 running\)") as exc:
        await warm_service(broker_for(world), "diffusers", poll_s=0)
    assert exc.value.status == 409


# --- warm refuses a node without room ---------------------------------------------------------------------------


def _full_node(broker: Broker, *, admissible_mb: int, held: dict[str, int]) -> None:
    from argus_iris.models import NodeLedger, NodeState, Resident, ResidentSource

    state = NodeState(
        name="rtx3090",
        agent_url="http://rtx3090:9110",
        reachable=True,
        ledger=NodeLedger(
            total_mb=24000,
            free_mb=admissible_mb + 512,
            reserve_mb=0,
            headroom_mb=512,
            resident_mb=sum(held.values()),
            unattributed_mb=0,
            admissible_mb=admissible_mb,
        ),
        residents=[
            Resident(id=f"rtx3090/{s}", node="rtx3090", service=s, source=ResidentSource.ARGUS_HTTP, vram_mb=mb)
            for s, mb in held.items()
        ],
    )
    broker._apply("rtx3090", state)


async def test_warm_refused_when_the_node_cannot_admit_the_peak_seen() -> None:
    world = World()
    broker = broker_for(world)
    _full_node(broker, admissible_mb=4000, held={"diffusers": 6000})  # seen at 6000 MB
    _full_node(broker, admissible_mb=1000, held={"diffusers": 100})  # evicted; the card filled up since
    with pytest.raises(ActionError) as err:
        await warm_service(broker, "diffusers", poll_s=0)
    assert err.value.status == 409 and "5900 MB more" in str(err.value)
    assert not any("/containers/" in c or "/admin/wake" in c for c in world.calls)  # nothing was touched

    result = await warm_service(broker, "diffusers", poll_s=0, force=True)
    assert result.warmed


async def test_warm_uses_the_configured_warm_mb() -> None:
    config = {**CONFIG, "services": [{**CONFIG["services"][1], "warm_mb": 3000}]}
    world = World()
    broker = Broker(
        BrokerConfig.model_validate(config), client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    )
    _full_node(broker, admissible_mb=2999, held={})
    with pytest.raises(ActionError):
        await warm_service(broker, "diffusers", poll_s=0)
    _full_node(broker, admissible_mb=3000, held={})
    assert (await warm_service(broker, "diffusers", poll_s=0)).warmed


async def test_warm_without_a_known_size_is_not_refused() -> None:
    broker = broker_for(World())
    _full_node(broker, admissible_mb=0, held={})
    assert (await warm_service(broker, "diffusers", poll_s=0)).warmed


async def test_keep_alive_for_an_unknown_service_is_an_action_error() -> None:
    from argus_iris.broker.actions import set_keep_alive
    from argus_iris.models import Resident, ResidentSource

    gone = Resident(
        id="rtx3090/removed/x:1",
        node="rtx3090",
        service="removed",
        source=ResidentSource.OLLAMA,
        model="x:1",
        vram_mb=1,
    )
    with pytest.raises(ActionError, match="unknown service 'removed'") as exc:
        await set_keep_alive(broker_for(World()), gone, -1)
    assert exc.value.status == 404


@pytest.mark.parametrize("body", [b'["no"]', b'"no"', b"null"], ids=["list", "string", "null"])
async def test_a_non_object_refusal_from_the_agent_is_an_action_error(body) -> None:
    from argus_iris.broker.actions import container_action

    broker = Broker(
        BrokerConfig.model_validate(CONFIG),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403, content=body))),
    )
    svc = next(s for s in broker.config.services if s.id == "comfyui")
    with pytest.raises(ActionError, match="refused to stop imagegen_comfyui: HTTP 403"):
        await container_action(broker, svc, "stop")


def test_report_only_refuses_manual_actions(monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "caller-tok")
    monkeypatch.setenv("IRIS_REPORT_ONLY", "1")
    world = World()
    auth = {"Authorization": "Bearer caller-tok"}
    with TestClient(create_app(broker=broker_for(world), poll=False)) as client:
        assert client.post("/services/comfyui/warm").status_code == 401  # the token is still checked first
        for path in ("/services/comfyui/warm", "/residents/rtx3090/comfyui/evict"):
            r = client.post(path, headers=auth)
            assert r.status_code == 403 and "report-only" in r.json()["detail"]
        assert client.get("/health").json()["report_only"] is True


def test_report_only_applies_to_a_config_built_in_code(monkeypatch) -> None:
    from argus_iris.broker.policy import Policy

    monkeypatch.setenv("IRIS_REPORT_ONLY", "1")
    config = BrokerConfig.model_validate({**CONFIG, "admission": "enforce"})
    policy = Policy.model_validate({"residents": [{"match": {"node": "rtx3090"}, "idle_kill": "5m", "enforce": True}]})
    broker = Broker(config, policy=policy)
    assert broker.config.admission == "observe"
    assert not broker.policy.residents[0].enforce and broker.policy.residents[0].idle_kill == 300
