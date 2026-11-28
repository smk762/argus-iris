#!/usr/bin/env bash
# Deploy iris-agent on this host and wait until it serves this release (#65). Octopus runs it on every `iris-agent`
# target, one at a time.
#   IRIS_TAG=0.2.3 IRIS_AGENT_NODE=rtx3090 .octopus/scripts/deploy-agent.sh
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/lib.sh"

repo=$(repo_dir)
IRIS_TAG=$(var IRIS_TAG); export IRIS_TAG
IRIS_AGENT_NODE=$(var IRIS_AGENT_NODE); export IRIS_AGENT_NODE
HOST_IP=$(var HOST_IP 127.0.0.1); export HOST_IP
IRIS_AGENT_TOKEN=$(var IRIS_AGENT_TOKEN ""); export IRIS_AGENT_TOKEN
IRIS_AGENT_CONTAINERS=$(var IRIS_AGENT_CONTAINERS ""); export IRIS_AGENT_CONTAINERS
IRIS_AGENT_MANAGE_LABEL=$(var IRIS_AGENT_MANAGE_LABEL ""); export IRIS_AGENT_MANAGE_LABEL
IRIS_BROKER_URL=$(var IRIS_BROKER_URL ""); export IRIS_BROKER_URL
IRIS_AGENT_URL=$(var IRIS_AGENT_URL ""); export IRIS_AGENT_URL
IRIS_AGENT_POLL_S=$(var IRIS_AGENT_POLL_S 5); export IRIS_AGENT_POLL_S

# Replace the running agent in place: reuse the compose project that owns the iris-agent container (argus-iris where
# the root compose.yaml runs it, iris-agent on hosts set up by hand), else a second project clashes on container_name.
# An iris-agent that compose did not create (docker run) cannot be replaced by any project, and removing it here would
# lose whatever it was started with: stop and say so.
if docker inspect iris-agent >/dev/null 2>&1; then
  project=$(docker inspect iris-agent --format '{{index .Config.Labels "com.docker.compose.project"}}')
  if [ -z "$project" ]; then
    echo "the iris-agent container on this host was not created by compose, so this deploy cannot replace it." >&2
    echo "Note its settings (docker inspect iris-agent), remove it (docker rm -f iris-agent), then retry." >&2
    exit 1
  fi
else
  project=argus-iris
fi
echo "compose project: $project"
docker compose -p "$project" -f "$repo/deploy/agent.compose.yaml" pull iris-agent
docker compose -p "$project" -f "$repo/deploy/agent.compose.yaml" up -d --no-build iris-agent

health=$(wait_health "http://$(local_ip):9110/health" 60)
version=$(json_get version <<<"$health")
[ "$version" = "$IRIS_TAG" ] || { echo "agent reports $version, expected $IRIS_TAG" >&2; exit 1; }
echo "iris-agent $version healthy on $IRIS_AGENT_NODE"
