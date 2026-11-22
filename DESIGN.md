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
 ┌──────────────────────────── argus-iris broker (:8120) ─────────────────────────┐
 │ registry (tenant manifests)   scheduler (classes, aging, affinity, backfill)    │
 │ ledger per node               policy (windows, keepalive, idle-kill)            │
 │ adapters: comfyui · ollama · argus-http · external                              │
 │ history: SQLite (WAL) on a volume                                               │
 └──────────────┬──────────────────────────┬──────────────────────────┬────────────┘
                │ GET /snapshot            │                          │
        iris-agent @ morpheus      iris-agent @ dragon        iris-agent @ jarrah
        NVML + cgroup → service    NVML + cgroup → service    NVML + cgroup → service
        container start/stop       container start/stop       container start/stop
```

### Broker

One instance, on morpheus. Holds the authoritative state in memory and appends lease and job events to SQLite for
history and stats. After a restart, clients re-attach on their next heartbeat (the heartbeat carries the lease's
fields), so no external queue store is needed.

**Restart grace.** For one heartbeat interval after start-up iris does not know which leases are still running, so it
admits only against NVML free memory (no ledger credit) until the interval has passed. Queued requests are lost on a
restart; the client re-POSTs with the same idempotency key.

**Fail-open is accepted.** When iris is down or unreachable, callers run uncoordinated: the cortex client falls back to
its local coordinator, which for a remote target like ComfyUI gates nothing. darkroom then behaves as it did before
iris. A client that cannot reach a live iris runs outside the ledger; the NVML free-memory check is the only guard
against that.

### Node agent (`iris-agent`)

Runs on every GPU host, in the same package behind an `[agent]` extra. Salvaged from gothmog's `gpu-process-exporter`,
modernised:

- NVML via `nvidia-ml-py`: total, free and per-process VRAM; GPU utilisation. NVML reports host PIDs, so the agent
  runs with `pid: host` (and `cgroup: host`, or cgroup paths read relative to its own namespace).
- PID to compose service through `/proc/<pid>/cgroup` and one Docker lookup per new container ID, cached. gothmog ran
  `docker top` on every container for every new PID.
- Idle time per process: VRAM unchanged **and** utilisation near zero. gothmog used VRAM alone, which calls a busy
  process with a steady footprint idle.
- Host RAM, and per-container anonymous memory from the container's cgroup `memory.stat` (host `/sys/fs/cgroup`
  mounted read-only), because ComfyUI's host-RAM growth once hung morpheus (voluptas #16). This replaces voluptas's
  `docker exec … cat memory.stat`.
- Container actions the broker may ask for: `start`, `stop`, `restart`. Needed because voluptas's watcher stops
  ComfyUI after an idle spell and nothing starts it again. Needs the Docker socket (spoken to directly over the Unix
  socket, no Docker SDK), so the agent is root-equivalent on its host:
  - actions apply only to containers on the agent's own allowlist (`IRIS_AGENT_CONTAINERS`); a manifest POSTed to the
    broker can never widen it;
  - broker→agent calls carry a token separate from the callers' token;
  - reads (`/snapshot`, `/metrics`, `/health`) need no token, so Prometheus can scrape them.

Units: every memory figure on the wire is MiB (NVML bytes / 2^20), named `*_mb` for continuity with existing code.

### Adapters

| Adapter | Reads | Evicts | Warms |
|---|---|---|---|
| comfyui | `/system_stats`, `/queue`, `/object_info` for loaded models | `POST /free {unload_models, free_memory}`; container `restart` when host RAM is high and the queue is empty | container `start`, then wait for `/system_stats` |
| ollama | `/api/ps` (`size_vram`, `expires_at`) | `POST /api/generate {model, keep_alive: 0}` | `POST /api/generate {model, keep_alive: <window>}` |
| argus-http | `/health` (`vram_status`) | `POST /admin/unload` (lens has it; others add it) | first request, or `POST /admin/wake` |
| external | `health_path` up/down; VRAM and idle from the node agent | none, or container `stop` with `evict_by: stop` (opt-in per service, and for admission an enforced policy rule) | container `start`; admission's stops are started again once no lease is active on the node (#29) |

Adapters are configured by tenant manifests, not hard-coded per service as gothmog was. The argus-http adapter's
paths are per manifest, because the services that already have lifecycle endpoints spell them differently.
agent-composer#44 and voluptas#22 converge on one contract: `/health` with `loaded`, `model_state`, `idle_s` (and
`busy`), plus `POST /admin/unload|evict|wake`, guarded by `ARGUS_ADMIN_TOKEN` when it is set. agent-composer's
sidecars listen on loopback, so iris reaches them through rag-chat's gateway at
`<rag-chat>/api/runtime/residents/<name>`, which needs an API key as Bearer even for reads. A service's own `idle_s`
takes precedence over the agent's process-level idle time.

**ComfyUI specifics.** `POST /free` unloads every model at once, and stock ComfyUI has no endpoint that lists what is
loaded, so for eviction ComfyUI is a single resident. The affinity bonus comes from the checkpoint and LoRAs of the last
job iris granted on it (callers pass them on the lease), not from ComfyUI itself; gothmog's affinity read imogen's own
`/state`, which ComfyUI does not have. Warming means `start` through the agent and waiting for `/system_stats`, which is
what fixes ComfyUI staying down after voluptas's idle stop.

**Direct ComfyUI traffic is not gated.** voluptas's own LAN clients submit to ComfyUI without a lease. They are known,
so the comfyui adapter treats a non-empty `/queue` that iris did not grant as unannounced load: it counts ComfyUI's
measured VRAM in full and never evicts while the queue is busy.

## Admission and OOM avoidance

A lease's footprint has two parts, declared per kind in the manifest:

- `model_mb`: the weights the target service loads and **keeps loaded** after the lease ends (a ComfyUI checkpoint, an
  Ollama model). Once loaded it is a resident, measured by the agent.
- `working_mb`: activations, latents and KV cache, held only while the job runs.

Counting `lease VRAM + resident VRAM` would count the weights twice, because a lease's model already shows up as the
service's resident. So per node the ledger must satisfy:

```
Σ_services max(measured resident, Σ model_mb of its active leases)
  + Σ working_mb of active leases
  + unmanaged reserve + headroom                                     <=  total VRAM
