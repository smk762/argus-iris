# Changelog

Notable changes to `argus-iris`. Versions come from git tags via `hatch-vcs`.

## [Unreleased]

## [0.2.2] - 2026-11-25

Documentation only: the code is the same as 0.2.1.

### Changed

- README links the [guided demo](https://iris.dragonhound.dev), and its links work on PyPI.

## [0.2.1] - 2026-11-25

### Added

- **Self-registration** (#47). A service registers its `gpu-tenant.yaml`; its address comes from the node agents' container inventory, never from the caller. Agents can announce their own node.
- **Service directory** (#49). Containers labelled `argus.peer` are listed with their health: `/directory`, peer lookup, Prometheus service discovery and a dashboard view.
- **Suite settings and service health** (#51). Tokens can come from the suite file, and `/health` on the broker and agent is argus-cortex's `ServiceHealth`.
- **Pin restore** (#37). A pinned model that drops out is loaded again when no lease is active on the node and there is room.
- **Crowded-out reporting and `serves`** (#41). A service that nothing can make room for is flagged, and one lease target can have a service on several nodes.
- **External services** (#30). Read for up/down, and optionally stopped to free VRAM and started again.
- **Memory accounting.** A tenant's own GPU memory counts toward its own leases (#38), the broker fills in peaks clients can't report (#44), and room is judged by the larger of `warm_mb` and the observed peak (#45).
- `SECURITY.md` and `docs/operating.md`; a rewritten README (#56).

### Changed

- **Ports bind to localhost by default** (#56). Set `HOST_IP` (compose), `IRIS_HOST` or `IRIS_AGENT_HOST` to the host's LAN address. A multi-host install that relied on `0.0.0.0` must set it.
- **The image is multi-stage** (#56): no git history and no build context in it. `.dockerignore` now excludes env files and live config at any depth.
- Ollama placement skips hosts that lack the model and spreads concurrent leases (#26).
- Needs argus-cortex 0.16.1 (`>=0.16.1,<0.17`).

### Fixed

- Ollama keep-alive calls send the context the model was loaded with (#31), and model names compare case-insensitively (#28).
- A model loaded during an unload is no longer written off as the service's floor (#33).
- The directory probes the broker's own container on loopback (#54).
- Audit findings in the agent, broker and dashboard (#46, #55).

## [0.2.0] - 2026-11-24 [YANKED]

Tagged on the wrong commit: the same code as 0.1.0. Use 0.2.1.

## [0.1.0] - 2026-11-20

First release: node agents, leases and admission, eviction confirmed by NVML, residency policy, stall watchdog and dashboard.
