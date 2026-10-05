#!/bin/bash
# shellcheck disable=SC2154
# Sourced by each builder stage. Persist failures across both subshells and reboots.
mkdir -p /var/lib/idea
# EL8 stock images may expose Python only as platform-python.
BAKE_PYTHON=$(command -v python3 || command -v /usr/libexec/platform-python)
set -E
bake_stage_error() {
  # the command and its status: the bootstrap check reports the first line as the reason
  printf '%s: %s (exit %s)\n' "${BAKE_STAGE}" "${BASH_COMMAND}" "$?" >> /var/lib/idea/bake-failed
}
bake_stage_exit() {
  local code=$?
  [[ $code -ne 0 ]] || return 0
  trap - ERR EXIT
  printf '%s: stage exited %s\n' "${BAKE_STAGE}" "$code" >> /var/lib/idea/bake-failed
  # Nested stage exits must preserve the first failing check already reported by the child.
  if "${BAKE_PYTHON}" -c 'import json; assert any(not c["ok"] for c in json.load(open("/var/lib/idea/image-checks.json"))["checks"])' 2>/dev/null; then
    return 0
  fi
  /bin/bash "${SCRIPT_DIR}/image_checks.sh" "--failed-${BAKE_STAGE}"
}
trap bake_stage_error ERR
trap bake_stage_exit EXIT
