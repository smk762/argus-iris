"""Ollama: the loaded models from ``/api/ps``, with their VRAM share and keep-alive expiry."""

from __future__ import annotations

from datetime import datetime

import httpx

from argus_iris.broker.adapters.base import ModelReading, ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig
from argus_iris.models import MIB


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Ollama sends RFC 3339 with nanoseconds; Python takes microseconds at most.
        head, dot, tail = raw.partition(".")
        if dot:
            digits = "".join(c for c in tail if c.isdigit())
            zone = tail[len(digits) :]
            raw = f"{head}.{digits[:6]}{zone}"
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


class OllamaAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service

    async def read(self, client: httpx.AsyncClient) -> ServiceReading:
        try:
            resp = await client.get(f"{self.service.base_url}/api/ps", headers=self.service.headers())
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return unreachable(exc)
        models = []
        for m in data.get("models") or []:
            size, size_vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            models.append(
                ModelReading(
                    name=m.get("name") or m.get("model") or "?",
                    vram_mb=size_vram // MIB,
                    expires_at=_parse_time(m.get("expires_at")),
                    # size_vram < size: Ollama split the model across GPU and CPU because it did not fit.
                    detail={"size_mb": size // MIB, "partial_offload": 0 < size_vram < size},
                )
            )
        return ServiceReading(up=True, models=models)
