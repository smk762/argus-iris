"""The policy file (cron, windows, rules) and residency: idle-kill, keepalive pins, extend, restart, report-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argus_cortex.gpu_wire import LeaseRequest
from pydantic import ValidationError
from test_evict import CONFIG, SINGGUARD
from test_evict import World as Rtx4060

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.policy import Cron, Policy, ResidentRule, load_policy, parse_duration
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


def test_windows_last_their_elapsed_duration_across_dst():
    la = Policy.model_validate({"timezone": "America/Los_Angeles"}).zone
    open_at = [
        # Autumn (clocks back at 02:00 PDT on 2026-11-01): opened 00:30 PDT, so an hour later (01:30 PDT) it closed.
        # At 01:15 PST, 1h45m after it opened, the wall clock says only 45 minutes.
        Cron("30 0 * * *").open_since(datetime(2026, 11, 1, 9, 15, tzinfo=UTC).astimezone(la), 3600),
        # Spring (clocks forward at 02:00 PST on 2026-03-08): opened 01:30 PST, 45 minutes before 03:15 PDT.
        Cron("30 1 * * *").open_since(datetime(2026, 3, 8, 10, 15, tzinfo=UTC).astimezone(la), 3600),
    ]
    assert open_at == [None, datetime(2026, 3, 8, 9, 30, tzinfo=UTC)]


def test_node_scoped_window_does_not_match_a_lease_with_no_node():
    p = Policy.model_validate(
        {
            "windows": [
                {
                    "name": "rtx4060-batch",
                    "cron": "0 1 * * *",
                    "duration": "2h",
                    "tenant": "t",
                    "node": "rtx4060",
                    "class": "batch",
                },
            ],
        }
    )
    at = datetime(2026, 11, 17, 1, 30, tzinfo=UTC)
    assert p.window_class("t", "ollama:x", "rtx4060", at) == ("rtx4060-batch", "batch")
    assert p.window_class("t", "ollama:x", "gtx1070", at) is None
    assert p.window_class("t", "ollama:x", None, at) is None  # unplaced: may land off rtx4060


@pytest.mark.parametrize(
    "bad",
    [-5, float("nan"), float("inf"), ".inf", "9" * 400, "-5s", 10**400],
    ids=["negative", "nan", "inf", "yaml-inf", "long-string", "negative-s", "huge-int"],
)
def test_durations_must_be_finite_and_not_negative(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
    with pytest.raises(ValidationError):
        ResidentRule.model_validate({"idle_kill": bad})


def test_first_matching_rule_wins_and_ollama_matches_only_ollama():
    p = Policy.model_validate(
        {
            "residents": [
                {"match": {"ollama": "hf.co/*"}, "keepalive": "always"},
                {"match": {"node": "rtx4060"}, "idle_kill": 60},
            ]
        }
    )
    model = Resident(
        id="d/o/m", node="rtx4060", service="o", source=ResidentSource.OLLAMA, model="hf.co/x:q", vram_mb=1
    )
    other = Resident(id="d/xtts", node="rtx4060", service="xtts", source=ResidentSource.ARGUS_HTTP, vram_mb=1)
    assert p.rule_for(model)[0] == 0 and p.rule_for(other)[0] == 1


def test_no_policy_file_is_an_empty_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("IRIS_POLICY", raising=False)
    assert load_policy() == Policy()
    f = tmp_path / "policy.yaml"
    f.write_text("residents:\n  - match: {node: rtx4060}\n    idle_kill: 5m\n")
    monkeypatch.setenv("IRIS_POLICY", str(f))
    assert load_policy().residents[0].idle_kill == 300


def test_report_only_unenforces_every_rule(monkeypatch, tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("residents:\n  - match: {node: rtx4060}\n    idle_kill: 5m\n    enforce: true\n")
    monkeypatch.delenv("IRIS_REPORT_ONLY", raising=False)
    assert load_policy(f).residents[0].enforce
    monkeypatch.setenv("IRIS_REPORT_ONLY", "true")
    rule = load_policy(f).residents[0]
    assert not rule.enforce and rule.idle_kill == 300  # still reported: the dashboard shows what it would do


# --- residency on a fake rtx4060 (idle 60 s everywhere) -------------------------------------------------------------


async def make(rules: list[dict], **policy) -> tuple[Rtx4060, Broker]:
    rtx4060 = Rtx4060()
    broker = Broker(
        BrokerConfig.model_validate(CONFIG),
        client=httpx.AsyncClient(transport=httpx.MockTransport(rtx4060.handler)),
        policy=Policy.model_validate({"residents": rules, **policy}),
    )
    await broker.poll_once()
    return rtx4060, broker


def view(broker: Broker, rid: str):
    return next(r for r in broker.residents() if r.id == rid).policy


async def test_report_only_until_enforced():
    rtx4060, broker = await make([{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "30s"}])
    v = view(broker, "rtx4060/xtts")
    assert (v.enforce, v.next, v.due_in_s, v.idle_s) == (False, "evict", 0, 60)
    assert view(broker, "rtx4060/whisper") is None  # no rule: no policy
    await broker.residency.step()
    assert rtx4060.calls == []


async def test_idle_kill_evicts_when_due():
    rtx4060, broker = await make([{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "30s", "enforce": True}])
    await broker.residency.step()
    assert rtx4060.calls == ["POST http://rtx4060:9150/r/xtts/admin/evict"]
    await broker.residency.step()  # cooldown: not again at once
    assert len(rtx4060.calls) == 1
    assert broker.residency.actions[("rtx4060", "evict", "ok")] == 1


async def test_not_due_and_in_use_are_left_alone():
    rtx4060, broker = await make([{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "10m", "enforce": True}])
    assert view(broker, "rtx4060/xtts").due_in_s == 540
    await broker.residency.step()
    assert rtx4060.calls == []

    rtx4060, broker = await make([{"match": {"ollama": "*"}, "idle_kill": "30s", "enforce": True}])
    broker.leases.request(LeaseRequest(tenant="t", target=f"ollama:{SINGGUARD}", vram_mb=100))
    await broker.poll_once()
    assert view(broker, f"rtx4060/ollama/{SINGGUARD}").idle_s == 0  # a granted lease is using it
    await broker.residency.step()
    assert rtx4060.calls == []


async def test_pinned_ollama_model_is_held_at_minus_one_and_spared_by_admission():
    rtx4060, broker = await make(
        [{"match": {"ollama": "hf.co/inclusionAI/SingGuard-*"}, "keepalive": "always", "enforce": True}]
    )
    rid = f"rtx4060/ollama/{SINGGUARD}"
    assert (view(broker, rid).pinned, view(broker, rid).next) == (True, "pin")
    await broker.residency.step()
    assert rtx4060.calls == [f'POST http://rtx4060:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":-1}}']
    await broker.poll_once()
    assert (view(broker, rid).next, view(broker, rid).note) == (None, "pinned")  # held: nothing more to do
    assert broker.residency.pinned(next(r for r in broker.residents() if r.id == rid))


async def test_keep_alive_calls_send_back_the_loaded_context():
    # Ollama reloads a model for a request with another num_ctx: a bare pin reloaded SingGuard (loaded at 16384 by
    # rag-chat) at the default 4096, and rag-chat's next check reloaded it back, once a minute.
    rtx4060, broker = await make([{"match": {"ollama": "*"}, "keepalive": "always", "enforce": True}])
    rtx4060.context[SINGGUARD] = 16384
    await broker.poll_once()
    await broker.residency.step()
    assert rtx4060.calls == [
        f'POST http://rtx4060:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":-1,"options":{{"num_ctx":16384}}}}'
    ]


async def test_ollama_keep_alive_is_extended_until_idle_kill():
    rtx4060, broker = await make([{"match": {"ollama": "*"}, "idle_kill": "10m", "enforce": True}])
    soon = datetime.now(UTC) + timedelta(seconds=60)
    rtx4060.expires[SINGGUARD] = soon.isoformat().replace("+00:00", "Z")
    await broker.poll_once()
    v = view(broker, f"rtx4060/ollama/{SINGGUARD}")
    assert (v.next, v.note) == ("extend", "keep_alive to 660s")
    await broker.residency.step()
    assert rtx4060.calls == [f'POST http://rtx4060:11434/api/generate {{"model":"{SINGGUARD}","keep_alive":"660s"}}']


async def test_unloaded_but_holding_a_context_is_restarted(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "agent-tok")
    rtx4060, broker = await make(
        [{"match": {"resident": "rtx4060/whisper"}, "idle_kill": "30s", "restart_residual_mb": 64, "enforce": True}]
    )
    rtx4060.state["whisper"], rtx4060.vram["whisper"] = "unloaded", 94
    await broker.poll_once()
    v = view(broker, "rtx4060/whisper")
    assert (v.next, v.note) == ("restart", "unloaded but holding 94 MB: restart whisper")
    await broker.residency.step()
    assert rtx4060.calls == ["POST http://rtx4060:9110/containers/whisper/restart"]
    assert broker.residency.actions[("rtx4060", "restart", "ok")] == 1
    await broker.poll_once()
    assert not any(r.id == "rtx4060/whisper" for r in broker.residents())  # the 94 MB came back


async def test_a_failed_load_holding_memory_is_restarted(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "agent-tok")
    rtx4060, broker = await make(
        [{"match": {"resident": "rtx4060/whisper"}, "idle_kill": "30s", "restart_residual_mb": 64, "enforce": True}]
    )
    rtx4060.state["whisper"], rtx4060.vram["whisper"] = "error", 1350  # rtx4060/tts after an out-of-memory load (#40)
    await broker.poll_once()
    v = view(broker, "rtx4060/whisper")
    assert (v.next, v.note) == ("restart", "failed to load but holding 1350 MB: restart whisper")
    await broker.residency.step()
    assert rtx4060.calls == ["POST http://rtx4060:9110/containers/whisper/restart"]


async def test_without_a_threshold_an_unloaded_resident_is_just_reported():
    rtx4060, broker = await make([{"match": {"resident": "rtx4060/whisper"}, "idle_kill": "30s", "enforce": True}])
    rtx4060.state["whisper"], rtx4060.vram["whisper"] = "unloaded", 94
    await broker.poll_once()
    assert (view(broker, "rtx4060/whisper").next, view(broker, "rtx4060/whisper").note) == (
        None,
        "unloaded (94 MB left)",
    )


async def test_a_window_sets_the_lease_class():
    rtx4060, broker = await make(
        [], windows=[{"name": "always", "cron": "* * * * *", "duration": "1h", "tenant": "auditor", "class": "batch"}]
    )
    rec = broker.leases.request(LeaseRequest(tenant="auditor", priority="interactive"))
    assert (rec.priority, rec.window) == ("batch", "always")


def test_example_policy_loads_and_enforces_nothing():
    from pathlib import Path

    p = load_policy(Path(__file__).parent.parent / "deploy" / "policy.example.yaml")
    assert p.residents and not any(r.enforce for r in p.residents)


async def test_an_evicted_resident_is_not_evicted_again_until_it_loads_again():
    rtx4060, broker = await make([{"match": {"resident": "rtx3090/comfyui"}, "idle_kill": "30s", "enforce": True}])
    rtx4060.vram["imagegen_comfyui"] = 6000
    await broker.residency.step()
    assert rtx4060.calls == ['POST http://rtx3090:8188/free {"unload_models":true,"free_memory":true}']
    assert broker.residuals["rtx3090/comfyui"] == 400
    v = view(broker, "rtx3090/comfyui")
    assert (v.next, v.note) == (None, "unloaded (400 MB left)")  # ComfyUI says nothing; iris remembers
    comfy = next(r for r in broker.residents() if r.id == "rtx3090/comfyui")
    assert broker.residency.protected(comfy)  # nor does admission plan it
    rtx4060.vram["imagegen_comfyui"] = 5000  # a job loaded a checkpoint
    await broker.poll_once()
    assert "rtx3090/comfyui" not in broker.residuals and view(broker, "rtx3090/comfyui").next == "evict"


async def test_a_free_that_frees_nothing_is_not_repeated():
    rtx4060, broker = await make([{"match": {"resident": "rtx3090/comfyui"}, "idle_kill": "30s", "enforce": True}])
    rtx4060.vram["imagegen_comfyui"] = 400  # already at its floor: /free changes nothing, so NVML never confirms
    await broker.poll_once()
    await broker.residency.step()
    assert broker.residency.actions[("rtx3090", "evict", "failed")] == 1
    assert view(broker, "rtx3090/comfyui").next is None  # remembered: not every minute from now on


async def test_a_resident_on_the_cpu_is_never_restarted():
    rtx4060, broker = await make(
        [{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "30s", "restart_residual_mb": 64, "enforce": True}]
    )
    await broker.residency.step()  # due: evicted to the CPU, leaving its context behind
    assert rtx4060.calls == ["POST http://rtx4060:9150/r/xtts/admin/evict"]
    rtx4060.vram["xtts"] = 564  # tts on rtx4060 keeps this much after an evict to the CPU
    await broker.poll_once()
    v = view(broker, "rtx4060/xtts")
    assert (v.next, v.note) == (None, "on the CPU (564 MB left)")  # warm in RAM: a restart would lose it
    rtx4060.state["xtts"] = "unloaded"  # the sidecar's own CPU timer has unloaded it
    await broker.poll_once()
    assert view(broker, "rtx4060/xtts").next == "restart"


async def test_unload_after_frees_the_cpu_stage_too():
    rtx4060, broker = await make(
        [{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "30s", "unload_after": "600s", "enforce": True}]
    )
    rtx4060.residual["xtts"] = 94  # the CUDA context xtts keeps on the CPU, as on rtx4060
    await broker.residency.step()  # idle 60 s >= 30 s: GPU -> CPU
    assert rtx4060.calls == ["POST http://rtx4060:9150/r/xtts/admin/evict"]
    await broker.poll_once()
    v = view(broker, "rtx4060/xtts")
    assert (v.next, v.note) == (None, "on the CPU; unload in 570s")  # 30 + 600 - 60
    broker.residency._cooldown.clear()
    rule = broker.policy.residents[0]
    broker.policy.residents[0] = rule.model_copy(update={"unload_after": 0.0})  # or once 600 s have passed
    await broker.poll_once()
    assert view(broker, "rtx4060/xtts").next == "unload"
    await broker.residency.step()
    assert rtx4060.calls[-1] == "POST http://rtx4060:9150/r/xtts/admin/unload"
    assert rtx4060.state["xtts"] == "unloaded"
    assert broker.residency.actions[("rtx4060", "unload", "ok")] == 1  # no VRAM moved, and that is not a failure


async def test_a_model_loaded_during_an_unload_is_not_written_off_as_its_floor():
    """rtx4060/xtts, 2026-11-21: on the CPU at 146 MB, unload sent, and a TTS request loaded it back (1970 MB) during
    the wait. Recorded as its floor, it read as "unloaded" for good: idle-kill planned nothing, and admission, which
    spares what is already evicted, refused SingGuard's leases rather than evict it."""
    rtx4060, broker = await make(
        [{"match": {"resident": "rtx4060/xtts"}, "idle_kill": "30s", "unload_after": "0s", "enforce": True}]
    )
    rtx4060.residual["xtts"] = 146
    await broker.residency.step()  # GPU -> CPU
    await broker.poll_once()
    broker.residency._cooldown.clear()
    rtx4060.loads_on_post["xtts"] = 1970
    await broker.residency.step()
    assert rtx4060.calls[-1] == "POST http://rtx4060:9150/r/xtts/admin/unload"
    assert "rtx4060/xtts" not in broker.residuals
    xtts = next(r for r in broker.residents() if r.id == "rtx4060/xtts")
    assert not broker.residency.protected(xtts)  # admission may evict it
    assert view(broker, "rtx4060/xtts").next == "evict"  # and idle-kill will, once its cooldown is over

    broker.residuals["rtx4060/xtts"] = 1970  # a floor recorded before this fix, as on rtx3090 that day
    await broker.poll_once()
    assert "rtx4060/xtts" not in broker.residuals  # the service says gpu: its word wins


