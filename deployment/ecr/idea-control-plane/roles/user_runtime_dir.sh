#!/bin/bash
#
# PAM session hook (pam_exec, as root): give the user logging in a private tmux directory under a
# parent only root can write. tmux's default, tmux-<uid> under the shared /tmp, is a name any user
# can create first, and tmux then refuses to start for its owner. sshd sets TMUX_TMPDIR to the
# parent, so tmux finds the directory made here.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

[[ "${PAM_TYPE:-}" == open_session ]] || exit 0
BASE="${IDEA_USER_TMUX_BASE:-/run/idea-tmux}"
uid="$(id -u -- "${PAM_USER:?}")"
gid="$(id -g -- "${PAM_USER}")"
(( uid != 0 )) || exit 0
dir="${BASE}/tmux-${uid}"
# The parent is root's alone, so nothing but this hook makes or replaces an entry in it.
[[ -d "${dir}" ]] || mkdir -m 0700 "${dir}"
chown "${uid}:${gid}" "${dir}"
chmod 0700 "${dir}"
