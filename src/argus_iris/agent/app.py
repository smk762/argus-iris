"""iris-agent's FastAPI app: a polled snapshot, Prometheus metrics, and allow-listed container actions."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from argus_cortex.service import DependencyCheck, install_health
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

import argus_iris
from argus_iris.agent import procfs
from argus_iris.agent.docker import DEFAULT_SOCKET, MANAGE_LABEL, ContainerInfo, DockerClient, DockerError, label_true
from argus_iris.agent.nvml import NvmlSampler, Sample
from argus_iris.agent.tracker import ProcessTracker
from argus_iris.models import (
    SNAPSHOT_VERSION,
    ActionResult,
    ContainerAction,
    ContainerEntry,
    ContainerMemory,
    GpuProcess,
    NodeSnapshot,
    utcnow,
)

log = structlog.get_logger("argus_iris.agent")


def _csv(raw: str) -> frozenset[str]:
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


@dataclass
class AgentSettings:
    node: str = field(default_factory=socket.gethostname)
    poll_s: float = 5.0
    token: str | None = None  # broker→agent token; unset disables container actions
    containers: frozenset[str] = frozenset()  # containers the broker may start/stop/restart
    docker_socket: str = DEFAULT_SOCKET
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    idle_util_pct: int = 5
    # Also let the broker act on containers that carry this label set to true (argus.iris.manage=true), as set in the
    # service's own compose file on this host. Off unless IRIS_AGENT_MANAGE_LABEL is set: the label is the host's
    # decision, never the broker's or a manifest's.
    manage_label: str | None = None
    # Announce this node to the broker (POST /nodes, authenticated with the agent token), so a new GPU host needs no
    # broker config: IRIS_BROKER_URL, plus IRIS_AGENT_URL when the broker cannot reach the agent at the address it
    # sees the announcement come from.
    broker_url: str | None = None
    advertise_url: str | None = None
    port: int = 9110
    gpu: int = 0
    announce_s: float = 60.0

    def manages(self, name: str, labels: dict[str, str]) -> bool:
        return name in self.containers or (self.manage_label is not None and label_true(labels, self.manage_label))

    @classmethod
    def from_env(cls) -> AgentSettings:
        env = os.environ.get
        return cls(
            node=env("IRIS_AGENT_NODE") or socket.gethostname(),
            poll_s=float(env("IRIS_AGENT_POLL_S", "5")),
            token=env("IRIS_AGENT_TOKEN") or None,
            containers=_csv(env("IRIS_AGENT_CONTAINERS", "")),
            docker_socket=env("IRIS_AGENT_DOCKER_SOCKET", DEFAULT_SOCKET),
            proc_root=Path(env("IRIS_AGENT_PROC", "/proc")),
            cgroup_root=Path(env("IRIS_AGENT_CGROUP_ROOT", "/sys/fs/cgroup")),
            idle_util_pct=int(env("IRIS_AGENT_IDLE_UTIL_PCT", "5")),
            manage_label=_label_setting(env("IRIS_AGENT_MANAGE_LABEL", "")),
            broker_url=(env("IRIS_BROKER_URL") or "").rstrip("/") or None,
            advertise_url=(env("IRIS_AGENT_URL") or "").rstrip("/") or None,
            port=int(env("IRIS_AGENT_PORT", "9110")),
            gpu=int(env("IRIS_AGENT_GPU", "0")),
        )


def _label_setting(raw: str) -> str | None:
    """IRIS_AGENT_MANAGE_LABEL: unset/0/false -> off; 1/true -> argus.iris.manage; anything else is the label key."""
    raw = raw.strip()
    if raw.lower() in ("", "0", "false", "no", "off"):
        return None
    return MANAGE_LABEL if raw.lower() in ("1", "true", "yes", "on") else raw


class Agent:
    """Owns the sampler, tracker and container lookups; :meth:`poll_once` produces a :class:`NodeSnapshot`."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        sample: Callable[[], Sample] | None = None,
        docker: DockerClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sampler = None if sample else NvmlSampler()
        self._sample = sample or self._sampler.sample  # type: ignore[union-attr]
        self.docker = docker or DockerClient(settings.docker_socket)
        self._clock = clock
        self.tracker = ProcessTracker(settings.idle_util_pct)
        self._containers: dict[str, ContainerInfo | None] = {}  # container id -> info (None: not found)
        self._pid_cgroup: dict[int, tuple[str | None, str | None]] = {}  # pid -> (container id, cgroup path)
        self.snapshot: NodeSnapshot | None = None
        self.last_poll_monotonic: float | None = None
        self.docker_error: str | None = None
        self._poll_docker_error: str | None = None  # this poll's, published to docker_error when it ends

    async def aclose(self) -> None:
        await self.docker.aclose()
        if self._sampler:
            self._sampler.close()

    async def _container(self, cid: str) -> ContainerInfo | None:
        # One Docker lookup per new container ID, cached, rather than `docker top` on every container per new PID.
        if cid not in self._containers:
            try:
                self._containers[cid] = await self.docker.inspect(cid)
            except DockerError as exc:
                self._poll_docker_error = str(exc)
                return None  # not cached: retry next poll
        return self._containers[cid]

    def _cgroup_of(self, pid: int) -> tuple[str | None, str | None]:
        if pid not in self._pid_cgroup:
            text = procfs.read_cgroup(self.settings.proc_root, pid)
            self._pid_cgroup[pid] = (
                (procfs.container_id_from_cgroup(text), procfs.cgroup_v2_path(text)) if text else (None, None)
            )
        return self._pid_cgroup[pid]

    async def poll_once(self) -> NodeSnapshot:
        self._poll_docker_error = None
        sample = await asyncio.to_thread(self._sample)
        now, wall = self._clock(), utcnow()
        if sample.error:
            # Keep the trackers as they were: an NVML hiccup should not reset every process's idle time.
            tracked = []
            log.warning("nvml_sample_failed", error=sample.error)
        else:
            tracked = self.tracker.update(sample, now=now, wall=wall)

        live_pids = {t.pid for t in tracked}
        for gone in set(self._pid_cgroup) - live_pids:
            del self._pid_cgroup[gone]

        processes: list[GpuProcess] = []
        cgroups: dict[str, str] = {}  # container name -> cgroup path, for anon memory
        for t in tracked:
            cid, cg_path = self._cgroup_of(t.pid)
            info = await self._container(cid) if cid else None
            if info and cg_path:
                cgroups.setdefault(info.name, cg_path)
            processes.append(
                GpuProcess(
                    pid=t.pid,
                    gpu=t.gpu,
                    vram_mb=t.vram_mb,
                    container=info.name if info else None,
                    container_id=cid,
                    service=info.service if info else None,
                    sm_util_pct=t.sm_util_pct,
                    first_seen=t.first_seen,
                    idle_s=round(t.idle_s(now), 1),
                )
            )
        live_cids = {p.container_id for p in processes if p.container_id}
        for stale in set(self._containers) - live_cids:
            del self._containers[stale]

        # Allow-listed containers matter even with nothing on the GPU (ComfyUI holding offloaded weights in RAM).
        for name in sorted(self.settings.containers - set(cgroups)):
            try:
                info = await self.docker.inspect(name)
            except DockerError as exc:
                self._poll_docker_error = str(exc)
                continue
            if info and info.running and info.pid:
                text = procfs.read_cgroup(self.settings.proc_root, info.pid)
                path = procfs.cgroup_v2_path(text) if text else None
                if path:
                    cgroups[name] = path

        inventory: list[ContainerEntry] | None = None
        try:
            inventory = [
                entry.model_copy(update={"managed": self.settings.manages(entry.name, labels)})
                for entry, labels in await self.docker.list()
            ]
        except DockerError as exc:
            self._poll_docker_error = str(exc)

        containers = []
        for name, path in sorted(cgroups.items()):
            anon = procfs.read_anon_mb(self.settings.cgroup_root, path)
            if anon is not None:
                containers.append(ContainerMemory(container=name, anon_mb=anon))

        self.snapshot = NodeSnapshot(
            node=self.settings.node,
            taken_at=wall,
            agent_version=argus_iris.__version__,
            gpus=sample.gpus,
            processes=processes,
            host=procfs.read_host_memory(self.settings.proc_root / "meminfo"),
            containers=containers,
            nvml_error=sample.error,
            inventory=inventory,
        )
        self.last_poll_monotonic = now
        self.docker_error = self._poll_docker_error  # a Docker failure is reported only while it lasts
        return self.snapshot

    async def allowed(self, name: str) -> bool:
        """The broker may act on *name*: allow-listed, or labelled for it where the agent opted in to the label."""
        if name in self.settings.containers:
            return True
        if self.settings.manage_label is None:
            return False
        try:
            info = await self.docker.inspect(name)  # the label as it is now, not as the last poll saw it
        except DockerError:
            return False
        return info is not None and self.settings.manages(info.name, info.labels)

    async def announce(self, client: httpx.AsyncClient) -> bool:
        """Tell the broker this node exists (POST /nodes). False when the broker refused or could not be reached."""
        s = self.settings
        body = {"name": s.node, "agent_url": s.advertise_url, "port": s.port, "gpu": s.gpu}
        try:
            resp = await client.post(
                f"{s.broker_url}/nodes", json=body, headers={"Authorization": f"Bearer {s.token}"}, timeout=10.0
            )
        except httpx.HTTPError as exc:
            log.warning("announce_failed", broker=s.broker_url, error=str(exc))
            return False
        if resp.status_code >= 400:
            log.warning("announce_refused", broker=s.broker_url, status=resp.status_code, detail=resp.text[:200])
            return False
        return True

    async def act(self, name: str, action: ContainerAction) -> ActionResult:
        try:
            outcome = await self.docker.act(name, action)
        except DockerError as exc:
            log.warning("container_action_failed", container=name, action=action.value, error=str(exc))
            return ActionResult(container=name, action=action, ok=False, detail=str(exc))
        log.info("container_action", container=name, action=action.value, outcome=outcome)
        return ActionResult(container=name, action=action, ok=True, detail=outcome)


