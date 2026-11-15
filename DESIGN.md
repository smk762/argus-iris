# argus-iris design

Status: draft, 2026-11-15. Nothing is implemented yet; this is the plan the first releases build to.

## Problem

Three GPUs are shared by the Argus services and by repos outside the suite. Today nothing coordinates them:

- Only argus-lens and argus-curator take a lease (`argus_cortex.gpu.LeaseCoordinator`), and that lease is a host-local
  `flock` plus a free-VRAM snapshot. It cannot see another host, holds no reservation, and has no priority.
- ComfyUI, Ollama, a2f, MisoTTS, darkroom, atlas, proof and forge never take it.
- darkroom refuses a second generation with 409. mint and mask poll it every 5 s for up to 600 s, which is a hand-made
  queue with no priority.
- Idle and keep-alive policy is spread across repos and pulls in different directions: voluptas's `comfyui-cleanup`
  watcher, MisoTTS's `MISO_KEEP_ALIVE_GPU`, agent-composer's per-request `keep_alive=15m`, lens's `ARGUS_IDLE_UNLOAD_S`.
- The only OOM handling is lens's backoff retry.

argus-iris is one broker that decides **whether** a GPU job may start now, **on which node**, and **what has to be
unloaded first**. It does not run the work. The work stays in ComfyUI, Ollama, a2f and in-process models; callers ask
iris first and release when done.

## Nodes

| Node | GPU | VRAM | Role | Tenants |
|---|---|---|---|---|
| morpheus | RTX 3090 | 24 GB | ComfyUI workhorse, Argus dev stack | voluptas (ComfyUI, image-gen-diffusers, NudeNet, MisoTTS), lens, curator, desktop display |
| dragon | RTX 4060 | 8 GB | Ollama primary | agent-composer (incl. SingGuard on :9150), ai-code-auditor |
| jarrah | GTX 1070 Ti (sm_61, no bf16) | 8 GB | Ollama overflow | mithrandir, Immich ML |

Host clocks differ (morpheus local time, dragon and jarrah UTC). Iris stores and compares all times in UTC, and every
schedule window names its timezone explicitly.

## Why not Taskiq

A task queue answers "which worker runs this task". Iris answers "may this run now, where, and what must be evicted
first". ComfyUI and Ollama are not Taskiq workers, Taskiq's Redis brokers have no real priority, and its scheduler only
submits on a cron. The admission logic would be written by hand either way, so iris keeps a small purpose-built
scheduler and uses `croniter` for windows. No Redis.

## Concepts

