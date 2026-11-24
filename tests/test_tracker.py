from __future__ import annotations

from datetime import UTC, datetime

from argus_iris.agent.nvml import RawProcess, Sample
from argus_iris.agent.tracker import ProcessTracker
from argus_iris.models import GpuInfo

WALL = datetime(2026, 11, 15, tzinfo=UTC)


def gpu(util: int | None) -> GpuInfo:
    return GpuInfo(index=0, name="RTX 3090", uuid="GPU-x", total_mb=24576, free_mb=10000, used_mb=14576, util_pct=util)


def sample(vram: int, *, proc_util: dict[int, int] | None, card_util: int | None = 0) -> Sample:
    return Sample(gpus=[gpu(card_util)], processes=[RawProcess(0, 42, vram)], proc_util={0: proc_util})


def test_steady_vram_and_no_util_accumulates_idle() -> None:
    t = ProcessTracker()
    t.update(sample(1000, proc_util={}), now=0, wall=WALL)
    (rec,) = t.update(sample(1000, proc_util={}), now=30, wall=WALL)
    assert rec.idle_s(30) == 30


def test_util_resets_idle_even_with_steady_vram() -> None:
    # A VRAM-only rule would call this idle: an Ollama model generating at a fixed footprint.
    t = ProcessTracker()
    t.update(sample(1000, proc_util={}), now=0, wall=WALL)
    (rec,) = t.update(sample(1000, proc_util={42: 80}), now=30, wall=WALL)
    assert rec.idle_s(30) == 0
    assert rec.sm_util_pct == 80


def test_vram_change_resets_idle() -> None:
    t = ProcessTracker()
    t.update(sample(1000, proc_util={}), now=0, wall=WALL)
    (rec,) = t.update(sample(2000, proc_util={}), now=30, wall=WALL)
    assert rec.idle_s(30) == 0


def test_without_per_process_util_a_busy_card_keeps_everyone_busy() -> None:
    t = ProcessTracker()
    t.update(sample(1000, proc_util=None), now=0, wall=WALL)
    (rec,) = t.update(sample(1000, proc_util=None, card_util=90), now=30, wall=WALL)
    assert rec.idle_s(30) == 0
    assert rec.sm_util_pct is None
    (rec,) = t.update(sample(1000, proc_util=None, card_util=0), now=60, wall=WALL)
    assert rec.idle_s(60) == 30


def test_gone_processes_are_dropped() -> None:
    t = ProcessTracker()
    t.update(sample(1000, proc_util={}), now=0, wall=WALL)
    assert t.update(Sample(gpus=[gpu(0)], processes=[], proc_util={0: {}}), now=5, wall=WALL) == []
