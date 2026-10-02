#!/bin/bash
# shellcheck disable=SC2154
# Sourced by each builder stage. Persist failures across both subshells and reboots.
mkdir -p /var/lib/idea
# EL8 stock images may expose Python only as platform-python.
BAKE_PYTHON=$(command -v python3 || command -v /usr/libexec/platform-python)
set -E
bake_stage_error() {
  printf '%s\n' "${BAKE_STAGE}" >> /var/lib/idea/bake-failed
}
bake_stage_exit() {
  local code=$?
  if [[ $code -ne 0 ]]; then
    trap - ERR EXIT
    bake_stage_error
    # Nested stage exits must preserve the first failing check already reported by the child.
    if "${BAKE_PYTHON}" -c 'import json; assert any(not c["ok"] for c in json.load(open("/var/lib/idea/image-checks.json"))["checks"])' 2>/dev/null; then
      return
    fi
    /bin/bash "${SCRIPT_DIR}/image_checks.sh" "--failed-${BAKE_STAGE}"
  fi
}
trap bake_stage_error ERR
trap bake_stage_exit EXIT
