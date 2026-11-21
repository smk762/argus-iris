# CLAUDE.md — argus-iris

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

> Scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template). Flesh out **Layout** and **Gotchas** below as the code grows — the suite conventions are already correct and shared.

## What this is

VRAM-aware GPU lease broker for the Argus suite: priorities, schedules, keepalive and idle-kill across GPU nodes

It decides whether a GPU job may start now, on which node (morpheus, dragon, jarrah), and what must be unloaded first. It does not run the work. **[DESIGN.md](DESIGN.md) is the plan the first releases build to; read it before changing anything.** The client (`BrokerCoordinator`) and the lease and tenant-manifest wire models live in argus-cortex's `gpu` module, so callers and broker share one definition.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/dev/SUITE.md

## Layout

`src/argus_iris/` — the package. Rollout step 3: leases, placement and admission (`admission: observe` grants
everything and records `fits`; `enforce` queues, spills and evicts). Step 4's residency policy (`policy.py`, `residency.py`) is report-only per rule until `enforce: true`.

- `models.py` — agent snapshot (`NodeSnapshot`, `GpuProcess`, …) and the broker's read models (`NodeState`,
  `Resident`, `NodeLedger`). Lease and tenant-manifest models are **not** here: import them from
  `argus_cortex.gpu_wire`.
- `agent/` — `iris-agent` (`[agent]` extra, `argus-iris agent`, :9110). `nvml.py` samples, `tracker.py` keeps idle
  time (VRAM unchanged **and** no utilisation), `procfs.py` maps PIDs to containers through cgroups and reads anon
  memory, `docker.py` is a minimal Engine API client over the Unix socket, `app.py` serves `/snapshot`, `/metrics`,
  `/health` and token- and allowlist-gated `POST /containers/{name}/{start|stop|restart}`.
