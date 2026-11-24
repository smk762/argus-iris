from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

from argus_iris.broker.config import ServiceConfig


@dataclass
class ModelReading:
    """A model the service itself reports as loaded (Ollama). Services that cannot list theirs report none."""

    name: str
    vram_mb: int
    expires_at: datetime | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class ServiceReading:
    up: bool
    error: str | None = None
    busy: bool = False  # work in flight, whoever submitted it
    idle_s: float | None = None  # the service's own "since last used", better than the agent's process view
    queue_running: int | None = None
    queue_pending: int | None = None
    models: list[ModelReading] = field(default_factory=list)
    available: list[str] | None = None  # models the service can load without a pull (Ollama /api/tags); None: unknown
    detail: dict = field(default_factory=dict)


class Adapter(Protocol):
    service: ServiceConfig

    async def read(self, client: httpx.AsyncClient) -> ServiceReading: ...


def unreachable(exc: Exception) -> ServiceReading:
    return ServiceReading(up=False, error=f"{type(exc).__name__}: {exc}"[:300])