# --- host RAM (#20): ComfyUI's grows with use and only a restart returns it --------------------------------------

# The fake world is idle 60 s everywhere, under the 120 s default.
COMFY_RAM = {
    "match": {"resident": "rtx3090/comfyui"},
    "restart_host_ram_mb": 12288,
    "restart_host_ram_idle": "30s",
    "enforce": True,
}


async def test_idle_container_over_its_host_ram_limit_is_restarted(monkeypatch):
    monkeypatch.setenv("IRIS_AGENT_TOKEN", "agent-tok")
    rtx4060, broker = await make([COMFY_RAM])
    rtx4060.anon["imagegen_comfyui"] = 9000
    await broker.poll_once()
    comfy = next(r for r in broker.residents() if r.id == "rtx3090/comfyui")
    assert comfy.host_ram_mb == 9000 and comfy.policy.next is None  # under the limit
    rtx4060.anon["imagegen_comfyui"] = 14000
    await broker.poll_once()
    v = view(broker, "rtx3090/comfyui")
    assert (v.next, v.note) == (
        "restart",
        "idle and holding 14000 MB of host RAM (over 12288): restart imagegen_comfyui",
    )
    await broker.residency.step()
    assert rtx4060.calls == ["POST http://rtx3090:9110/containers/imagegen_comfyui/restart"]
    assert broker.residency.actions[("rtx3090", "restart", "ok")] == 1


