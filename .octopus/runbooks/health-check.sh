#!/usr/bin/env bash
# Runbook "Health check" (#65), scheduled hourly against production: verify.sh with no expected version. A runbook
# has no release, so IRIS_TAG (bound to the release's package version) is skipped, not blanked: under Octopus a blank
# variable falls through to the project's own value.
IRIS_ENV=${IRIS_ENV:-production} IRIS_SKIP_VERSION=1 exec "$(dirname "$0")/../scripts/verify.sh"