- `broker/` — `config.py` (YAML, `IRIS_CONFIG`), `adapters/` (comfyui, ollama, argus-http, external; read-only; external is up/down from `health_path` only, freed by a container stop only with `evict_by: stop`, which admission uses only under an enforced policy rule and `Broker.stopped` re-warms),
  argus-http validates `/health` as cortex's `ResidentHealth` and reads older shapes best-effort, logging
  `resident_off_contract` (and `detail.off_contract` on the resident); a truthy non-bool `busy` counts as busy,
  `state.py` (`Broker` polls agents and adapters, ticks the lease book every second; `compose_node` is the pure
  accounting), `leases.py` (`LeaseBook`: tenants, `candidates()` (placement order: up, fewest granted leases on the service, model resident, config order; an Ollama whose `/api/tags` lacks the model is skipped unless none has it), admission (`_serve_queue`: class order
  with aging, head reservation, spill, `_plan` eviction plans run by the broker's `_evict_for`), request_id idempotency, `resume`,
  heartbeat/TTL expiry (a beat also carries `progress`/`stage`, published to `/events` when they change), the 30 s first-beat reclaim, the stall watchdog (`_watch`: heartbeating with no GPU activity for `lease_stall_s` -> `stalled_at`, `yield_requested` while it blocks the queue, reclaimed as `refused` only with `lease_stall_reclaim`), a tenant's own memory (#36: `tenant_of` links residents by `tenants.<t>.containers` or a process's compose service; `_own_credit` counts its `process` residents toward its in-process leases; its residents are never victims for its own lease), unpolled-queue drop; synchronous with an injectable clock, so test
  it by moving the clock and calling `tick()`), `policy.py` (the operator's YAML: cron, windows, resident rules; `IRIS_POLICY`), `residency.py` (`Residency`:
  per-resident `ResidentPolicy` view on every poll, `step()` acts on enforced rules: pin/extend Ollama keep_alive,
  idle-kill evict, `unload_after` (CPU stage -> unloaded; confirmed by the reply, since only RAM moves), restart
  when unloaded (never merely on the CPU) but holding `restart_residual_mb`, or idle `restart_host_ram_idle` with
  container anon memory (`Resident.host_ram_mb`) over `restart_host_ram_mb` (default: the manifest's
  `host_ram_restart_mb`); restore (#35): `Residency.pins` remembers pinned residents seen loaded and warms one
  gone `RESTORE_AFTER_POLLS` polls (Ollama: promptless keep_alive -1 with its old `num_ctx`; argus-http: `warm_path`)
  only with no lease active/queued on the node and room, backing off on failure or a quick re-drop; `/health`
  `pins_missing`; `Broker.residuals` remembers what an
  eviction left so it is not repeated), `actions.py` (warm: 409 unless `force` while the node cannot admit `warm_mb` or the most it was seen holding, `Broker.peaks`; agent starts the container, wait for health, call `warm_path`;
  `evict_resident`: adapter route per source, 409 while busy, confirmed by the GPU's NVML free memory rising by half
  the resident's VRAM, not by the service's word; reports `residual_mb`).
- `server/app.py` — the broker API (`[server]` extra, `argus-iris serve`, :8120): `/nodes`, `/residents`, `/events`
  (SSE: `node` and `lease` events), `/metrics`, `/health`, `/ui/`, the lease routes (`POST /tenants`, `POST /leases`,
  `GET /leases/{id}?wait_s=`, `.../heartbeat`, `.../release`, `GET /leases`, `/queue`, `/tenants`; bearer token
  checked only when `ARGUS_GPU_BROKER_TOKEN` is set) `POST /services/{id}/warm` and `POST /residents/{id}/evict` (resident ids contain slashes; both need that token). Wrapped in
  cortex's `WriteGuard`. Test the lease routes with cortex's own `BrokerCoordinator` over `httpx.ASGITransport`.
- `ui/` — the dashboard, served by the broker at `/ui/` (`/` redirects). Plain HTML and ES modules with no build
  step, shipped in the wheel (the pattern argus-mask and argus-survey use). It is a thin client over `/health`,
  `/config` and `/events`, and the warm POST, so it never computes what the broker decides. `vendor/dh/` holds
  `dh-tokens.css` and `dh-theme.js`, copied unchanged from dh-shared 1.2.7 (via argus-mask): **never edit them**.
  Re-copy them from a newer sync instead. Colours come from `--dh-*` tokens only.
- `cli.py` — Typer app; entry point `argus-iris` (`serve`, `agent`, `version`).
- `compose.yaml` includes `deploy/broker.compose.yaml` and `deploy/agent.compose.yaml` with `deploy/.env` (project
  `argus-iris`); `deploy/` — example broker config, policy, and the compose files for the broker and the agents.

## Commands

```bash
make install   # uv venv + editable install with the "dev" extras
make test      # pytest --tb=short -q
make lint      # ruff check + format --check (pinned ruff 0.15.16)
make format    # ruff format + --fix
```

Run one test: `uv run --no-sync pytest tests/test_x.py::test_name -q`.

## Conventions & gotchas

- **Versioning is git-tag-derived** (`hatch-vcs`). Never hand-edit a version; `src/argus_iris/_version.py` is generated (gitignored). Tag `vX.Y.Z` to release.
- **`src/` layout, `py.typed`, Pydantic v2, `structlog`.** Ruff line-length 120; keep it clean (`make lint`) before committing.
- **Any endpoint that does something is a boundary.** There's no auth by default — treat reaching the port as host access. Gate state-changing routes behind the shared read-only / origin guards, and fence any caller-supplied path under a configured root.
- Wire schemas (if any) are checked in and verified in CI — regenerate and commit when the types change, don't let the committed copy drift.
- **Ports:** a2f holds :8110 on morpheus, so the broker is :8120; the agent is :9110 (gothmog's exporter used :9101).
- **The agent is root-equivalent** (Docker socket, `pid: host`). Container actions stay behind `IRIS_AGENT_TOKEN`
  **and** the agent's own `IRIS_AGENT_CONTAINERS` allowlist; never let broker config or a POSTed manifest widen it.
- **Memory is MiB** everywhere (`*_mb`), times are aware UTC.
- **Don't 4xx for "busy"** on lease routes: cortex's client treats any 4xx other than 404 as a caller error. Busy is
  `queued`.

## Branches

`dev` is the default branch and the base for every PR, as in the rest of the suite. Cut feature branches from
`origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never push work to `main` or open a PR
against it, except the release PR from `dev`: `main` must never be ahead of `dev`. A release fast-forwards `main` to a
`dev` commit that passed CI (`git push origin origin/dev:main`), then tags `vX.Y.Z` on it. If `main` is protected, the
release is a reviewed PR from `dev` into `main`; its merge commit is the one thing `main` may have that `dev` lacks
(same tree). Leave it there and don't merge `main` back into `dev`.

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow. Release publishes to PyPI (OIDC trusted publishing) + GHCR on `v*` tags. Run `copier update` to pull template (CI/release/tooling) changes.
