#!/bin/bash
#
# Facts about the built control-plane image that only the image can settle. CI runs it as root in
# a throwaway container of the image, with no network and no credentials:
#   docker run --rm --network none --user 0 --entrypoint /bin/bash \
#     -v "$PWD/deployment/ecr/idea-control-plane/image_check.sh:/tmp/image_check.sh:ro" \
#     "$IMAGE" /tmp/image_check.sh
# It prints what it finds and exits 1 if any fact differs from what the roles rely on.
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
ROLES=/opt/idea/roles
failed=0
fail() { echo "FAIL: $*"; failed=1; }
pass() { echo "ok: $*"; }

echo "== set-id files (allowed: pbs_iff, sudo)"
setid="$(find / -xdev -type f -perm /6000 | sort)"
printf '%s\n' "${setid}"
if [[ "${setid}" == $'/opt/pbs/sbin/pbs_iff\n/usr/bin/sudo' ]]; then
  pass "only pbs_iff and sudo carry set-id bits"
else
  fail "set-id files differ from pbs_iff and sudo"
fi
IDEA_SETID_IMAGE_LIST=/dev/null "${ROLES}/setid_strip.sh" --build / || fail "setid_strip.sh --build / failed on the built image"
# the bastion (after a site hook) and the scheduler (after PBS starts) run it on /usr and /opt
files="$(find /usr /opt -xdev -type f | wc -l)"
start="$(date +%s%N)"
"${ROLES}/setid_strip.sh" /usr /opt || fail "setid_strip.sh /usr /opt failed on the built image"
echo "setid_strip.sh /usr /opt: ${files} files in $(( ($(date +%s%N) - start) / 1000000 )) ms"

# positive control: a set-id file a start-time install would leave is cleared by the role's call
planted=/usr/local/bin/idea-image-check-setid
install -m 4755 /bin/true "${planted}"
"${ROLES}/setid_strip.sh" /usr /opt >/dev/null || true
if [[ -u "${planted}" ]]; then fail "setid_strip.sh /usr /opt left a planted set-uid file"; else pass "the start-time strip clears a planted set-uid file"; fi
rm -f "${planted}"
# after a site hook only the files the image shipped set-id are cleared again
listed="$(head -n 1 /opt/idea/setid-cleared.list 2>/dev/null || true)"
echo "files the build cleared: $(wc -l < /opt/idea/setid-cleared.list 2>/dev/null || echo 0)"
if [[ -n "${listed}" && -f "${listed}" ]]; then
  install -m 4755 /bin/true "${planted}"
  chmod u+s "${listed}"
  "${ROLES}/setid_strip.sh" --image-list
  if [[ -u "${listed}" ]]; then fail "--image-list left the bit on ${listed}"; else pass "--image-list clears a bit put back on an image file"; fi
  if [[ -u "${planted}" ]]; then pass "--image-list leaves a tool a hook installed as it was"; else fail "--image-list cleared a file the image did not ship set-id"; fi
  rm -f "${planted}"
else
  fail "the build recorded no cleared set-id files in /opt/idea/setid-cleared.list"
fi

echo "== service account for license checks"
if id ideaserviceaccount && (( $(id -u ideaserviceaccount) < 1000 )); then
  pass "ideaserviceaccount exists with a system id"
else
  fail "ideaserviceaccount is missing or not a system account"
fi

echo "== commands that relied on set-id bits (now run only with the caller's own rights)"
for each in su newgrp sg passwd gpasswd mount umount write wall at crontab pkexec; do
  path="$(command -v "${each}" 2>/dev/null)" && echo "  ${each}: ${path} $(rpm -qf "${path}" 2>/dev/null)"
done

echo "== file capabilities"
# Roles start as root, so a capability on a file only widens what other users can run. Allowed,
# each only under the condition checked here:
#   /usr/bin/newuidmap, /usr/bin/newgidmap (shadow-utils, cap_setuid / cap_setgid): they map only
#     the ranges in /etc/subuid and /etc/subgid, and the image has none (system accounts get none,
#     directory users are not in those files).
#   /usr/libexec/sssd/* (sssd helpers): only when not executable by other users, so only root and
#     the sssd group can start them.
# Anything else fails with the exact choice to make. Packages of this family that commonly carry
# capabilities and are not installed here (they would fail the check if a dependency brought
# them): iputils arping and clockdiff (cap_net_raw), gstreamer's gst-ptp-helper (cap_net_admin,
# cap_net_bind_service), systemd helpers.
if ! command -v getcap >/dev/null; then
  fail "getcap is missing; the image installs libcap so this check can run"
