# Operating argus-iris

## Run

For the compose files, copy [deploy/.env.example](../deploy/.env.example) to `deploy/.env` and fill in the host's
section.

```bash
docker compose up -d              # broker + this host's agent
docker compose up -d iris-agent   # agent only (other GPU hosts)
docker compose build              # ghcr.io/smk762/argus-iris:${IRIS_TAG} from this checkout (set IRIS_TAG=local)
```

[compose.yaml](../compose.yaml) includes [deploy/broker.compose.yaml](../deploy/broker.compose.yaml) and
[deploy/agent.compose.yaml](../deploy/agent.compose.yaml) with `deploy/.env`; each also runs on its own with `-f`.

On each GPU host (see [deploy/agent.compose.yaml](../deploy/agent.compose.yaml)):

```bash
IRIS_AGENT_NODE=rtx3090 IRIS_AGENT_TOKEN=... IRIS_AGENT_CONTAINERS=comfyui argus-iris agent   # :9110
```

It listens on localhost unless `IRIS_AGENT_HOST` is set (the broker: `IRIS_HOST`; compose: `HOST_IP`).
The agent needs `pid: host`, `cgroup: host`, NVML, the Docker socket and `/sys/fs/cgroup` read-only. Container actions are disabled
unless `IRIS_AGENT_TOKEN` is set, and apply only to containers in `IRIS_AGENT_CONTAINERS`.

The broker, on one host (see [deploy/iris.example.yaml](../deploy/iris.example.yaml)):

```bash
argus-iris serve --config iris.yaml   # :8120 — /nodes, /residents, /leases, /tenants, /events, /metrics, /health
```

