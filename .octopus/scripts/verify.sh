#!/usr/bin/env bash
# Fail unless the broker serves this release, in the mode its environment expects, and reaches every agent its
# config names (#65). Also the scheduled health-check runbook, with IRIS_SKIP_VERSION=1 (any version).
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/lib.sh"

env_name=$(var IRIS_ENV)
port=$(var IRIS_PORT "$([ "$env_name" = staging ] && echo 8121 || echo 8120)")
url="http://$(local_ip):$port"
tag=""
if [ "$(var IRIS_SKIP_VERSION "")" != 1 ]; then tag=$(var IRIS_TAG); fi

health=$(wait_health "$url/health" 90)
fail=0
version=$(json_get version <<<"$health")
if [ -n "$tag" ] && [ "$version" != "$tag" ]; then echo "broker reports $version, expected $tag" >&2; fail=1; fi

report_only=$(json_get report_only <<<"$health")
if [ "$report_only" = null ]; then report_only=false; fi  # a broker from before IRIS_REPORT_ONLY cannot be in it
want=$([ "$env_name" = staging ] && echo true || echo false)
if [ "$report_only" != "$want" ]; then echo "report_only is $report_only, expected $want" >&2; fail=1; fi

# The first poll can land before every agent answers: give unreachable agents a few polls.
for attempt in 1 2 3 4 5 6 7; do
  down=$(json_get agents <<<"$health" | python3 -c 'import json,sys; print(" ".join(n for n, ok in json.load(sys.stdin).items() if not ok))')
  if [ -z "$down" ] || [ "$attempt" = 7 ]; then break; fi
  sleep 5
  health=$(curl -fsS --max-time 3 "$url/health")
done
if [ -n "$down" ]; then echo "agents unreachable: $down" >&2; fail=1; fi

echo "broker $version at $url: mode $(json_get mode <<<"$health"), report_only $report_only, agents $(json_get agents <<<"$health")"
exit $fail