```

A lease whose model is already resident needs only `working_mb` of new room, which is what makes the affinity bonus
pay off. `headroom` defaults to 512 MiB per node (CUDA context overhead is not in anyone's declared figures).

NVML free memory is checked as well, to catch tenants iris does not know about. When a lease does not fit:

1. Build an eviction plan from residents that are idle, not pinned, not under an active lease, and lower priority than
   the request, least recently used first.
2. Evict through the adapters and wait for NVML to confirm the memory came back.
3. Admit, or keep the lease queued if the plan cannot free enough.

Unlike the current lease this is a reservation: two admitted leases can never add up to more than the card.

**Learned footprints.** While a lease is active the agent samples the target process's peak VRAM. On a shared server
(ComfyUI, Ollama) the agent sees the server, not the tenant, so a sample counts only when that lease was the sole
active lease on the service and the service had no unannounced queue. Iris keeps a p95 per (tenant, kind) and uses it
whenever it is larger than the declared figure. That replaces hand-kept tables such as lens's
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
`yield_requested: true` and the client releases at its next safe point: between atlas tiles, proof seeds, lens batches. A client
that ignores it is not killed, but from the first `yield_requested` iris stops extending the lease: it ends at
`yield_grace` (default 120 s) even if heartbeats keep arriving, and the memory is treated as free once NVML confirms it.

**Liveness.**

- A retried `POST /leases` must not create a second lease. This needs a client-generated request id on
  `LeaseRequest`, which has been asked of cortex.
- A queued request stays queued only while its client is polling it (`GET /leases/{id}?wait_s=`). One with no poll for
  about 2 × `wait_s` is dropped, not granted.
- A grant that receives no first heartbeat within 30 s is reclaimed.
- A heartbeat proves the client is alive, not that it works. A grant with no GPU activity for `lease_stall_s` (30 min;
  its resident never busy or used in the last minute, no change in `progress`, `stage` or `peak_vram_mb`; for a
  target with no resident, GPU utilisation on the node) is marked stalled. While a queued lease waits on its node,
  its heartbeat replies set `yield_requested`. With `lease_stall_reclaim` it is ended `refused` ("reclaimed") once it
  has ignored that for `lease_stall_s / 2`, and a `resume` of it stays refused, so a keepalive loop that outlived its
  job cannot hold the reservation (2026-11-18: a 17 GB proof-eval lease beat for 1h38m with ComfyUI idle, blocking
  an atlas bake).

**Placement.** A lease names a target, not a host:

- `comfyui`: the node(s) running ComfyUI (morpheus).
- `ollama:<model>`: dragon first, spill to jarrah when dragon cannot admit it and the model fits jarrah. SingGuard was
  pinned on dragon (about 5.3 GiB of 8 GiB) until 2026-11-22, which sent any general model larger than an embedder to
  jarrah and crowded out the voice engines (#40); the guard now runs on morpheus (argus-retina), and dragon's copy is
  idle-killed after a minute. Where both fit, the Ollama serving
  fewer granted leases goes first (then the one with the model loaded), so concurrent jobs spread over the two; an
  Ollama whose `/api/tags` lacks the model is skipped unless none has it. A lease that fits nowhere waits on the node
  its running leases would free.
- `xtts`: dragon's XTTS sidecar first, then morpheus's argus-echo XTTS (same studio speakers), which declares
  `serves: [xtts]` (#40). A service answers a target named after its id, its adapter, or anything in `serves`, so one
  target can have a service on several nodes, and the grant's `base_url` says which one to call.
- `in-process` with constraints: `min_vram_mb`, `bf16`, `min_cc`, or an explicit node for a process pinned to a host.

**Room that never comes back** (#40). Each poll, iris sums what admission can never free on a node: pinned residents
(a pin that dropped out and is due to be restored included), processes it has no adapter for and no tenant owns
(Frigate), externals it may not stop, and the context an unloaded, CPU-parked or failed service keeps. A service whose
need (the larger of `warm_mb` and the most it has been seen holding; a resident over its `warm_mb` logs `resident_over_warm_mb` once) exceeds what is left beside that is `crowded_out`: on
`/health` (`crowded_out`), the node's services (`need_mb`, `room_mb`), `iris_service_crowded_out`, the dashboard, and a
`service_crowded_out` log naming the biggest holders. It is a report, not an action: the operator moves a pin, adds a
node for the target (`serves`), or lets `restart_residual_mb` return a residue.

## Policy: schedules, keepalive, idle-kill

One YAML policy file, owned by the operator, merged over the hints in tenant manifests:

```yaml
timezone: America/Los_Angeles      # example: morpheus's local zone (UTC-8 in November); times on the wire are UTC
nodes:
  morpheus: { reserve_mb: 3500 }   # desktop display
