"""The suite's service directory (#48): every container labelled ``argus.peer`` on any node, where to reach it, and
what its ``/health`` says.

The agents already report every container on their hosts with its published ports (#47's ``inventory``). A container
whose compose file labels it ``argus.peer: <name>`` (argus-cortex#210, #212) is an entry here:

- **URL:** the node agent's host and the container's published port, by the same rules as a registered resident
  (:func:`~argus_iris.broker.registry.container_port`): ``argus.port`` picks the container port when it publishes
  several, and a loopback-only bind is reported, not served. Nobody supplies a host.
- **Health:** read every ``directory_health_s`` from ``argus.health`` (default ``/health``), tolerantly. An
  argus-cortex ``ServiceHealth`` body gives version, wire versions, capabilities and checks; anything else is up or
  down by its HTTP status. Only those fields are kept, never the body.
- **Config wins:** ``peers:`` in the broker config names URLs (a host with no agent, a service outside Docker) that
  come first for that peer.
- **Itself:** the broker's own container (found from ``/proc/self/mountinfo``) is probed on loopback at its container
  port: a container cannot reach itself through its host's published port (Docker hairpin), so the LAN URL would
  read as down. The entry still lists the LAN URL callers use.
- **Wire:** the entries and the lookup reply are argus-cortex's ``service.DirectoryEntry``, ``EntryHealth`` and
  ``PeerLookup`` (#50), the models cortex's ``peers`` client reads. They are frozen, so an entry is replaced, never
  changed in place. A container whose labels cortex's model refuses (a peer name outside ``PEER_NAME_PATTERN``, a
  health path with a space) is left out and logged once: one bad label never takes the directory down.

Read-only: nothing here starts, stops or changes a service.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from argus_cortex.service import DependencyCheck, DirectoryEntry, EntryHealth, PeerLookup, ServiceHealth
from pydantic import ValidationError

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.registry import container_port, host_of
from argus_iris.models import ContainerEntry, utcnow

log = structlog.get_logger("argus_iris.directory")

PEER, SERVICE, HEALTH, PORT = "argus.peer", "argus.service", "argus.health", "argus.port"
STATUSES = ("ok", "degraded", "starting")
_MAX_ITEMS = 32  # of wire versions, capabilities and checks kept from one health body
_RANK = {"ok": 0, "degraded": 1, "starting": 2, None: 3, "down": 4}


_CONTAINER_ID = re.compile(r"/containers/([0-9a-f]{64})/")


def own_container_id(mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """This process's Docker container id, or None outside a container. Docker mounts the container's own
    ``hostname``/``hosts`` files from ``.../containers/<id>/``; the hostname (the short id unless compose sets one)
    is the fallback, matched as a prefix."""
    with contextlib.suppress(OSError), open(mountinfo, encoding="utf-8") as f:
        if m := _CONTAINER_ID.search(f.read()):
            return m.group(1)
    host = socket.gethostname()
    return host if re.fullmatch(r"[0-9a-f]{12,64}", host) else None


def _text(v: Any, limit: int = 200) -> str | None:
    return str(v)[:limit] if isinstance(v, str | int | float) and not isinstance(v, bool) else None


def _contract(data: dict[str, Any]) -> ServiceHealth | None:
    """The body as argus-cortex's ``ServiceHealth``, or None when it is not one (or speaks another major)."""
    try:
        return ServiceHealth.model_validate(data)
    except ValueError:  # pydantic's ValidationError is one
        return None


def _tolerant_checks(raw: Any) -> dict[str, DependencyCheck]:
    checks: dict[str, DependencyCheck] = {}
    if isinstance(raw, dict):
        for name, c in list(raw.items())[:_MAX_ITEMS]:
            if isinstance(c, dict) and isinstance(c.get("ok"), bool):
                checks[str(name)[:64]] = DependencyCheck(ok=c["ok"], detail=_text(c.get("detail")))
    return checks


def _tolerant_capabilities(raw: Any) -> tuple[str, ...]:
    """The names that are on, as ``ServiceHealth.capability_names`` gives them: a list's strings, or the sorted names
    a map marks ``true``. Anything else in either form is dropped."""
    if isinstance(raw, dict):
        names = sorted(k for k, on in raw.items() if isinstance(k, str) and on is True)
    elif isinstance(raw, list):
        names = [c for c in raw if isinstance(c, str)]
    else:
        return ()
    return tuple(c[:64] for c in names[:_MAX_ITEMS])


