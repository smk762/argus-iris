"""A minimal Docker Engine API client over the Unix socket: inspect, start, stop, restart.

Only what the agent needs, so it does not pull in the Docker SDK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from argus_iris.models import ContainerAction

DEFAULT_SOCKET = "/var/run/docker.sock"
# Docker's container name grammar (an ID matches it too). It has no "/", "?" or "#", so a ref cannot step out of
# /containers/{ref}/ to another Engine endpoint on the root-equivalent socket.
_REF = re.compile(r"/?[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class DockerError(RuntimeError):
    pass


def _checked(ref: str) -> str:
    if not _REF.fullmatch(ref):
        raise DockerError(f"not a container name or ID: {ref!r}")
    return ref.lstrip("/")


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    service: str  # compose service label, else the container name
    running: bool
    pid: int | None  # main process, host PID namespace


def parse_inspect(data: dict) -> ContainerInfo:
    name = (data.get("Name") or "").lstrip("/") or data.get("Id", "")[:12]
    labels = (data.get("Config") or {}).get("Labels") or {}
    state = data.get("State") or {}
    pid = state.get("Pid") or None
    return ContainerInfo(
        id=data.get("Id", ""),
        name=name,
        service=labels.get("com.docker.compose.service") or name,
        running=bool(state.get("Running")),
        pid=pid,
    )


class DockerClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://docker",
            timeout=httpx.Timeout(10.0, read=40.0),  # stop/restart wait up to the container's stop timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def inspect(self, ref: str) -> ContainerInfo | None:
        """A container by ID or name; ``None`` when it does not exist."""
        try:
            resp = await self._client.get(f"/containers/{_checked(ref)}/json")
        except httpx.HTTPError as exc:
            raise DockerError(f"docker unreachable: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DockerError(f"inspect {ref}: HTTP {resp.status_code}")
        return parse_inspect(resp.json())

    async def act(self, name: str, action: ContainerAction) -> str:
        """Run *action*; returns ``"done"`` or ``"unchanged"`` (already in that state)."""
        try:
            resp = await self._client.post(f"/containers/{_checked(name)}/{action.value}")
        except httpx.HTTPError as exc:
            raise DockerError(f"docker unreachable: {exc}") from exc
        if resp.status_code == 204:
            return "done"
        if resp.status_code == 304:
            return "unchanged"
        detail = resp.text[:200]
        raise DockerError(f"{action.value} {name}: HTTP {resp.status_code} {detail}")
