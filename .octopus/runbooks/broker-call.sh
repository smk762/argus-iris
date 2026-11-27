#!/usr/bin/env bash
# Runbooks "Evict resident" and "Warm service" (#65): one authenticated call to the production broker, run on the
# broker target. The answer is printed whatever it is; a 4xx/5xx fails the run.
#   RUNBOOK=evict RESIDENT_ID=rtx3090/lens EVICT_MODE=auto .octopus/runbooks/broker-call.sh
#   RUNBOOK=warm SERVICE_ID=lens .octopus/runbooks/broker-call.sh
# shellcheck source=../scripts/lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/../scripts/lib.sh"

url="http://$(local_ip):$(var IRIS_PORT 8120)"
token=$(var ARGUS_GPU_BROKER_TOKEN)
quote() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/:"))' "$1"; }

case "$(var RUNBOOK)" in
  # Resident ids hold slashes (node/service/model); the route takes them as a path.
  evict) path="/residents/$(quote "$(var RESIDENT_ID)")/evict?mode=$(var EVICT_MODE auto)" ;;
  warm) path="/services/$(quote "$(var SERVICE_ID)")/warm?force=$(var WARM_FORCE false)" ;;
  *) echo "RUNBOOK must be evict or warm" >&2; exit 2 ;;
esac

body=$(mktemp)
trap 'rm -f "$body"' EXIT
code=$(curl -sS -o "$body" -w '%{http_code}' -X POST -H "Authorization: Bearer $token" "$url$path")
python3 -m json.tool "$body" 2>/dev/null || cat "$body"
echo "POST $path -> $code"
[ "${code:0:1}" = 2 ]