async def test_host_ram_restart_waits_for_idle_and_spares_busy_leased_and_pinned():
    rtx4060, broker = await make([{**COMFY_RAM, "restart_host_ram_idle": "5m"}])
    rtx4060.anon["imagegen_comfyui"] = 14000
    await broker.poll_once()
    v = view(broker, "rtx3090/comfyui")  # idle 60 s of 300
    assert (v.next, v.note) == (None, "holding 14000 MB of host RAM (over 12288): restart once idle 300s")

    rtx4060, broker = await make([COMFY_RAM])
    rtx4060.anon["imagegen_comfyui"], rtx4060.comfy_queue = 14000, 1
    await broker.poll_once()
    assert view(broker, "rtx3090/comfyui").next is None  # a job in the queue
    rtx4060.comfy_queue = 0
    broker.leases.request(LeaseRequest(tenant="t", target="comfyui", node="rtx3090", vram_mb=100))
    await broker.poll_once()
    v = view(broker, "rtx3090/comfyui")
    assert (v.idle_s, v.next) == (0, None)  # under a lease

    rtx4060, broker = await make([{**COMFY_RAM, "keepalive": "always"}])
    rtx4060.anon["imagegen_comfyui"] = 14000
    await broker.poll_once()
    assert view(broker, "rtx3090/comfyui").next is None
    await broker.residency.step()
    assert rtx4060.calls == []


