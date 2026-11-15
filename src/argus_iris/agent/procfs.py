"""Reading /proc and cgroup v2 files: container IDs, host memory, container anonymous memory.

Pure functions over paths, so tests point them at a temp directory.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus_iris.models import MIB, HostMemory

# A container ID is 64 hex characters. Seen as:
#   0::/system.slice/docker-<id>.scope               (systemd cgroup driver)
#   0::/docker/<id>                                  (cgroupfs driver)
#   0::/kubepods/.../cri-containerd-<id>.scope       (containerd)
#   0::/.../libpod-<id>.scope                        (podman)
_CONTAINER_ID = re.compile(r"(?:^|[/-])([0-9a-f]{64})(?:\.scope)?(?:/|$)")


def read_cgroup(proc_root: Path, pid: int) -> str | None:
    try:
        return (proc_root / str(pid) / "cgroup").read_text()
    except OSError:
        return None


def cgroup_v2_path(cgroup_text: str) -> str | None:
    """The unified-hierarchy path (the ``0::`` line), e.g. ``/system.slice/docker-<id>.scope``.

    ``None`` for a path relative to a private cgroup namespace (``/../..``): the agent then runs without
    ``cgroup: host`` and the path cannot be found under the host's ``/sys/fs/cgroup``.
    """
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            return path if path and not path.startswith("/..") else None
    return None


def container_id_from_cgroup(cgroup_text: str) -> str | None:
    """The container ID a process belongs to, or ``None`` for a host process."""
    for line in cgroup_text.splitlines():
        path = line.split(":", 2)[-1]
        match = _CONTAINER_ID.search(path)
        if match:
            return match.group(1)
    return None


def read_host_memory(meminfo: Path) -> HostMemory | None:
    try:
        text = meminfo.read_text()
    except OSError:
        return None
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0])  # kB
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return HostMemory(total_mb=fields["MemTotal"] // 1024, available_mb=fields["MemAvailable"] // 1024)


def read_anon_mb(cgroup_root: Path, cgroup_path: str) -> int | None:
    """``anon`` from a cgroup's ``memory.stat``, in MiB.

    Anonymous memory is what ComfyUI's offloaded weights occupy and what only a restart returns (voluptas #16); page
    cache is reclaimable and not counted.
    """
    stat = cgroup_root / cgroup_path.lstrip("/") / "memory.stat"
    try:
        text = stat.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "anon" and value.strip().isdigit():
            return int(value) // MIB
    return None
