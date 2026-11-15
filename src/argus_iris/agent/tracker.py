"""Per-process idle tracking across samples.

A process is busy in a sample when its VRAM changed or it used the GPU. gothmog looked at VRAM alone, which calls a
busy process with a steady footprint (an Ollama model mid-generation) idle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from argus_iris.agent.nvml import Sample


@dataclass
class Tracked:
    gpu: int
    pid: int
    vram_mb: int
    sm_util_pct: int | None
    first_seen: datetime
    last_busy: float  # monotonic seconds

    def idle_s(self, now: float) -> float:
        return max(0.0, now - self.last_busy)


class ProcessTracker:
    def __init__(self, idle_util_pct: int = 5) -> None:
        self.idle_util_pct = idle_util_pct
        self._records: dict[tuple[int, int], Tracked] = {}

    def update(self, sample: Sample, *, now: float, wall: datetime) -> list[Tracked]:
        """Fold *sample* in and return the processes currently on the GPUs. Gone processes are dropped."""
        device_util = {g.index: g.util_pct for g in sample.gpus}
        seen: set[tuple[int, int]] = set()
        for raw in sample.processes:
            key = (raw.gpu, raw.pid)
            seen.add(key)
            per_proc = sample.proc_util.get(raw.gpu)
            if per_proc is not None:
                util = per_proc.get(raw.pid, 0)
                busy_now = util > self.idle_util_pct
            else:
                # No per-process figure: a busy card means every process on it may be busy. Never call busy idle.
                util = None
                card = device_util.get(raw.gpu)
                busy_now = card is None or card > self.idle_util_pct
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = Tracked(raw.gpu, raw.pid, raw.vram_mb, util, wall, now)
                continue
            if busy_now or rec.vram_mb != raw.vram_mb:
                rec.last_busy = now
            rec.vram_mb = raw.vram_mb
            rec.sm_util_pct = util
        for key in set(self._records) - seen:
            del self._records[key]
        return [self._records[k] for k in sorted(seen)]
