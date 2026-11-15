from __future__ import annotations

from pathlib import Path

import pytest

from argus_iris.agent import procfs

CID = "0d6d7c8d242e9e49ac9dc43ecf19af0a8078e3b66c144c7cff4d1ae5ff081e38"


@pytest.mark.parametrize(
    "text",
    [
        f"0::/system.slice/docker-{CID}.scope\n",
        f"0::/docker/{CID}\n",
        f"0::/kubepods/besteffort/pod1/cri-containerd-{CID}.scope\n",
        f"0::/machine.slice/libpod-{CID}.scope/container\n",
        f"12:memory:/docker/{CID}\n0::/\n",  # cgroup v1 hybrid
    ],
)
def test_container_id_from_cgroup(text: str) -> None:
    assert procfs.container_id_from_cgroup(text) == CID


@pytest.mark.parametrize("text", ["0::/user.slice/user-1000.slice/session-784.scope\n", "0::/\n", ""])
def test_host_process_has_no_container(text: str) -> None:
    assert procfs.container_id_from_cgroup(text) is None


def test_cgroup_v2_path() -> None:
    assert procfs.cgroup_v2_path(f"0::/system.slice/docker-{CID}.scope\n") == f"/system.slice/docker-{CID}.scope"
    assert procfs.cgroup_v2_path("12:memory:/x\n") is None
    # Seen from a private cgroup namespace (agent without `cgroup: host`): unusable, so refused.
    assert procfs.cgroup_v2_path("0::/../../init.scope\n") is None


def test_read_host_memory(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       31722496 kB\nMemFree:  100 kB\nMemAvailable:   18841600 kB\n")
    mem = procfs.read_host_memory(meminfo)
    assert mem is not None
    assert (mem.total_mb, mem.available_mb) == (30979, 18400)
    assert procfs.read_host_memory(tmp_path / "missing") is None


def test_read_anon_mb(tmp_path: Path) -> None:
    cg = tmp_path / "system.slice" / f"docker-{CID}.scope"
    cg.mkdir(parents=True)
    (cg / "memory.stat").write_text(f"anon {5552 * 1024 * 1024}\nfile 999999999\nanon_thp 0\n")
    assert procfs.read_anon_mb(tmp_path, f"/system.slice/docker-{CID}.scope") == 5552
    assert procfs.read_anon_mb(tmp_path, "/nope") is None
