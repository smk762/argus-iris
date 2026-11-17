from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from argus_iris.broker.actions import ActionError, warm_service
from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.state import Broker
from argus_iris.server.app import create_app

CONFIG = {
    "nodes": {"morpheus": {"agent_url": "http://morpheus:9110"}},
    "services": [
        {
            "id": "comfyui",
            "node": "morpheus",
            "adapter": "comfyui",
            "base_url": "http://morpheus:8188",
            "container": "voluptas_comfyui",
        },
        {
            "id": "diffusers",
            "node": "morpheus",
            "adapter": "argus-http",
            "base_url": "http://morpheus:8011",
            "container": "voluptas_image_gen_diffusers",
            "warm_path": "/admin/wake",
        },
        {"id": "nocontainer", "node": "morpheus", "adapter": "argus-http", "base_url": "http://morpheus:9999"},
        {"id": "ollama", "node": "morpheus", "adapter": "ollama", "base_url": "http://morpheus:11434"},
    ],
    "warm_timeout_s": 1,
}


class World:
    """ComfyUI and diffusers that are down until the agent starts their container."""

    def __init__(
        self, *, comfy_up: bool = False, comes_back: bool = True, wake: tuple[int, dict] | None = None
    ) -> None:
        self.up = {"voluptas_comfyui": comfy_up, "voluptas_image_gen_diffusers": False}
        self.comes_back = comes_back
        self.wake = wake or (200, {"status": "ready", "model_state": "gpu", "unloaded": None})
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        self.calls.append(f"{method} {url} {request.headers.get('authorization', '')}".strip())
        if url.startswith("http://morpheus:9110/containers/") and method == "POST":
            name = url.split("/")[4]
            if self.comes_back:
                self.up[name] = True
            return httpx.Response(200, json={"container": name, "action": "start", "ok": True, "detail": "done"})
        if url.startswith("http://morpheus:8188/"):
            if not self.up["voluptas_comfyui"]:
                raise httpx.ConnectError("refused")
            if url.endswith("/queue"):
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            return httpx.Response(200, json={"devices": []})
        if url.startswith("http://morpheus:8011/"):
            if not self.up["voluptas_image_gen_diffusers"]:
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
    assert "POST http://morpheus:9110/containers/voluptas_comfyui/start Bearer agent-tok" in world.calls


async def test_warm_leaves_a_running_service_alone() -> None:
    world = World(comfy_up=True)
    result = await warm_service(broker_for(world), "comfyui", poll_s=0)
    assert result.up and not result.started_container and result.detail == "already up"
    assert not any("/containers/" in c for c in world.calls)


async def test_warm_starts_then_calls_warm_path() -> None:
    world = World()
    result = await warm_service(broker_for(world), "diffusers", poll_s=0)
    assert result.started_container and result.warmed and result.up
    assert any(c.startswith("POST http://morpheus:8011/admin/wake") for c in world.calls)


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
        name="morpheus",
        agent_url="http://morpheus:9110",
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
            Resident(id=f"morpheus/{s}", node="morpheus", service=s, source=ResidentSource.ARGUS_HTTP, vram_mb=mb)
            for s, mb in held.items()
        ],
    )
    broker._apply("morpheus", state)


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
