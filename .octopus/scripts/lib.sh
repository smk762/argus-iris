# Shared by the deploy and runbook scripts (#65). They read plain environment variables only, so a script runs the same
# by hand as in an Octopus step. Octopus defines get_octopusvariable in the step's own shell, not in a script that
# step starts, so each step's inline script exports what these need first (.octopus/README.md, "Steps").
set -euo pipefail

# Where the broker and the agent share one compose project (the root compose.yaml starts both as `argus-iris`), each of
# these scripts runs one compose file and compose calls the other's container an orphan, on stderr, which Octopus
# marks as a warning on a healthy step. They are not orphans.
export COMPOSE_IGNORE_ORPHANS=1

# var NAME [DEFAULT]: $NAME, else DEFAULT, else fail.
var() {
  local value=${!1:-}
  if [ -z "$value" ]; then
    if [ $# -ge 2 ]; then value=$2; else echo "missing variable: $1" >&2; exit 2; fi
  fi
  printf '%s' "$value"
}

# output NAME VALUE: an Octopus output variable. set_octopusvariable only prints a service message to stdout, which
# Octopus reads from any process, so this writes that message itself (IRIS_IN_OCTOPUS=1, set by the step).
output() {
  local enc_name enc_value
  enc_name=$(printf '%s' "$1" | base64 -w0)
  enc_value=$(printf '%s' "$2" | base64 -w0)
  echo "output $1=$2"
  if [ -n "${IRIS_IN_OCTOPUS:-}" ]; then echo "##octopus[setVariable name='$enc_name' value='$enc_value']"; fi
}

# The repository checkout or extracted source package: the directory holding deploy/.
repo_dir() {
  local here
  here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
  var REPO_DIR "$here"
}

# local_ip: where to reach a port published on HOST_IP from this host. 0.0.0.0 (every interface) is a bind address,
# not one to call.
local_ip() {
  local ip
  ip=$(var HOST_IP 127.0.0.1)
  if [ "$ip" = 0.0.0.0 ]; then ip=127.0.0.1; fi
  printf '%s' "$ip"
}

# wait_health URL SECONDS: poll URL until it answers 2xx; print the body.
wait_health() {
  local url=$1 deadline=$((SECONDS + $2)) body
  until body=$(curl -fsS --max-time 3 "$url" 2>/dev/null); do
    if [ $SECONDS -ge $deadline ]; then echo "no healthy answer from $url after $2 s" >&2; return 1; fi
    sleep 2
  done
  printf '%s' "$body"
}

# json_get FIELD: a top-level field of the JSON on stdin (python3 is on every host; jq is not).
json_get() {
  python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print(json.dumps(v) if isinstance(v,(dict,list,bool)) or v is None else v)' "$1"
}
