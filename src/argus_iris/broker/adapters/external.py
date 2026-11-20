"""external: a service outside the suite, read for up/down only.

Immich's machine-learning server is the first: Immich calls it, no Argus stage does, and it speaks Immich's own
``/predict``, not the resident contract. Its health route (``GET /ping``, plain ``pong``) says nothing about models,
work in flight or idle time, so a 2xx on ``health_path`` is all this adapter reads: the VRAM comes from the agent's
view of the service's container, and the idle time from the agent's tracker (no memory change and no utilisation).
"""

from __future__ import annotations

import httpx

from argus_iris.broker.adapters.base import ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig


class ExternalAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service

    async def read(self, client: httpx.AsyncClient) -> ServiceReading:
        try:
            resp = await client.get(
                f"{self.service.base_url}{self.service.health_path}", headers=self.service.headers()
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return unreachable(exc)
        return ServiceReading(up=True)
