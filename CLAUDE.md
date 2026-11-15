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

`src/argus_iris/` — the package. Rollout step 2 (observe-only): nothing admits, evicts or warms yet.

- `models.py` — agent snapshot (`NodeSnapshot`, `GpuProcess`, …) and the broker's read models (`NodeState`,
  `Resident`, `NodeLedger`). Lease and tenant-manifest models are **not** here: import them from
  `argus_cortex.gpu_wire`.
- `agent/` — `iris-agent` (`[agent]` extra, `argus-iris agent`, :9110). `nvml.py` samples, `tracker.py` keeps idle
  time (VRAM unchanged **and** no utilisation), `procfs.py` maps PIDs to containers through cgroups and reads anon
  memory, `docker.py` is a minimal Engine API client over the Unix socket, `app.py` serves `/snapshot`, `/metrics`,
  `/health` and token- and allowlist-gated `POST /containers/{name}/{start|stop|restart}`.
- `broker/` — `config.py` (YAML, `IRIS_CONFIG`), `adapters/` (comfyui, ollama, argus-http; read-only),
  `state.py` (`Broker` polls agents and adapters; `compose_node` is the pure accounting).
- `server/app.py` — the broker API (`[server]` extra, `argus-iris serve`, :8120): `/nodes`, `/residents`, `/events`
  (SSE), `/metrics`, `/health`. Wrapped in cortex's `WriteGuard`.
- `cli.py` — Typer app; entry point `argus-iris` (`serve`, `agent`, `version`).
- `deploy/` — example broker config and compose files for the broker and the agents.

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

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow. Release publishes to PyPI (OIDC trusted publishing) + GHCR on `v*` tags. Run `copier update` to pull template (CI/release/tooling) changes.
