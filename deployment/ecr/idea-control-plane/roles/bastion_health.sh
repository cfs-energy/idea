#!/bin/bash
# The bastion task's health check: is sshd serving, not has someone taken every startup slot.
# MaxStartups is global, so a client holding 50 or more unauthenticated connections could refuse
# ssh-keyscan three times running and have a healthy task replaced, dropping every session on it,
# and its replacement would meet the same flood. When sshd turns a connection away it still
# accepts it, answers "Not allowed at this time" and closes, which only a listening, responsive
# sshd does; loopback is exempt from penalties, so here that answer means MaxStartups. That
# answer counts as healthy and is noted for the metrics process. No answer at all is not.
# Usage: bastion_health.sh [port]
port=${1:-22}
timeout 3 ssh-keyscan -t ed25519 -p "${port}" 127.0.0.1 >/dev/null 2>&1 && exit 0
# shellcheck disable=SC2016  # expanded by the inner shell
reply=$(timeout 1 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1" && IFS= read -r line <&3; printf %s "${line:-}"' check "${port}" 2>/dev/null)
case "${reply}" in
  SSH-*) exit 0 ;;
  'Not allowed at this time'*)
    drops=${SSHD_DROPS_FILE:-/run/idea-sshd-drops}
    if [[ -f "${drops}" ]]; then echo 'loopback check refused' >> "${drops}"; fi
    exit 0
    ;;
esac
exit 1
