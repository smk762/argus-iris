"""argus-http: Argus-style services read through a health path.

Two health shapes exist, and both are read:

- top level (argus-lens, voluptas's image-gen-diffusers): ``model_state``, ``busy``, ``idle_s``, ``gpu`` (memory info);
- a ``gpu`` block (agent-composer's sidecars, directly or through rag-chat's ``/api/runtime/residents/<name>``):
  ``{"gpu": {"loaded", "model_state", "idle_s", "free_vram_mb"}}``.

The VRAM figure itself comes from the agent's per-process view of the service's container.
"""

from __future__ import annotations

import httpx

from argus_iris.broker.adapters.base import ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig

_FIELDS = ("loaded", "model_state", "idle_s", "busy")


def parse_health(body: object) -> ServiceReading:
    reading = ServiceReading(up=True)
    if not isinstance(body, dict):
        return reading
    fields: dict = {}
    gpu = body.get("gpu")
    if isinstance(gpu, dict) and any(k in gpu for k in _FIELDS):
        fields.update({k: gpu[k] for k in _FIELDS if k in gpu})
    for key in _FIELDS:  # top level wins: it is the service's own view, not a nested summary
        if key in body:
            fields[key] = body[key]
    reading.busy = fields.get("busy") is True
    idle = fields.get("idle_s")
    reading.idle_s = float(idle) if isinstance(idle, int | float) and not isinstance(idle, bool) else None
    reading.detail = {k: fields[k] for k in ("loaded", "model_state") if k in fields}
    for key in ("status", "vram_status", "model"):
        if key in body:
            reading.detail[key] = body[key]
    return reading


class ArgusHttpAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service

    async def read(self, client: httpx.AsyncClient) -> ServiceReading:
        url = f"{self.service.base_url}{self.service.health_path}"
        try:
            resp = await client.get(url, headers=self.service.headers())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return unreachable(exc)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return parse_health(body)