Check a config and policy without starting anything (the deployment pipeline runs this in the release's image):

```bash
argus-iris check -c iris.yaml --policy policy.yaml --previous-config live/iris.yaml --previous-policy live/policy.yaml
```

It prints the admission mode, the nodes and the enforced rules as JSON, and exits 1 when a new file does not load.
Given the files deployed now, `approval` lists what needs a person to look at it: any difference between the old and
the new config or policy as the broker reads them (a comment or a default spelled out is not one), naming a changed
admission mode and each enforced rule that is new or changed. A deployed file this release no longer reads cannot be
compared, so that needs approval too.

`IRIS_REPORT_ONLY=1` makes a broker report and never act: every policy rule is unenforced and admission is `observe`,
whatever the files say, the manual warm and evict routes answer 403, and `/health` shows `report_only: true`. A staging broker beside production needs it, because
enforced rules act through the services' own APIs (Ollama `keep_alive`, argus-http evict and warm), not through the
agent token. [deploy/broker.staging.compose.yaml](../deploy/broker.staging.compose.yaml) runs one on `:8121`.

### Octopus Deploy

[.octopus/](../.octopus/README.md) deploys the broker and the agents through Octopus Deploy: a report-only staging
broker first, then production, with the image pinned per release and runbooks for evict, warm, restart and health.
The [write-up](https://dragonhound.dev/updates/2026-11-28-iris-octopus-deploy/) covers what it replaced, what was tested and what it cost.

### Adding a service or a GPU host

A service needs two settings: `ARGUS_GPU_BROKER_URL=http://<broker>:8120` and `ARGUS_GPU_BROKER_TOKEN`. With
argus-cortex 0.15 the client takes the broker coordinator from the URL alone, finds `gpu-tenant.yaml`, registers it at
startup (`POST /tenants`) and renews it every five minutes. Older clients register with their first lease and also need
`ARGUS_GPU_COORDINATOR=broker`. Each manifest resident becomes a service once some node's agent reports its container
(by name, else compose service): the node is that agent's, and `base_url` is that agent's host plus the container's
published port (the resident's `port` picks one when it publishes several). The caller's own host is never used. A
resident with `admin_auth` is called with the admin token the client sent (`X-Argus-Admin-Token`, from its
`ARGUS_ADMIN_TOKEN`), and only that: an operator's `token_env` never goes to a registered host. `iris.yaml` entries win:
a resident whose container a configured service already reads is left to it. `GET /services` lists every service with
its origin, and `GET /registry` shows each tenant's residents and where they landed or why not (the dashboard's
Services view).

Removal needs nothing either. A client signs off on shutdown (`DELETE /tenants/{tenant}`), which only lapses the
registration: its services stay while their containers exist, because iris may be the one that stopped them, and go
once an agent reports the container removed. `?purge=true` (the dashboard's Forget) drops it at once. Registrations,
with their tokens, persist in `$IRIS_STATE_DIR/registrations.json` (mode 0600; the compose file mounts a volume).

For iris to start or restart a registered container, the host's agent must allow it: on `IRIS_AGENT_CONTAINERS`, or,
where the agent sets `IRIS_AGENT_MANAGE_LABEL=1`, labelled `argus.iris.manage: "true"` in the service's own compose file.
Neither the broker nor a manifest can widen that.

A new GPU host runs `iris-agent` with `IRIS_AGENT_NODE`, `IRIS_AGENT_TOKEN` and `IRIS_BROKER_URL`. It announces itself
every minute (`POST /nodes`, authenticated with the agent token) and becomes a node at the address it called from
(`IRIS_AGENT_URL` overrides that). Configured nodes win on name; `DELETE /nodes/{name}` forgets an announced one.
`register_services: false` or `register_nodes: false` in `iris.yaml` turns either off.

### Service directory

Any container whose compose file labels it `argus.peer: <name>` (the name callers ask for: `nudenet`, `singguard`,
`lens`) is listed in iris's service directory, GPU or not. iris finds it in the node agents' inventories and reaches it
at the agent's host and the published port. The rules match registered residents: the optional `argus.port` label picks
one when a container publishes several, and a loopback-only bind is reported and not served. Health comes from
`argus.health` (default `/health`) every 30 s. A body that validates as argus-cortex's `ServiceHealth` is read through
that model (`health.contract` is true); anything else is read field by field and is up or down by its HTTP status.
`health.capabilities` is always a list of the names that are on, whether the service sent a list or a map of name to
boolean (`service_version` 1.1, argus-cortex 0.16.1).
`peers:` in `iris.yaml` adds URLs for peers outside the agents' view; configured peers come first.

The entries and the lookup reply are argus-cortex's wire models (`argus_cortex.service.DirectoryEntry`, `EntryHealth`,
`PeerLookup`; `directory_version` 1.0, `service-directory-schema.json`), the ones cortex's `peers` client reads. A peer
name is lowercase letters, digits and single hyphens (`^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`). A container labelled with
anything else, or with a health path that is not a path, is left out of the directory and logged once
(`directory_label_refused`); a `peers:` key that is not a peer name fails when the config loads.

- `GET /directory`: every entry, with its URL (or why it has none) and health.
- `GET /directory/{peer}`: where to reach a peer, best first (healthiest, then node order). Callers use it to find a
  peer (`argus_cortex.peers.peer_url(..., discover=True)`). It decides where work is sent, so it needs
  `ARGUS_GPU_BROKER_TOKEN` set on the broker and presented by the caller. A peer with nothing reachable, or a name no
  peer can have, is a 404.
- `GET /directory/sd`: Prometheus HTTP service discovery (`http_sd_configs`), one target per reachable service, labelled
  `argus_peer`, `argus_service`, `node`, `container` and `argus_health_path`.

The dashboard's **Suite** view (`/ui/#suite`) lists them, with any failing checks at the top.

iris is in its own directory: its compose files label the broker `argus.peer: iris` and each host's agent
`argus.peer: iris-agent`.

### Health

`GET /health` on the broker and on the agent is argus-cortex's `ServiceHealth` (`service`, `version`, `status`,
`started_at`, `wire`, `checks`, ...) with each one's own fields beside it, unchanged: the broker's `mode`, `polls`,
`leases`, `agents`, `pins_missing`, `crowded_out` and `registered`; the agent's `node`, `last_poll_age_s`, `nvml_error`,
`docker_error` and `actions_enabled`. `status` is `degraded` while the broker cannot reach an agent (check `agents`), or
while the agent has no NVML sample (check `nvml`); both read the last poll, so they are never stale, and the route always
answers 200. `GET /health?checks=0` skips the checks.

### Suite settings

`argus-iris serve` reads the tokens its config names (`caller_token_env`, `agent_token_env`, each service's
`token_env`) from the suite file (`argus_cortex.settings`: `~/.config/argus/suite.env`, or `/etc/argus/suite.env` in a
container) when the environment leaves them unset or blank. The environment wins, nothing else is taken from the file,
and only the names are printed. `ARGUS_ENV_FILE=off` disables it. The agent does not read the file.

### Leases

Callers take leases with the broker coordinator (above). A service target goes to the configured services that serve it, in config order (`ollama-rtx4060` before
`ollama-gtx1070`), preferring a node where the model is already loaded and skipping services that are down; the grant
carries that node and its `base_url`. A service also answers the names in its `serves` list (rtx3090's argus-echo
XTTS has `serves: [xtts]`, so an `xtts` lease that does not fit on rtx4060 goes there). When `ARGUS_GPU_BROKER_TOKEN` is set on the broker, the lease routes require it
as a bearer token; unset, they are open. `GET /leases` lists what is queued or granted (`?all=true` adds the last ten
minutes' finished leases).

One manual action exists: bring a service back up. If its health check fails, the node's agent starts its container
(it must be on that agent's `IRIS_AGENT_CONTAINERS`), and iris waits for it to answer:

```bash
curl -X POST -H "Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN" http://rtx3090:8120/services/comfyui/warm
```

It needs `ARGUS_GPU_BROKER_TOKEN` (callers) and `IRIS_AGENT_TOKEN` (matching the agents') set on the broker.

The other manual action frees a resident's VRAM through its adapter: Ollama gets `keep_alive: 0` for that model,
ComfyUI `POST /free`, an argus-http service its `evict_path` (GPU to CPU, kept warm) or else its `unload_path`, and an
external service (Immich's ML server) has its container stopped, only with `evict_by: stop` (else 422). A busy
resident is refused with 409; iris then waits until the agent sees the memory come back (`evict_confirm_s`, 30 s) and
reports what the resident still holds (a CUDA context, say, which only a restart frees):

```bash
curl -X POST -H "Authorization: Bearer $ARGUS_GPU_BROKER_TOKEN" \
  "http://rtx3090:8120/residents/rtx4060/xtts/evict?mode=auto"   # mode: auto | evict | unload
```

The broker also serves a dashboard at `http://<broker>:8120/ui/` (`/` redirects there). **GPUs** has a card per node
and then each node in full: VRAM split into
residents, unattributed, held-back and admissible memory, plus residents with idle times, services with a warm button, residents with an evict button,
and the node's leases (marked "over" where admission would have refused them). A service is flagged when what the node
can never free (pins, unmanaged processes, unloaded residues) leaves less room than it needs; `/health` lists those
under `crowded_out`.
It updates live from `/events`. **Services** (`/ui/#services`) lists every service with where it came from, how iris
reaches it and whether its agent will start it, each registered tenant with where its residents landed (or why not),
and every node, with Forget for registrations and announced nodes. **Connect** (`/ui/#connect`) has the settings to copy
for a new service or GPU host. To warm, evict or forget from the page, enter `ARGUS_GPU_BROKER_TOKEN` under "Broker
token". It is kept in that tab's session storage only.

