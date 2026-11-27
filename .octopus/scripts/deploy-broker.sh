#!/usr/bin/env bash
# Swap in the config render-config.sh wrote and (re)start the broker for this environment (#65). Production is the
# real broker (deploy/broker.compose.yaml, project argus-iris, :8120); staging the report-only one
# (deploy/broker.staging.compose.yaml, project argus-iris-staging, :8121).
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/lib.sh"

repo=$(repo_dir)
env_name=$(var IRIS_ENV)
root=$(var IRIS_DEPLOY_ROOT "/srv/argus-iris/$env_name")
IRIS_TAG=$(var IRIS_TAG); export IRIS_TAG
HOST_IP=$(var HOST_IP 127.0.0.1); export HOST_IP

if [ ! -f "$root/iris.yaml.next" ] && [ ! -f "$root/iris.yaml" ]; then
  echo "nothing rendered in $root: run render-config.sh first" >&2; exit 1
fi
export IRIS_CONFIG_FILE="$root/iris.yaml" IRIS_POLICY_FILE="$root/policy.yaml"

case "$env_name" in
  production)
    project=argus-iris file=broker.compose.yaml service=argus-iris
    # Only production gets tokens; compose passes these through to the container.
    for t in ARGUS_GPU_BROKER_TOKEN IRIS_AGENT_TOKEN ARGUS_ADMIN_TOKEN RAG_CHAT_API_KEY LENS_ADMIN_TOKEN ECHO_ADMIN_TOKEN; do
      export "$t=$(var "$t" "")"
    done
    ;;
  staging)
    project=argus-iris-staging file=broker.staging.compose.yaml service=argus-iris-staging
    IRIS_STAGING_PORT=$(var IRIS_PORT 8121); export IRIS_STAGING_PORT
    ;;
  *) echo "unknown environment: $env_name" >&2; exit 2 ;;
esac

docker compose -p "$project" -f "$repo/deploy/$file" pull "$service"

# Swap only once the image is here, and file by file, so a retry of this step (a failed pull or up) picks up where it
# stopped: a file with no .next was already swapped in.
for f in iris.yaml policy.yaml; do
  if [ -f "$root/$f.next" ]; then
    if [ -f "$root/$f" ]; then cp -p "$root/$f" "$root/$f.prev"; fi
    mv "$root/$f.next" "$root/$f"
  else
    echo "$root/$f.next is gone: $f was already swapped in"
  fi
done

# Always recreate: each file is bind-mounted by inode, so the mv above leaves a running container on the old one, and
# compose sees nothing to change when only the files did (the same image, a config-only release).
docker compose -p "$project" -f "$repo/deploy/$file" up -d --force-recreate "$service"
