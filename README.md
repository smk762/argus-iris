# argus-iris

VRAM-aware GPU lease broker for the Argus suite: priorities, schedules, keepalive and idle-kill across GPU nodes

Status: observe-only (rollout step 2). Agents report per-process VRAM, idle time and host RAM; the broker composes
them with ComfyUI, Ollama and Argus service readings into residents and a per-node ledger. Nothing is admitted or
evicted yet. See [DESIGN.md](DESIGN.md).

## Run

On each GPU host (see [deploy/agent.compose.yaml](deploy/agent.compose.yaml)):

```bash
IRIS_AGENT_NODE=morpheus IRIS_AGENT_TOKEN=... IRIS_AGENT_CONTAINERS=voluptas_comfyui argus-iris agent   # :9110
```

The agent needs `pid: host`, `cgroup: host`, NVML, the Docker socket and `/sys/fs/cgroup` read-only. Container actions are disabled
unless `IRIS_AGENT_TOKEN` is set, and apply only to containers in `IRIS_AGENT_CONTAINERS`.

The broker, on morpheus (see [deploy/iris.example.yaml](deploy/iris.example.yaml)):

```bash
argus-iris serve --config iris.yaml   # :8120 — /nodes, /residents, /events, /metrics, /health
```

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

## Install

```bash
uv pip install 'argus-iris[server]'   # broker
uv pip install 'argus-iris[agent]'    # node agent
```

## Develop

```bash
make install   # venv + editable install with the "dev" extras
make test
make lint
```

## CI / Release

- **CI** runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow.
- **Release** publishes to PyPI (OIDC trusted publishing) and GHCR on `v*` tags.
- Versioning is derived from git tags via `hatch-vcs` — tag `vX.Y.Z` to cut a release.

This repo was scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template).
Run `copier update` to pull template changes (CI, release, tooling).
