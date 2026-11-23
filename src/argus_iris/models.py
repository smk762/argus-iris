"""Wire types between iris-agent and the broker, and the broker's read API.

The lease and tenant-manifest models are not here: they live in argus-cortex's ``gpu`` module so callers and broker
share one definition. These are iris-internal (agent snapshot) or iris's own read surface (nodes, residents).

Every memory figure is MiB (bytes / 2**20), named ``*_mb``. Every timestamp is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

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


class PortMap(BaseModel):
    private: int  # the port inside the container
    public: int | None = None  # the host port it is published on; None when it is not published
    proto: str = "tcp"
    # Host addresses it is published on ("0.0.0.0", "::", "127.0.0.1", a LAN address). Loopback only: no other host,
    # the broker included, can reach it.
    ips: list[str] = Field(default_factory=list)


class ContainerEntry(BaseModel):
    """One container on the agent's host, running or not: how the broker finds a registered tenant's resident (its
    node and published port) without anyone writing it into the broker's config."""

    id: str
    name: str
    service: str | None = None  # compose service label
    project: str | None = None  # compose project label
    running: bool
    host_network: bool = False  # network_mode: host, so its ports are the host's own
    ports: list[PortMap] = Field(default_factory=list)
    # The agent would start, stop or restart it: on IRIS_AGENT_CONTAINERS, or labelled argus.iris.manage=true where
    # the agent opted in to the label (IRIS_AGENT_MANAGE_LABEL).
    managed: bool = False
    # Its argus.* labels only (argus.peer, argus.service, argus.health, argus.port: the service directory, #48).
    # Other labels can hold anything, so they never leave the host.
    labels: dict[str, str] = Field(default_factory=dict)


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
    # Every container on the host. None from an agent too old to list them (or while Docker is unreachable).
    inventory: list[ContainerEntry] | None = None


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
    peak_mb: int | None = None  # the most VRAM iris has seen it hold since the broker started (sampled each poll)
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
    need_mb: int | None = None  # VRAM it takes warm: its configured warm_mb, else the most iris has seen it hold
    room_mb: int | None = None  # the most it can get beside what the node can never free (NodeLedger.held_mb)
    # need_mb > room_mb (#40): admission can never make room for it on this node, whatever it evicts.
    crowded_out: bool = False


class NodeLedger(BaseModel):
    """Observe-only accounting: where the card's memory is, by what iris can see."""

    total_mb: int
    free_mb: int  # NVML
    reserve_mb: int
    headroom_mb: int
    resident_mb: int  # sum of residents iris attributes
    unattributed_mb: int  # NVML used minus resident_mb: other users, CUDA contexts, the display
    admissible_mb: int  # what a new lease could get without evicting anything
    pinned_mb: int = 0  # held by pinned residents, a dropped pin awaiting restore included
    # What admission can never free (#40): pinned_mb, processes with no adapter (Frigate), externals iris may not stop,
    # and the context an unloaded service keeps (only a restart returns it).
    held_mb: int = 0
    room_mb: int | None = None  # total - reserve - headroom - held_mb: the most any other service can get


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


# ── service directory (#48) ───────────────────────────────────────────────────


class DependencyCheck(BaseModel):
    ok: bool
    detail: str | None = None


class EntryHealth(BaseModel):
    """What a directory entry's ``/health`` said, read tolerantly: in full when it is argus-cortex's ``ServiceHealth``
    (cortex#210), and as up or down from the HTTP status otherwise."""

    checked_at: datetime
    status: Literal["ok", "degraded", "starting", "down"]
    http_status: int | None = None  # None: no answer
    contract: bool = False  # the body is a ServiceHealth (service, version and status at the top level)
    service: str | None = None
    version: str | None = None
    wire: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    checks: dict[str, DependencyCheck] = Field(default_factory=dict)
    error: str | None = None


class DirectoryEntry(BaseModel):
    """One labelled container (``argus.peer``) on a node, or a peer the broker config names."""

    peer: str
    origin: Literal["label", "config"]
    service: str | None = None  # the argus.service label, else what its health says
    node: str | None = None
    container: str | None = None
    running: bool = True
    url: str | None = None  # an agent's host and the published port; None: not reachable, see reason
    reason: str | None = None
    health_path: str = "/health"
    health: EntryHealth | None = None


class PeerLookup(BaseModel):
    """``GET /directory/{peer}``: where callers reach *peer*, best first (cortex#207)."""

    peer: str
    urls: list[DirectoryEntry]
