"""The policy file (cron, windows, rules) and residency: idle-kill, keepalive pins, extend, restart, report-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argus_cortex.gpu_wire import LeaseRequest
from pydantic import ValidationError
from test_evict import CONFIG, SINGGUARD
from test_evict import World as Dragon

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.policy import Cron, Policy, load_policy, parse_duration
from argus_iris.broker.state import Broker
from argus_iris.models import Resident, ResidentSource

# --- the file ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "s"), [(90, 90), ("90s", 90), ("15m", 900), ("5h", 18000), ("1.5d", 129600)])
def test_durations(raw, s):
    assert parse_duration(raw) == s


def test_bad_duration_and_cron_are_refused():
    with pytest.raises(ValueError):
        parse_duration("soon")
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)
    with pytest.raises(ValidationError):
        Policy.model_validate({"timezone": "Mars/Olympus"})
    with pytest.raises(ValidationError):
        Policy.model_validate({"residents": [{"idle_kil": "5m"}]})  # a typo is an error, not a silent no-op


def test_cron_fields():
    c = Cron("*/15 8-17 * * 1-5")
    assert c.matches(datetime(2026, 11, 16, 8, 45)) and not c.matches(datetime(2026, 11, 16, 8, 50))
    assert not c.matches(datetime(2026, 11, 15, 9, 0))  # Sunday
    assert Cron("0 0 * * 7").matches(datetime(2026, 11, 15, 0, 0))  # 7 is Sunday too
    either = Cron("0 0 1 * 1")  # both day fields restricted: the 1st, or any Monday
    assert either.matches(datetime(2026, 12, 1, 0, 0)) and either.matches(datetime(2026, 11, 16, 0, 0))
    assert not either.matches(datetime(2026, 11, 17, 0, 0))


def test_windows_open_across_midnight_in_the_policy_zone():
    p = Policy.model_validate(
        {
            "timezone": "America/Los_Angeles",
            "windows": [
                {
                    "name": "audit",
                    "cron": "0 23 * * *",
                    "duration": "5h",
                    "tenant": "ai-code-auditor",
                    "target": "ollama:*",
                    "class": "batch",
                },
            ],
        }
    )
    at = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)  # 01:30 in Los Angeles: opened 23:00 the day before
    assert p.window_class("ai-code-auditor", "ollama:qwen3:8b", None, at) == ("audit", "batch")
    assert p.window_class("ai-code-auditor", "comfyui", None, at) is None
    assert p.window_class("someone-else", "ollama:qwen3:8b", None, at) is None
    assert p.window_class("ai-code-auditor", "ollama:qwen3:8b", None, at + timedelta(hours=3)) is None


def test_first_matching_rule_wins_and_ollama_matches_only_ollama():
    p = Policy.model_validate(
        {
            "residents": [
                {"match": {"ollama": "hf.co/*"}, "keepalive": "always"},
                {"match": {"node": "dragon"}, "idle_kill": 60},
            ]
        }
    )
    model = Resident(id="d/o/m", node="dragon", service="o", source=ResidentSource.OLLAMA, model="hf.co/x:q", vram_mb=1)
    other = Resident(id="d/xtts", node="dragon", service="xtts", source=ResidentSource.ARGUS_HTTP, vram_mb=1)
    assert p.rule_for(model)[0] == 0 and p.rule_for(other)[0] == 1


def test_no_policy_file_is_an_empty_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("IRIS_POLICY", raising=False)
    assert load_policy() == Policy()
    f = tmp_path / "policy.yaml"
    f.write_text("residents:\n  - match: {node: dragon}\n    idle_kill: 5m\n")
    monkeypatch.setenv("IRIS_POLICY", str(f))
    assert load_policy().residents[0].idle_kill == 300


# --- residency on a fake dragon (idle 60 s everywhere) -------------------------------------------------------------


async def make(rules: list[dict], **policy) -> tuple[Dragon, Broker]:
    dragon = Dragon()
    broker = Broker(
        BrokerConfig.model_validate(CONFIG),
        client=httpx.AsyncClient(transport=httpx.MockTransport(dragon.handler)),
        policy=Policy.model_validate({"residents": rules, **policy}),
    )
    await broker.poll_once()
    return dragon, broker


def view(broker: Broker, rid: str):
    return next(r for r in broker.residents() if r.id == rid).policy


async def test_report_only_until_enforced():
    dragon, broker = await make([{"match": {"resident": "dragon/xtts"}, "idle_kill": "30s"}])
    v = view(broker, "dragon/xtts")
    assert (v.enforce, v.next, v.due_in_s, v.idle_s) == (False, "evict", 0, 60)
    assert view(broker, "dragon/whisper") is None  # no rule: no policy
    await broker.residency.step()
    assert dragon.calls == []


async def test_idle_kill_evicts_when_due():
    dragon, broker = await make([{"match": {"resident": "dragon/xtts"}, "idle_kill": "30s", "enforce": True}])
    await broker.residency.step()
    assert dragon.calls == ["POST http://dragon:9150/r/xtts/admin/evict"]
    await broker.residency.step()  # cooldown: not again at once
    assert len(dragon.calls) == 1
    assert broker.residency.actions[("dragon", "evict", "ok")] == 1


async def test_not_due_and_in_use_are_left_alone():
    dragon, broker = await make([{"match": {"resident": "dragon/xtts"}, "idle_kill": "10m", "enforce": True}])
    assert view(broker, "dragon/xtts").due_in_s == 540
    await broker.residency.step()
    assert dragon.calls == []

    dragon, broker = await make([{"match": {"ollama": "*"}, "idle_kill": "30s", "enforce": True}])
    broker.leases.request(LeaseRequest(tenant="t", target=f"ollama:{SINGGUARD}", vram_mb=100))
    await broker.poll_once()
    assert view(broker, f"dragon/ollama/{SINGGUARD}").idle_s == 0  # a granted lease is using it
    await broker.residency.step()
    assert dragon.calls == []


async def test_pinned_ollama_model_is_held_at_minus_one_and_spared_by_admission():
    dragon, broker = await make(
        [{"match": {"ollama": "hf.co/inclusionAI/SingGuard-*"}, "keepalive": "always", "enforce": True}]
    )
    rid = f"dragon/ollama/{SINGGUARD}"
    assert (view(broker, rid).pinned, view(broker, rid).next) == (True, "pin")
    await broker.residency.step()
    assert dragon.calls == [f'POST http://dragon:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":-1}}']
    await broker.poll_once()
    assert (view(broker, rid).next, view(broker, rid).note) == (None, "pinned")  # held: nothing more to do
    assert broker.residency.pinned(next(r for r in broker.residents() if r.id == rid))


async def test_ollama_keep_alive_is_extended_until_idle_kill():
    dragon, broker = await make([{"match": {"ollama": "*"}, "idle_kill": "10m", "enforce": True}])
    soon = datetime.now(UTC) + timedelta(seconds=60)
    dragon.expires[SINGGUARD] = soon.isoformat().replace("+00:00", "Z")
    await broker.poll_once()
    v = view(broker, f"dragon/ollama/{SINGGUARD}")
    assert (v.next, v.note) == ("extend", "keep_alive to 660s")
    await broker.residency.step()
    assert dragon.calls == [f'POST http://dragon:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":"660s"}}']


async def test_unloaded_but_holding_a_context_is_restarted(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "agent-tok")
    dragon, broker = await make(
        [{"match": {"resident": "dragon/whisper"}, "idle_kill": "30s", "restart_residual_mb": 64, "enforce": True}]
    )
    dragon.state["whisper"], dragon.vram["whisper"] = "unloaded", 94
    await broker.poll_once()
    v = view(broker, "dragon/whisper")
    assert (v.next, v.note) == ("restart", "unloaded but holding 94 MB: restart whisper")
    await broker.residency.step()
    assert dragon.calls == ["POST http://dragon:9110/containers/whisper/restart"]
    assert broker.residency.actions[("dragon", "restart", "ok")] == 1
    await broker.poll_once()
    assert not any(r.id == "dragon/whisper" for r in broker.residents())  # the 94 MB came back


async def test_without_a_threshold_an_unloaded_resident_is_just_reported():
    dragon, broker = await make([{"match": {"resident": "dragon/whisper"}, "idle_kill": "30s", "enforce": True}])
    dragon.state["whisper"], dragon.vram["whisper"] = "unloaded", 94
    await broker.poll_once()
    assert (view(broker, "dragon/whisper").next, view(broker, "dragon/whisper").note) == (None, "unloaded (94 MB left)")


async def test_a_window_sets_the_lease_class():
    dragon, broker = await make(
        [], windows=[{"name": "always", "cron": "* * * * *", "duration": "1h", "tenant": "auditor", "class": "batch"}]
    )
    rec = broker.leases.request(LeaseRequest(tenant="auditor", priority="interactive"))
    assert (rec.priority, rec.window) == ("batch", "always")


def test_example_policy_loads_and_enforces_nothing():
    from pathlib import Path

    p = load_policy(Path(__file__).parent.parent / "deploy" / "policy.example.yaml")
    assert p.residents and not any(r.enforce for r in p.residents)


async def test_an_evicted_resident_is_not_evicted_again_until_it_loads_again():
    dragon, broker = await make([{"match": {"resident": "morpheus/comfyui"}, "idle_kill": "30s", "enforce": True}])
    dragon.vram["voluptas_comfyui"] = 6000
    await broker.residency.step()
    assert dragon.calls == ['POST http://morpheus:8188/free {"unload_models":true,"free_memory":true}']
    assert broker.residuals["morpheus/comfyui"] == 400
    v = view(broker, "morpheus/comfyui")
    assert (v.next, v.note) == (None, "unloaded (400 MB left)")  # ComfyUI says nothing; iris remembers
    comfy = next(r for r in broker.residents() if r.id == "morpheus/comfyui")
    assert broker.residency.protected(comfy)  # nor does admission plan it
    dragon.vram["voluptas_comfyui"] = 5000  # a job loaded a checkpoint
    await broker.poll_once()
    assert "morpheus/comfyui" not in broker.residuals and view(broker, "morpheus/comfyui").next == "evict"


async def test_a_free_that_frees_nothing_is_not_repeated():
    dragon, broker = await make([{"match": {"resident": "morpheus/comfyui"}, "idle_kill": "30s", "enforce": True}])
    dragon.vram["voluptas_comfyui"] = 400  # already at its floor: /free changes nothing, so NVML never confirms
    await broker.poll_once()
    await broker.residency.step()
    assert broker.residency.actions[("morpheus", "evict", "failed")] == 1
    assert view(broker, "morpheus/comfyui").next is None  # remembered: not every minute from now on


async def test_a_resident_on_the_cpu_is_never_restarted():
    dragon, broker = await make(
        [{"match": {"resident": "dragon/xtts"}, "idle_kill": "30s", "restart_residual_mb": 64, "enforce": True}]
    )
    await broker.residency.step()  # due: evicted to the CPU, leaving its context behind
    assert dragon.calls == ["POST http://dragon:9150/r/xtts/admin/evict"]
    dragon.vram["xtts"] = 564  # tts on dragon keeps this much after an evict to the CPU
    await broker.poll_once()
    v = view(broker, "dragon/xtts")
    assert (v.next, v.note) == (None, "on the CPU (564 MB left)")  # warm in RAM: a restart would lose it
    dragon.state["xtts"] = "unloaded"  # the sidecar's own CPU timer has unloaded it
    await broker.poll_once()
    assert view(broker, "dragon/xtts").next == "restart"


async def test_unload_after_frees_the_cpu_stage_too():
    dragon, broker = await make(
        [{"match": {"resident": "dragon/xtts"}, "idle_kill": "30s", "unload_after": "600s", "enforce": True}]
    )
    dragon.residual["xtts"] = 94  # the CUDA context xtts keeps on the CPU, as on dragon
    await broker.residency.step()  # idle 60 s >= 30 s: GPU -> CPU
    assert dragon.calls == ["POST http://dragon:9150/r/xtts/admin/evict"]
    await broker.poll_once()
    v = view(broker, "dragon/xtts")
    assert (v.next, v.note) == (None, "on the CPU; unload in 570s")  # 30 + 600 - 60
    broker.residency._cooldown.clear()
    rule = broker.policy.residents[0]
    broker.policy.residents[0] = rule.model_copy(update={"unload_after": 0.0})  # or once 600 s have passed
    await broker.poll_once()
    assert view(broker, "dragon/xtts").next == "unload"
    await broker.residency.step()
    assert dragon.calls[-1] == "POST http://dragon:9150/r/xtts/admin/unload"
    assert dragon.state["xtts"] == "unloaded"
    assert broker.residency.actions[("dragon", "unload", "ok")] == 1  # no VRAM moved, and that is not a failure