windows:
  - name: nightly-audit
    cron: "0 1 * * *"              # opens at 01:00
    duration: 5h
    tenant: ai-code-auditor
    target: "ollama:*"
    class: batch                   # or background: what the window's jobs run at
  - name: forge-training
    cron: "0 0 * * 6"              # Saturday midnight
    duration: 30h
    tenant: argus-forge
    node: morpheus
    class: background
residents:                         # first matching rule wins
  - match: { node: jarrah, ollama: "qwen3:8b" }
    keepalive: { cron: "0 8 * * 1-5", duration: 10h }   # pinned in working hours: never evicted then
    idle_kill: 15m
  - match: { node: jarrah, ollama: "*" }
    idle_kill: 5m                  # one model fills the card; keep it moving
  - match: { node: morpheus, service: comfyui }
    idle_kill: 30m                 # replaces COMFYUI_IDLE_TIMEOUT_SECONDS
```

- **keepalive**: a resident is pinned during its window. For Ollama, iris refreshes `keep_alive` before it expires.
- **idle_kill**: time since the resident was last used, after which iris evicts it. That is its last lease, or, for
  residents no lease ever touches (observed tenants, direct ComfyUI traffic), the agent's idle time.
- **windows**: a time range for a scheduled, unattended operation, such as ai-code-auditor's overnight runs or a forge
  training session. A window names the tenant (and optionally target or node) it is for and the class its jobs run at:
  `batch` to get through before morning, `background` to use only what is spare. Outside its window the tenant's
  leases run at the class they ask for. A window does not stop other tenants running; it only sets the
  priority for this one. Jobs can also carry `not_before`.

## Tenant manifests

Every repo that uses a GPU, inside the suite or not, commits `gpu-tenant.yaml`. The schema lives in argus-cortex
(`gpu-tenant-schema.json`, generated and drift-tested like the taxonomy and provenance schemas). A service POSTs its
manifest to iris on startup; iris also accepts manifests from its config directory for tenants that never call it.

```yaml
tenant: argus-darkroom
kinds:
  - kind: generate
    target: comfyui
    model_mb: 6500               # checkpoint, stays resident in ComfyUI
    working_mb: 2500             # latents and activations, only while running
    class: interactive           # default; callers may pass a lower class through
    preemptible: false
  - kind: expand
    target: "ollama:qwen3:8b"
    model_mb: 5200
    working_mb: 800              # KV cache
    class: interactive
