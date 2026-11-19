#!/bin/bash
#
# tmux on the bastion (installed as /usr/local/bin/tmux, ahead of /usr/bin on sshd's PATH). sshd
# points TMUX_TMPDIR at a parent only root writes, where the login hook made this user's private
# directory. If the hook could not make it, tmux could not start there, and the browser terminal,
# which starts tmux, would fail: this session uses tmux's default location instead and says so.
dir="${TMUX_TMPDIR:-}/tmux-$(id -u)"
if [[ -n "${TMUX_TMPDIR:-}" ]] && ! [[ -d "${dir}" && ! -L "${dir}" && -O "${dir}" ]]; then
  echo "warning: ${dir} is missing or not yours; tmux uses its default location for this session" >&2
  logger -t idea-tmux "${dir} missing for uid $(id -u); tmux fell back to its default location" 2>/dev/null || true
  unset TMUX_TMPDIR
fi
exec "${IDEA_TMUX_BIN:-/usr/bin/tmux}" "$@"
