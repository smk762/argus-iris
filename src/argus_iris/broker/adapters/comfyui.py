"""ComfyUI: up/down and queue depth.

Stock ComfyUI has no endpoint listing loaded models, and ``POST /free`` unloads them all, so iris treats ComfyUI as a
single resident whose size comes from the agent's per-process VRAM. A non-empty queue iris did not grant is
clients that take no lease (unannounced load); it marks the resident busy.
"""

from __future__ import annotations

import asyncio

import httpx

from argus_iris.broker.adapters.base import ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig
from argus_iris.models import MIB


class ComfyUIAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service

    async def read(self, client: httpx.AsyncClient) -> ServiceReading:
        base, headers = self.service.base_url, self.service.headers()
        try:
            stats_resp, queue_resp = await asyncio.gather(
                client.get(f"{base}/system_stats", headers=headers), client.get(f"{base}/queue", headers=headers)
            )
            stats_resp.raise_for_status()
            queue_resp.raise_for_status()
            stats, queue = stats_resp.json(), queue_resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return unreachable(exc)
        running = len(queue.get("queue_running") or [])
        pending = len(queue.get("queue_pending") or [])
        detail: dict = {}
        devices = stats.get("devices") or []
        if devices:
            dev = devices[0]
            for key in ("vram_total", "vram_free", "torch_vram_total", "torch_vram_free"):
                if isinstance(dev.get(key), int | float):
                    detail[f"{key}_mb"] = int(dev[key]) // MIB
        version = (stats.get("system") or {}).get("comfyui_version")
        if version:
            detail["comfyui_version"] = version
        return ServiceReading(
            up=True,
            busy=running + pending > 0,
            queue_running=running,
            queue_pending=pending,
            detail=detail,
        )