def read_health(
    status_code: int | None, body: Any, *, error: str | None = None, now: datetime | None = None
) -> EntryHealth:
    """An :class:`EntryHealth` from a ``/health`` answer, whatever its shape.

    A body that validates as cortex's ``ServiceHealth`` is read through that model (``contract``). Anything else is
    read field by field, best-effort, and is up or down by its HTTP status. Either way only the directory's own
    fields are kept, bounded in number and length, never the body."""
    now = now or utcnow()
    if status_code is None:
        return EntryHealth(checked_at=now, status="down", error=error[:200] if error else None)
    data = body if isinstance(body, dict) else {}
    health = _contract(data)
    if health is not None:
        said: str | None = health.status
        fields: dict[str, Any] = {
            "service": health.service,
            "version": health.version,
            "wire": {k[:64]: v[:32] for k, v in list(health.wire.items())[:_MAX_ITEMS]},
            "capabilities": tuple(c[:64] for c in health.capability_names[:_MAX_ITEMS]),
            "checks": {k[:64]: c for k, c in list(health.checks.items())[:_MAX_ITEMS]},
        }
    else:
        said = data.get("status") if data.get("status") in STATUSES else None
        wire = data.get("wire")
        fields = {
            "service": _text(data.get("service"), 100),
            "version": _text(data.get("version"), 100),
            "wire": {str(k)[:64]: v[:32] for k, v in list(wire.items())[:_MAX_ITEMS] if isinstance(v, str)}
            if isinstance(wire, dict)
            else {},
            "capabilities": _tolerant_capabilities(data.get("capabilities")),
            "checks": _tolerant_checks(data.get("checks")),
        }
    if status_code == 503:
        status = said or "starting"
    elif 200 <= status_code < 300:
        status = said or "ok"
    else:
        status = "down"
    return EntryHealth(
        checked_at=now,
        status=status,
        http_status=status_code,
        contract=health is not None,
        error=None if status != "down" else f"HTTP {status_code}",
        **fields,
    )


