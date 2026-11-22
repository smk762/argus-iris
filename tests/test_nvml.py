"""NvmlSampler against a fake pynvml: init and shutdown stay balanced."""

from __future__ import annotations

import sys
import types

from argus_iris.agent.nvml import NvmlSampler


class _NVMLError(Exception):
    pass


def _fake_pynvml(monkeypatch) -> types.SimpleNamespace:
    calls = types.SimpleNamespace(init=0, shutdown=0)
    mod = types.ModuleType("pynvml")
    mod.NVMLError = _NVMLError

    def init() -> None:
        calls.init += 1

    def shutdown() -> None:
        calls.shutdown += 1

    def count() -> int:
        raise _NVMLError("GPU is lost")  # a persistent failure, every poll

    mod.nvmlInit, mod.nvmlShutdown, mod.nvmlDeviceGetCount = init, shutdown, count
    monkeypatch.setitem(sys.modules, "pynvml", mod)
    return calls


def test_failed_samples_do_not_leak_nvml_inits(monkeypatch) -> None:
    calls = _fake_pynvml(monkeypatch)
    sampler = NvmlSampler()
    errors = [sampler.sample().error for _ in range(3)]
    sampler.close()
    assert all(e and "GPU is lost" in e for e in errors)
    assert (calls.init, calls.shutdown) == (3, 3)