class _SnapshotCollector:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def collect(self):
        snap = self.agent.snapshot
        node = self.agent.settings.node
        total = GaugeMetricFamily("iris_agent_gpu_vram_total_mb", "Total VRAM", labels=["node", "gpu"])
        free = GaugeMetricFamily("iris_agent_gpu_vram_free_mb", "Free VRAM (NVML)", labels=["node", "gpu"])
        util = GaugeMetricFamily("iris_agent_gpu_util_pct", "GPU utilisation", labels=["node", "gpu"])
        pvram = GaugeMetricFamily(
            "iris_agent_process_vram_mb", "VRAM per process", labels=["node", "gpu", "service", "container", "pid"]
        )
        pidle = GaugeMetricFamily(
            "iris_agent_process_idle_seconds",
            "Seconds since the process last changed VRAM or used the GPU",
            labels=["node", "gpu", "service", "container", "pid"],
        )
        ram = GaugeMetricFamily("iris_agent_host_ram_available_mb", "Host MemAvailable", labels=["node"])
        anon = GaugeMetricFamily(
            "iris_agent_container_anon_mb", "Container anonymous memory (cgroup)", labels=["node", "container"]
        )
        up = GaugeMetricFamily("iris_agent_nvml_up", "1 when the last NVML sample succeeded", labels=["node"])
        if snap:
            up.add_metric([node], 0.0 if snap.nvml_error else 1.0)
            for g in snap.gpus:
                total.add_metric([node, str(g.index)], g.total_mb)
                free.add_metric([node, str(g.index)], g.free_mb)
                if g.util_pct is not None:
                    util.add_metric([node, str(g.index)], g.util_pct)
            for p in snap.processes:
                labels = [node, str(p.gpu), p.service or "", p.container or "", str(p.pid)]
                pvram.add_metric(labels, p.vram_mb)
                pidle.add_metric(labels, p.idle_s)
            if snap.host:
                ram.add_metric([node], snap.host.available_mb)
            for c in snap.containers:
                anon.add_metric([node, c.container], c.anon_mb)
        yield from (total, free, util, pvram, pidle, ram, anon, up)


