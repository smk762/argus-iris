"""iris-agent: per-host GPU sampler (NVML + cgroups) and allow-listed container actions.

Behind the ``[agent]`` extra. Run with ``argus-iris agent``; it needs ``pid: host``, NVML, the Docker socket and the
host's ``/sys/fs/cgroup`` read-only.
"""
