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
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

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


class Residency:
    def __init__(self, broker: Broker, policy: Policy, *, clock=utcnow) -> None:
        self.broker = broker
        self.policy = policy
        self._clock = clock
        self._cooldown: dict[str, datetime] = {}
        self.actions: Counter[tuple[str, str, str]] = Counter()  # (node, action, ok | failed) -> count

    # --- reading -------------------------------------------------------------------------------------------------

    def pinned(self, r: Resident) -> bool:
        """Kept by the policy now. Enforced or not: a pin only ever protects, so admission honours it either way."""
        match = self.policy.rule_for(r)
        return bool(match and self.policy.kept(match[1], self._clock()))

    def evicted(self, r: Resident) -> bool:
        """Already evicted: holding no more than its last confirmed eviction left behind."""
        left = self.broker.residuals.get(r.id)
        return left is not None and r.vram_mb <= left + RESIDUAL_SLACK_MB

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
        ollama = r.source == ResidentSource.OLLAMA
        left = (r.expires_at - now).total_seconds() if r.expires_at else None
        if pinned:
            if ollama and (r.expires_at is None or r.expires_at - now < PIN_HORIZON):
                return "pin", "pinned: keep_alive -1"
            return None, "pinned"
        if due is None:
            return None, None
        if due > 0:
            if ollama and left is not None and left < REFRESH_MARGIN_S and left < due:
                return "extend", f"keep_alive to {int(due + REFRESH_MARGIN_S)}s"
            return None, None
        if r.source == ResidentSource.PROCESS:
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
            svc = next((s for s in self.broker.config.services if s.id == r.service), None)
            if rule.restart_residual_mb is not None and r.vram_mb >= rule.restart_residual_mb and svc and svc.container:
                return "restart", f"unloaded but holding {r.vram_mb} MB: restart {svc.container}"
            return None, f"unloaded ({r.vram_mb} MB left)" if r.vram_mb else "unloaded"
        return "evict", "idle-kill due"

    def annotate(self, state: NodeState) -> None:
        now = self._clock()
        for r in state.residents:
            if r.id in self.broker.residuals and not self.evicted(r):
                del self.broker.residuals[r.id]  # it loaded a model again
            r.policy = self.view(r, now)

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
        for r in self.broker.residents():
            v = r.policy
            if v is None or not v.enforce or v.next is None:
                continue
            until = self._cooldown.get(r.id)
            if until and now < until:
                continue
            self._cooldown[r.id] = now + timedelta(seconds=COOLDOWN_S)
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
                elif v.next == "restart":
                    svc = next(s for s in self.broker.config.services if s.id == r.service)
                    await container_action(self.broker, svc, "restart")
            except ActionError as exc:
                outcome = "failed"
                log.warning("residency_failed", resident=r.id, action=v.next, error=str(exc))
            self.actions[(r.node, v.next, outcome)] += 1
            log.info("residency_action", resident=r.id, action=v.next, outcome=outcome, note=v.note, idle_s=v.idle_s)
