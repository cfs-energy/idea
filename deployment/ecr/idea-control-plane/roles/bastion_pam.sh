#!/bin/bash
#
# The bastion's edits to the sshd PAM stack (the file is the first argument, /etc/pam.d/sshd at
# start). Container sessions have no host audit login UID to set, and each session runs the hook
# that makes the user's private tmux directory. Running it again changes nothing.
set -euo pipefail
FILE="${1:-/etc/pam.d/sshd}"
# rewritten in place, keeping the file's owner and mode
kept="$(grep -v 'pam_loginuid.so' "${FILE}" || true)"
printf '%s\n' "${kept}" > "${FILE}"
grep -q 'user_runtime_dir.sh' "${FILE}" \
  || echo 'session optional pam_exec.so quiet /opt/idea/roles/user_runtime_dir.sh' >> "${FILE}"
