"""Wire types between iris-agent and the broker, and the broker's read API.

The lease and tenant-manifest models are not here: they live in argus-cortex's ``gpu`` module so callers and broker
share one definition. These are iris-internal (agent snapshot) or iris's own read surface (nodes, residents).

Every memory figure is MiB (bytes / 2**20), named ``*_mb``. Every timestamp is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

SNAPSHOT_VERSION = "1"

MIB = 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


# ── agent snapshot ────────────────────────────────────────────────────────────


class GpuInfo(BaseModel):
    index: int
    name: str
    uuid: str
    total_mb: int
    free_mb: int
    used_mb: int
    util_pct: int | None = None
    compute_capability: str | None = None  # "8.6"
    bf16: bool | None = None


class GpuProcess(BaseModel):
    pid: int
    gpu: int
    vram_mb: int
    container: str | None = None  # container name, None for a host process
    container_id: str | None = None
    service: str | None = None  # compose service label, else the container name
    sm_util_pct: int | None = None  # per-process utilisation, when the driver reports it
    first_seen: datetime
    idle_s: float  # time since VRAM last changed or the process was last busy


class HostMemory(BaseModel):
    total_mb: int
    available_mb: int


class ContainerMemory(BaseModel):
    container: str
    anon_mb: int  # cgroup memory.stat ``anon``: what restarting the container gives back


class NodeSnapshot(BaseModel):
    version: str = SNAPSHOT_VERSION
    node: str
    taken_at: datetime
    agent_version: str
    gpus: list[GpuInfo]
    processes: list[GpuProcess]
    host: HostMemory | None = None
    containers: list[ContainerMemory] = Field(default_factory=list)
    nvml_error: str | None = None


class ContainerAction(StrEnum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class ActionResult(BaseModel):
    container: str
    action: ContainerAction
    ok: bool
    detail: str | None = None


# ── broker read API ───────────────────────────────────────────────────────────


class ResidentSource(StrEnum):
    COMFYUI = "comfyui"
    OLLAMA = "ollama"
    ARGUS_HTTP = "argus-http"
    EXTERNAL = "external"  # a configured service outside the suite, read for up/down only
    PROCESS = "process"  # a GPU process no configured service claims


class ResidentPolicy(BaseModel):
    """What the operator's policy says about a resident now (rollout step 4), for the API and the dashboard."""

    rule: int  # index of the first matching rule in the policy file
    enforce: bool  # iris acts on it; otherwise it only reports
    pinned: bool  # keepalive holds it now: never evicted, by idle-kill or admission
    idle_s: float | None = None  # idle as the policy counts it: since its last lease, or the agent's view if shorter
    idle_kill_s: float | None = None
    due_in_s: float | None = None  # until idle-kill; 0 when due
    next: str | None = None  # what iris does (or, not enforcing, would do) next: evict, restart, refresh keep_alive
    note: str | None = None


class Resident(BaseModel):
    """A model or process holding VRAM on a node, as iris sees it."""

    id: str  # "<node>/<service>" or "<node>/<service>/<model>"
    node: str
    service: str
    source: ResidentSource
    model: str | None = None
    vram_mb: int
    idle_s: float | None = None
    busy: bool = False  # the service reports work in flight (ComfyUI queue, ...)
    host_ram_mb: int | None = None  # its container's anonymous memory (cgroup), when the agent reports it
    expires_at: datetime | None = None  # Ollama keep_alive expiry
    detail: dict = Field(default_factory=dict)
    tenant: str | None = None  # the tenant whose own process or service this is, when iris can link it (#36)
    policy: ResidentPolicy | None = None  # set when a policy rule matches


class ServiceState(BaseModel):
    id: str
    node: str
    adapter: str
    base_url: str
    up: bool
    error: str | None = None
    queue_running: int | None = None
    queue_pending: int | None = None
    available: list[str] | None = None  # models it has pulled (Ollama); None when it cannot say


class NodeLedger(BaseModel):
    """Observe-only accounting: where the card's memory is, by what iris can see."""

    total_mb: int
    free_mb: int  # NVML
    reserve_mb: int
    headroom_mb: int
    resident_mb: int  # sum of residents iris attributes
    unattributed_mb: int  # NVML used minus resident_mb: other users, CUDA contexts, the display
    admissible_mb: int  # what a new lease could get without evicting anything


class NodeState(BaseModel):
    name: str
    agent_url: str
    reachable: bool
    last_seen: datetime | None = None
    error: str | None = None
    gpu: GpuInfo | None = None
    host: HostMemory | None = None
    ledger: NodeLedger | None = None
    residents: list[Resident] = Field(default_factory=list)
    services: list[ServiceState] = Field(default_factory=list)
