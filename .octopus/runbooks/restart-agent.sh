#!/usr/bin/env bash
# Runbook "Restart agent" (#65): restart iris-agent on the target Octopus runs it on, then wait for its health.
# shellcheck source=../scripts/lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/../scripts/lib.sh"

docker restart iris-agent
health=$(wait_health "http://$(local_ip):9110/health" 60)
echo "iris-agent $(json_get version <<<"$health") healthy"
