#!/usr/bin/env bash
# Write the broker's next config and policy, validate them with the release's own image, and decide whether the
# change needs a person to approve it (#65). Nothing running changes: deploy-broker.sh swaps the files in.
#   IRIS_ENV=production IRIS_TAG=0.2.3 IRIS_CONFIG_BODY="$(cat iris.yaml)" IRIS_POLICY_BODY="$(cat policy.yaml)" \
#     .octopus/scripts/render-config.sh
# Octopus passes IRIS_CONFIG_B64 and IRIS_POLICY_B64 (base64 of each file) instead of the *_BODY pair.
# Output variables: NeedsApproval (True/False) and ApprovalReasons.
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "$0")/lib.sh"

env_name=$(var IRIS_ENV)
tag=$(var IRIS_TAG)
root=$(var IRIS_DEPLOY_ROOT "/srv/argus-iris/$env_name")
install -d -m 0750 "$root"

# The live config lists LAN hosts and stays out of git. With Config as Code every non-sensitive Octopus variable is
# committed to this public repo, so Octopus holds both files as sensitive variables, base64 (a sensitive value is one
# line). By hand, the plain *_BODY form is simpler.
body() {
  if [ -n "${!2:-}" ]; then printf '%s' "${!2}" | base64 -d; else var "$1" "${3:-}"; fi
}
config_body=$(body IRIS_CONFIG_BODY IRIS_CONFIG_B64)
policy_body=$(body IRIS_POLICY_BODY IRIS_POLICY_B64 "residents: []")
printf '%s\n' "$config_body" >"$root/iris.yaml.next"
printf '%s\n' "$policy_body" >"$root/policy.yaml.next"
chmod 0640 "$root/iris.yaml.next" "$root/policy.yaml.next"
if [ -f "$root/iris.yaml" ]; then
  diff -u "$root/policy.yaml" "$root/policy.yaml.next" --label deployed --label next || true
  diff -u "$root/iris.yaml" "$root/iris.yaml.next" --label deployed --label next || true
fi

# Validated by the code that will read it, so a bad file fails here and the running broker is never restarted onto
# it. A clean environment: IRIS_REPORT_ONLY would hide the enforced rules this compares.
summary=$(docker run --rm --entrypoint argus-iris -v "$root:/cfg:ro" -e ARGUS_ENV_FILE=off \
  "ghcr.io/smk762/argus-iris:$tag" check -c /cfg/iris.yaml.next --policy /cfg/policy.yaml.next \
  --previous-config /cfg/iris.yaml --previous-policy /cfg/policy.yaml)
echo "$summary"

reasons=$(python3 -c 'import json,sys; print("; ".join(json.load(sys.stdin)["approval"]))' <<<"$summary")
if [ -n "$reasons" ]; then output NeedsApproval True; else output NeedsApproval False; fi
output ApprovalReasons "${reasons:-none}"
