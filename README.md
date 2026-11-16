# argus-iris

VRAM-aware GPU lease broker for the Argus suite: priorities, schedules, keepalive and idle-kill across GPU nodes

Status: observe-only (rollout step 2). Agents report per-process VRAM, idle time and host RAM; the broker composes
them with ComfyUI, Ollama and Argus service readings into residents and a per-node ledger. Nothing is admitted or
evicted yet; the one action is a manual warm (below). See [DESIGN.md](DESIGN.md).

## Run

For the compose files, copy [deploy/.env.example](deploy/.env.example) to `deploy/.env` and fill in the host's
section. The broker needs `RAG_CHAT_API_KEY` to read agent-composer's residents on dragon.

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

One manual action exists: bring a service back up. If its health check fails, the node's agent starts its container
(it must be on that agent's `IRIS_AGENT_CONTAINERS`), and iris waits for it to answer:

```bash
curl -X POST -H "Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN" http://morpheus:8120/services/comfyui/warm
```

It needs `ARGUS_GPU_BROKER_TOKEN` (callers) and `IRIS_AGENT_TOKEN` (matching the agents') set on the broker.

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
