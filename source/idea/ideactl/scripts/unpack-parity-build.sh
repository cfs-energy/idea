#!/usr/bin/env bash
# Unpacks the private parity build job's outputs over this job's checkout. Only the three output
# directories are accepted, so nothing the checkout runs outside the test namespace (scripts,
# actions, the tool list) can come from the build job. Any other member stops the job.
set -euo pipefail
ARCHIVE=${1:?archive}
DESTINATION=${2:?destination}
OUTSIDE=$(tar -tzf "$ARCHIVE" | grep -Ev '^source/idea/ideactl/(node_modules|dist|\.parity-oracles)(/|$)' || true)
if [[ -n "$OUTSIDE" ]]; then
  echo "::error::The parity build archive holds files outside its three output directories:"
  printf '%s\n' "$OUTSIDE" | head -n 20
  exit 1
fi
tar -xzf "$ARCHIVE" -C "$DESTINATION"
