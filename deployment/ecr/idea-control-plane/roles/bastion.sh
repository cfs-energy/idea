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
PasswordAuthentication yes
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
