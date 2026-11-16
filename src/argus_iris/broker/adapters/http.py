"""argus-http: Argus-style services read through a health path.

The resident contract is argus-cortex's :class:`~argus_cortex.gpu_wire.ResidentHealth`: top-level ``model_state``,
``busy`` (a bool), ``idle_s``, and an optional memory-only ``gpu`` block. A body that validates is read as that.

Services not yet on the contract are still read, the older shapes best-effort, and logged once as off-contract:

- top level with a non-bool ``busy`` (voluptas's Miso: an object while working, null when idle);
- a ``gpu`` block carrying the state (argus-lens): ``{"gpu": {"loaded", "model_state", "idle_s"}}``.

The VRAM figure itself comes from the agent's per-process view of the service's container, never from here.
"""

from __future__ import annotations

import httpx
import structlog
from argus_cortex.gpu_wire import ResidentHealth
from pydantic import ValidationError

from argus_iris.broker.adapters.base import ServiceReading, unreachable
from argus_iris.broker.config import ServiceConfig

log = structlog.get_logger("argus_iris.adapters.http")

_FIELDS = ("loaded", "model_state", "idle_s", "busy")
_PASSTHROUGH = ("status", "vram_status", "model")


def _why(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in exc.errors())[:300]


def parse_health(body: object) -> ServiceReading:
    """Read a ``/health`` body. ``detail["off_contract"]`` says why it is not a ``ResidentHealth``, when it isn't."""
    reading = ServiceReading(up=True)
    if not isinstance(body, dict):
        reading.detail = {"off_contract": "body is not a JSON object"}
        return reading
    try:
        health = ResidentHealth.model_validate(body)
    except ValidationError as exc:
        _parse_legacy(body, reading)
        reading.detail["off_contract"] = _why(exc)
    else:
        reading.busy = health.busy
        reading.idle_s = health.idle_s
        reading.detail = {"model_state": health.model_state}
    for key in _PASSTHROUGH:
        if key in body:
            reading.detail[key] = body[key]
    return reading


def _parse_legacy(body: dict, reading: ServiceReading) -> None:
    fields: dict = {}
    gpu = body.get("gpu")
    if isinstance(gpu, dict) and any(k in gpu for k in _FIELDS):
        fields.update({k: gpu[k] for k in _FIELDS if k in gpu})
    for key in _FIELDS:  # top level wins: it is the service's own view, not a nested summary
        if key in body:
            fields[key] = body[key]
    # Anything truthy is work in flight: Miso reports an object while busy. Reading it as idle would let an idle
    # policy unload a model mid-request.
    reading.busy = bool(fields.get("busy"))
    idle = fields.get("idle_s")
    usable = isinstance(idle, int | float) and not isinstance(idle, bool) and idle >= 0
    reading.idle_s = None if reading.busy or not usable else float(idle)
    reading.detail = {k: fields[k] for k in ("loaded", "model_state") if k in fields}


class ArgusHttpAdapter:
    def __init__(self, service: ServiceConfig) -> None:
        self.service = service
        self._off_contract: str | None = None  # the last reason logged, so a steady state is logged once

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
        reading = parse_health(body)
        why = reading.detail.get("off_contract")
        if why != self._off_contract:
            if why:
                log.warning("resident_off_contract", service=self.service.id, reason=why)
            else:
                log.info("resident_on_contract", service=self.service.id)
            self._off_contract = why
        return reading
