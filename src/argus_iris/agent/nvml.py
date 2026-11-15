"""NVML sampling: card memory and utilisation, per-process VRAM and SM utilisation.

Synchronous (pynvml is); the agent calls :meth:`NvmlSampler.sample` from a worker thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from argus_iris.models import MIB, GpuInfo


@dataclass(frozen=True)
class RawProcess:
    gpu: int
    pid: int
    vram_mb: int


@dataclass
class Sample:
    gpus: list[GpuInfo] = field(default_factory=list)
    processes: list[RawProcess] = field(default_factory=list)
    # pid -> max SM utilisation since the last sample. ``None`` per GPU when the driver does not report it.
    proc_util: dict[int, dict[int, int] | None] = field(default_factory=dict)
    error: str | None = None


def _text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


class NvmlSampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._last_util_ts: dict[int, int] = {}

    def _init(self):
        import pynvml  # noqa: PLC0415 - behind the [agent] extra

        if not self._ready:
            pynvml.nvmlInit()
            self._ready = True
        return pynvml

    def close(self) -> None:
        with self._lock:
            if self._ready:
                import pynvml  # noqa: PLC0415

                pynvml.nvmlShutdown()
                self._ready = False

    def sample(self) -> Sample:
        with self._lock:
            try:
                nv = self._init()
                return self._sample(nv)
            except Exception as exc:  # noqa: BLE001 - any NVML failure becomes a reported error, not a crash
                self._ready = False  # re-init next time (driver reload, container restart)
                return Sample(error=f"{type(exc).__name__}: {exc}")

    def _sample(self, nv) -> Sample:
        out = Sample()
        for idx in range(nv.nvmlDeviceGetCount()):
            handle = nv.nvmlDeviceGetHandleByIndex(idx)
            mem = nv.nvmlDeviceGetMemoryInfo(handle)
            try:
                util = int(nv.nvmlDeviceGetUtilizationRates(handle).gpu)
            except nv.NVMLError:
                util = None
            try:
                major, minor = nv.nvmlDeviceGetCudaComputeCapability(handle)
                cc, bf16 = f"{major}.{minor}", major >= 8
            except nv.NVMLError:
                cc, bf16 = None, None
            out.gpus.append(
                GpuInfo(
                    index=idx,
                    name=_text(nv.nvmlDeviceGetName(handle)),
                    uuid=_text(nv.nvmlDeviceGetUUID(handle)),
                    total_mb=int(mem.total) // MIB,
                    free_mb=int(mem.free) // MIB,
                    used_mb=int(mem.used) // MIB,
                    util_pct=util,
                    compute_capability=cc,
                    bf16=bf16,
                )
            )
            # Compute (CUDA) and graphics (the desktop's Xorg) both hold VRAM; a process can be in both lists.
            vram: dict[int, int] = {}
            for getter in (nv.nvmlDeviceGetComputeRunningProcesses, nv.nvmlDeviceGetGraphicsRunningProcesses):
                try:
                    procs = getter(handle)
                except nv.NVMLError:
                    continue
                for p in procs:
                    used = int(p.usedGpuMemory or 0) // MIB  # None where the driver cannot attribute (WSL)
                    vram[p.pid] = max(vram.get(p.pid, 0), used)
            out.processes.extend(RawProcess(gpu=idx, pid=pid, vram_mb=mb) for pid, mb in vram.items())
            out.proc_util[idx] = self._process_util(nv, idx, handle)
        return out

    def _process_util(self, nv, idx: int, handle) -> dict[int, int] | None:
        try:
            samples = nv.nvmlDeviceGetProcessUtilization(handle, self._last_util_ts.get(idx, 0))
        except nv.NVMLError_NotFound:
            return {}  # no samples since the last call: nothing ran
        except nv.NVMLError:
            return None  # not supported on this card/driver
        util: dict[int, int] = {}
        for s in samples:
            util[s.pid] = max(util.get(s.pid, 0), int(s.smUtil))
            self._last_util_ts[idx] = max(self._last_util_ts.get(idx, 0), int(s.timeStamp))
        return util