- **Node**: one GPU on one host. Described by VRAM, compute capability, bf16 support, and the services that run on it.
  Each has a reserved amount for things iris does not manage (for example morpheus's desktop display).
- **Resident**: a model held in VRAM by a service: a ComfyUI checkpoint, an Ollama model, the a2f engine, a lens
  captioner. Read and evicted through an **adapter**.
- **Tenant**: a service that uses a GPU. **Managed** tenants take leases; **observed** tenants do not, but iris sees
  their VRAM through the node agent and evicts them through adapters.
- **Lease**: permission to use VRAM on a node for a while. Fields: tenant, kind, target, VRAM (MB), priority class,
  preemptible, TTL with heartbeat, optional `not_before` and `deadline`. A grant names the node and, for service
  targets, the `base_url` to call.

## Components

```
 callers (cortex BrokerCoordinator)            Studio page        Prometheus
        │  POST /leases, heartbeat, release          │ /events SSE       │ /metrics
        ▼                                            ▼                   ▼
 ┌──────────────────────────── argus-iris broker (:8110) ─────────────────────────┐
 │ registry (tenant manifests)   scheduler (classes, aging, affinity, backfill)    │
 │ ledger per node               policy (windows, keepalive, idle-kill)            │
 │ adapters: comfyui · ollama · argus-http · immich · container                    │
 │ history: SQLite (WAL) on a volume                                               │
 └──────────────┬──────────────────────────┬──────────────────────────┬────────────┘
                │ GET /snapshot            │                          │
        iris-agent @ morpheus      iris-agent @ dragon        iris-agent @ jarrah
        NVML + cgroup → service    NVML + cgroup → service    NVML + cgroup → service
        container start/stop       container start/stop       container start/stop
```

### Broker

One instance. Holds the authoritative state in memory and appends lease and job events to SQLite for history and stats.
After a restart, clients re-attach on their next heartbeat (the heartbeat carries the lease's fields), so no external
queue store is needed.

### Node agent (`iris-agent`)

Runs on every GPU host, in the same package behind an `[agent]` extra. Salvaged from gothmog's `gpu-process-exporter`,
modernised:

- NVML via `nvidia-ml-py`: total, free and per-process VRAM; GPU utilisation.
- PID to compose service through `/proc/<pid>/cgroup` and one Docker lookup per new container, not a scan of every
  container on every poll.
- Idle time per process (VRAM unchanged and utilisation near zero).
- Host RAM, because ComfyUI's host-RAM growth once hung morpheus (voluptas #16).
- Container actions the broker may ask for: `start`, `stop`, `restart`. Needed because voluptas's watcher can leave
  ComfyUI stopped. Needs the Docker socket, so the agent is a privileged service and listens on the LAN only behind a
  shared token.

### Adapters

| Adapter | Reads | Evicts | Warms |
|---|---|---|---|
| comfyui | `/system_stats`, `/queue`, `/object_info` for loaded models | `POST /free {unload_models, free_memory}`; container `restart` when host RAM is high and the queue is empty | container `start`, then wait for `/system_stats` |
| ollama | `/api/ps` (`size_vram`, `expires_at`) | `POST /api/generate {model, keep_alive: 0}` | `POST /api/generate {model, keep_alive: <window>}` |
| argus-http | `/health` (`vram_status`) | `POST /admin/unload` (lens has it; others add it) | first request |
| immich | node agent only | set `MACHINE_LEARNING_MODEL_TTL` low; never kill | none |
| container | node agent | `stop` (last resort, opt-in per tenant) | `start` |

Adapters are configured by tenant manifests, not hard-coded per service as gothmog was.

## Admission and OOM avoidance

Per node, the ledger must satisfy:

```
active lease VRAM + measured resident VRAM + unmanaged reserve + headroom  <=  total VRAM
```

NVML free memory is checked as well, to catch tenants iris does not know about. When a lease does not fit:

1. Build an eviction plan from residents that are idle, not pinned, not under an active lease, and lower priority than
   the request, least recently used first.
2. Evict through the adapters and wait for NVML to confirm the memory came back.
3. Admit, or keep the lease queued if the plan cannot free enough.

Unlike the current lease this is a reservation: two admitted leases can never add up to more than the card.

**Learned footprints.** While a lease is active the agent samples the tenant's peak VRAM. Iris keeps a p95 per
(tenant, kind) and uses it whenever it is larger than the declared figure. That replaces hand-kept tables such as lens's
`gpu/capacity.py` and curator's `VRAM_MB`, and catches estimates that are wrong.

## Scheduling

Priority classes, highest first:

| Class | Examples | Preemptible |
|---|---|---|
| `realtime` | mask a2f during a broadcast | never |
| `interactive` | darkroom for a person (safelight, studio), curator `/faces`, SingGuard checks | no |
| `batch` | atlas bakes, proof runs, mint sample runs, lens batches | yes, cooperatively |
| `background` | forge training, bulk re-captioning | yes, cooperatively |

Within a class, the score is gothmog's: a wait bonus so nothing starves, plus an affinity bonus when the job's
checkpoint or LoRA is already loaded on the node. A lower job may **backfill** only if it fits beside the reservation
held for the head of the queue.

**Preemption is cooperative.** A CUDA kernel cannot be stopped safely, so the heartbeat response carries
`yield: true` and the client releases at its next safe point: between atlas tiles, proof seeds, lens batches. A client
that ignores it is not killed; its lease simply runs to its TTL.

**Placement.** A lease names a target, not a host:

- `comfyui`: the node(s) running ComfyUI (morpheus).
- `ollama:<model>`: dragon first, spill to jarrah when dragon cannot admit it and the model fits jarrah.
- `in-process` with constraints: `min_vram_mb`, `bf16`, `min_cc`, or an explicit node for a process pinned to a host.

## Policy: schedules, keepalive, idle-kill

One YAML policy file, owned by the operator, merged over the hints in tenant manifests:

```yaml
timezone: America/Los_Angeles      # example: morpheus's local zone (UTC-8 in November); times on the wire are UTC
nodes:
  morpheus: { reserve_mb: 3500 }   # desktop display
windows:
  - name: overnight-batch
    cron: "0 0 * * *"              # opens at midnight
    duration: 7h
    allow: { class: batch, node: morpheus }
residents:
  - match: { node: dragon, ollama: "singguard:*" }
    keepalive: always              # pinned: never evicted
  - match: { node: dragon, ollama: "qwen3:8b" }
    keepalive: { cron: "0 8 * * 1-5", duration: 10h }
    idle_kill: 15m
  - match: { node: jarrah, ollama: "*" }
    idle_kill: 5m                  # one model fills the card; keep it moving
  - match: { node: morpheus, service: comfyui }
    idle_kill: 30m                 # replaces COMFYUI_IDLE_TIMEOUT_SECONDS
```

- **keepalive**: a resident is pinned during its window. For Ollama, iris refreshes `keep_alive` before it expires.
- **idle_kill**: time since the resident's last lease, after which iris evicts it.
- **windows**: when a class may run on a node; jobs can also carry `not_before`.

## Tenant manifests

Every repo that uses a GPU, inside the suite or not, commits `gpu-tenant.yaml`. The schema lives in argus-cortex
(`gpu-tenant-schema.json`, generated and drift-tested like the taxonomy and provenance schemas). A service POSTs its
manifest to iris on startup; iris also accepts manifests from its config directory for tenants that never call it.

```yaml
tenant: argus-darkroom
kinds:
  - kind: generate
    target: comfyui
    vram_mb: 9000
    class: interactive           # default; callers may pass a lower class through
    preemptible: false
  - kind: expand
    target: "ollama:qwen3:8b"
    vram_mb: 6000
    class: interactive
residents: []                    # darkroom holds nothing itself; ComfyUI and Ollama do
```

SUITE.md gains a short GPU column summarising each repo's manifest, and argus-ci checks the file exists in repos that
use a GPU.

## API (v1)

All payloads are versioned with `argus_cortex.wire` (`lease_version`); writes go through cortex's `WriteGuard`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/leases` | Request a lease. Long-polls up to `wait_s`, or returns `202` with an id to watch. |
| `GET` | `/leases/{id}/events` | SSE: `queued` (with position) → `granted` / `expired` / `refused`. |
| `POST` | `/leases/{id}/heartbeat` | Keep alive; response carries `yield` and the remaining TTL. |
| `DELETE` | `/leases/{id}` | Release; optionally reports the observed peak VRAM. |
| `POST` | `/tenants` | Register a manifest. |
| `GET` | `/nodes`, `/residents`, `/queue`, `/stats` | State for the dashboard and for operators. |
| `POST` | `/residents/{id}/evict`, `/residents/{id}/warm` | Manual actions from the dashboard. |
| `GET` | `/events` | SSE stream of every state change, for the Studio page. |
| `GET` | `/metrics` | Prometheus (gothmog's gauges plus queue, wait and eviction counters). |
| `GET` | `/health` | Liveness and the reachability of each agent. |

## Client: argus-cortex

The client lives in cortex so every service gets it through the existing `GpuCoordinator` protocol:

- `BrokerCoordinator` in `argus_cortex.gpu`, behind `[remote]`, selected by `ARGUS_GPU_COORDINATOR=broker` and
  `ARGUS_GPU_BROKER_URL`.
- `lease()` gains optional `priority=`, `target=`, `kind=` and `preemptible=`; existing calls keep working. It yields
  a handle with `should_yield()`, `node` and `base_url`, and runs the heartbeat in the background. An async `alease()`
  serves the FastAPI services.
- The lease and manifest wire models live in cortex (pydantic only), so client and broker share one definition.
- **Fails open**: if iris is unreachable, the coordinator logs a warning and falls back to `LeaseCoordinator` (or
  `NullCoordinator`), and a target lease falls back to the caller's configured URL.

## Who takes which lease

| Caller | Lease |
|---|---|
| argus-darkroom | One per generation on `comfyui`; one per prompt expansion on `ollama:<model>`. The class comes from the caller (`X-Argus-Priority`): mint sample runs are `batch`, safelight and studio users `interactive`. Darkroom queues behind iris instead of returning 409, so mint's and mask's retry loops go. |
| argus-atlas | `batch` on `comfyui`, per tile so it can yield. |
| argus-mask | A pinned `realtime` resident for a2f while a broadcast is live; `interactive` for MisoTTS voice generation. |
| argus-mint | Bio writer on `ollama:<model>` (`batch` for sample runs). Renders go through darkroom. |
| argus-lens, argus-curator | No code change: `ARGUS_GPU_COORDINATOR=broker`. |
| argus-proof | `batch` on `comfyui` or in-process with constraints. |
| argus-forge | Exclusive whole-card `background` lease on morpheus, inside a window. |
| agent-composer, SingGuard | SingGuard's model is a pinned resident on dragon. Other requests stop sending `keep_alive=15m` once iris owns residency. |
| ai-code-auditor | Was a gothmog capacity-token client: moves to an `ollama:<model>` lease. |
| voluptas, mithrandir, Immich | Observed first; iris takes over the idle policy (see rollout). |

## Taking over existing lifecycle code

Iris must not fight the per-repo mechanisms it replaces, so each one is handed over explicitly:

| Mechanism | Repo | Handover |
|---|---|---|
| `comfyui-cleanup` stop after `COMFYUI_IDLE_TIMEOUT_SECONDS` and RAM restart | voluptas | Set the stop timeout to `0` (disabled) once iris's comfyui adapter owns idle-kill and the RAM restart. Keep the output/temp cleanup, which iris does not do. |
| `MISO_KEEP_ALIVE_GPU` / `_CPU` | voluptas (MisoTTS) | Leave it; register MisoTTS as an argus-http resident and let iris evict earlier when it needs the memory. |
| per-request `keep_alive=15m` | agent-composer | Drop it from requests (or set it per model from the policy) so the server-side keep-alive iris sets is not overridden. |
| `ARGUS_IDLE_UNLOAD_S` | argus-lens | Unset when lens runs with the broker coordinator. |
| `MACHINE_LEARNING_MODEL_TTL` | Immich (mithrandir) | Set low; iris only observes it. |

## Dashboard

A Studio page, fed by `/events` and `/stats`:

- per node: VRAM bar split into leases, residents, reserve and free; host RAM; agent reachability
- queue per class with wait times and position, and the lease each job is waiting on
- residents with idle time, keepalive window, and evict / warm buttons
- history: waits, evictions, OOM refusals, learned vs declared footprints per tenant

Prometheus `/metrics` remains for Grafana.

## What is salvaged from gothmog

| Kept | From | Changed |
|---|---|---|
| Affinity scoring (checkpoint and LoRA hits, wait bonus, run-lag penalty) | `server/gpu_pool.py` `_score` | Moves into the per-class score; weights in the policy file. |
| Capacity tokens with TTL | `server/gpu_pool.py` `CapacityManager` | Becomes the lease, with heartbeat instead of a fixed 2 h TTL and a real VRAM ledger instead of "all backends idle". |
| Idle reaper with per-backend TTL | `server/gpu_residents.py` | Becomes `idle_kill` in the policy, evicting through adapters. |
| Per-process VRAM exporter | `gpu-process-exporter/` | Becomes `iris-agent`: cgroup lookup, host RAM, container actions, snapshot API. |
| Prometheus gauges | `server/metrics.py` | Kept, renamed under `iris_`. |

Dropped: the LangGraph/LangChain workflow layer, module-global singletons, hard-coded backend URLs, the "wait for peers
idle, then proceed anyway" loop (no OOM protection), and a new HTTP client per poll.

## Rollout

1. **cortex**: lease and manifest wire models, `gpu-tenant-schema.json`, `BrokerCoordinator`, the additive `lease()`
   arguments. Minor release.
2. **iris, observe-only**: agents on all three hosts, adapters read-only, the Studio page. No evictions, no leases
   enforced. This shows real footprints and idle times before iris acts on them.
3. **iris, leases**: admission, the ledger, placement, priority classes. lens and curator switch by environment
   variable; darkroom adopts the client and queues instead of returning 409.
4. **iris, residency**: idle-kill and keepalive, one tenant at a time, retiring each mechanism in the handover table.
5. atlas, mask, mint, proof and the outside repos adopt leases; cooperative yield; learned footprints; schedule
   windows; forge.

## Open questions

- Where does iris itself run? morpheus is the obvious host, but the broker going down there should not stop dragon's
  and jarrah's work: the fail-open path covers this, but confirm it is acceptable.
- Should iris also gate ComfyUI work that voluptas's own clients submit directly, or only Argus callers? Gating it
  means voluptas's clients adopt the lease too.
- Auth between callers, agents and the broker: a shared bearer token on the LAN to start with?