residents: []                    # darkroom holds nothing itself; ComfyUI and Ollama do
```

SUITE.md gains a short GPU column summarising each repo's manifest, and argus-ci checks the file exists in repos that
use a GPU.

## API (v1)

The lease and manifest models are `argus_cortex.gpu_wire` (`LeaseRequest`, `LeaseStatus`, `Heartbeat`,
`HeartbeatReply`, `LeaseRelease` at `lease_version` 1.0; `TenantManifest` at `tenant_version` 1.0), with committed
schemas `gpu-lease-schema.json` and `gpu-tenant-schema.json`. Iris imports them; it does not define its own. Writes go
through cortex's `WriteGuard` and take an optional `Authorization: Bearer <ARGUS_GPU_BROKER_TOKEN>`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/leases` | `LeaseRequest` → `LeaseStatus` (`queued` with `position` and `reason`, `granted`, or `refused`). Iris may hold the request up to `wait_s`. `resume=<id>` re-attaches running work after an iris restart: granted at once, same id. |
| `GET` | `/leases/{id}?wait_s=N` | `LeaseStatus`, long-polled until the state changes or N elapses. `404` when unknown; the client re-POSTs. |
| `POST` | `/leases/{id}/heartbeat` | `Heartbeat` (`peak_vram_mb`) → `HeartbeatReply` (`state`, `yield_requested`, `ttl_s`, `reason`). `404` when unknown; the client re-attaches with `resume`. |
| `POST` | `/leases/{id}/release` | `LeaseRelease` (`outcome` ok / failed / oom / cancelled, `peak_vram_mb`, `detail`). `cancelled` on a queued lease drops it from the queue. |
| `POST` | `/tenants` | Register a `TenantManifest`. |
| `GET` | `/nodes`, `/residents`, `/queue`, `/stats` | State for the dashboard and for operators. |
| `POST` | `/services/{id}/warm` | Manual: start the service's container through its agent if the service is down, wait for its health check, then call its `warm_path`. A stopped service has no resident, so this targets the service. |
| `POST` | `/residents/{id}/evict` | Manual eviction from the dashboard (step 4). |
| `GET` | `/events` | SSE stream of every state change, for the Studio page. |
| `GET` | `/metrics` | Prometheus (gothmog's gauges plus queue, wait and eviction counters). |
| `GET` | `/health` | Liveness and the reachability of each agent. |

How the client reacts to errors constrains the status codes: a 5xx, a transport error or a `404` on `POST /leases`
makes it fall back to its local lease; any other 4xx raises in the caller. So "busy" is always `queued`, never a 4xx.
`LeaseRequest.priority` and `.preemptible` are `None` unless the caller set them; iris fills them from the manifest's
kind, else `interactive` and not preemptible.

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
| agent-composer, SingGuard | SingGuard checks go through rag-chat on :9150 to argus-retina's SingGuard server on morpheus (agent-composer#77), which takes its own leases. Dragon's Ollama copy (`SingGuard-4b` Q4_K_M, about 5.3 GiB) was a pinned resident until 2026-11-22 and is now idle-killed; jarrah has it pulled too. Other requests stop sending `keep_alive=15m` once iris owns residency. agent-composer has one `OLLAMA_URL` and no host routing, so spilling its work to jarrah needs it to call the grant's `base_url` through the cortex client. |
| ai-code-auditor | Was a gothmog capacity-token client: moves to an `ollama:<model>` lease. |
| voluptas, mithrandir, Immich | Observed first; iris takes over the idle policy (see rollout). |

## Taking over existing lifecycle code

Iris must not fight the per-repo mechanisms it replaces, so each one is handed over explicitly:

| Mechanism | Repo | Handover |
|---|---|---|
| `comfyui-cleanup` stop after `COMFYUI_IDLE_TIMEOUT_SECONDS` and RAM restart | voluptas | It used to `docker stop` ComfyUI after 30 min idle, and nothing started it again. voluptas#22 makes `0` mean disabled and the default. The #16 RAM restart stays until the comfyui adapter owns it; then set `COMFYUI_MEM_RESTART_MB=0`. |
| `comfyui_cleanup.sh` `POST /free` every 15 min | voluptas | It freed models whatever the queue held. voluptas#22 skips `/free` while a job is queued. Set `COMFYUI_CLEANUP_FREE=0` once iris owns residency; the history, output and temp cleanup stay, since iris does not do them. |
| `MISO_KEEP_ALIVE_GPU` / `_CPU` | voluptas (MisoTTS) | Leave it; register MisoTTS as an argus-http resident and let iris evict earlier when it needs the memory. |
| per-request `keep_alive=15m` and `OLLAMA_KEEP_ALIVE=15m` | agent-composer | rag-chat and rag-ingest send it on every Ollama call, so any request re-pins its model whatever iris decided. agent-composer#44 adds `OLLAMA_KEEP_ALIVE=server`, which stops sending it. Switch to that when iris owns Ollama residency (step 4); the default stays `15m`. |
| `/api/runtime/ollama/evict` with no model, `/ollama/wake` | agent-composer | The no-model evict unloads every loaded model, including other tenants'. Route both through iris's `/residents/{id}/evict` and `/warm`. |
| Infinity manager idle reaper (`INFINITY_IDLE_TIMEOUT=300`) | agent-composer | Leave it; Infinity is an argus-http resident (0.8 GiB loaded, 1.3 GiB peak, held until the process stops). Since agent-composer#44 it answers 409 `busy` to an evict with requests in flight. |
| `run_with_oom_wait` (retry on CUDA OOM for up to 180 s) | agent-composer | Keep it as the fallback when iris is down. A lease refusal must come back as a distinct error, not one shaped like an OOM, or the loop polls for 180 s. |
| `STT_KEEP_ALIVE_GPU` | agent-composer (Whisper) | Leave it; agent-composer#44 gives Whisper `/admin/unload`, so iris can evict it earlier. |
| `ARGUS_IDLE_UNLOAD_S` | argus-lens | Unset when lens runs with the broker coordinator. |
| `MACHINE_LEARNING_MODEL_TTL` | Immich (mithrandir) | Set low; iris only observes it. |

## Dashboard

A Studio page, fed by `/events` and `/stats`:

- per node: VRAM bar split into leases, residents, reserve and free; host RAM; agent reachability
- queue per class with wait times and position, and the lease each job is waiting on
- residents with idle time, keepalive window, and evict buttons; services with a warm button
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

## Decisions

Settled 2026-11-15:

- iris runs on morpheus; fail-open when it is down is acceptable (see Broker).
- ComfyUI work that voluptas's own clients submit directly is not gated for now. Those LAN consumers are known, and
  the comfyui adapter treats their queue as unannounced load.
- Auth: a shared bearer token for callers, and a separate one for broker→agent calls. Revisit if the agent or broker
  needs per-caller identity, for example to attribute metrics.
- Footprints are declared as `model_mb` plus `working_mb` (see Admission). cortex PR #78 has a single `vram_mb` so far; the split (as an optional `model_mb` beside the total) has been asked of cortex.
- argus-cortex owns rollout step 1 (wire models, schema, `BrokerCoordinator`); iris starts at step 2.
