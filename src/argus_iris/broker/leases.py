"""Leases, observe-only (rollout step 3, first half): every request is placed and granted at once, and recorded.

The routes and their semantics are DESIGN.md's ("API (v1)", "Scheduling" > "Liveness"): ``request_id`` makes a
retried POST idempotent, ``resume`` re-attaches running work under the same id, a grant without a first heartbeat is
reclaimed, a granted lease expires ``ttl_s`` after its last heartbeat, and a queued request that nobody polls is
dropped. What is *not* here yet is admission: nothing waits for VRAM and nothing is evicted. Instead each grant
records whether it would have fit (``fits``), so footprints, waits and per-kind usage come from real leases before
admission is switched on.

Placement follows the target: a service target (``ollama:<model>``, ``comfyui``) goes to the services that serve it,
in config order (so ``ollama-dragon`` before ``ollama-jarrah``), preferring a node where the model is already
resident and skipping services that are down. Spilling to the next node *because the first is full* is an admission
decision and arrives with admission. ``in-process`` goes to the constrained node, the tenant's own node, or the
first node that meets ``min_cc`` and ``bf16``.

:class:`LeaseBook` is synchronous and takes its clock and node view as arguments, so it can be tested without a
server; the HTTP layer adds long-polling on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from argus_cortex.gpu_wire import (
    IN_PROCESS,
    GpuConstraints,
    Heartbeat,
    HeartbeatReply,
    LeaseRelease,
    LeaseRequest,
    LeaseState,
    LeaseStatus,
    PriorityClass,
    ReleaseOutcome,
    TenantManifest,
    target_model,
    target_service,
)
from pydantic import BaseModel

from argus_iris.broker.config import BrokerConfig
from argus_iris.models import NodeState, utcnow

log = structlog.get_logger("argus_iris.leases")

FIRST_BEAT_S = 30.0  # a grant with no heartbeat by then is reclaimed (DESIGN.md, Liveness) ...
UNPOLLED_MIN_S = 30.0  # ... and a queued request nobody has polled for 2 x wait_s (at least this) is dropped
RETAIN_S = 600.0  # finished leases stay readable (and idempotent) this long
ACTIVE: frozenset[str] = frozenset({"queued", "granted"})


class LeaseRecord(BaseModel):
    """A lease as the broker keeps it: the request, resolved against the tenant's manifest, plus what happened."""

    lease_id: str
    tenant: str
    caller: str = ""
    kind: str
    target: str
    request_id: str | None = None
    state: LeaseState
    reason: str | None = None
    node: str | None = None
    device: int | None = None
    base_url: str | None = None
    vram_mb: int
    model_mb: int | None = None
    priority: PriorityClass
    preemptible: bool
    ttl_s: float
    fits: bool | None = None  # would admission have granted it now? None: no ledger for the node
    admissible_mb: int | None = None  # what the node had to give, by the ledger, when it was granted
    resumed: bool = False
    heartbeats: int = 0
    peak_vram_mb: int | None = None
    outcome: ReleaseOutcome | None = None
    detail: str | None = None
    created_at: datetime
    not_before: datetime | None = None
    deadline: datetime | None = None
    granted_at: datetime | None = None
    last_heartbeat: datetime | None = None
    last_poll: datetime | None = None
    expires_at: datetime | None = None
    ended_at: datetime | None = None
    wait_s: float = 0.0

    @property
    def working_mb(self) -> int:
        return self.vram_mb - (self.model_mb or 0)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def status(self, position: int | None = None) -> LeaseStatus:
        return LeaseStatus(
            lease_id=self.lease_id,
            state=self.state,
            node=self.node,
            device=self.device,
            base_url=self.base_url,
            vram_mb=self.vram_mb,
            ttl_s=self.ttl_s,
            position=position if self.state == "queued" else None,
            reason=self.reason,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class Placement:
    node: str | None
    device: int | None = None
    base_url: str | None = None
    fits: bool | None = None
    admissible_mb: int | None = None
    why: str | None = None


def _model_key(name: str | None) -> str | None:
    """Ollama lists ``nomic-embed-text`` as ``nomic-embed-text:latest``; compare the two as one."""
    if not name:
        return None
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _cc(raw: str | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _meets(state: NodeState | None, c: GpuConstraints) -> bool:
    """Whether a node meets the GPU constraints. A node whose GPU is unknown (agent down) is given the benefit."""
    gpu = state.gpu if state else None
    if gpu is None:
        return True
    if c.min_cc is not None and (cc := _cc(gpu.compute_capability)) is not None and cc < c.min_cc:
        return False
    return not (c.bf16 and gpu.bf16 is False)


def _new_mb(rec: LeaseRecord, state: NodeState) -> int:
    """VRAM *rec* adds on top of the node's ledger, which already counts residents: a model that is loaded needs only
    its working set (DESIGN.md, Admission)."""
    model = _model_key(target_model(rec.target))
    loaded = model is not None and any(_model_key(r.model) == model for r in state.residents)
    return rec.working_mb + (0 if loaded else (rec.model_mb or 0))


def _fit(
    node: str, state: NodeState | None, rec: LeaseRecord, others: list[LeaseRecord]
) -> tuple[bool | None, int | None]:
    """Would *rec* fit on *node* beside the leases already granted there? ``(None, None)`` with no ledger."""
    if state is None or state.ledger is None:
        return None, None
    held = sum(_new_mb(o, state) for o in others if o.node == node and o.state == "granted")
    admissible = max(0, state.ledger.admissible_mb - held)
    return _new_mb(rec, state) <= admissible, admissible


def place(
    rec: LeaseRecord,
    constraints: GpuConstraints,
    home: str | None,
    config: BrokerConfig,
    nodes: dict[str, NodeState],
    others: list[LeaseRecord],
) -> Placement:
    """Where *rec* runs. Observe-only: the preferred node, whether or not it fits (``fits`` says which)."""
    wanted = constraints.node
    if rec.target == IN_PROCESS:
        names = [wanted] if wanted else ([home] if home else list(config.nodes))
        names = [n for n in names if n in config.nodes and _meets(nodes.get(n), constraints)]
        if not names:
            return Placement(None, why=f"no node meets {constraints.model_dump(exclude_none=True) or 'the request'}")
        reachable = [n for n in names if (s := nodes.get(n)) and s.reachable] or names
        fitting = [n for n in reachable if _fit(n, nodes.get(n), rec, others)[0]]
        node = (fitting or reachable)[0]
        fits, admissible = _fit(node, nodes.get(node), rec, others)
        return Placement(node, config.nodes[node].gpu, None, fits, admissible)

    name = target_service(rec.target)
    services = [s for s in config.services if name in (s.id, s.adapter)]
    services = [s for s in services if (not wanted or s.node == wanted) and _meets(nodes.get(s.node), constraints)]
    if not services:
        return Placement(None, why=f"no configured service serves {rec.target}" + (f" on {wanted}" if wanted else ""))

    def up(svc_id: str, node: str) -> bool:
        state = nodes.get(node)
        return bool(state and any(s.id == svc_id and s.up for s in state.services))

    live = [s for s in services if up(s.id, s.node)] or services  # all down: keep config order and let it fail there
    model = _model_key(target_model(rec.target))
    warm = [
        s
        for s in live
        if model
        and (st := nodes.get(s.node))
        and any(r.service == s.id and _model_key(r.model) == model for r in st.residents)
    ]
    svc = (warm or live)[0]
    fits, admissible = _fit(svc.node, nodes.get(svc.node), rec, others)
    return Placement(svc.node, config.nodes[svc.node].gpu, svc.base_url, fits, admissible)


class LeaseBook:
    """Every lease the broker knows, and the tenants that registered a manifest.

    *nodes* returns the broker's current node view (for placement); *clock* returns aware UTC (tests move it).
    *on_change* is called with each lease whose state or placement changed (the SSE stream).
    """

    def __init__(
        self,
        config: BrokerConfig,
        nodes: Callable[[], dict[str, NodeState]],
        *,
        clock: Callable[[], datetime] = utcnow,
        on_change: Callable[[LeaseRecord], None] | None = None,
    ) -> None:
        self.config = config
        self._nodes = nodes
        self._clock = clock
        self._on_change = on_change
        self.tenants: dict[str, TenantManifest] = {}
        self.leases: dict[str, LeaseRecord] = {}
        self._by_request: dict[tuple[str, str], str] = {}
        self._constraints: dict[str, GpuConstraints] = {}  # per lease: the manifest's, merged with the request's
        self._waiters: dict[str, asyncio.Event] = {}
        self.ended: Counter[tuple[str, str, str]] = Counter()  # (tenant, kind, how it ended) -> count

    # --- tenants -------------------------------------------------------------------------------------------------

    def register(self, manifest: TenantManifest) -> bool:
        """Store *manifest*, replacing the tenant's last one. True when it is new or changed."""
        changed = self.tenants.get(manifest.tenant) != manifest
        self.tenants[manifest.tenant] = manifest
        if changed:
            log.info("tenant_registered", tenant=manifest.tenant, kinds=[k.kind for k in manifest.kinds])
        return changed

    # --- requests ------------------------------------------------------------------------------------------------

    def request(self, req: LeaseRequest) -> LeaseRecord:
        now = self._clock()
        if req.resume:
            return self._resume(req, now)
        if req.request_id and (known := self._by_request.get((req.tenant, req.request_id))) in self.leases:
            rec = self.leases[known]
            rec.last_poll = now
            return rec

        rec = self._record(req, uuid.uuid4().hex, now)
        if req.request_id:
            self._by_request[(req.tenant, req.request_id)] = rec.lease_id
        if rec.deadline and rec.deadline <= now:
            self._end(rec, "refused", now, reason="deadline already passed")
        elif rec.not_before and rec.not_before > now:
            rec.reason = f"not before {rec.not_before.isoformat()}"
            self._changed(rec)
        else:
            self._grant(rec, now)
        return rec

    def _record(self, req: LeaseRequest, lease_id: str, now: datetime, **override: object) -> LeaseRecord:
        """A queued record for *req*, its unset fields filled from the tenant manifest's kind, and filed."""
        manifest = self.tenants.get(req.tenant)
        kind = manifest.kind_for(req.kind) if manifest else None
        fields: dict = {
            "lease_id": lease_id,
            "tenant": req.tenant,
            "caller": req.caller,
            "kind": req.kind,
            # A caller that names a kind but no target (the client's default) gets the kind's target.
            "target": kind.target if kind and req.target == IN_PROCESS else req.target,
            "request_id": req.request_id,
            "state": "queued",
            # The caller's figure wins; a caller that sends none gets its manifest's declared footprint.
            "vram_mb": req.vram_mb or (kind.vram_mb if kind else 0),
            "model_mb": req.model_mb if req.vram_mb else (kind.model_mb if kind else None),
            "priority": req.priority or (kind.priority if kind else "interactive"),
            "preemptible": req.preemptible if req.preemptible is not None else bool(kind and kind.preemptible),
            "ttl_s": req.ttl_s,
            "created_at": now,
            "not_before": req.not_before,
            "deadline": req.deadline,
            "last_poll": now,
            "wait_s": req.wait_s,
        }
        rec = LeaseRecord(**(fields | override))
        self.leases[lease_id] = rec
        self._constraints[lease_id] = _merge(kind.constraints if kind else GpuConstraints(), req.constraints)
        return rec

    def _resume(self, req: LeaseRequest, now: datetime) -> LeaseRecord:
        """Re-attach running work under its old id: granted at once, whatever the book says (DESIGN.md, API)."""
        assert req.resume
        rec = self.leases.get(req.resume)
        if rec is not None and rec.state == "granted":
            self._beat(rec, now)
            return rec
        rec = self._record(req, req.resume, now, resumed=True, not_before=None, deadline=None)
        self._grant(rec, now)
        self._beat(rec, now)  # the work is running: it has, in effect, just heartbeaten
        log.info("lease_resumed", lease=rec.lease_id, tenant=rec.tenant, kind=rec.kind, node=rec.node)
        return rec

    def _grant(self, rec: LeaseRecord, now: datetime) -> None:
        manifest = self.tenants.get(rec.tenant)
        others = [o for o in self.leases.values() if o is not rec]
        where = place(
            rec,
            self._constraints.get(rec.lease_id, GpuConstraints()),
            manifest.node if manifest else None,
            self.config,
            self._nodes(),
            others,
        )
        rec.state = "granted"
        rec.node, rec.device, rec.base_url = where.node, where.device, where.base_url
        rec.fits, rec.admissible_mb = where.fits, where.admissible_mb
        rec.reason = where.why or ("observe-only: granted without admission" if where.fits is False else None)
        rec.granted_at = now
        # Until the first heartbeat a grant lives only FIRST_BEAT_S (or half its TTL, if longer, so a client that
        # beats every ttl/3 is never reclaimed early).
        rec.expires_at = now + timedelta(seconds=min(rec.ttl_s, max(FIRST_BEAT_S, rec.ttl_s / 2)))
        log.info(
            "lease_granted",
            lease=rec.lease_id,
            tenant=rec.tenant,
            kind=rec.kind,
            target=rec.target,
            node=rec.node,
            vram_mb=rec.vram_mb,
            fits=rec.fits,
            reason=rec.reason,
        )
        self._changed(rec)

    # --- the life of a lease -------------------------------------------------------------------------------------

    def get(self, lease_id: str) -> LeaseRecord | None:
        rec = self.leases.get(lease_id)
        if rec is not None:
            rec.last_poll = self._clock()
        return rec

    def position(self, rec: LeaseRecord) -> int | None:
        if rec.state != "queued":
            return None
        ahead = 0
        for other in self.leases.values():  # arrival order
            if other is rec:
                return ahead
            ahead += other.state == "queued"
        return ahead

    def heartbeat(self, lease_id: str, hb: Heartbeat) -> HeartbeatReply | None:
        """``None`` (404) for an unknown or expired lease: the client then re-attaches with ``resume``."""
        rec = self.leases.get(lease_id)
        if rec is None or rec.state == "expired":
            return None
        now = self._clock()
        if hb.peak_vram_mb is not None:
            rec.peak_vram_mb = max(rec.peak_vram_mb or 0, hb.peak_vram_mb)
        if rec.state == "granted":
            self._beat(rec, now)
        return HeartbeatReply(state=rec.state, ttl_s=rec.ttl_s, reason=rec.reason if rec.state != "granted" else None)

    def _beat(self, rec: LeaseRecord, now: datetime) -> None:
        rec.heartbeats += 1
        rec.last_heartbeat = now
        rec.expires_at = now + timedelta(seconds=rec.ttl_s)

    def release(self, lease_id: str, rel: LeaseRelease) -> LeaseRecord | None:
        rec = self.leases.get(lease_id)
        if rec is None:
            return None
        if rel.peak_vram_mb is not None:
            rec.peak_vram_mb = max(rec.peak_vram_mb or 0, rel.peak_vram_mb)
        if rec.active:
            rec.outcome, rec.detail = rel.outcome, rel.detail
            reason = "cancelled while queued" if rec.state == "queued" else None
            self._end(rec, "released", self._clock(), reason=reason)
        return rec

    def tick(self) -> None:
        """Advance time: grant what may now start, expire, drop and prune. The broker calls this every second."""
        now = self._clock()
        for rec in list(self.leases.values()):
            if rec.state == "queued":
                if rec.deadline and rec.deadline <= now:
                    self._end(rec, "refused", now, reason="deadline passed while queued")
                elif rec.last_poll and (now - rec.last_poll).total_seconds() > max(UNPOLLED_MIN_S, 2 * rec.wait_s):
                    self._end(rec, "expired", now, reason="nobody polled the queued request")
                elif not rec.not_before or rec.not_before <= now:
                    self._grant(rec, now)
            elif rec.state == "granted" and rec.expires_at and rec.expires_at <= now:
                why = "no heartbeat since the grant" if rec.heartbeats == 0 else "no heartbeat within the TTL"
                self._end(rec, "expired", now, reason=why)
            elif not rec.active and rec.ended_at and (now - rec.ended_at).total_seconds() > RETAIN_S:
                self._forget(rec)

    def _end(self, rec: LeaseRecord, state: LeaseState, now: datetime, *, reason: str | None = None) -> None:
        rec.state, rec.ended_at, rec.expires_at = state, now, None
        if reason:
            rec.reason = reason
        how = rec.outcome if state == "released" and rec.outcome else state
        self.ended[(rec.tenant, rec.kind, how)] += 1
        held = (now - rec.granted_at).total_seconds() if rec.granted_at else None
        log.info(
            "lease_ended",
            lease=rec.lease_id,
            tenant=rec.tenant,
            kind=rec.kind,
            node=rec.node,
            state=state,
            outcome=rec.outcome,
            held_s=held,
            peak_vram_mb=rec.peak_vram_mb,
            reason=rec.reason,
        )
        self._changed(rec)

    def _forget(self, rec: LeaseRecord) -> None:
        self.leases.pop(rec.lease_id, None)
        self._constraints.pop(rec.lease_id, None)
        if rec.request_id:
            self._by_request.pop((rec.tenant, rec.request_id), None)

    # --- change notification -------------------------------------------------------------------------------------

    def _changed(self, rec: LeaseRecord) -> None:
        event = self._waiters.pop(rec.lease_id, None)
        if event is not None:
            event.set()
        if self._on_change is not None:
            self._on_change(rec)

    async def wait(self, lease_id: str, wait_s: float) -> LeaseRecord | None:
        """The lease, once its state changes or *wait_s* elapses (a long poll). ``None`` when unknown."""
        rec = self.get(lease_id)
        if rec is None or wait_s <= 0 or rec.state != "queued":
            return rec
        event = self._waiters.setdefault(lease_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), wait_s)
        return self.get(lease_id)

    # --- views ---------------------------------------------------------------------------------------------------

    def active(self, node: str | None = None) -> list[LeaseRecord]:
        return [r for r in self.leases.values() if r.active and (node is None or r.node == node)]


def _merge(base: GpuConstraints, over: GpuConstraints) -> GpuConstraints:
    """The manifest kind's constraints, with any the request sets taking precedence."""
    return base.model_copy(update=over.model_dump(exclude_none=True))
