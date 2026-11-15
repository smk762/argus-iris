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

`src/argus_iris/` — the package. Key modules (fill in as they land):

- `__init__.py` — version shim (`__version__` from `hatch-vcs`).
- `cli.py` — Typer app; entry point `argus-iris` (`[project.scripts]`).
- `server/` — FastAPI app (`server.app:app`); optional `[server]` extra. Reuse argus-cortex's write-guard / env-flag helpers for any state-changing route.
- `models.py` — if this package has a wire contract, keep the Pydantic types here and treat them as the API surface.

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

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow. Release publishes to PyPI (OIDC trusted publishing) + GHCR on `v*` tags. Run `copier update` to pull template (CI/release/tooling) changes.
