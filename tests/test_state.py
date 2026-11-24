"""Composing a service's reading into residents (broker/state.py)."""

from __future__ import annotations

from argus_iris.broker.adapters import ServiceReading
from argus_iris.broker.adapters.base import ModelReading
from argus_iris.broker.config import ServiceConfig
from argus_iris.broker.state import _service_residents


def test_a_busy_ollama_marks_every_model_busy() -> None:
    svc = ServiceConfig.model_validate(
        {"id": "ollama", "node": "rtx4060", "adapter": "ollama", "base_url": "http://rtx4060:11434"}
    )
    reading = ServiceReading(up=True, busy=True, models=[ModelReading("a:1", 1000), ModelReading("b:2", 500)])
    residents = _service_residents("rtx4060", svc, reading, [])
    assert [(r.model, r.busy, r.idle_s) for r in residents] == [("a:1", True, None), ("b:2", True, None)]
