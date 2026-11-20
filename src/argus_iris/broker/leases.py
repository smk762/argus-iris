"""Leases: placement, admission against VRAM, eviction plans, and the liveness rules of DESIGN.md.

The routes and their semantics are DESIGN.md's ("API (v1)", "Scheduling" > "Liveness"): ``request_id`` makes a
retried POST idempotent, ``resume`` re-attaches running work under the same id, a grant without a first heartbeat is
reclaimed, a granted lease expires ``ttl_s`` after its last heartbeat, and a queued request that nobody polls is
dropped.

**Stalls.** A heartbeat proves the client is alive, not that it is working: a keepalive loop that outlives its job
holds the reservation forever. So a granted lease with no GPU activity for ``lease_stall_s`` (its resident never busy
or recently used, no ``progress``/``stage``/``peak_vram_mb`` change; for a target with no resident, GPU utilisation
on the node) is marked stalled. While a queued lease waits on its node, its heartbeat replies set ``yield_requested``;
with ``lease_stall_reclaim`` it is ended ``refused`` once it has ignored that for ``lease_stall_s / 2``, so the
keepalive's ``resume`` cannot re-grant it.

**Placement** follows the target. A service target (``ollama:<model>``, ``comfyui``) goes to the services that serve
it: up before down, then the one serving fewest granted leases, then one where the model is already resident, then
config order (``ollama-dragon`` before ``ollama-jarrah``). An Ollama that lists its pulled models and lacks this one
is skipped, unless none has it. ``in-process`` goes to the constrained node, the tenant's own node, or any node that
meets ``min_cc`` and ``bf16``.

**Admission** has two modes (``admission`` in the broker config):

- ``observe`` (the default): every request is granted at once on the first candidate. Each grant records whether it
  would have fit (``fits``), so footprints and waits come from real leases before admission acts on them.
- ``enforce``: a lease is granted on the first candidate where it fits (DESIGN.md's inequality, see :func:`_fit`),
  so a full dragon spills to jarrah. Where none fits, iris plans an eviction on a candidate (idle residents, least
  recently used first, never busy, pinned, under a live lease, or of a higher class than the request) and grants once
  the agent sees the memory back; with nothing to evict the lease waits, queued. One that could never fit on any
  candidate is refused. The queue is served by class, with a wait bonus so nothing starves, and the head's
  reservation is kept: a lower lease is granted on that node only beside it.

**Eviction plans** avoid thrash and evict as little as they can. A resident of the request's own class may be
evicted (jarrah's "one model fills the card, keep it moving" needs it), but only once it has been idle, and loaded,
for ``admission_same_class_idle_s`` (300 s; lower classes need only ``admission_min_idle_s``), so two tenants of one
class cannot ping-pong a card. Lower classes go first, then the longest idle; victims the shortfall does not need are
dropped; and across candidate nodes the cheapest plan wins (fewest same-class victims, then fewest MB evicted).
Within a class the queue serves the tenant holding fewest grants first.

:class:`LeaseBook` is synchronous and takes its clock, node view and an eviction callback as arguments, so it can be
tested without a server; the broker runs the evictions and the HTTP layer adds long-polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from argus_cortex.gpu_wire import (
    IN_PROCESS,
    PRIORITY_CLASSES,
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
from argus_iris.broker.policy import Policy
from argus_iris.models import NodeState, Resident, ResidentSource, ServiceState, utcnow

log = structlog.get_logger("argus_iris.leases")

FIRST_BEAT_S = 30.0  # a grant with no heartbeat by then is reclaimed (DESIGN.md, Liveness) ...
UNPOLLED_MIN_S = 30.0  # ... and a queued request nobody has polled for 2 x wait_s (at least this) is dropped
RETAIN_S = 600.0  # finished leases stay readable (and idempotent) this long
ACTIVE: frozenset[str] = frozenset({"queued", "granted"})
AGING_S = 300.0  # a queued lease gains one priority class per this long waited, so nothing starves
EVICT_RETRY_S = 60.0  # a resident whose eviction was not confirmed is left alone this long
YIELD_REASON = "stalled: no GPU activity under the lease while another waits for the node"
RECENT_USE_S = 60.0  # a lease's resident idle less than this counts as GPU activity under the lease
_FREED_STATES = frozenset({"unloaded", "cpu"})


def _rank(priority: str) -> int:
    """0 for realtime ... 3 for background."""
    return PRIORITY_CLASSES.index(priority)


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
    window: str | None = None  # the policy window that set its class, if one was open
    fits: bool | None = None  # would admission have granted it now? None: no ledger for the node
    admissible_mb: int | None = None  # what the node had to give, by the ledger, when it was granted
    resumed: bool = False
    heartbeats: int = 0
    peak_vram_mb: int | None = None
    progress: float | None = (
        None  # 0-1, from the caller's heartbeat (cortex lease_version 1.1); a change counts as activity
    )
    stage: str | None = None  # what the work says it is doing now
    active_at: datetime | None = None  # the last sign of GPU work under a granted lease (see LeaseBook._watch)
    stalled_at: datetime | None = None  # when it was marked stalled: heartbeating, but no GPU activity
    yield_requested: bool = False  # a stalled lease blocks a queued one: its heartbeats ask it to give the GPU back
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
class Candidate:
    """A place a lease could run: a node, and for a service target the service and the URL to call there."""

    node: str
    device: int
    base_url: str | None = None
    service: str | None = None


def _model_key(name: str | None) -> str | None:
    """A model as Ollama resolves it, for comparison only: ``:latest`` implied, and case-insensitive (Ollama serves
    ``Qwen2.5:7b`` to a request for ``qwen2.5:7b``)."""
    if not name:
        return None
    name = name.lower()
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
    node: str, state: NodeState | None, rec: LeaseRecord, granted: list[LeaseRecord], reserved: int = 0
) -> tuple[bool | None, int | None]:
    """Would *rec* fit on *node* beside the leases granted there and *reserved* (the queue head's claim)?

    The ledger's ``admissible_mb`` already holds DESIGN.md's inequality for residents (managed residents + reserve +
    headroom <= total, and NVML free); granted leases add what is not yet resident. ``(None, None)`` with no ledger.
    """
    if state is None or state.ledger is None:
        return None, None
    held = sum(_new_mb(o, state) for o in granted if o.node == node and o is not rec)
    admissible = max(0, state.ledger.admissible_mb - held - reserved)
    return _new_mb(rec, state) <= admissible, admissible


def _capacity(state: NodeState | None, config: BrokerConfig, node: str) -> int | None:
    """The most a lease could ever get on *node*: the card less reserve and headroom. None when unknown."""
    if state is None or state.ledger is None:
        return None
    cfg = config.nodes[node]
    return state.ledger.total_mb - cfg.reserve_mb - cfg.headroom_mb


def candidates(
    rec: LeaseRecord,
    constraints: GpuConstraints,
    home: str | None,
    config: BrokerConfig,
    nodes: dict[str, NodeState],
    load: Mapping[str, int] | None = None,
) -> tuple[list[Candidate], str | None]:
    """Where *rec* could run, best first, or ``([], why)`` when nowhere.

    *load* counts the granted leases on each service: of two that are up and have the model, the one serving fewer
    goes first, ahead of the one that has it loaded already (a load costs seconds, waiting behind a job far more).
    """
    wanted = constraints.node
    if rec.target == IN_PROCESS:
        names = [wanted] if wanted else ([home] if home else list(config.nodes))
        names = [n for n in names if n in config.nodes and _meets(nodes.get(n), constraints)]
        if not names:
            return [], f"no node meets {constraints.model_dump(exclude_none=True) or 'the request'}"
        names.sort(key=lambda n: not ((s := nodes.get(n)) and s.reachable))  # stable: reachable first
        return [Candidate(n, config.nodes[n].gpu) for n in names], None

    name = target_service(rec.target)
    services = [s for s in config.services if name in (s.id, s.adapter)]
    services = [s for s in services if (not wanted or s.node == wanted) and _meets(nodes.get(s.node), constraints)]
    if not services:
        return [], f"no configured service serves {rec.target}" + (f" on {wanted}" if wanted else "")
    model = _model_key(target_model(rec.target))
    load = load or {}

    def state_of(svc) -> ServiceState | None:
        node = nodes.get(svc.node)
        return next((s for s in node.services if s.id == svc.id), None) if node else None

    def lacks(svc) -> bool:
        """The service says which models it has pulled, and this is not one of them."""
        st = state_of(svc)
        return bool(model and st and st.available is not None and model not in {_model_key(m) for m in st.available})

    if model and not all(lacks(s) for s in services):  # none has it: leave them all, the caller may mean to pull
        services = [s for s in services if not lacks(s)]

    def order(svc) -> tuple[bool, int, bool]:
        state, st = nodes.get(svc.node), state_of(svc)
        warm = bool(
            model and state and any(r.service == svc.id and _model_key(r.model) == model for r in state.residents)
        )
        return (not (st and st.up), load.get(svc.id, 0), not warm)  # stable sort keeps config order within a group

    services.sort(key=order)
    return [Candidate(s.node, config.nodes[s.node].gpu, s.base_url, s.id) for s in services], None


class LeaseConflict(ValueError):
    """The request names a lease that is not the caller's (a caller error, not "busy")."""


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
        evict: Callable[[str, list[str]], None] | None = None,
        pinned: Callable[[Resident], bool] | None = None,
        policy: Policy | None = None,
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
        # Admission. *evict* is asked to evict residents for a lease and must call evictions_done when finished.
        self._evict = evict
        self._evicting: dict[str, set[str]] = {}  # lease id -> residents being evicted for it
        self._evict_failed: dict[str, datetime] = {}  # resident id -> when its eviction was last not confirmed
        self._last_class: dict[tuple[str, str], str] = {}  # (node, model or service) -> class of its last lease
        self._last_grant: dict[tuple[str, str], datetime] = {}  # (node, model or service) -> its last grant
        self.evictions: Counter[tuple[str, str]] = Counter()  # (node, "confirmed" | "failed") -> count
        self._pinned = pinned or (lambda r: False)  # the policy's keepalive pins (rollout step 4)
        self.policy = policy or Policy()
        self._last_used: dict[tuple[str, str], datetime] = {}  # (node, model or service) -> when its last lease ended
        self._services: dict[str, str | None] = {}  # lease id -> the service it was granted on
        self._reclaimed: dict[str, datetime] = {}  # lease id -> when a stall reclaimed it (resume must not re-grant)
        self._yield_since: dict[str, datetime] = {}  # lease id -> when it was first asked to yield (the reclaim clock)
        self._polling: Counter[str] = Counter()  # lease id -> long polls held open on it now (it is being polled)

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
            if req.resume in self._reclaimed:
                return self._refuse_reclaimed(req, now)
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
            self._serve_queue(now)
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
        constraints = _merge(kind.constraints if kind else GpuConstraints(), req.constraints)
        # An open policy window sets the tenant's class (DESIGN.md, "windows"), whatever the caller asked for.
        window = self.policy.window_class(
            req.tenant, fields["target"], constraints.node or (manifest.node if manifest else None), now
        )
        if window:
            fields["window"], fields["priority"] = window
        rec = LeaseRecord(**(fields | override))
        self.leases[lease_id] = rec
        self._constraints[lease_id] = constraints
        return rec

    def _refuse_reclaimed(self, req: LeaseRequest, now: datetime) -> LeaseRecord:
        """A stalled lease that was reclaimed stays ended: its keepalive's resume gets the refusal back."""
        rec = self.leases.get(req.resume or "")
        if rec is not None and rec.tenant != req.tenant:
            raise LeaseConflict(f"lease {req.resume!r} belongs to another tenant")
        if rec is None:  # forgotten since: answer with a fresh refused record under the old id
            rec = self._record(req, req.resume or "", now, resumed=True)
            self._end(rec, "refused", now, reason="reclaimed: stalled with no GPU activity")
        return rec

    def _resume(self, req: LeaseRequest, now: datetime) -> LeaseRecord:
        """Re-attach running work under its old id: granted at once, whatever the book says (DESIGN.md, API)."""
        assert req.resume
        rec = self.leases.get(req.resume)
        if rec is not None and rec.tenant != req.tenant:
            # Re-attaching would keep another tenant's grant alive; re-recording would replace its lease.
            raise LeaseConflict(f"lease {req.resume!r} belongs to another tenant")
        if rec is not None and rec.state == "granted":
            self._beat(rec, now)
            return rec
        rec = self._record(req, req.resume, now, resumed=True, not_before=None, deadline=None)
        cands, why = self._candidates(rec)  # the work is already running: account for it where it is likeliest
        self._grant(rec, now, cands[0] if cands else None, why=why)
        self._beat(rec, now)  # the work is running: it has, in effect, just heartbeaten
        log.info("lease_resumed", lease=rec.lease_id, tenant=rec.tenant, kind=rec.kind, node=rec.node)
        return rec

    def _candidates(self, rec: LeaseRecord) -> tuple[list[Candidate], str | None]:
        manifest = self.tenants.get(rec.tenant)
        return candidates(
            rec,
            self._constraints.get(rec.lease_id, GpuConstraints()),
            manifest.node if manifest else None,
            self.config,
            self._nodes(),
            Counter(self._services.get(o.lease_id) for o in self._granted()),
        )

    def _granted(self) -> list[LeaseRecord]:
        return [o for o in self.leases.values() if o.state == "granted"]

    def _grant(self, rec: LeaseRecord, now: datetime, where: Candidate | None, *, why: str | None = None) -> None:
        state = self._nodes().get(where.node) if where else None
        fits, admissible = _fit(where.node, state, rec, self._granted()) if where else (None, None)
        rec.state = "granted"
        rec.node = where.node if where else None
        rec.device = where.device if where else None
        rec.base_url = where.base_url if where else None
        rec.fits, rec.admissible_mb = fits, admissible
        if why is None and fits is False:
            why = "observe-only: granted without admission" if self.config.admission == "observe" else None
        rec.reason = why
        rec.granted_at = rec.active_at = now
        # Until the first heartbeat a grant lives only FIRST_BEAT_S (or half its TTL, if longer, so a client that
        # beats every ttl/3 is never reclaimed early).
        rec.expires_at = now + timedelta(seconds=min(rec.ttl_s, max(FIRST_BEAT_S, rec.ttl_s / 2)))
        if where:
            key = (where.node, _model_key(target_model(rec.target)) or where.service or "")
            self._last_class[key], self._last_grant[key] = rec.priority, now
            self._services[rec.lease_id] = where.service
        log.info(
            "lease_granted",
            lease=rec.lease_id,
            tenant=rec.tenant,
            kind=rec.kind,
            target=rec.target,
            priority=rec.priority,
            node=rec.node,
            vram_mb=rec.vram_mb,
            fits=rec.fits,
            waited_s=(now - rec.created_at).total_seconds(),
            reason=rec.reason,
        )
        self._changed(rec)

    # --- admission -----------------------------------------------------------------------------------------------

    def _queue(self, now: datetime) -> list[LeaseRecord]:
        """Queued leases that may start now, in the order they are served.

        By class, raised one class per ``AGING_S`` waited so nothing starves; within a class, the tenant holding the
        fewest grants first, so one busy tenant cannot hog it; then arrival.
        """
        holding = Counter(o.tenant for o in self.leases.values() if o.state == "granted")
        ready = [r for r in self.leases.values() if r.state == "queued" and (not r.not_before or r.not_before <= now)]
        ready.sort(
            key=lambda r: (
                _rank(r.priority) - int((now - r.created_at).total_seconds() // AGING_S),
                holding[r.tenant],
                r.created_at,
            )
        )
        return ready

    def _serve_queue(self, now: datetime) -> None:
        reserved: Counter[str] = Counter()  # node -> MB claimed by queued leases ahead
        for rec in self._queue(now):
            if rec.lease_id in self._evicting:
                if rec.node:
                    reserved[rec.node] += rec.vram_mb
                continue
            self._admit(rec, now, reserved)

    def _admit(self, rec: LeaseRecord, now: datetime, reserved: Counter[str]) -> None:
        cands, why = self._candidates(rec)
        if not cands:
            self._grant(rec, now, None, why=why)  # nothing iris manages: the caller uses its own URL
            return
        if self.config.admission == "observe":
            self._grant(rec, now, cands[0])
            return

        nodes, granted = self._nodes(), self._granted()
        for c in cands:
            fits, _ = _fit(c.node, nodes.get(c.node), rec, granted, reserved[c.node])
            if fits is not False:  # fits, or no ledger to say otherwise (agent down): fail open
                self._grant(rec, now, c)
                return

        caps = [_capacity(nodes.get(c.node), self.config, c.node) for c in cands]
        if all(cap is not None for cap in caps) and all(
            _new_mb(rec, nodes[c.node]) > cap for c, cap in zip(cands, caps, strict=True)
        ):
            self._end(
                rec, "refused", now, reason=f"needs {rec.vram_mb} MB; the most any candidate offers is {max(caps)} MB"
            )
            return

        plans = []  # (cost, candidate order, candidate, victims, short)
        for i, c in enumerate(cands):
            state = nodes[c.node]
            _, admissible = _fit(c.node, state, rec, granted, reserved[c.node])
            short = _new_mb(rec, state) - (admissible or 0)
            victims = self._plan(rec, c, state, short, granted, now)
            if victims:
                same = sum(1 for v in victims if self._class_of(v) == rec.priority)
                plans.append(((same, sum(v.vram_mb for v in victims)), i, c, victims, short))
        if plans and self._evict is not None:  # no evictor: nothing would ever finish the plan, so wait instead
            _, _, c, victims, short = min(plans, key=lambda p: (p[0], p[1]))
            ids = [v.id for v in victims]
            self._evicting[rec.lease_id] = set(ids)
            rec.node = c.node
            self._say(rec, f"evicting {', '.join(v.id.split('/', 1)[1] for v in victims)} on {c.node} for {short} MB")
            reserved[c.node] += rec.vram_mb
            log.info("admission_evicting", lease=rec.lease_id, node=c.node, short_mb=short, residents=ids)
            self._evict(rec.lease_id, ids)
            return

        def still_short(c: Candidate) -> int:
            """What the node would still lack once the leases granted on it are released: wait where that frees it."""
            state = nodes[c.node]
            _, admissible = _fit(c.node, state, rec, granted, reserved[c.node])
            held = sum(_new_mb(o, state) for o in granted if o.node == c.node)
            return max(0, _new_mb(rec, state) - (admissible or 0) - held)

        first = min(cands, key=still_short)  # ties keep placement order
        _, admissible = _fit(first.node, nodes.get(first.node), rec, granted, reserved[first.node])
        rec.node = first.node
        reserved[first.node] += rec.vram_mb  # the head's reservation: nothing lower takes this node's memory
        self._say(
            rec,
            f"waiting for {_new_mb(rec, nodes[first.node]) - (admissible or 0)} MB on {first.node}: "
            + ("nothing idle to evict" if self._evict is not None else "eviction is off"),
        )

    def _say(self, rec: LeaseRecord, reason: str) -> None:
        if rec.reason != reason:
            rec.reason = reason
            self._changed(rec)

    def _class_of(self, r: Resident) -> str:
        """A resident takes the class of the last lease that used it; background if none has."""
        return self._last_class.get((r.node, _model_key(r.model) or r.service), "background")

    def _stoppable(self, r: Resident) -> bool:
        """An external resident admission may stop: ``evict_by: stop``, and a policy rule matching it enforced. A
        stop takes the whole service away (its clients fall back), so it is opted into per resident, observe first."""
        svc = next((s for s in self.config.services if s.id == r.service and s.node == r.node), None)
        rule = self.policy.rule_for(r)
        return bool(svc and svc.evict_by == "stop" and rule and rule[1].enforce)

    def _plan(
        self,
        rec: LeaseRecord,
        where: Candidate,
        state: NodeState,
        short: int,
        granted: list[LeaseRecord],
        now: datetime,
    ) -> list[Resident] | None:
        """The fewest residents on *where* whose eviction frees *short* MB, or None if no set can.

        Eligible: freeable (through an adapter, or an external service iris may stop: :meth:`_stoppable`), idle long
        enough, and not busy, pinned, under a live lease, being evicted, recently failed, already unloaded, the model
        asked for, or of a higher class than the request. A same-class resident must also have
        been idle, and loaded, for ``admission_same_class_idle_s``. Lower classes are taken first, then the longest
        idle; then any victim the rest already cover is dropped.
        """
        busy = set().union(*self._evicting.values()) if self._evicting else set()
        here = [o for o in granted if o.node == where.node]
        in_use_models = {_model_key(target_model(o.target)) for o in here} - {None}
        in_use_services = {
            s.id
            for o in here
            for s in self.config.services_on(where.node)
            if target_service(o.target) in (s.id, s.adapter)
        }
        wanted = _model_key(target_model(rec.target))
        mine = _rank(rec.priority)
        eligible: list[Resident] = []
        for r in state.residents:
            key = _model_key(r.model)
            failed = self._evict_failed.get(r.id)
            theirs = _rank(self._class_of(r))
            if (
                r.source == ResidentSource.PROCESS  # no adapter to evict it
                or (r.source == ResidentSource.EXTERNAL and not self._stoppable(r))
                or r.busy
                or r.idle_s is None
                or r.idle_s < self.config.admission_min_idle_s
                or r.id in busy
                or (failed and (now - failed).total_seconds() < EVICT_RETRY_S)
                or r.detail.get("model_state") in _FREED_STATES  # holds only a context: evicting frees nothing
                or any(fnmatch.fnmatchcase(r.id, pat) for pat in self.config.pinned)
                or self._pinned(r)
                or (key is not None and (key == wanted or key in in_use_models))
                or (key is None and r.service in in_use_services)
                or theirs < mine  # a higher class than the request
            ):
                continue
            if theirs == mine:  # same class: only once it has settled, so two tenants cannot ping-pong the card
                loaded = self._last_grant.get((r.node, key or r.service))
                settle = self.config.admission_same_class_idle_s
                if r.idle_s < settle or (loaded and (now - loaded).total_seconds() < settle):
                    continue
            eligible.append(r)

        eligible.sort(key=lambda r: (-_rank(self._class_of(r)), -(r.idle_s or 0)))  # lowest class, then idlest
        victims: list[Resident] = []
        freed = 0
        for r in eligible:
            victims.append(r)
            freed += r.vram_mb
            if freed >= short:
                break
        else:
            return None
        for r in sorted(victims, key=lambda r: r.vram_mb):  # drop what the others already cover, smallest first
            if freed - r.vram_mb >= short:
                victims.remove(r)
                freed -= r.vram_mb
        return victims

    def evictions_done(self, lease_id: str, failed: list[str]) -> None:
        """The broker finished evicting for *lease_id*; *failed* were not confirmed. Serve the queue again."""
        now = self._clock()
        for rid in self._evicting.pop(lease_id, set()):
            outcome = "failed" if rid in failed else "confirmed"
            self.evictions[(rid.split("/", 1)[0], outcome)] += 1
            if rid in failed:
                self._evict_failed[rid] = now
        self._serve_queue(now)

    # --- the life of a lease -------------------------------------------------------------------------------------

    def get(self, lease_id: str) -> LeaseRecord | None:
        rec = self.leases.get(lease_id)
        if rec is not None:
            rec.last_poll = self._clock()
        return rec

    def position(self, rec: LeaseRecord) -> int | None:
        if rec.state != "queued":
            return None
        now = self._clock()
        ready = self._queue(now)
        # Leases whose not_before is still ahead come after everything that may start now, soonest first.
        later = sorted(
            (r for r in self.leases.values() if r.state == "queued" and r not in ready),
            key=lambda r: (r.not_before or now, r.created_at),
        )
        return (ready + later).index(rec)

    def heartbeat(self, lease_id: str, hb: Heartbeat) -> HeartbeatReply | None:
        """``None`` (404) for an unknown or expired lease: the client then re-attaches with ``resume``."""
        rec = self.leases.get(lease_id)
        if rec is None or rec.state == "expired":
            return None
        now = self._clock()
        shown = (rec.peak_vram_mb, rec.progress, rec.stage)
        if hb.peak_vram_mb is not None:
            rec.peak_vram_mb = max(rec.peak_vram_mb or 0, hb.peak_vram_mb)
        # getattr: cortex before lease_version 1.1 has no progress on Heartbeat.
        if (progress := getattr(hb, "progress", None)) is not None:
            rec.progress = progress
        if (stage := getattr(hb, "stage", None)) is not None:
            rec.stage = stage
        if rec.state == "granted":
            self._beat(rec, now)
        if (rec.peak_vram_mb, rec.progress, rec.stage) != shown:
            if rec.state == "granted":
                self._active(rec, now)  # the work reported something new: it is working
            if self._on_change is not None:
                self._on_change(rec)  # the dashboard only; not _changed, which would wake a long poll for nothing
        return HeartbeatReply(
            state=rec.state,
            ttl_s=rec.ttl_s,
            yield_requested=rec.yield_requested,
            reason=YIELD_REASON
            if rec.yield_requested and rec.state == "granted"
            else (rec.reason if rec.state != "granted" else None),
        )

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
            self._serve_queue(self._clock())
        return rec

    def tick(self) -> None:
        """Advance time: grant what may now start, expire, drop and prune. The broker calls this every second."""
        now = self._clock()
        for rec in list(self.leases.values()):
            if rec.state == "queued":
                if rec.deadline and rec.deadline <= now:
                    self._end(rec, "refused", now, reason="deadline passed while queued")
                elif (
                    not self._polling[rec.lease_id]  # a long poll held open longer than the limit is still a poll
                    and rec.last_poll
                    and (now - rec.last_poll).total_seconds() > max(UNPOLLED_MIN_S, 2 * rec.wait_s)
                ):
                    self._end(rec, "expired", now, reason="nobody polled the queued request")
            elif rec.state == "granted" and rec.expires_at and rec.expires_at <= now:
                why = "no heartbeat since the grant" if rec.heartbeats == 0 else "no heartbeat within the TTL"
                self._end(rec, "expired", now, reason=why)
            elif rec.state == "granted":
                self._watch(rec, now)
            elif not rec.active and rec.ended_at and (now - rec.ended_at).total_seconds() > RETAIN_S:
                self._forget(rec)
        for lid, when in list(self._reclaimed.items()):
            if (now - when).total_seconds() > 24 * 3600:
                del self._reclaimed[lid]
        self._serve_queue(now)

    # --- stalls --------------------------------------------------------------------------------------------------

    def _working(self, rec: LeaseRecord, state: NodeState | None) -> bool:
        """Does the node show GPU work under *rec* now? Its resident busy or just used; with none, any GPU load."""
        if state is None or not state.reachable:
            return True  # no view: never call a lease stalled on missing data
        key = self._use_key(rec)
        mine = [r for r in state.residents if self._resident_key(r) == key]
        if mine:
            return any(r.busy or (r.idle_s is not None and r.idle_s < RECENT_USE_S) for r in mine)
        return bool(state.gpu and state.gpu.util_pct)

    def _active(self, rec: LeaseRecord, now: datetime) -> None:
        rec.active_at = now
        if rec.stalled_at is not None:
            rec.stalled_at, rec.yield_requested = None, False
            self._yield_since.pop(rec.lease_id, None)
            log.info("lease_unstalled", lease=rec.lease_id, tenant=rec.tenant, kind=rec.kind, node=rec.node)
            self._changed(rec)

    def _blocking(self, rec: LeaseRecord) -> bool:
        return any(o.state == "queued" and o.node == rec.node for o in self.leases.values())

    def _watch(self, rec: LeaseRecord, now: datetime) -> None:
        """Mark a granted lease stalled after ``lease_stall_s`` without GPU activity; ask it to yield, or reclaim it."""
        limit = self.config.lease_stall_s
        if limit is None or rec.node is None:
            return
        if self._working(rec, self._nodes().get(rec.node)):
            self._active(rec, now)
            return
        quiet = (now - (rec.active_at or rec.granted_at or now)).total_seconds()
        if quiet < limit:
            return
        if rec.stalled_at is None:
            rec.stalled_at = now
            log.warning(
                "lease_stalled", lease=rec.lease_id, tenant=rec.tenant, kind=rec.kind, node=rec.node, quiet_s=quiet
            )
            self._changed(rec)
        blocking = self._blocking(rec)
        if blocking != rec.yield_requested:
            rec.yield_requested = blocking
            if blocking:
                self._yield_since[rec.lease_id] = now
                log.info("lease_yield_requested", lease=rec.lease_id, tenant=rec.tenant, node=rec.node)
            else:
                self._yield_since.pop(rec.lease_id, None)
            self._changed(rec)
        # The reclaim clock runs from the yield request, not the stall: a lease stalled long before anyone queued
        # still gets lease_stall_s / 2 of heartbeat replies asking it to yield before it is taken back.
        asked = self._yield_since.get(rec.lease_id)
        if blocking and self.config.lease_stall_reclaim and asked and (now - asked).total_seconds() >= limit / 2:
            self._reclaimed[rec.lease_id] = now
            self._end(
                rec, "refused", now, reason=f"reclaimed: no GPU activity for {int(quiet // 60)} min", how="reclaimed"
            )

    def _end(
        self, rec: LeaseRecord, state: LeaseState, now: datetime, *, reason: str | None = None, how: str | None = None
    ) -> None:
        if rec.state == "granted" and rec.node:
            self._last_used[(rec.node, self._use_key(rec))] = now
        rec.state, rec.ended_at, rec.expires_at = state, now, None
        self._yield_since.pop(rec.lease_id, None)
        if reason:
            rec.reason = reason
        how = how or (rec.outcome if state == "released" and rec.outcome else state)
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
        self._services.pop(rec.lease_id, None)
        self._evicting.pop(rec.lease_id, None)
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
        self._polling[lease_id] += 1
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), wait_s)
        finally:
            self._polling[lease_id] -= 1
            if self._polling[lease_id] <= 0:
                del self._polling[lease_id]
        return self.get(lease_id)

    # --- views ---------------------------------------------------------------------------------------------------

    def _use_key(self, rec: LeaseRecord) -> str:
        return _model_key(target_model(rec.target)) or self._services.get(rec.lease_id) or target_service(rec.target)

    @staticmethod
    def _resident_key(r: Resident) -> str:
        return _model_key(r.model) or r.service

    def in_use(self, r: Resident) -> bool:
        """A granted lease is using *r* now (its model, or its service for a target with no model)."""
        key = self._resident_key(r)
        return any(o.state == "granted" and o.node == r.node and self._use_key(o) == key for o in self.leases.values())

    def last_used(self, r: Resident) -> datetime | None:
        """When the last lease on *r* ended, if any did since the broker started."""
        return self._last_used.get((r.node, self._resident_key(r)))

    def active(self, node: str | None = None) -> list[LeaseRecord]:
        return [r for r in self.leases.values() if r.active and (node is None or r.node == node)]


def _merge(base: GpuConstraints, over: GpuConstraints) -> GpuConstraints:
    """The manifest kind's constraints, with any the request sets taking precedence."""
    return base.model_copy(update=over.model_dump(exclude_none=True))