async def test_host_ram_limit_defaults_to_the_tenant_manifest_hint():
    from argus_cortex.gpu_wire import TenantManifest

    rtx4060, broker = await make(
        [{"match": {"resident": "rtx3090/comfyui"}, "idle_kill": "1h", "restart_host_ram_idle": 30}]
    )
    rtx4060.anon["imagegen_comfyui"] = 14000
    await broker.poll_once()
    assert view(broker, "rtx3090/comfyui").next is None  # no limit anywhere
    broker.leases.register(
        TenantManifest(
            tenant="imagegen",
            residents=[
                {"name": "comfyui", "adapter": "comfyui", "container": "imagegen_comfyui", "host_ram_restart_mb": 12288}
            ],
        )
    )
    await broker.poll_once()
    v = view(broker, "rtx3090/comfyui")
    assert (v.enforce, v.next) == (False, "restart")  # reported; acting still needs the rule's enforce


def test_a_policy_with_crons_serialises_for_get_policy():
    import json

    p = Policy.model_validate(
        {
            "windows": [{"name": "w", "cron": "0 1 * * *", "duration": "5h", "tenant": "t", "class": "batch"}],
            "residents": [{"keepalive": {"cron": "0 2 * * *", "duration": "1h"}}],
        }
    )
    data = json.loads(p.model_dump_json(by_alias=True))
    assert data["windows"][0]["cron"] == "0 1 * * *" and data["residents"][0]["keepalive"]["cron"] == "0 2 * * *"
