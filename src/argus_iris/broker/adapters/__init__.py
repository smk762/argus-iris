"""Service adapters: read what a service holds in VRAM (and, from rollout step 4, evict and warm it).

Observe-only for now: every adapter only reads.
"""

from __future__ import annotations

from argus_iris.broker.adapters.base import Adapter, ModelReading, ServiceReading
from argus_iris.broker.adapters.comfyui import ComfyUIAdapter
from argus_iris.broker.adapters.http import ArgusHttpAdapter
from argus_iris.broker.adapters.ollama import OllamaAdapter
from argus_iris.broker.config import ServiceConfig

__all__ = ["Adapter", "ModelReading", "ServiceReading", "build_adapter"]


def build_adapter(service: ServiceConfig) -> Adapter:
    match service.adapter:
        case "comfyui":
            return ComfyUIAdapter(service)
        case "ollama":
            return OllamaAdapter(service)
        case "argus-http":
            return ArgusHttpAdapter(service)
    raise ValueError(f"unknown adapter {service.adapter!r}")  # pragma: no cover - the config validates it
