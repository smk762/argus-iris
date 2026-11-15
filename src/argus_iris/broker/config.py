"""Broker configuration: the nodes, their agents, and the services iris reads through adapters.

Loaded from the YAML file named by ``IRIS_CONFIG`` (see ``deploy/iris.example.yaml``). Policy (windows, keepalive,
idle-kill) and tenant manifests are later rollout steps and get their own files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_HEADROOM_MB = 512


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
    adapter: Literal["comfyui", "ollama", "argus-http"]
    base_url: str
    # Container name on the node, to match the agent's GPU processes to this service (and, later, to warm it).
    container: str | None = None
    # argus-http lifecycle routes, named as in argus-cortex's TenantResident (the contract of voluptas#22 and
    # agent-composer#44). unload frees VRAM and RAM; evict moves GPU -> CPU where the service has a CPU stage.
    health_path: str = "/health"
    unload_path: str | None = "/admin/unload"
    evict_path: str | None = None
    warm_path: str | None = None
    # Name of an environment variable holding a bearer token sent on every request to this service (rag-chat's
    # resident gateway needs one even for reads; ARGUS_ADMIN_TOKEN guards /admin/*). Never the token itself.
    token_env: str | None = None

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
