"""Restoring pinned residents that dropped out (#35): a pin keeps a model loaded, not only uneviced."""

from __future__ import annotations

from datetime import timedelta

from argus_cortex.gpu_wire import LeaseRelease, LeaseRequest
from test_evict import SINGGUARD
from test_policy import make, view

from argus_iris.broker import residency
from argus_iris.broker.policy import Policy

RID = f"rtx4060/ollama/{SINGGUARD}"
PIN = {"match": {"ollama": "hf.co/inclusionAI/SingGuard-*"}, "keepalive": "always", "enforce": True}
RESTORE = (
    f'POST http://rtx4060:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":-1,"options":{{"num_ctx":16384}}}}'
)


async def pinned(rule: dict = PIN):
    """rtx4060 with SingGuard pinned and already held at -1, loaded at 16384 by its caller; whisper off the card."""
    rtx4060, broker = await make([rule])
    rtx4060.vram["whisper"] = 0
    rtx4060.context[SINGGUARD] = 16384
    rtx4060.expires[SINGGUARD] = "2318-01-01T00:00:00Z"
    await broker.poll_once()
    assert RID in broker.residency.pins
    return rtx4060, broker


def drop(rtx4060) -> None:
    """Something other than iris unloads SingGuard (a manual unload, a spill test, a restart)."""
    rtx4060.ollama_models.remove(SINGGUARD)
    rtx4060.context.pop(SINGGUARD)
    rtx4060.vram["ollama"] = 300


async def gone(broker, polls: int = residency.RESTORE_AFTER_POLLS) -> None:
    for _ in range(polls):
        await broker.poll_once()


async def test_a_pinned_ollama_model_comes_back_at_the_context_and_size_it_had():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    await gone(broker)
    await broker.residency.step()
    assert rtx4060.calls == [RESTORE]
    await broker.poll_once()
    r = next(r for r in broker.residents() if r.id == RID)
    assert (r.vram_mb, r.detail["context_length"]) == (4000, 16384)
    assert broker.residency.actions[("rtx4060", "restore", "ok")] == 1
    assert broker.residency.missing_pins() == []


async def test_not_restored_after_a_single_missed_poll():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    await gone(broker, 1)
    await broker.residency.step()
    assert rtx4060.calls == []


async def test_waits_while_a_lease_is_active_or_queued_on_the_node():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    rec = broker.leases.request(LeaseRequest(tenant="composer", target="ollama:qwen3:8b", vram_mb=100))
    await gone(broker)
    await broker.residency.step()
    assert rtx4060.calls == []
    [missing] = broker.residency.missing_pins()
    assert missing["note"] == "restore once no lease is active on rtx4060"

    broker.leases.release(rec.lease_id, LeaseRelease(outcome="ok"))
    await broker.poll_once()
    await broker.residency.step()
    assert rtx4060.calls == [RESTORE]


async def test_waits_for_room_and_never_evicts_for_it():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    rtx4060.vram["whisper"] = 3000  # the card filled up after it went
    await gone(broker)
    await broker.residency.step()
    assert rtx4060.calls == []
    [missing] = broker.residency.missing_pins()
    assert missing["note"].startswith("restore once rtx4060 can admit 4000 MB")


async def test_a_failed_restore_backs_off():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    rtx4060.generate_fails = True
    await gone(broker)
    await broker.residency.step()
    await broker.residency.step()  # not retried at once
    assert len(rtx4060.calls) == 1
    assert broker.residency.actions[("rtx4060", "restore", "failed")] == 1
    memo = broker.residency.pins[RID]
    assert memo.fails == 1 and memo.next_try is not None


async def test_gone_again_soon_after_a_restore_doubles_the_wait():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    await gone(broker)
    await broker.residency.step()
    await broker.poll_once()  # restored and seen
    drop(rtx4060)  # and dropped again at once (a spill, a broken model): don't fight it
    await broker.poll_once()
    memo = broker.residency.pins[RID]
    assert memo.fails == 1
    assert memo.next_try - broker.residency._clock() > timedelta(seconds=residency.COOLDOWN_S)
    await gone(broker)
    await broker.residency.step()
    assert len(rtx4060.calls) == 1  # only the first restore


async def test_report_only_and_restore_false_leave_it_gone():
    for rule in ({**PIN, "enforce": False}, {**PIN, "restore": False}):
        rtx4060, broker = await pinned(rule)
        drop(rtx4060)
        await gone(broker)
        await broker.residency.step()
        assert rtx4060.calls == []


async def test_forgotten_once_the_policy_no_longer_pins_it():
    rtx4060, broker = await pinned()
    drop(rtx4060)
    await gone(broker)
    broker.residency.policy = Policy.model_validate({"residents": []})
    await broker.residency.step()
    assert rtx4060.calls == []
    assert RID not in broker.residency.pins


async def test_a_pinned_argus_http_resident_left_unloaded_is_warmed():
    rule = {"match": {"resident": "rtx4060/xtts"}, "keepalive": "always", "enforce": True}
    rtx4060, broker = await make([rule])
    next(s for s in broker.config.services if s.id == "xtts").warm_path = "/admin/wake"
    rtx4060.vram["whisper"] = 0
    await broker.poll_once()
    assert "rtx4060/xtts" in broker.residency.pins
    rtx4060.state["xtts"], rtx4060.vram["xtts"] = "unloaded", 140  # an unload by hand
    await gone(broker)
    assert view(broker, "rtx4060/xtts").note == "pinned, unloaded: restore due"
    await broker.residency.step()
    assert rtx4060.calls == ["POST http://rtx4060:9150/r/xtts/admin/wake"]
    await broker.poll_once()
    assert view(broker, "rtx4060/xtts").note == "pinned (restored)"
