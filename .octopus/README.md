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
   | the broker host | Staging, Production | `iris-broker`, `iris-agent` |
   | every other GPU host | Production | `iris-agent` |

4. **Variables.** With Config as Code, every non-sensitive variable is committed to this public repo
   ([project/variables.ocl](project/variables.ocl): `IRIS_ENV`, `IRIS_TAG`, `IRIS_PORT`, `IRIS_AGENT_NODE`). Anything
   private goes in as **sensitive** (project → Variables), which Octopus keeps in its database:

   | Name | Value | Scope |
   |---|---|---|
   | `IRIS_CONFIG_B64` | `base64 -w0 deploy/iris.yaml` (it lists LAN hosts; a sensitive value is one line) | |
   | `IRIS_POLICY_B64` | `base64 -w0 deploy/policy.yaml` | |
   | `HOST_IP` | the LAN address services bind to | per target |
   | `IRIS_BROKER_URL` | the production broker's URL | Production |
   | `IRIS_AGENT_CONTAINERS` | e.g. `comfyui` | per target |
   | `IRIS_AGENT_TOKEN`, `ARGUS_GPU_BROKER_TOKEN`, `ARGUS_ADMIN_TOKEN`, `RAG_CHAT_API_KEY`, `LENS_ADMIN_TOKEN`, `ECHO_ADMIN_TOKEN` | | Production only |

   Staging gets no secrets on purpose: the staging broker runs with `IRIS_REPORT_ONLY=1` and no tokens, so it can
   read everything and change nothing.

   Edit the process and the non-sensitive variables as OCL in this repo, not in the Octopus UI: a UI save commits
   with the signed-in user's name and email (the login), and pushes at once.

5. **Process.** It is [project/deployment_process.ocl](project/deployment_process.ocl) (see [Steps](#steps)). Then set `TF_VAR_release_trigger=true` in `.env` and apply again.

## Steps

Every script step is *Run a Script* (bash, inline), references the **GitHub releases** feed package
`smk762/argus-iris` (extracted, reference name `iris-src`), and starts with a prelude: Octopus defines
`get_octopusvariable` only in the step's own shell, so it exports what the script needs, then finds the extracted
source (`Octopus.Action.Package[iris-src].ExtractedPath`, or the one folder inside it) as `REPO_DIR`.
Scripts run through `bash`: the source package is GitHub's zip of the tag, which carries no file modes, so nothing
in it is executable.

| # | Step | Runs on | Environments | Then |
|---|---|---|---|---|
| 1 | Render broker config | tag `iris-broker` | both | `bash "$REPO_DIR/.octopus/scripts/render-config.sh"` |
| 2 | Approve policy change | Manual intervention; run condition `#{Octopus.Action[Render broker config].Output.NeedsApproval}`; instructions show `#{Octopus.Action[Render broker config].Output.ApprovalReasons}` | Production | |
| 3 | Deploy agents | tag `iris-agent`, rolling, window 1 | Production | `bash "$REPO_DIR/.octopus/scripts/deploy-agent.sh"` |
| 4 | Deploy broker | tag `iris-broker` | both | `bash "$REPO_DIR/.octopus/scripts/deploy-broker.sh"` |
| 5 | Verify | tag `iris-broker` | both | `bash "$REPO_DIR/.octopus/scripts/verify.sh"` |

Step 1 also references the **GHCR** feed package `smk762/argus-iris` as `argus-iris` (not acquired). Its version becomes
`IRIS_TAG` and the release trigger watches it.

Nothing on a host changes before the approval: step 1 only writes `.next` files beside the deployed ones. A rejected
change therefore leaves the agents and the broker on the release they were running.

`deploy-broker.sh` swaps the files in and recreates the broker. When both files are byte for byte the deployed ones it
leaves the container to compose, so a release that changes neither the image nor the config does not restart it.

`render-config.sh` validates the new files with `argus-iris check`, run from the release's own image. A file that
does not load fails the deployment before the running broker restarts. Approval is needed when the config or policy
differs at all from the deployed one as the broker reads it, when the deployed files no longer load, or when there is
no previous config (the first deployment).

## Runbooks

They are OCL too, one file each in [project/runbooks/](project/runbooks/), and run on Production only. Same prelude;
each runs on tag `iris-broker` unless noted. The prompted variables are in `variables.ocl`, scoped to their runbook, so
a deployment never asks for them.

| Runbook | Prompted variables | Calls |
|---|---|---|
| Evict resident | `RESIDENT_ID`, `EVICT_MODE` (auto, evict, unload) | `RUNBOOK=evict bash "$REPO_DIR/.octopus/runbooks/broker-call.sh"` |
| Warm service | `SERVICE_ID`, `WARM_FORCE` | `RUNBOOK=warm bash "$REPO_DIR/.octopus/runbooks/broker-call.sh"` |
| Restart agent | none; pick the targets when running it (tag `iris-agent`, one at a time) | `bash "$REPO_DIR/.octopus/runbooks/restart-agent.sh"` |
| Health check | none; scheduled hourly | `IRIS_SKIP_VERSION=1 bash "$REPO_DIR/.octopus/scripts/verify.sh"` |

Guided failure is off for all four, whatever the environment says: Production has it on, and a failed hourly health
check would otherwise wait for a person and hold up the next one.

A runbook has no release. It takes the newest `iris-src` package, and its own OCL from the branch it is run on (any
branch by hand; a schedule always uses the default branch). The hourly schedule is Terraform's: once the runbook is on
`dev`, set `TF_VAR_health_check_trigger=true` in `.env` and apply.

## By hand

The same scripts without Octopus, from a checkout on the broker host:

```bash
IRIS_ENV=production IRIS_TAG=0.2.3 IRIS_CONFIG_BODY="$(cat deploy/iris.yaml)" IRIS_POLICY_BODY="$(cat deploy/policy.yaml)" \
  .octopus/scripts/render-config.sh
IRIS_ENV=production HOST_IP=0.0.0.0 .octopus/runbooks/health-check.sh
```