else
  caps="$(find / -xdev -type f -exec getcap {} + 2>/dev/null | sort)"
  printf '%s\n' "${caps:-none}"
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    file="${line%% *}"
    case "${file}" in
      /usr/bin/newuidmap|/usr/bin/newgidmap)
        if [[ -s /etc/subuid || -s /etc/subgid ]]; then
          fail "${file} has capabilities and /etc/subuid or /etc/subgid has ranges: empty them, or clear the file (setcap -r ${file}) in the Dockerfile strip layer"
        else
          pass "${file} capabilities allowed: no subordinate id ranges exist"
        fi ;;
      /usr/libexec/sssd/*)
        if (( 8#$(stat -c %a "${file}") & 8#001 )); then
          fail "${file} has capabilities and any user can run it: remove other-execute (chmod o-x) in the Dockerfile, or clear it (setcap -r)"
        else
          pass "${file} capabilities allowed: only root and the sssd group can run it"
        fi ;;
      *)
        fail "file capability outside the allowlist: ${line}. Either clear it in the Dockerfile strip layer (setcap -r ${file}) or add ${file} to the allowed cases in image_check.sh with the reason it is safe" ;;
    esac
  done <<<"${caps}"
fi

echo "== sssd"
sssd_version="$(rpm -q --qf '%{VERSION}' sssd-common)" \
  || fail "sssd-common is not installed (rpm -qa 'sssd*' lists what is); the bastion starts /usr/sbin/sssd"
echo "sssd-common ${sssd_version}"
getcap /usr/libexec/sssd/* 2>/dev/null || true
ls -l /usr/libexec/sssd/ || echo "WARNING: no /usr/libexec/sssd; rpm -ql sssd-common shows where its helpers are, and the capability allowlist above names that path"
# The bastion starts sssd itself as root (sssd -i) and its sssd.conf names no service user. The
# build strip clears set-id bits but leaves file capabilities, so a helper that relies on
# capabilities keeps them under any version. Sign-in breaks only if a helper relied on a set-id
# bit and sssd does not run it as root: that is the case checked below. On 2.9 every helper runs
# as root, so a cleared bit there is harmless.
grep -q '^/usr/sbin/sssd -i ' "${ROLES}/bastion.sh" || fail "bastion.sh no longer starts sssd as root with sssd -i"
if [[ ! -f /opt/idea/ideactl/dist/src/container/bastion.js ]]; then
  fail "/opt/idea/ideactl/dist/src/container/bastion.js is missing, so the sssd.conf it writes cannot be read; check the ideactl build stage of the Dockerfile"
elif grep -Eq '^user *=' /opt/idea/ideactl/dist/src/container/bastion.js; then
  fail "the bastion sssd.conf names a service user; its helpers then need privileges the strip removes"
fi
sssd_cleared="$(grep '^/usr/libexec/sssd/' /opt/idea/setid-cleared.list 2>/dev/null || true)"
echo "sssd helpers whose set-id bit the build cleared: ${sssd_cleared:-none}"
case "${sssd_version}" in
  2.9.*) pass "sssd ${sssd_version} runs its helpers as root here" ;;
  *)
    if [[ -n "${sssd_cleared}" ]]; then
      fail "sssd ${sssd_version} ships set-id helpers (${sssd_cleared//$'\n'/ }); keep their bits in setid_strip.sh or confirm this version runs them as root"
    else
      echo "WARNING: sssd ${sssd_version} is not the 2.9 this check was written for; its helpers carry no set-id bit the build cleared, so sign-in is unaffected by the strip. Confirm a directory login on dev and update this note."
    fi ;;
esac

echo "== OpenPBS start scripts and file modes"
ls -l /opt/pbs/sbin/pbs_iff /opt/pbs/sbin/pbs_rcp || fail "OpenPBS is not under /opt/pbs (the pbs build stage installs it there with --prefix=/opt/pbs)"
pbs_modes="$(grep -nE 'chmod|pbs_probe|4755|u\+s' /opt/pbs/libexec/pbs_habitat /opt/pbs/libexec/pbs_init.d /opt/pbs/libexec/pbs_postinstall 2>/dev/null)"
printf '%s\n' "${pbs_modes:-no mode changes found}"
if grep -Eq 'pbs_probe|pbs_rcp|pbs_iff|4755|u\+s' <<<"${pbs_modes}"; then
  echo "PBS start-up can set file modes; scheduler.sh clears set-id bits again after pbs_init.d start"
fi
if awk '/pbs_init.d start/{s=1} s && /setid_strip.sh/{found=1} END{exit !found}' "${ROLES}/scheduler.sh"; then
  pass "scheduler.sh clears set-id bits after PBS starts"
else
  fail "scheduler.sh does not clear set-id bits after pbs_init.d start"
fi

echo "== sshd PAM stack after the bastion's edits"
pam="$(mktemp)"
cp /etc/pam.d/sshd "${pam}" || fail "/etc/pam.d/sshd is missing; openssh-server installs it (rpm -qf /etc/pam.d/sshd)"
"${ROLES}/bastion_pam.sh" "${pam}" && "${ROLES}/bastion_pam.sh" "${pam}"
cat "${pam}"
hook='session optional pam_exec.so quiet /opt/idea/roles/user_runtime_dir.sh'
if [[ "$(grep -cxF "${hook}" "${pam}")" == 1 ]]; then
  pass "the tmux hook is in the session stack once"
else
  fail "the tmux hook line is missing or repeated"
fi
if grep -q pam_loginuid.so "${pam}"; then fail "pam_loginuid is still in the stack"; fi
[[ "$(tail -n 1 "${pam}")" == "${hook}" ]] || fail "the tmux hook is not the last session line"
ls /usr/lib64/security/pam_exec.so >/dev/null || fail "pam_exec.so is not at /usr/lib64/security; find it with rpm -ql pam | grep pam_exec and compare with the module path in /etc/pam.d/sshd"
rm -f "${pam}"

echo "== tmux for an unprivileged user, with TMUX_TMPDIR as sshd sets it"
# The default session PATH is compiled into the binary that runs sessions. Up to OpenSSH 9.7 that
# is sshd itself; from 9.8 sshd only listens and starts sshd-session (and from 10.0 sshd-auth),
# which Fedora-family packages install under /usr/libexec/openssh. Every one present is searched.
sshd_session_path_check() {
  local binaries=() searched=() found=() bin
  read -r -a binaries <<< "${IDEA_CHECK_SSHD_BINARIES:-/usr/sbin/sshd /usr/libexec/openssh/sshd-session /usr/libexec/openssh/sshd-auth}"
  for bin in "${binaries[@]}"; do
    [[ -f "${bin}" ]] || continue
    searched+=("${bin}")
    if grep -aq '/usr/local/bin:/usr/bin' "${bin}"; then found+=("${bin}"); fi
  done
  if (( ${#found[@]} )); then
    pass "sshd's default PATH puts /usr/local/bin, where the bastion installs the tmux wrapper, first (found in ${found[*]})"
  elif (( ${#searched[@]} )); then
    fail "no sshd binary carries a default PATH starting /usr/local/bin:/usr/bin; searched ${searched[*]}. Check where this OpenSSH keeps it: rpm -ql openssh-server | grep -E 'sshd(-session|-auth)?$' and grep -ac /usr/local/bin:/usr/bin on each"
  else
    fail "no sshd binary found; looked for ${binaries[*]}. List the package's files with rpm -ql openssh-server"
  fi
}
sshd_session_path_check
grep -q '^SetEnv TMUX_TMPDIR=/run/idea-tmux$' "${ROLES}/bastion.sh" || fail "bastion.sh no longer sets TMUX_TMPDIR"
install -d -m 0755 -o root -g root /run/idea-tmux
for user in idea-check-a idea-check-b; do useradd --no-create-home --shell /bin/bash "${user}"; done
PAM_TYPE=open_session PAM_USER=idea-check-a "${ROLES}/user_runtime_dir.sh" || fail "the login hook failed"
uid_a="$(id -u idea-check-a)"
stat -c '%U %a %n' "/run/idea-tmux/tmux-${uid_a}"
[[ "$(stat -c '%U %a' "/run/idea-tmux/tmux-${uid_a}")" == "idea-check-a 700" ]] || fail "the hook made the wrong directory"
as() { local user=$1; shift; runuser -u "${user}" -- env -i PATH=/usr/bin:/bin TMUX_TMPDIR=/run/idea-tmux "$@"; }
if as idea-check-a bash "${ROLES}/tmux.sh" -f /dev/null new-session -d -s check 'sleep 30' \
  && as idea-check-a bash "${ROLES}/tmux.sh" has-session -t check \
  && [[ -S "/run/idea-tmux/tmux-${uid_a}/default" ]]; then
  pass "tmux starts in the private directory"
else
  fail "tmux did not start for an unprivileged user in /run/idea-tmux"
fi
as idea-check-a bash "${ROLES}/tmux.sh" kill-server 2>/dev/null || true
# no hook run for this user: the wrapper must still start tmux, in the default place, and warn
warning="$(as idea-check-b bash "${ROLES}/tmux.sh" -f /dev/null new-session -d -s check 'sleep 30' 2>&1 >/dev/null)"
echo "${warning}"
plain() { runuser -u idea-check-b -- env -i PATH=/usr/bin:/bin /usr/bin/tmux "$@"; }
if [[ "${warning}" == *"default location"* ]] && plain has-session -t check 2>/dev/null; then
  pass "without the private directory tmux still starts, with a warning"
else
  fail "tmux has no working fallback when the private directory is missing"
fi
plain kill-server 2>/dev/null || true

if (( failed )); then
  echo "image check failed"
  exit 1
fi
echo "image check passed"
