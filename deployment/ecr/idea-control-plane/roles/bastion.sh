#!/bin/bash
set -euo pipefail

python3.13 /opt/idea/roles/bastion.py
unset IDEA_LDAP_CERTIFICATE
install -d -m 0755 /run/sshd
# Container sessions have no host audit login UID to set.
sed -i '/pam_loginuid.so/d' /etc/pam.d/sshd
cat > /etc/ssh/sshd_config <<'EOF'
Port 22
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
UsePAM yes
PermitRootLogin no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
# The container host joins the directory, so NFSv4 owner names resolve to the account ids the
# accounts service wrote and this check holds. (An unjoined host maps every owner to nobody and
# would refuse each authorized_keys file.)
StrictModes yes
PasswordAuthentication no
KbdInteractiveAuthentication no
LoginGraceTime 30
MaxAuthTries 6
PermitUserEnvironment no
UseDNS no
ClientAliveInterval 60
ClientAliveCountMax 3
Subsystem sftp internal-sftp
EOF
# Offer post-quantum key exchange first, as the host bootstrap does: only the algorithms this
# sshd knows, followed by its own defaults, so nothing older stops connecting.
IDEA_PQ_KEX=$(ssh -Q kex 2>/dev/null | grep -E '^(mlkem768x25519-sha256|sntrup761x25519-sha512@openssh\.com)$' | paste -sd, -)
IDEA_DEFAULT_KEX=$(/usr/sbin/sshd -T 2>/dev/null | awk '/^kexalgorithms /{print $2}')
if [[ -n "${IDEA_PQ_KEX}" ]]; then
  IDEA_KEX=$(printf '%s,%s' "${IDEA_PQ_KEX}" "${IDEA_DEFAULT_KEX}" | tr ',' '\n' | awk 'NF && !seen[$0]++' | paste -sd, -)
  echo "KexAlgorithms ${IDEA_KEX}" >> /etc/ssh/sshd_config
fi
# Carry the host bootstrap's outbound SSH behavior into user sessions.
cat > /etc/ssh/ssh_config.d/idea.conf <<'EOF'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
EOF
if [[ -n "${IDEA_PBS_SERVER:-}" ]]; then
  # job ids end in the server's short name (eg. 1.scheduler) and qdel / qstat resolve that
  # suffix, so search the cluster's private zone as the host bootstrap does
  PRIVATE_ZONE="${IDEA_PBS_SERVER#*.}"
  if [[ "${PRIVATE_ZONE}" != "${IDEA_PBS_SERVER}" ]] && ! grep -q "^search .*${PRIVATE_ZONE}" /etc/resolv.conf; then
    # rewritten in place: the runtime bind-mounts resolv.conf, so sed -i cannot rename over it
    RESOLV="$(cat /etc/resolv.conf)"
    SEARCH="$(grep -m1 '^search ' <<< "${RESOLV}" | cut -d' ' -f2-)"
    { grep -v '^search ' <<< "${RESOLV}"; echo "search ${PRIVATE_ZONE}${SEARCH:+ ${SEARCH}}"; } > /etc/resolv.conf
  fi
  cat > /etc/pbs.conf <<EOF
PBS_SERVER=${IDEA_PBS_SERVER}
PBS_START_SERVER=0
PBS_START_SCHED=0
PBS_START_COMM=0
PBS_START_MOM=0
PBS_EXEC=/opt/pbs
PBS_HOME=/var/spool/pbs
PBS_SCP=/usr/bin/scp
EOF
  # shellcheck disable=SC2016  # expanded by the login shell, not here
  echo 'export PATH=/opt/pbs/bin:/opt/pbs/sbin:$PATH' > /etc/profile.d/pbs.sh
fi
# Site customizations, as compute nodes and desktops run theirs from the shared /apps file
# system: tools users expect on the login node. It runs as root, so only a file root owns and
# no one else can write (a desktop user with sudo can write to /apps); it gets two minutes,
# its output goes to the task log, and sshd starts whatever it did.
SITE_HOOK="${IDEA_CLUSTER_HOME:-}/${IDEA_MODULE_ID:-bastion-host}/userdata_customizations.sh"
if [[ -n "${IDEA_CLUSTER_HOME:-}" && -f "${SITE_HOOK}" ]]; then
  # stat -L: the file the hook path leads to is the one that runs
  read -r HOOK_UID HOOK_MODE < <(stat -L -c '%u %a' "${SITE_HOOK}") || HOOK_UID=
  if [[ "${HOOK_UID}" != 0 ]] || (( 8#${HOOK_MODE:-777} & 8#022 )); then
    echo "skipping site hook ${SITE_HOOK}: it must be owned by root and writable by no one else (owner ${HOOK_UID:-unknown}, mode ${HOOK_MODE:-unknown})"
  else
    echo "running site hook ${SITE_HOOK}"
    timeout 120 /bin/bash "${SITE_HOOK}" || echo "site hook failed with status $?; continuing"
  fi
fi
/usr/sbin/sshd -t
/usr/sbin/sssd -i --logger=stderr &
sssd_pid=$!
/usr/sbin/sshd -D -e &
sshd_pid=$!
trap 'kill "$sshd_pid" "$sssd_pid" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
# Losing directory authentication is a service failure, even if port 22 still answers.
wait -n "$sshd_pid" "$sssd_pid"
exit 1
