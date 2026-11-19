# argus-iris

VRAM-aware GPU lease broker for the Argus suite: priorities, schedules, keepalive and idle-kill across GPU nodes

Status: rollout step 3 (leases and admission). Agents report per-process VRAM, idle time and host RAM; the broker
composes them with ComfyUI, Ollama and Argus service readings into residents and a per-node ledger, and serves the lease
API that argus-cortex's `BrokerCoordinator` calls. With `admission: observe` (the default) it grants every lease at once
and records whether it would have fit; with `admission: enforce` it queues what does not fit, spills to the next node,
and evicts idle residents to make room. A residency policy (`IRIS_POLICY`, see [deploy/policy.example.yaml](deploy/policy.example.yaml)) adds
idle-kill, keepalive pins and restarts per resident (step 4); each rule only reports what it would do until it says
`enforce: true`, so residents are handed over one at a time. See [DESIGN.md](DESIGN.md).

## Run

For the compose files, copy [deploy/.env.example](deploy/.env.example) to `deploy/.env` and fill in the host's
section. The broker needs `RAG_CHAT_API_KEY` to read agent-composer's residents on dragon.

```bash
docker compose up -d              # broker + this host's agent (morpheus)
docker compose up -d iris-agent   # agent only (dragon, jarrah)
docker compose build              # ghcr.io/smk762/argus-iris:${IRIS_TAG} from this checkout (set IRIS_TAG=local)
```

[compose.yaml](compose.yaml) includes [deploy/broker.compose.yaml](deploy/broker.compose.yaml) and
[deploy/agent.compose.yaml](deploy/agent.compose.yaml) with `deploy/.env`; each also runs on its own with `-f`.

On each GPU host (see [deploy/agent.compose.yaml](deploy/agent.compose.yaml)):

```bash
IRIS_AGENT_NODE=morpheus IRIS_AGENT_TOKEN=... IRIS_AGENT_CONTAINERS=voluptas_comfyui argus-iris agent   # :9110
```

The agent needs `pid: host`, `cgroup: host`, NVML, the Docker socket and `/sys/fs/cgroup` read-only. Container actions are disabled
unless `IRIS_AGENT_TOKEN` is set, and apply only to containers in `IRIS_AGENT_CONTAINERS`.

The broker, on morpheus (see [deploy/iris.example.yaml](deploy/iris.example.yaml)):

```bash
argus-iris serve --config iris.yaml   # :8120 — /nodes, /residents, /leases, /tenants, /events, /metrics, /health
```

Callers take leases with `ARGUS_GPU_COORDINATOR=broker` and `ARGUS_GPU_BROKER_URL=http://<broker>:8120` (argus-cortex
0.7+). A service target goes to the configured services that serve it, in config order (`ollama-dragon` before
`ollama-jarrah`), preferring a node where the model is already loaded and skipping services that are down; the grant
carries that node and its `base_url`. When `ARGUS_GPU_BROKER_TOKEN` is set on the broker, the lease routes require it
as a bearer token; unset, they are open. `GET /leases` lists what is queued or granted (`?all=true` adds the last ten
minutes' finished leases).

One manual action exists: bring a service back up. If its health check fails, the node's agent starts its container
(it must be on that agent's `IRIS_AGENT_CONTAINERS`), and iris waits for it to answer:

```bash
curl -X POST -H "Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN" http://morpheus:8120/services/comfyui/warm
```

It needs `ARGUS_GPU_BROKER_TOKEN` (callers) and `IRIS_AGENT_TOKEN` (matching the agents') set on the broker.

The other manual action frees a resident's VRAM through its adapter: Ollama gets `keep_alive: 0` for that model,
ComfyUI `POST /free`, an argus-http service its `evict_path` (GPU to CPU, kept warm) or else its `unload_path`. A busy
resident is refused with 409; iris then waits until the agent sees the memory come back (`evict_confirm_s`, 30 s) and
reports what the resident still holds (a CUDA context, say, which only a restart frees):

```bash
curl -X POST -H "Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN" \
  "http://morpheus:8120/residents/dragon/xtts/evict?mode=auto"   # mode: auto | evict | unload
```

The broker also serves a dashboard at `http://<broker>:8120/ui/` (`/` redirects there): VRAM per node split into
residents, unattributed, held-back and admissible memory, plus residents with idle times, services with a warm button, residents with an evict button,
and the node's leases (marked "over" where admission would have refused them).
It updates live from `/events`. To warm from the page, enter `ARGUS_GPU_BROKER_TOKEN` under "Broker token". It is kept
in that tab's session storage only.

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
