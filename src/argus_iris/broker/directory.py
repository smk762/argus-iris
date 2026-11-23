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

Read-only: nothing here starts, stops or changes a service.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.registry import container_port, host_of
from argus_iris.models import ContainerEntry, DependencyCheck, DirectoryEntry, EntryHealth, PeerLookup, utcnow

log = structlog.get_logger("argus_iris.directory")

PEER, SERVICE, HEALTH, PORT = "argus.peer", "argus.service", "argus.health", "argus.port"
STATUSES = ("ok", "degraded", "starting")
_RANK = {"ok": 0, "degraded": 1, "starting": 2, None: 3, "down": 4}


def _text(v: Any, limit: int = 200) -> str | None:
    return str(v)[:limit] if isinstance(v, str | int | float) and not isinstance(v, bool) else None


def read_health(status_code: int | None, body: Any, *, error: str | None = None) -> EntryHealth:
    """An :class:`EntryHealth` from a ``/health`` answer, whatever its shape."""
    now = utcnow()
    if status_code is None:
        return EntryHealth(checked_at=now, status="down", error=error)
    data = body if isinstance(body, dict) else {}
    said = data.get("status") if data.get("status") in STATUSES else None
    if status_code == 503:
        status = said or "starting"
    elif 200 <= status_code < 300:
        status = said or "ok"
    else:
        status = "down"
    checks: dict[str, DependencyCheck] = {}
    raw_checks = data.get("checks")
    if isinstance(raw_checks, dict):
        for name, c in list(raw_checks.items())[:32]:
            if isinstance(c, dict) and isinstance(c.get("ok"), bool):
                checks[str(name)[:64]] = DependencyCheck(ok=c["ok"], detail=_text(c.get("detail")))
    wire = data.get("wire")
    caps = data.get("capabilities")
    return EntryHealth(
        checked_at=now,
        status=status,
        http_status=status_code,
        contract=all(isinstance(data.get(k), str) for k in ("service", "version", "status")),
        service=_text(data.get("service"), 100),
        version=_text(data.get("version"), 100),
        wire={str(k)[:64]: str(v)[:32] for k, v in wire.items() if isinstance(v, str)}
        if isinstance(wire, dict)
        else {},
        capabilities=[str(c)[:64] for c in caps[:32] if isinstance(c, str)] if isinstance(caps, list) else [],
        checks=checks,
        error=None if status != "down" else f"HTTP {status_code}",
    )


class Directory:
    def __init__(self, config: BrokerConfig, client: httpx.AsyncClient, clock: Callable[[], datetime] = utcnow) -> None:
        self.config = config
        self._client = client
        self._clock = clock
        self.entries: list[DirectoryEntry] = []
        self._health: dict[tuple[str, str | None, str | None], EntryHealth] = {}  # (peer, node, container|url)

    @staticmethod
    def _key(e: DirectoryEntry) -> tuple[str, str | None, str | None]:
        return (e.peer, e.node, e.container or e.url)

    def rebuild(self, inventories: dict[str, list[ContainerEntry]]) -> None:
        """Entries from the config's peers and every node's last inventory, keeping each one's last health."""
        entries: list[DirectoryEntry] = [
            DirectoryEntry(peer=peer, origin="config", url=url)
            for peer, urls in self.config.peers.items()
            for url in urls
        ]
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
                entries.append(
                    DirectoryEntry(
                        peer=peer[:100],
                        origin="label",
                        service=(c.labels.get(SERVICE) or None),
                        node=node,
                        container=c.name,
                        running=c.running,
                        url=f"http://{host_of(self.config.nodes[node].agent_url)}:{port}" if port else None,
                        reason=None if port else why,
                        health_path=path[:200],
                    )
                )
        for e in entries:
            e.health = self._health.get(self._key(e))
            if e.service is None and e.health and e.health.service:
                e.service = e.health.service
        keys = {self._key(e) for e in entries}
        self._health = {k: h for k, h in self._health.items() if k in keys}
        self.entries = entries

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
            h.checked_at = now  # the directory's clock, so "due" is measured on one clock
            self._health[self._key(e)] = h
            e.health = h
            if e.service is None and h.service:
                e.service = h.service
        for e in self.entries:
            if not e.running:
                self._health.pop(self._key(e), None)
                e.health = None

    async def _probe_one(self, e: DirectoryEntry) -> EntryHealth:
        try:
            resp = await self._client.get(f"{e.url}{e.health_path}", timeout=self.config.directory_probe_timeout_s)
        except httpx.HTTPError as exc:
            return read_health(None, None, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            body = resp.json()
        except ValueError:
            body = None
        return read_health(resp.status_code, body)

    def lookup(self, peer: str) -> PeerLookup:
        """Where to reach *peer*, best first: configured, then running and healthiest, then node order."""
        order = {n: i for i, n in enumerate(self.config.nodes)}
        hits = [e for e in self.entries if e.peer == peer and e.url and e.running]
        hits.sort(
            key=lambda e: (
                e.origin != "config",
                _RANK[e.health.status if e.health else None],
                order.get(e.node or "", len(order)),
            )
        )
        return PeerLookup(peer=peer, urls=hits)

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
