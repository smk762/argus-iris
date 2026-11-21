"""Residency (rollout step 4): idle-kill, keepalive and restart of residents, by the operator's policy.

Every poll, each resident a policy rule matches gets a :class:`~argus_iris.models.ResidentPolicy`: whether it is
pinned, how long it has been idle as the policy counts it, when idle-kill is due, and what iris does next. Every
``residency_s`` the broker acts on those whose rule says ``enforce: true``; the rest are reported only, so residents
are handed over one at a time (#6).

- **Idle** is the shorter of the time since the resident's last lease ended and the service's (or agent's) own idle
  time; a resident under a granted lease, or busy, is not idle.
- **Pinned** (``keepalive: always`` or an open keepalive window): never evicted, by idle-kill or admission. An Ollama
  model is held at ``keep_alive: -1`` so neither Ollama's own timer nor a request's ``keep_alive`` drops it.
- **Idle-kill** (``idle_kill``): once due, the resident is evicted through its adapter (to the CPU where the service
  can). Until then an Ollama model's ``keep_alive`` is extended before it runs out, so with ``OLLAMA_KEEP_ALIVE=server``
  iris, not Ollama's default, decides when it goes.
- **Unload after** (``unload_after``): a resident idle-kill moved to the CPU is unloaded fully once idle this much
  longer, freeing host RAM as well (the sidecars' own CPU timers stop running when their GPU timer is -1).
- **Restart** (``restart_residual_mb``): a resident that is unloaded (not merely on the CPU, where a restart would
  throw away the warm copy) but still holds at least this much VRAM (a CUDA
  context, an allocator cache: modernbert's 94 MB, Miso's 2 GB) and is due is restarted through its node's agent,
  which only restarts containers on its own allowlist.
- **Host RAM** (``restart_host_ram_mb``, else the tenant manifest's ``host_ram_restart_mb``): a resident whose
  container's anonymous memory is above it, and which has been idle ``restart_host_ram_idle`` (120 s), is restarted
  the same way. ComfyUI keeps offloaded weights in host RAM and only a restart returns them (voluptas#16). A restart
  throws away whatever the service has loaded, so never while pinned, busy or under a lease.
- **Restore** (#35): a pin only stops iris evicting a resident; anything else can still drop it (an unload by hand,
  a restart, an OOM, Ollama loading another model over it). iris remembers each pinned resident it has seen loaded
  (its VRAM, and an Ollama model's context), and once it has been gone ``RESTORE_AFTER_POLLS`` polls warms it again:
  Ollama with a promptless ``keep_alive: -1`` at the context it had, argus-http through ``warm_path``. Only while
  the rule is enforced (and not ``restore: false``), no lease is active or queued on the node, and the node can
  admit what it held; a failed restore, or a model gone again within ``RESTORE_STAY_S``, doubles the wait.
- **External services** (``adapter: external``): iris cannot free one unless it has ``evict_by: stop``; then
  idle-kill stops its container, and so may admission when a lease needs the room, both only while the matching rule
  is enforced (a rule with nothing but ``enforce: true`` opts it into admission alone). One admission stopped is
  warmed again (the agent starts it) once no lease is active on its node and the node can admit it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from argus_iris.broker.config import ServiceConfig
from argus_iris.broker.policy import Policy, ResidentRule
from argus_iris.models import NodeState, Resident, ResidentPolicy, ResidentSource, utcnow

if TYPE_CHECKING:
    from argus_iris.broker.state import Broker

log = structlog.get_logger("argus_iris.residency")

REFRESH_MARGIN_S = 120.0  # extend an Ollama keep_alive when it has less than this left
PIN_HORIZON = timedelta(days=30)  # an Ollama expiry further off than this is a keep_alive of -1
RESIDUAL_SLACK_MB = 64  # a resident within this of what its last eviction left is still evicted
COOLDOWN_S = 60.0  # after acting on a resident, leave it this long (a failed evict is not retried every step)
_FREED = frozenset({"unloaded", "cpu"})
RESTORE_AFTER_POLLS = 2  # a pinned resident gone this many polls in a row is restored
RESTORE_STAY_S = 600.0  # gone again within this of a restore counts as a failed restore
RESTORE_BACKOFF_MAX_S = 1800.0


@dataclass
class PinMemory:
    """A pinned resident as iris last saw it loaded, kept so it can be restored once it is gone (#35)."""

    resident: Resident
    missed: int = 0  # polls in a row it has been gone (or, argus-http, unloaded)
    restored_at: datetime | None = None
    fails: int = 0
    next_try: datetime | None = None
    note: str | None = None  # why it is not being restored yet


def on_gpu(r: Resident) -> bool:
    """The service says its model holds VRAM now (cortex's ``ResidentHealth.on_gpu``). False when it says nothing."""
    return r.detail.get("model_state") == "gpu"


class Residency:
    def __init__(self, broker: Broker, policy: Policy, *, clock=utcnow) -> None:
        self.broker = broker
        self.policy = policy
        self._clock = clock
        self._cooldown: dict[str, datetime] = {}
        self.pins: dict[str, PinMemory] = {}  # resident id -> the pinned resident as last seen loaded
        self.actions: Counter[tuple[str, str, str]] = Counter()  # (node, action, ok | failed) -> count

    # --- reading -------------------------------------------------------------------------------------------------

    def pinned(self, r: Resident) -> bool:
        """Kept by the policy now. Enforced or not: a pin only ever protects, so admission honours it either way."""
        match = self.policy.rule_for(r)
        return bool(match and self.policy.kept(match[1], self._clock()))

    def evicted(self, r: Resident) -> bool:
        """Already evicted: holding no more than its last eviction left behind, and not saying it has a model on the
        GPU. The service's own word wins: a residual recorded while a request was loading the model would otherwise
        hide a loaded model from idle-kill and admission for good."""
        left = self.broker.residuals.get(r.id)
        return left is not None and r.vram_mb <= left + RESIDUAL_SLACK_MB and not on_gpu(r)

    def protected(self, r: Resident) -> bool:
        """Admission must not plan to evict *r*: pinned, or already evicted (evicting it again frees nothing)."""
        return self.pinned(r) or self.evicted(r)

    def _idle(self, r: Resident, now: datetime) -> float | None:
        if r.busy or self.broker.leases.in_use(r):
            return 0.0
        seen = [r.idle_s] if r.idle_s is not None else []
        if (last := self.broker.leases.last_used(r)) is not None:
            seen.append(max(0.0, (now - last).total_seconds()))
        return min(seen) if seen else None

    def _service(self, r: Resident) -> ServiceConfig | None:
        return next((s for s in self.broker.config.services if s.id == r.service and s.node == r.node), None)

    def host_ram_limit(self, r: Resident, rule: ResidentRule) -> int | None:
        """The rule's ``restart_host_ram_mb``, else a registered manifest's hint for the resident's container."""
        if rule.restart_host_ram_mb is not None:
            return rule.restart_host_ram_mb
        svc = self._service(r)
        if svc is None or not svc.container:
            return None
        for manifest in self.broker.leases.tenants.values():
            if manifest.node and manifest.node != r.node:
                continue
            for tr in manifest.residents:
                if tr.container == svc.container and tr.host_ram_restart_mb:
                    return tr.host_ram_restart_mb
        return None

    def view(self, r: Resident, now: datetime) -> ResidentPolicy | None:
        match = self.policy.rule_for(r)
        if match is None:
            return None
        index, rule = match
        pinned = self.policy.kept(rule, now)
        idle = self._idle(r, now)
        due = None if rule.idle_kill is None or idle is None else max(0.0, rule.idle_kill - idle)
        nxt, note = self._decide(r, rule, pinned, due, idle, now)
        return ResidentPolicy(
            rule=index,
            enforce=rule.enforce,
            pinned=pinned,
            idle_s=idle,
            idle_kill_s=rule.idle_kill,
            due_in_s=due,
            next=nxt,
            note=note,
        )

    def _decide(
        self, r: Resident, rule: ResidentRule, pinned: bool, due: float | None, idle: float | None, now: datetime
    ) -> tuple[str | None, str | None]:
        ram_note = None
        limit = None if pinned else self.host_ram_limit(r, rule)
        svc = self._service(r)
        if limit is not None and r.host_ram_mb is not None and r.host_ram_mb > limit and svc and svc.container:
            held = f"holding {r.host_ram_mb} MB of host RAM (over {limit})"
            if idle is not None and idle >= rule.restart_host_ram_idle:
                return "restart", f"idle and {held}: restart {svc.container}"
            ram_note = f"{held}: restart once idle {int(rule.restart_host_ram_idle)}s"
        nxt, note = self._decide_vram(r, rule, pinned, due, idle, now)
        return nxt, note or ram_note

    def _decide_vram(
        self, r: Resident, rule: ResidentRule, pinned: bool, due: float | None, idle: float | None, now: datetime
    ) -> tuple[str | None, str | None]:
        ollama = r.source == ResidentSource.OLLAMA
        left = (r.expires_at - now).total_seconds() if r.expires_at else None
        external = r.source == ResidentSource.EXTERNAL
        stoppable = external and (svc := self._service(r)) is not None and svc.evict_by == "stop"
        if pinned:
            if ollama and (r.expires_at is None or r.expires_at - now < PIN_HORIZON):
                return "pin", "pinned: keep_alive -1"
            memo = self.pins.get(r.id)
            if memo and memo.missed and not self._loaded(r):
                return None, f"pinned, unloaded: {memo.note or 'restore due'}"
            if memo and memo.restored_at and (now - memo.restored_at).total_seconds() < RESTORE_STAY_S:
                return None, "pinned (restored)"
            return None, "pinned"
        if due is None:
            if stoppable:
                return None, "stopped when a lease needs the room" if rule.enforce else "would stop for a lease"
            return None, None
        if due > 0:
            if ollama and left is not None and left < REFRESH_MARGIN_S and left < due:
                return "extend", f"keep_alive to {int(due + REFRESH_MARGIN_S)}s"
            return None, None
        if r.source == ResidentSource.PROCESS or (external and not stoppable):
            return None, "due, but iris has no adapter for it"
        state = r.detail.get("model_state")
        if state == "cpu":
            # Evicted to the CPU: warm in host RAM, and what it still holds on the GPU is the price of that. A
            # restart would throw the warm copy away, so only once the service has unloaded it fully.
            if rule.unload_after is not None and rule.idle_kill is not None and idle is not None:
                left_s = rule.idle_kill + rule.unload_after - idle
                if left_s <= 0:
                    return "unload", "on the CPU past unload_after: unload fully"
                return None, f"on the CPU; unload in {int(left_s)}s"
            return None, f"on the CPU ({r.vram_mb} MB left)" if r.vram_mb else "on the CPU"
        if state in _FREED or self.evicted(r):
            svc = self._service(r)
            if rule.restart_residual_mb is not None and r.vram_mb >= rule.restart_residual_mb and svc and svc.container:
                return "restart", f"unloaded but holding {r.vram_mb} MB: restart {svc.container}"
            return None, f"unloaded ({r.vram_mb} MB left)" if r.vram_mb else "unloaded"
        return "evict", "idle-kill due"

    def annotate(self, state: NodeState) -> None:
        now = self._clock()
        if state.reachable:
            self._track_pins(state, now)
        for r in state.residents:
            if r.id in self.broker.residuals and not self.evicted(r):
                del self.broker.residuals[r.id]  # it loaded a model again
            r.policy = self.view(r, now)

    # --- restoring pins (#35) ------------------------------------------------------------------------------------

    @staticmethod
    def _loaded(r: Resident) -> bool:
        """Holding its model: an Ollama model is listed only while loaded; an argus-http service says so."""
        if r.source == ResidentSource.OLLAMA:
            return True
        if r.source == ResidentSource.ARGUS_HTTP:
            return on_gpu(r)
        return False  # nothing else can be warmed back

    def _track_pins(self, state: NodeState, now: datetime) -> None:
        """Remember pinned residents seen loaded on *state*'s node, and count polls for those now gone."""
        present = {r.id: r for r in state.residents}
        for r in state.residents:
            match = self.policy.rule_for(r)
            if match is None or not self.policy.kept(match[1], now) or not self._loaded(r):
                continue
            memo = self.pins.get(r.id)
            if memo is None:
                self.pins[r.id] = PinMemory(resident=r.model_copy(deep=True))
                continue
            memo.resident, memo.missed, memo.note = r.model_copy(deep=True), 0, None
            if memo.fails and memo.restored_at and (now - memo.restored_at).total_seconds() >= RESTORE_STAY_S:
                memo.fails = 0  # it stayed: a later drop starts the backoff afresh
        for rid, memo in self.pins.items():
            if memo.resident.node != state.name:
                continue
            r = present.get(rid)
            if r is not None and self._loaded(r):
                continue
            memo.missed += 1
            if memo.missed == 1 and memo.restored_at and (now - memo.restored_at).total_seconds() < RESTORE_STAY_S:
                memo.fails += 1  # restored, and gone again soon after: back off rather than fight whatever drops it
                memo.next_try = now + self._backoff(memo.fails)

    @staticmethod
    def _backoff(fails: int) -> timedelta:
        return timedelta(seconds=min(COOLDOWN_S * 2**fails, RESTORE_BACKOFF_MAX_S))

    def _restore_blocked(self, memo: PinMemory) -> str | None:
        """Why *memo*'s resident cannot be restored now, or None."""
        node = memo.resident.node
        busy = self.broker.leases.active(node) or [
            o for o in self.broker.leases.active() if o.node is None and o.state == "queued"
        ]
        if busy:
            return f"restore once no lease is active on {node}"
        state = self.broker.nodes.get(node)
        if state is None or not state.reachable or state.ledger is None:
            return f"restore once {node}'s agent answers"
        held = next((r.vram_mb for r in state.residents if r.id == memo.resident.id), 0)
        need = memo.resident.vram_mb - held
        if need > state.ledger.admissible_mb:
            return f"restore once {node} can admit {need} MB ({state.ledger.admissible_mb} MB now)"
        return None

    async def _restore(self, now: datetime) -> None:
        from argus_iris.broker.actions import ActionError, set_keep_alive, warm_service  # noqa: PLC0415

        for rid, memo in list(self.pins.items()):
            if memo.missed < RESTORE_AFTER_POLLS:
                continue
            r = memo.resident
            match = self.policy.rule_for(r)
            svc = self._service(r)
            if match is None or not self.policy.kept(match[1], now) or svc is None:
                del self.pins[rid]  # no longer pinned (policy changed, window closed) or no longer configured
                continue
            rule = match[1]
            if not rule.enforce or not rule.restore or (memo.next_try and now < memo.next_try):
                continue
            if r.source == ResidentSource.ARGUS_HTTP and not svc.warm_path:
                memo.note = f"{svc.id} has no warm_path"
                continue
            if why := self._restore_blocked(memo):
                memo.note = why
                continue
            memo.next_try = now + self._backoff(memo.fails)  # a slow load is not started again on the next step
            outcome = "ok"
            try:
                if r.source == ResidentSource.OLLAMA:
                    await set_keep_alive(self.broker, r, -1, timeout_s=self.broker.config.warm_timeout_s)
                else:
                    result = await warm_service(self.broker, svc.id)
                    outcome = "ok" if result.up else "failed"
            except ActionError as exc:
                outcome = "failed"
                log.warning("residency_failed", resident=rid, action="restore", error=str(exc))
            if outcome == "ok":
                memo.restored_at, memo.note = now, None
            else:
                memo.fails += 1
                memo.next_try = now + self._backoff(memo.fails)
            self.actions[(r.node, "restore", outcome)] += 1
            log.info(
                "residency_action",
                resident=rid,
                action="restore",
                outcome=outcome,
                vram_mb=r.vram_mb,
                context_length=r.detail.get("context_length"),
            )

    def missing_pins(self) -> list[dict]:
        """Pinned residents gone now, for /health: id, polls gone, and why each waits."""
        return [
            {"resident": rid, "missed_polls": m.missed, "fails": m.fails, "note": m.note}
            for rid, m in self.pins.items()
            if m.missed
        ]

    # --- acting --------------------------------------------------------------------------------------------------

    async def step(self) -> None:
        """Act on every enforced resident whose policy says what to do next."""
        from argus_iris.broker.actions import (  # noqa: PLC0415 - actions imports the broker module
            ActionError,
            container_action,
            evict_resident,
            set_keep_alive,
        )

        now = self._clock()
        await self._rewarm(now)
        await self._restore(now)
        restarted: set[tuple[str, str]] = set()  # (node, container): Ollama's models share one container
        for r in self.broker.residents():
            v = r.policy
            if v is None or not v.enforce or v.next is None:
                continue
            until = self._cooldown.get(r.id)
            if until and now < until:
                continue
            self._cooldown[r.id] = now + timedelta(seconds=COOLDOWN_S)
            svc = self._service(r)
            if v.next == "restart":
                if svc is None or not svc.container or (r.node, svc.container) in restarted:
                    continue
                # A restart takes every resident of the container with it (Ollama's models share one): never while
                # another of them is pinned, busy or under a lease.
                if any(
                    o.id != r.id and (o.busy or (o.policy and o.policy.pinned) or self.broker.leases.in_use(o))
                    for o in self.broker.residents()
                    if o.node == r.node and o.service == r.service
                ):
                    continue
                restarted.add((r.node, svc.container))
            outcome = "ok"
            try:
                if v.next == "pin":
                    await set_keep_alive(self.broker, r, -1)
                elif v.next == "extend":
                    await set_keep_alive(self.broker, r, f"{int((v.due_in_s or 0) + REFRESH_MARGIN_S)}s")
                elif v.next == "evict":
                    result = await evict_resident(self.broker, r.id, reason="idle-kill")
                    outcome = "ok" if result.confirmed else "failed"
                elif v.next == "unload":
                    # Off the GPU already: this frees host RAM, which NVML cannot confirm. The service's word is it.
                    result = await evict_resident(self.broker, r.id, mode="unload", reason="idle-kill: unload_after")
                    outcome = "ok" if result.model_state == "unloaded" else "failed"
                elif v.next == "restart" and svc is not None:
                    await container_action(self.broker, svc, "restart")
            except ActionError as exc:
                outcome = "failed"
                log.warning("residency_failed", resident=r.id, action=v.next, error=str(exc))
            self.actions[(r.node, v.next, outcome)] += 1
            log.info("residency_action", resident=r.id, action=v.next, outcome=outcome, note=v.note, idle_s=v.idle_s)

    async def _rewarm(self, now: datetime) -> None:
        """Start again what admission stopped for room, once no lease is active on its node. The warm itself refuses
        while the node cannot admit what the service was seen holding, so it is retried on a later step."""
        from argus_iris.broker.actions import ActionError, warm_service  # noqa: PLC0415 - actions imports the broker

        for service_id in list(self.broker.stopped):
            svc = next((s for s in self.broker.config.services if s.id == service_id), None)
            if svc is None:  # pragma: no cover - only configured services are stopped
                self.broker.stopped.pop(service_id, None)
                continue
            key = f"rewarm:{service_id}"
            until = self._cooldown.get(key)
            if (until and now < until) or self.broker.leases.active(svc.node):
                continue
            self._cooldown[key] = now + timedelta(seconds=COOLDOWN_S)
            outcome = "ok"
            try:
                result = await warm_service(self.broker, service_id)
                outcome = "ok" if result.up else "failed"
            except ActionError as exc:
                outcome = "failed"
                log.info("residency_rewarm_deferred", service=service_id, error=str(exc))
            self.actions[(svc.node, "rewarm", outcome)] += 1
            log.info("residency_action", service=service_id, action="rewarm", outcome=outcome)
