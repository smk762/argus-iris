"""Broker configuration: the nodes, their agents, and the services iris reads through adapters.

Loaded from the YAML file named by ``IRIS_CONFIG`` (see ``deploy/iris.example.yaml``). Policy (windows, keepalive,
idle-kill) and tenant manifests are later rollout steps and get their own files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_HEADROOM_MB = 512

# Same rule as argus-cortex's TenantResident.RoutePath: an absolute path joined to base_url, never a URL.
RoutePath = Annotated[str, Field(pattern=r"^/\S*$", max_length=200)]


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_url: str
    reserve_mb: int = 0  # memory iris never hands out (morpheus's desktop display)
    headroom_mb: int = DEFAULT_HEADROOM_MB  # CUDA contexts and estimates that run small
    gpu: int = 0  # NVML index on the host; one node is one GPU

    @field_validator("agent_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.rstrip("/")


class ServiceConfig(BaseModel):
    """A service on a node that holds residents, read through an adapter."""

    model_config = ConfigDict(extra="forbid")

    id: str
    node: str
    # external: a service outside the suite (Immich's ML server) read for up/down only, through health_path; its VRAM
    # is attributed through `container` like any other.
    adapter: Literal["comfyui", "ollama", "argus-http", "external"]
    base_url: str
    # Container name on the node, to match the agent's GPU processes to this service (and, later, to warm it).
    container: str | None = None
    # argus-http lifecycle routes, named as in argus-cortex's TenantResident (the contract of voluptas#22 and
    # agent-composer#44). unload frees VRAM and RAM; evict moves GPU -> CPU where the service has a CPU stage.
    health_path: RoutePath = "/health"
    unload_path: RoutePath | None = "/admin/unload"
    evict_path: RoutePath | None = None
    warm_path: RoutePath | None = None
    # VRAM the service takes once warm. A manual warm is refused while the node cannot admit it; unset, iris uses the
    # most it has seen the service hold since the broker started.
    warm_mb: int | None = Field(default=None, ge=0)
    # Name of an environment variable holding a bearer token sent on every request to this service (rag-chat's
    # resident gateway needs one even for reads; ARGUS_ADMIN_TOKEN guards /admin/*). Never the token itself.
    token_env: str | None = None
    # external only: how iris may free it. None: it cannot (observe only; an evict is refused). "stop": the node's
    # agent stops the container (its own IRIS_AGENT_CONTAINERS allowlist still applies) and a warm starts it again.
    # For a service whose clients fall back elsewhere when it is down (Immich to its CPU ML), so a stop costs speed,
    # not an outage. Admission stops it only while a policy rule matching it says `enforce: true`.
    evict_by: Literal["stop"] | None = None

    @model_validator(mode="after")
    def _check_evict_by(self) -> ServiceConfig:
        if self.evict_by and (self.adapter != "external" or not self.container):
            raise ValueError(f"{self.id}: evict_by needs adapter: external and a container")
        return self

    def headers(self) -> dict[str, str]:
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    @field_validator("base_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.rstrip("/")


class BrokerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_s: float = 5.0
    agent_timeout_s: float = 3.0
    nodes: dict[str, NodeConfig]
    services: list[ServiceConfig] = Field(default_factory=list)
    # Browser origins allowed to make state-changing requests (the Studio page). Non-browser clients are unaffected.
    trusted_origins: list[str] = Field(default_factory=list)
    # Env vars holding the tokens (never the tokens themselves). The caller token is what argus-cortex's client sends
    # (ARGUS_GPU_BROKER_TOKEN); manual actions are disabled until it is set. The agent token is sent to the agents.
    caller_token_env: str = "ARGUS_GPU_BROKER_TOKEN"
    agent_token_env: str = "IRIS_AGENT_TOKEN"
    # How long a warm waits for a started service to answer its health check.
    warm_timeout_s: float = Field(180.0, gt=0, le=900)
    # How long an evict waits to see the memory come back on the agent's NVML view.
    evict_confirm_s: float = Field(30.0, gt=0, le=300)
    # Admission: "observe" grants every lease at once and records whether it would have fit; "enforce" queues leases
    # that do not fit, spills to the next candidate node, and evicts idle residents to make room (DESIGN.md).
    admission: Literal["observe", "enforce"] = "observe"
    # A resident must have been idle this long before admission evicts it (so a model is not evicted between two
    # requests of one conversation).
    admission_min_idle_s: float = Field(60.0, ge=0)
    # A resident of the request's own class is evicted only once it has been idle, and loaded, this long: so two
    # tenants of one class cannot ping-pong a card between their models.
    admission_same_class_idle_s: float = Field(300.0, ge=0)
    # Resident ids (fnmatch patterns) admission never evicts, e.g. "dragon/ollama-dragon/hf.co/inclusionAI/SingGuard-*".
    # The policy file of rollout step 4 adds windows; this is the always-pinned part.
    pinned: list[str] = Field(default_factory=list)
    # A granted lease that keeps heartbeating but shows no GPU activity this long (its resident never busy or used,
    # no progress, stage or peak VRAM change) is marked stalled; while a queued lease waits on its node, its heartbeats
    # ask it to yield. None: never. A keepalive loop that outlives its work otherwise holds the reservation forever.
    lease_stall_s: float | None = Field(1800.0, gt=0)
    # Also take the reservation back from a stalled lease that blocks a queued one (it ends refused, "reclaimed"),
    # once it has ignored the yield for lease_stall_s / 2 more.
    lease_stall_reclaim: bool = False
    # How often the residency policy (IRIS_POLICY: idle-kill, keepalive, restart) is acted on.
    residency_s: float = Field(15.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> BrokerConfig:
        ids = [s.id for s in self.services]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate service ids: {dupes}")
        unknown = sorted({s.node for s in self.services} - set(self.nodes))
        if unknown:
            raise ValueError(f"services name unknown nodes: {unknown}")
        return self

    def services_on(self, node: str) -> list[ServiceConfig]:
        return [s for s in self.services if s.node == node]


def load_config(path: str | os.PathLike[str] | None = None) -> BrokerConfig:
    path = path or os.environ.get("IRIS_CONFIG")
    if not path:
        raise RuntimeError("no broker config: pass --config or set IRIS_CONFIG")
    data = yaml.safe_load(Path(path).read_text()) or {}
    return BrokerConfig.model_validate(data)
