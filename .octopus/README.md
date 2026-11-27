# Deploying iris with Octopus Deploy

Issue [#65](https://github.com/smk762/argus-iris/issues/65). Octopus Cloud deploys the broker and the agents through
polling Tentacles: Staging (a report-only broker on `:8121`), then Production (the broker on `:8120` and every agent).

| Path | What | Managed by |
|---|---|---|
| `terraform/` | Space, environments, lifecycle, GHCR and GitHub feeds, Git credential, project, release trigger | Terraform |
| `project/` | Deployment process, runbooks, non-sensitive variables (OCL) | Octopus Config as Code: edit in the UI, it commits to a branch, PR to `dev` |
| `scripts/` | What each deployment step does | This repo |
| `runbooks/` | What each runbook does | This repo |

The scripts read environment variables only, so each one runs the same by hand as in a step. Each step's inline
script exports the variables and calls one of them (see [Steps](#steps)).

## Setup, in order

1. **Instance.** Create an Octopus Cloud instance. Skip the signup's *Add New Project*: Terraform creates the project
   in its own space, with Git storage. Make an API key (Profile → My API keys) with *Full access*: Terraform creates
   a space, a system-level permission, and a custom scope can only name spaces that already exist. Give it an expiry.
2. **Terraform.** Create a fine-grained GitHub token for this repository only, with *Contents: read and write*.
   ```bash
   cd .octopus/terraform
   cp .env.example .env                    # instance URL, API key, your user id, GitHub token
   set -a; . ./.env; set +a
   terraform init && terraform apply
   ```
   `.env` and the state hold both secrets; they stay local and out of git.
3. **Tentacles.** On each host: Infrastructure → Deployment Targets → Add → Linux → *Polling Tentacle*, and run the
   script it generates. Name the target after the node, as the broker config does. Only outbound connections, so no
   inbound ports.

   | Target | Environments | Target tags |
   |---|---|---|
   | morpheus | Staging, Production | `iris-broker`, `iris-agent` |
   | dragon | Production | `iris-agent` |
   | jarrah | Production | `iris-agent` |

4. **Variables** (project → Variables). Sensitive ones are kept in Octopus, never in git.

   | Name | Value | Scope |
   |---|---|---|
   | `IRIS_ENV` | `#{Octopus.Environment.Name \| ToLower}` | |
   | `IRIS_TAG` | `#{Octopus.Action[Render broker config].Package[argus-iris].PackageVersion}` (check the exact form) | |
   | `IRIS_PORT` | `8121` / `8120` | Staging / Production |
   | `IRIS_AGENT_NODE` | `#{Octopus.Machine.Name}` | |
   | `HOST_IP` | the LAN address services bind to | per target |
   | `IRIS_BROKER_URL` | the production broker's URL | Production |
   | `IRIS_AGENT_CONTAINERS` | e.g. `voluptas_comfyui` | per target |
   | `IRIS_CONFIG_BODY` | the whole `iris.yaml` (multi-line). It lists LAN hosts, so it stays out of this public repo | |
   | `IRIS_POLICY_BODY` | the whole `policy.yaml` (multi-line) | |
   | `IRIS_AGENT_TOKEN`, `ARGUS_GPU_BROKER_TOKEN`, `ARGUS_ADMIN_TOKEN`, `RAG_CHAT_API_KEY`, `LENS_ADMIN_TOKEN`, `ECHO_ADMIN_TOKEN` | sensitive | Production only |

   Staging gets no secrets on purpose: the staging broker runs with `IRIS_REPORT_ONLY=1` and no tokens, so it can
   read everything and change nothing.

5. **Process** (see [Steps](#steps)), then set `TF_VAR_release_trigger=true` in `.env` and apply again.

## Steps

Every step is *Run a Script* (bash, inline), references the **GitHub releases** feed package `smk762/argus-iris`
(extracted, reference name `iris-src`), and starts with this prelude:

```bash
# Octopus defines get_octopusvariable only in this shell, so pass what the scripts need on as environment.
for v in IRIS_ENV IRIS_TAG IRIS_PORT IRIS_AGENT_NODE HOST_IP IRIS_BROKER_URL IRIS_AGENT_CONTAINERS \
         IRIS_CONFIG_BODY IRIS_POLICY_BODY IRIS_AGENT_TOKEN ARGUS_GPU_BROKER_TOKEN ARGUS_ADMIN_TOKEN \
         RAG_CHAT_API_KEY LENS_ADMIN_TOKEN ECHO_ADMIN_TOKEN RESIDENT_ID EVICT_MODE SERVICE_ID WARM_FORCE; do
  val=$(get_octopusvariable "$v"); [ -n "$val" ] && export "$v=$val"
done
export IRIS_IN_OCTOPUS=1 REPO_DIR=$(ls -d "$PWD"/iris-src/*/ | head -n1)
```

| # | Step | Runs on | Environments | Then |
|---|---|---|---|---|
| 1 | Deploy agents | tag `iris-agent`, rolling, window 1 | Production | `"$REPO_DIR/.octopus/scripts/deploy-agent.sh"` |
| 2 | Render broker config | tag `iris-broker` | both | `"$REPO_DIR/.octopus/scripts/render-config.sh"` |
| 3 | Approve policy change | Manual intervention; run condition `#{Octopus.Action[Render broker config].Output.NeedsApproval}`; instructions show `#{Octopus.Action[Render broker config].Output.ApprovalReasons}` | Production | |
| 4 | Deploy broker | tag `iris-broker` | both | `"$REPO_DIR/.octopus/scripts/deploy-broker.sh"` |
| 5 | Verify | tag `iris-broker` | both | `"$REPO_DIR/.octopus/scripts/verify.sh"` |

Step 2 also references the **GHCR** feed package `smk762/argus-iris` as `argus-iris` (not acquired). Its version is
the release's version and the release trigger watches it. Step 2, not step 1, because it runs in both environments.

`render-config.sh` validates the new files with `argus-iris check`, run from the release's own image. A file that
does not load fails the deployment before the running broker restarts. Approval is needed when admission changes, an
enforced rule is new or changed, or there is no previous config (the first deployment).

## Runbooks

Same prelude; run on tag `iris-broker` unless noted.

| Runbook | Prompted variables | Script |
|---|---|---|
| Evict resident | `RESIDENT_ID`, `EVICT_MODE` (auto, evict, unload) | `RUNBOOK=evict "$REPO_DIR/.octopus/runbooks/broker-call.sh"` |
| Warm service | `SERVICE_ID`, `WARM_FORCE` | `RUNBOOK=warm "$REPO_DIR/.octopus/runbooks/broker-call.sh"` |
| Restart agent | target (tag `iris-agent`) | `"$REPO_DIR/.octopus/runbooks/restart-agent.sh"` |
| Health check | (scheduled hourly, Production) | `"$REPO_DIR/.octopus/runbooks/health-check.sh"` |

## By hand

The same scripts without Octopus, from a checkout on the broker host:

```bash
IRIS_ENV=production IRIS_TAG=0.2.3 IRIS_CONFIG_BODY="$(cat deploy/iris.yaml)" IRIS_POLICY_BODY="$(cat deploy/policy.yaml)" \
  .octopus/scripts/render-config.sh
IRIS_ENV=production HOST_IP=0.0.0.0 .octopus/runbooks/health-check.sh
```
