"""argus-http: Argus-style services (lens, MisoTTS, agent-composer's XTTS/TTS/Infinity) read through a health path.

Their health payloads differ, so the reading passes through the JSON (and ``vram_status`` when present) as detail;
the VRAM figure comes from the agent's per-process view of the service's container.
"""

from __future__ import annotations

import httpx

from argus_iris.broker.adapters.base import ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig


class ArgusHttpAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service

    async def read(self, client: httpx.AsyncClient) -> ServiceReading:
        try:
            resp = await client.get(f"{self.service.base_url}{self.service.health_path}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return unreachable(exc)
        try:
            body = resp.json()
        except ValueError:
            body = None
        detail: dict = {}
        if isinstance(body, dict):
            for key in ("vram_status", "status", "model", "loaded"):
                if key in body:
                    detail[key] = body[key]
        return ServiceReading(up=True, detail=detail)
