"""Self-registration: services from tenant manifests, and nodes from agents that announce themselves.

Adding a GPU service should need nothing from the operator. A service's ``gpu-tenant.yaml`` already says what it keeps
resident (adapter, container, lifecycle routes); the node agents already see every container on their hosts. So a
manifest resident becomes a :class:`~argus_iris.broker.config.RegisteredService` as soon as some agent reports its
container: the node is that agent's, and ``base_url`` is that agent's host and the container's published port. The
caller never supplies a host the broker will send a token to.

- **Config wins.** A resident whose container a ``services:`` entry already names on that node is left to the entry,
  and a registered id never takes a configured one.
- **Lapse, not expiry.** A registration not renewed within ``registration_ttl_s`` is lapsed. Its services stay while
  their containers still exist (stopped included: iris may have stopped it, and a warm starts it again) and go once
  an agent reports the container removed. ``DELETE /tenants/{tenant}`` forgets one at once.
- **Admin token.** Sent with the registration (``X-Argus-Admin-Token``), kept with it (file mode 0600), and used only
  for that tenant's services with ``admin_auth``.
- **Nodes.** An agent with ``IRIS_BROKER_URL`` announces itself, authenticated with the agent token, so it proves it
  already holds the secret the broker would send it. Configured nodes win on name.

Everything is in memory, and written to ``<state_dir>/registrations.json`` when a state directory is set.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
from argus_cortex.gpu_wire import IN_PROCESS, TenantManifest, TenantResident, target_service
from pydantic import ValidationError

from argus_iris.broker.config import BrokerConfig, NodeConfig, RegisteredService, ServiceConfig
from argus_iris.models import ContainerEntry, utcnow

log = structlog.get_logger("argus_iris.registry")

STATE_FILE = "registrations.json"
STATE_VERSION = 1
NODE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
# cortex's adapter kinds -> iris's adapters. immich and container have no resident contract: up/down only.
ADAPTERS = {
    "comfyui": "comfyui",
    "ollama": "ollama",
    "argus-http": "argus-http",
    "immich": "external",
    "container": "external",
}


class RegistrationError(ValueError):
    """A registration iris refuses: a bad node name or agent URL."""


@dataclass
class Instance:
    """One process registering a tenant: several can (argus-echo's engines, each in its own container, one tenant)."""

    token: str | None
    seen_at: datetime


@dataclass
class Registration:
    manifest: TenantManifest
    registered_at: datetime
    # Container id of each registering process ("" when it could not tell) -> its token and last renewal.
    instances: dict[str, Instance] = field(default_factory=dict)

    @property
    def seen_at(self) -> datetime:
        return max(i.seen_at for i in self.instances.values())

    @property
    def container_ids(self) -> list[str]:
        return [cid for cid in self.instances if cid]

    def token_for(self, container_id: str | None = None) -> str | None:
        """The admin token of the instance running in *container_id*, else the one renewed last that sent any."""
        if container_id:
            for cid, inst in self.instances.items():
                if cid and (container_id.startswith(cid) or cid.startswith(container_id)) and inst.token:
                    return inst.token
        ranked = sorted(self.instances.values(), key=lambda i: i.seen_at, reverse=True)
        return next((i.token for i in ranked if i.token), None)


@dataclass
class AnnouncedNode:
    agent_url: str
    gpu: int
    announced_at: datetime
    seen_at: datetime


@dataclass
class Placement:
    """Where one manifest resident landed on one node, or why it did not."""

    tenant: str
    resident: str
    node: str | None = None
    service: str | None = None  # the registered service id, when one was made
    covered_by: str | None = None  # a configured service that already reads this container
    reason: str | None = None  # why it is not a service (yet)


@dataclass
class Registry:
    config: BrokerConfig
    state_dir: Path | None = None
    clock: Callable[[], datetime] = utcnow
    tenants: dict[str, Registration] = field(default_factory=dict)
    announced: dict[str, AnnouncedNode] = field(default_factory=dict)
    inventory: dict[str, list[ContainerEntry]] = field(default_factory=dict)  # node -> last inventory it reported
    services: list[RegisteredService] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.configured = list(self.config.services)
        self.configured_nodes = dict(self.config.nodes)

    # ── tenants ──────────────────────────────────────────────────────────────────────────────────────────────────

    def register(self, manifest: TenantManifest, *, token: str | None = None, container_id: str | None = None) -> bool:
        """Store or renew *manifest*'s registration. True when anything iris acts on changed (not just the time)."""
        now = self.clock()
        container_id = container_id.strip().lower() if container_id else None
        if container_id and not CONTAINER_ID.fullmatch(container_id):
            container_id = None  # not a Docker id: ignore it rather than refuse the registration
        token = token or None
        key = container_id or ""
        reg = self.tenants.get(manifest.tenant)
        if reg is None or reg.manifest != manifest:
            reg = self.tenants[manifest.tenant] = Registration(manifest, now, dict(reg.instances) if reg else {})
            changed = True
        else:
            inst = reg.instances.get(key)
            changed = inst is None or inst.token != token
        reg.instances[key] = Instance(token, now)
        # Instances gone well past the lapse (containers recreated under new ids) are dropped, so the map stays small.
        stale = now - timedelta(seconds=4 * self.config.registration_ttl_s)
        for cid in [c for c, i in reg.instances.items() if i.seen_at < stale]:
            del reg.instances[cid]
        if changed:
            log.info(
                "tenant_registration",
                tenant=manifest.tenant,
                residents=[r.name for r in manifest.residents],
                admin_token=bool(token),
                container_id=container_id[:12] if container_id else None,
            )
        self.save() if changed else self._touch()
        return changed

    def forget(self, tenant: str, *, purge: bool = False, container_id: str | None = None) -> bool:
        """A tenant signed off. cortex's client does this on every shutdown, including when iris itself stopped its
        container, so by default it only lapses the registration now: its services stay while their containers exist,
        and a warm can still start them. With *container_id*, only that instance lapses (one of argus-echo's engines
        stopping says nothing about the others). *purge* drops it and its services at once (the dashboard's Forget)."""
        reg = self.tenants.get(tenant)
        if reg is None:
            return False
        if purge:
            del self.tenants[tenant]
        else:
            gone = self.clock() - timedelta(seconds=self.config.registration_ttl_s + 1)
            cid = (container_id or "").strip().lower()
            for key, inst in reg.instances.items():
                if not cid or key == cid:
                    inst.seen_at = min(inst.seen_at, gone)
        log.info("tenant_forgotten" if purge else "tenant_signed_off", tenant=tenant, container_id=container_id)
        self.save()
        return True

    def lapsed(self, reg: Registration) -> bool:
        """Every instance has gone without renewing for registration_ttl_s (or signed off)."""
        return self.clock() - reg.seen_at > timedelta(seconds=self.config.registration_ttl_s)

    # ── nodes ────────────────────────────────────────────────────────────────────────────────────────────────────

    def announce(self, name: str, agent_url: str, gpu: int = 0) -> str:
        """An agent says it runs node *name* at *agent_url*. Returns ``configured``, ``added``, ``changed`` or
        ``renewed``; raises :class:`RegistrationError` for a bad name or URL."""
        if not NODE_NAME.fullmatch(name):
            raise RegistrationError(f"node name {name!r} is not a slug")
        agent_url = _agent_url(agent_url)
        if name in self.configured_nodes:
            return "configured"
        if not self.config.register_nodes:
            raise RegistrationError("this broker does not take announced nodes (register_nodes: false)")
        now = self.clock()
        was = self.announced.get(name)
        if was and (was.agent_url, was.gpu) == (agent_url, gpu):
            was.seen_at = now
            self._touch()
            return "renewed"
        self.announced[name] = AnnouncedNode(agent_url, gpu, was.announced_at if was else now, now)
        self._apply_nodes()
        log.info("node_announced", node=name, agent_url=agent_url, gpu=gpu, was=was.agent_url if was else None)
        self.save()
        return "changed" if was else "added"

    def forget_node(self, name: str) -> bool:
        if self.announced.pop(name, None) is None:
            return False
        self.config.nodes.pop(name, None)
        self.inventory.pop(name, None)
        log.info("node_forgotten", node=name)
        self.save()
        return True

    def _apply_nodes(self) -> None:
        for name, node in self.announced.items():
            if name not in self.configured_nodes:
                self.config.nodes[name] = NodeConfig(agent_url=node.agent_url, gpu=node.gpu)

    # ── resolution ───────────────────────────────────────────────────────────────────────────────────────────────

    def observe(self, node: str, inventory: list[ContainerEntry] | None) -> None:
        """A node's agent reported its containers. None (old agent, Docker down) keeps the last view: nothing is
        removed on the strength of an agent that could not say."""
        if inventory is not None:
            self.inventory[node] = inventory

    def reconcile(self) -> bool:
        """Recompute the registered services and put them, after the configured ones, into ``config.services`` (in
        place: the lease book and residency hold the same list). True when the set of services changed."""
        services: list[RegisteredService] = []
        placements: list[Placement] = []
        if self.config.register_services:
            taken = {s.id for s in self.configured}
            for tenant in sorted(self.tenants):
                reg = self.tenants[tenant]
                for r in reg.manifest.residents:
                    for p, svc in self._place(reg, r, taken):
                        placements.append(p)
                        if svc is not None:
                            services.append(svc)
                            taken.add(svc.id)
        changed = _fingerprint(services) != _fingerprint(self.services)
        if not changed:
            services = self.services  # keep the objects the adapters already hold
        for svc in services:  # tokens can change without the service doing so
            reg = self.tenants[svc.tenant]
            res = next(r for r in reg.manifest.residents if r.name == svc.resident)
            entry = next((e for e in self.inventory.get(svc.node, []) if e.name == svc.container), None)
            svc.with_token(reg.token_for(entry.id if entry else None) if res.admin_auth else None)
        self.services, self.placements = services, placements
        self.config.services[:] = [*self.configured, *services]
        if changed:
            log.info("registered_services", services=[f"{s.node}/{s.id}" for s in services])
        return changed

    def _place(
        self, reg: Registration, r: TenantResident, taken: set[str]
    ) -> list[tuple[Placement, RegisteredService | None]]:
        tenant = reg.manifest.tenant
        adapter = ADAPTERS.get(r.adapter)
        if adapter is None:
            return [(Placement(tenant, r.name, reason=f"adapter {r.adapter!r} is not one iris reads"), None)]
        found = self._containers(reg, r)
        if not found:
            node = reg.manifest.node
            port = getattr(r, "port", None) or _url_port(r.base_url)
            if self.lapsed(reg):
                return [
                    (Placement(tenant, r.name, reason="registration lapsed and no agent reports its container"), None)
                ]
            if node in self.config.nodes and port and not r.container:
                # No container to look for (a host process): trust the manifest's node and port, never its host.
                found = {node: None}
            else:
                why = (
                    f"no agent reports container {r.container!r}"
                    if r.container
                    else "the manifest names no container, and the client did not say which container it runs in"
                )
                return [(Placement(tenant, r.name, node=node, reason=why), None)]
        out: list[tuple[Placement, RegisteredService | None]] = []
        covers = {
            node: next((s.id for s in self.configured if s.node == node and s.container == entry.name), None)
            for node, entry in found.items()
            if entry is not None
        }
        several = sum(1 for node in found if not covers.get(node)) > 1
        for node, entry in found.items():
            p = Placement(tenant, r.name, node=node)
            if covers.get(node):
                p.covered_by = covers[node]
                out.append((p, None))
                continue
            port, why = _port(r, entry)
            if port is None:
                p.reason = why
                out.append((p, None))
                continue
            sid = _service_id(r.name, node, tenant, taken, several)
            p.service = sid
            scheme, path = _scheme_path(r.base_url)
            serves = [r.name] if sid != r.name else []
            if r.target != IN_PROCESS and (t := target_service(r.target)) not in (sid, adapter, *serves):
                serves.append(t)
            svc = RegisteredService(
                id=sid,
                node=node,
                adapter=adapter,  # type: ignore[arg-type]
                base_url=f"{scheme}://{_host(self.config.nodes[node].agent_url)}:{port}{path}",
                serves=serves,
                container=entry.name if entry else r.container,
                health_path=r.health_path,
                unload_path=r.unload_path,
                evict_path=r.evict_path,
                warm_path=r.warm_path,
                warm_mb=r.vram_mb,
                tenant=tenant,
                resident=r.name,
                managed=entry.managed if entry else None,
            )
            out.append((p, svc))
        return out

    def _containers(self, reg: Registration, r: TenantResident) -> dict[str, ContainerEntry | None]:
        """The node -> container each node's agent reports for resident *r*: by name, else compose service label; for a
        resident that names none, the container the registering process runs in."""
        found: dict[str, ContainerEntry | None] = {}
        for node in self.config.nodes:
            inv = self.inventory.get(node) or []
            if r.container:
                hits = [e for e in inv if e.name == r.container] or [e for e in inv if e.service == r.container]
            else:
                hits = [e for e in inv if any(_same_id(e.id, cid) for cid in reg.container_ids)]
            if not hits:
                continue
            # Two compose projects with the same service on one host: a registrant's own, else a running one.
            mine = [e for e in hits if any(_same_id(e.id, cid) for cid in reg.container_ids)]
            running = [e for e in hits if e.running]
            found[node] = (mine or running or sorted(hits, key=lambda e: e.name))[0]
        return found

    # ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────

    def describe_tenants(self) -> list[dict[str, Any]]:
        out = []
        for tenant in sorted(self.tenants):
            reg = self.tenants[tenant]
            out.append(
                {
                    "tenant": tenant,
                    "repo": reg.manifest.repo,
                    "description": reg.manifest.description,
                    "kinds": [k.kind for k in reg.manifest.kinds],
                    "residents": [r.name for r in reg.manifest.residents],
                    "registered_at": reg.registered_at,
                    "seen_at": reg.seen_at,
                    "lapsed": self.lapsed(reg),
                    "admin_token": reg.token_for() is not None,
                    "instances": len(reg.instances),
                    "needs_admin_token": any(r.admin_auth for r in reg.manifest.residents),
                    "placements": [
                        {k: v for k, v in vars(p).items() if k != "tenant" and v is not None}
                        for p in self.placements
                        if p.tenant == tenant
                    ],
                }
            )
        return out

    def describe_services(self) -> list[dict[str, Any]]:
        """Every service iris reads now, configured and registered, for ``GET /services``."""
        out: list[dict[str, Any]] = []
        for s in self.config.services:
            row = s.model_dump(mode="json", exclude={"token_env"})
            row["origin"] = "tenant" if isinstance(s, RegisteredService) else "config"
            if isinstance(s, RegisteredService):
                reg = self.tenants.get(s.tenant)
                row["lapsed"] = bool(reg and self.lapsed(reg))
                row["admin_token"] = bool(s.headers())
            else:
                row["admin_token"] = bool(s.headers()) if s.token_env else None
                entry = next((e for e in self.inventory.get(s.node, []) if e.name == s.container), None)
                row["managed"] = entry.managed if entry else None
            out.append(row)
        return out

    def describe_nodes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, cfg in self.config.nodes.items():
            a = self.announced.get(name) if name not in self.configured_nodes else None
            out.append(
                {
                    "name": name,
                    "agent_url": cfg.agent_url,
                    "gpu": cfg.gpu,
                    "origin": "agent" if a else "config",
                    "announced_at": a.announced_at if a else None,
                    "seen_at": a.seen_at if a else None,
                    "containers": len(self.inventory[name]) if name in self.inventory else None,
                }
            )
        return out

    # ── persistence ──────────────────────────────────────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path | None:
        return self.state_dir / STATE_FILE if self.state_dir else None

    def _touch(self) -> None:
        # A renewal only moves seen_at: write it now and then, not on every heartbeat-sized renewal.
        now = self.clock()
        last = getattr(self, "_saved_at", None)
        if last is None or now - last > timedelta(seconds=self.config.registration_ttl_s / 3):
            self.save()

    def save(self) -> None:
        path = self.path
        if path is None:
            return
        self._saved_at = self.clock()
        data = {
            "version": STATE_VERSION,
            "tenants": {
                t: {
                    "manifest": reg.manifest.model_dump(mode="json"),
                    "registered_at": reg.registered_at.isoformat(),
                    "instances": {
                        cid: {"token": i.token, "seen_at": i.seen_at.isoformat()} for cid, i in reg.instances.items()
                    },
                }
                for t, reg in self.tenants.items()
            },
            "nodes": {
                n: {
                    "agent_url": a.agent_url,
                    "gpu": a.gpu,
                    "announced_at": a.announced_at.isoformat(),
                    "seen_at": a.seen_at.isoformat(),
                }
                for n, a in self.announced.items()
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds admin tokens
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("registrations_not_saved", path=str(path), error=str(exc))

    def load(self) -> None:
        path = self.path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("registrations_unreadable", path=str(path), error=str(exc))
            return
        for t, row in (data.get("tenants") or {}).items():
            try:
                instances = {
                    cid: Instance(i.get("token"), datetime.fromisoformat(i["seen_at"]))
                    for cid, i in (row.get("instances") or {}).items()
                    if not cid or CONTAINER_ID.fullmatch(cid)
                }
                if not instances:
                    raise ValueError("no instances")
                self.tenants[t] = Registration(
                    TenantManifest.model_validate(row["manifest"]),
                    datetime.fromisoformat(row["registered_at"]),
                    instances,
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                log.warning("registration_skipped", tenant=t, error=str(exc)[:300])
        for n, row in (data.get("nodes") or {}).items():
            with contextlib.suppress(KeyError, TypeError, ValueError, RegistrationError):
                if NODE_NAME.fullmatch(n):
                    self.announced[n] = AnnouncedNode(
                        _agent_url(row["agent_url"]),
                        int(row.get("gpu", 0)),
                        datetime.fromisoformat(row["announced_at"]),
                        datetime.fromisoformat(row["seen_at"]),
                    )
        self._apply_nodes()
        log.info("registrations_loaded", tenants=sorted(self.tenants), nodes=sorted(self.announced))


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _same_id(a: str, b: str) -> bool:
    """Docker ids, one maybe the 12-character short form."""
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _fingerprint(services: list[ServiceConfig]) -> list[str]:
    return [s.model_dump_json() for s in services]


def _agent_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise RegistrationError(f"agent_url is not a URL: {exc}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RegistrationError("agent_url must be an http:// or https:// URL with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise RegistrationError("agent_url must not carry credentials, a query or a fragment")
    return url.rstrip("/")


def _host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return f"[{host}]" if ":" in host else host


def _url_port(url: str | None) -> int | None:
    if not url:
        return None
    with contextlib.suppress(ValueError):
        return urlsplit(url).port
    return None


def _scheme_path(url: str | None) -> tuple[str, str]:
    """The scheme and path prefix of a manifest's base_url (``http``, ``""`` without one). Its host is never used."""
    if not url:
        return "http", ""
    parts = urlsplit(url)
    return parts.scheme or "http", parts.path.rstrip("/")


def _port(r: TenantResident, entry: ContainerEntry | None) -> tuple[int | None, str | None]:
    """The host port that reaches resident *r* in *entry*, or None and why not."""
    want = getattr(r, "port", None) or _url_port(r.base_url)
    if entry is None or entry.host_network:
        return (want, None) if want else (None, "no port: set port in the manifest")
    tcp = [p for p in entry.ports if p.proto == "tcp"]
    if want:
        hit = next((p for p in tcp if p.private == want), None) or next((p for p in tcp if p.public == want), None)
        if hit is None:
            return None, f"{entry.name} has no port {want}"
        if hit.public is None:
            return None, f"{entry.name} does not publish port {want}"
        return hit.public, None
    published = [p for p in tcp if p.public is not None]
    if len(published) == 1:
        return published[0].public, None
    if not published:
        return None, f"{entry.name} publishes no port"
    ports = ", ".join(str(p.private) for p in published)
    return None, f"{entry.name} publishes several ports ({ports}): set port in the manifest"


def _service_id(name: str, node: str, tenant: str, taken: set[str], several: bool) -> str:
    for sid in ([f"{name}-{node}"] if several else [name, f"{name}-{node}"]) + [f"{tenant}-{name}-{node}"]:
        if sid not in taken:
            return sid
    n = 2
    while f"{tenant}-{name}-{node}-{n}" in taken:
        n += 1
    return f"{tenant}-{name}-{node}-{n}"
