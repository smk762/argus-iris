"""The Ollama adapter: /api/ps parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from argus_iris.broker.adapters.ollama import OllamaAdapter, _parse_time
from argus_iris.broker.config import ServiceConfig

SERVICE = ServiceConfig.model_validate(
    {"id": "ollama", "node": "rtx4060", "adapter": "ollama", "base_url": "http://rtx4060:11434"}
)


def test_parse_time_keeps_a_numeric_offset() -> None:
    # Go marshals a non-UTC host's time with its offset, after the nanoseconds.
    got = [
        _parse_time("2024-06-01T10:00:00.123456789-07:00"),
        _parse_time("2024-06-01T10:00:00.5+05:30"),
        _parse_time("2024-06-01T10:00:00.123456789Z"),
    ]
    assert got == [
        datetime(2024, 6, 1, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-7))),
        datetime(2024, 6, 1, 10, 0, 0, 500000, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        datetime(2024, 6, 1, 10, 0, 0, 123456, tzinfo=UTC),
    ]


def _client(ps_body) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=ps_body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        "proxy error",
        {"models": ["a string, not a model"]},
        {"models": [{"name": "x:1", "size": "lots"}]},
        {"models": [{"name": "x:1", "size_vram": {"a": 1}}]},
    ],
    ids=["list", "string", "entry", "size", "size_vram"],
)
async def test_a_malformed_ps_body_is_an_unreachable_reading(body) -> None:
    async with _client(body) as client:
        reading = await OllamaAdapter(SERVICE).read(client)
    assert not reading.up and reading.error


async def test_a_well_formed_ps_body_still_reads() -> None:
    body = {"models": [{"name": "x:1", "size": 2 << 30, "size_vram": 1 << 30, "expires_at": "2024-06-01T10:00:00Z"}]}
    async with _client(body) as client:
        reading = await OllamaAdapter(SERVICE).read(client)
    assert reading.up and [(m.name, m.vram_mb, m.detail["partial_offload"]) for m in reading.models] == [
        ("x:1", 1024, True)
    ]