def create_app(settings: AgentSettings | None = None, *, agent: Agent | None = None, poll: bool = True) -> FastAPI:
    settings = settings or (agent.settings if agent else AgentSettings.from_env())
    agent = agent or Agent(settings)
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_SnapshotCollector(agent))

    async def _poller() -> None:
        while True:
            try:
                await agent.poll_once()
            except Exception:  # noqa: BLE001 - keep polling; the failure is logged
                log.exception("agent_poll_failed")
            await asyncio.sleep(settings.poll_s)

    async def _announcer() -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await agent.announce(client)
                except Exception:  # noqa: BLE001 - keep announcing; the failure is logged
                    log.exception("agent_announce_failed")
                await asyncio.sleep(settings.announce_s)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_poller(), name="iris-agent-poll") if poll else None
        announcing = bool(poll and settings.broker_url and settings.token)
        announcer = asyncio.create_task(_announcer(), name="iris-agent-announce") if announcing else None
        log.info(
            "agent_started",
            node=settings.node,
            poll_s=settings.poll_s,
            actions="enabled" if settings.token else "disabled (no IRIS_AGENT_TOKEN)",
            containers=sorted(settings.containers),
            manage_label=settings.manage_label,
            broker=settings.broker_url if announcing else None,
        )
        try:
            yield
        finally:
            try:
                running = [t for t in (task, announcer) if t]
                for t in running:
                    t.cancel()
                for t, res in zip(running, await asyncio.gather(*running, return_exceptions=True), strict=True):
                    if isinstance(res, Exception):  # CancelledError is not one
                        log.error("agent_task_failed", task=t.get_name(), error=repr(res))
            finally:
                await agent.aclose()

    app = FastAPI(title="iris-agent", version=argus_iris.__version__, lifespan=lifespan)
    app.state.agent = agent

    @app.get("/snapshot", response_model=NodeSnapshot)
    async def snapshot() -> NodeSnapshot:
        if agent.snapshot is None:
            raise HTTPException(503, "no sample yet")
        return agent.snapshot

    # /health is argus-cortex's ServiceHealth, with the agent's own fields beside it. `status` keeps its meaning:
    # degraded until the first sample and while NVML fails (read from the last poll, so never cached), never 503.
    async def nvml_sampled() -> DependencyCheck:
        snap = agent.snapshot
        if snap is None:
            return DependencyCheck(ok=False, detail="no sample yet")
        return DependencyCheck(ok=not snap.nvml_error, detail="NVML error" if snap.nvml_error else None)

    def own_health() -> dict:
        snap = agent.snapshot
        age = None if agent.last_poll_monotonic is None else round(time.monotonic() - agent.last_poll_monotonic, 1)
        return {
            "node": settings.node,
            "last_poll_age_s": age,
            "nvml_error": snap.nvml_error if snap else None,
            "docker_error": agent.docker_error,
            "actions_enabled": bool(settings.token),
        }

    install_health(
        app,
        service="argus-iris",
        version=argus_iris.__version__,
        wire={"snapshot_version": SNAPSHOT_VERSION},
        checks={"nvml": nvml_sampled},
        extra=own_health,
        check_ttl_s=0,
    )

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/containers/{name}/{action}", response_model=ActionResult)
    async def container_action(
        name: str, action: ContainerAction, authorization: str | None = Header(default=None)
    ) -> ActionResult:
        if not settings.token:
            raise HTTPException(403, "container actions are disabled: IRIS_AGENT_TOKEN is not set")
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), settings.token.encode()):
            raise HTTPException(401, "bad or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not await agent.allowed(name):
            raise HTTPException(403, f"container {name!r} is not on this agent's allowlist")
        result = await agent.act(name, action)
        if not result.ok:
            raise HTTPException(502, result.detail or "docker action failed")
        return result

    return app