class Directory:
    def __init__(
        self,
        config: BrokerConfig,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] = utcnow,
        self_id: str | None = None,
    ) -> None:
        self.config = config
        self._self_id = self_id  # the broker's own container id: probed on loopback, not through the LAN
        self._probe_base: dict[tuple[str, str | None, str | None], str] = {}  # key -> loopback base URL
        self._client = client
        self._clock = clock
        self.entries: list[DirectoryEntry] = []
        self._health: dict[tuple[str, str | None, str | None], EntryHealth] = {}  # (peer, node, container|url)
        self._refused: set[tuple[str, str]] = set()  # (node, container) already logged for a refused label

    @staticmethod
    def _key(e: DirectoryEntry) -> tuple[str, str | None, str | None]:
        return (e.peer, e.node, e.container or e.url)

    def _with_health(self, e: DirectoryEntry) -> DirectoryEntry:
        """*e* with its last health, and the service its health names when no label did. A copy: entries are frozen."""
        h = self._health.get(self._key(e))
        return e.model_copy(update={"health": h, "service": e.service or (h.service if h else None)})

    def rebuild(self, inventories: dict[str, list[ContainerEntry]]) -> None:
        """Entries from the config's peers and every node's last inventory, keeping each one's last health."""
        entries: list[DirectoryEntry] = [
            DirectoryEntry(peer=peer, origin="config", url=url)  # names and URLs were checked when the config loaded
            for peer, urls in self.config.peers.items()
            for url in urls
        ]
        refused: set[tuple[str, str]] = set()
        probe_base: dict[tuple[str, str | None, str | None], str] = {}
        for node in self.config.nodes:
            for c in inventories.get(node, []):
                peer = (c.labels.get(PEER) or "").strip()
                if not peer:
                    continue
                want = None
                with contextlib.suppress(ValueError):
                    want = int(c.labels[PORT]) if c.labels.get(PORT) else None
                port, why = container_port(c, want, "set the argus.port label")
                path = c.labels.get(HEALTH) or "/health"
                if not path.startswith("/"):
                    path = f"/{path}"
                try:
                    entry = DirectoryEntry(
                        peer=peer,
                        origin="label",
                        service=(c.labels.get(SERVICE) or None),
                        node=node,
                        container=c.name,
                        running=c.running,
                        url=f"http://{host_of(self.config.nodes[node].agent_url)}:{port}" if port else None,
                        reason=None if port else why,
                        health_path=path,
                    )
                except ValidationError as exc:
                    # A label cortex's model refuses (a peer name callers could never ask for, a health path with a
                    # space). Skip the container, say so once, and keep the rest of the directory.
                    refused.add((node, c.name))
                    if (node, c.name) not in self._refused:
                        log.warning(
                            "directory_label_refused",
                            node=node,
                            container=c.name,
                            fields=sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}),
                            peer=peer[:100],
                        )
                    continue
                entries.append(entry)
                if port and self._is_self(c):
                    private = next((p.private for p in c.ports if p.public == port and p.proto == "tcp"), port)
                    probe_base[self._key(entry)] = f"http://127.0.0.1:{private}"
        self._refused = refused
        self._probe_base = probe_base
        entries = [self._with_health(e) for e in entries]
        keys = {self._key(e) for e in entries}
        self._health = {k: h for k, h in self._health.items() if k in keys}
        self.entries = entries

    def _is_self(self, c: ContainerEntry) -> bool:
        sid = self._self_id
        return bool(sid) and (c.id == sid or (len(sid) < 64 and c.id.startswith(sid)))

    def _due(self, e: DirectoryEntry) -> bool:
        if not (e.url and e.running):
            return False
        h = self._health.get(self._key(e))
        return h is None or (self._clock() - h.checked_at).total_seconds() >= self.config.directory_health_s

    async def probe(self, *, all_: bool = False) -> None:
        """Read the health of every reachable entry that is due (never read, or read directory_health_s ago), or of
        all of them; concurrently. A new entry is read on the next call, not a whole interval later."""
        targets = [e for e in self.entries if (e.url and e.running) and (all_ or self._due(e))]
        results = await asyncio.gather(*(self._probe_one(e) for e in targets))
        now = self._clock()
        for e, h in zip(targets, results, strict=True):
            # The directory's clock, so "due" is measured on one clock.
            self._health[self._key(e)] = h.model_copy(update={"checked_at": now})
        for e in self.entries:
            if not e.running:
                self._health.pop(self._key(e), None)
        # Entries are frozen and a rebuild may have replaced the list meanwhile: attach to the current one.
        self.entries = [self._with_health(e) for e in self.entries]

    async def _probe_one(self, e: DirectoryEntry) -> EntryHealth:
        try:
            base = self._probe_base.get(self._key(e), e.url)
            resp = await self._client.get(f"{base}{e.health_path}", timeout=self.config.directory_probe_timeout_s)
        except httpx.HTTPError as exc:
            return read_health(None, None, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            body = resp.json()
        except ValueError:
            body = None
        return read_health(resp.status_code, body)

    def lookup(self, peer: str) -> PeerLookup | None:
        """Where to reach *peer*, best first: configured, then running and healthiest, then node order. None when
        *peer* is not a name a peer can have, so nothing could be listed under it."""
        order = {n: i for i, n in enumerate(self.config.nodes)}
        hits = [e for e in self.entries if e.peer == peer and e.url and e.running]
        hits.sort(
            key=lambda e: (
                e.origin != "config",
                _RANK[e.health.status if e.health else None],
                order.get(e.node or "", len(order)),
            )
        )
        try:
            return PeerLookup(peer=peer, urls=hits)
        except ValidationError:
            return None

    def service_discovery(self) -> list[dict[str, Any]]:
        """Prometheus ``http_sd`` target groups: one per reachable, running entry."""
        out = []
        for e in self.entries:
            if not (e.url and e.running):
                continue
            parts = urlsplit(e.url)
            labels = {"argus_peer": e.peer, "argus_origin": e.origin, "argus_health_path": e.health_path}
            for k, v in (("argus_service", e.service), ("node", e.node), ("container", e.container)):
                if v:
                    labels[k] = v
            if parts.scheme == "https":
                labels["__scheme__"] = "https"
            out.append({"targets": [parts.netloc], "labels": labels})
        return out
