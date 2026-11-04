#!/bin/bash
set -euo pipefail

node /opt/idea/ideactl/dist/src/container/bastion.js
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
# Per-source limits stand in for the EC2 host's fail2ban, set so only brute force reaches them:
# many users can share one NAT address. A source may hold 30 unauthenticated connections, below
# the 50 where random drops start for everyone. Each connection that fails authentication adds
# 2 seconds of refusal, and the address is refused only once more than 30 seconds are pending, so
# about 15 failed connections in quick succession, or a failure more than every 2 seconds
# sustained; refusals stop at most 10 minutes after the attempts do.
MaxStartups 50:30:200
PerSourceMaxStartups 30
PerSourceNetBlockSize 32:128
PerSourcePenalties authfail:2s min:30s max:10m
EOF
# Never penalize the task's own ssh-keyscan check, the load balancer's TCP checks (from its
# private addresses) or the browser terminals (from the cluster manager): loopback and the VPC.
# Nor the SSH allowlist (ssh_client_ip): an office reaches the bastion through one NAT address,
# and a penalty there would refuse every user in it. An entry wider than /8 stays penalized: no
# wider block was ever assigned to one organisation, so it is the open internet (0.0.0.0/0 on a
# public bastion), not a site. An entry sshd would reject (host bits set) is dropped rather than
# stopping sshd.
IDEA_PENALTY_EXEMPT=127.0.0.0/8,::1
exempt_networks() {
  local source=$1 cidr address prefix
  while IFS= read -r -d, cidr; do
    [[ -n "${cidr}" ]] || continue
    if [[ ! "${cidr}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] \
      || (( 10#${BASH_REMATCH[1]} > 255 || 10#${BASH_REMATCH[2]} > 255 || 10#${BASH_REMATCH[3]} > 255 \
        || 10#${BASH_REMATCH[4]} > 255 || 10#${BASH_REMATCH[5]} > 32 )); then
      echo "ignoring ${source} ${cidr}: not an IPv4 network"
      continue
    fi
    address=$(( (10#${BASH_REMATCH[1]} << 24) | (10#${BASH_REMATCH[2]} << 16) | (10#${BASH_REMATCH[3]} << 8) | 10#${BASH_REMATCH[4]} ))
    prefix=$(( 10#${BASH_REMATCH[5]} ))
    if (( prefix < 8 )); then
      echo "not exempting ${source} ${cidr} from penalties: wider than /8"
    elif (( address & (0xffffffff >> prefix) )); then
      echo "ignoring ${source} ${cidr}: host bits set"
    else
      IDEA_PENALTY_EXEMPT+=",${cidr}"
    fi
  done <<< "$2,"
}
exempt_networks "VPC CIDR" "${IDEA_VPC_CIDRS:-}"
exempt_networks "SSH client CIDR" "${IDEA_SSH_CLIENT_CIDRS:-}"
echo "PerSourcePenaltyExemptList ${IDEA_PENALTY_EXEMPT}" >> /etc/ssh/sshd_config
# Offer post-quantum key exchange first, as the host bootstrap does: only the algorithms this
# sshd knows, followed by its own defaults, so nothing older stops connecting.
IDEA_PQ_KEX=$(ssh -Q kex 2>/dev/null | grep -E '^(mlkem768x25519-sha256|sntrup761x25519-sha512@openssh\.com)$' | paste -sd, -)
IDEA_DEFAULT_KEX=$(/usr/sbin/sshd -T 2>/dev/null | awk '/^kexalgorithms /{print $2}')
if [[ -n "${IDEA_PQ_KEX}" ]]; then
  IDEA_KEX=$(printf '%s,%s' "${IDEA_PQ_KEX}" "${IDEA_DEFAULT_KEX}" | tr ',' '\n' | awk 'NF && !seen[$0]++' | paste -sd, -)
  echo "KexAlgorithms ${IDEA_KEX}" >> /etc/ssh/sshd_config
fi
# Carry the host bootstrap's outbound SSH behavior into user sessions. Host keys are not checked
# by design: compute nodes and desktops come and go, a new one reuses a released private address
# with a freshly generated key, and the shared home directory would keep the old key, so a
# remembered key (accept-new) refuses `ssh <node>` on reuse. No host certificate authority signs
# their keys, and only desktops and the bastion register keys (for the browser terminal, by
# instance, not address), so there is nothing to check a compute node against. The targets are
# private VPC addresses, which only an instance holding that address can answer, and logins use
# keys, so a substitute host cannot reuse what it receives.
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
  # the server's own server_name (roles/scheduler.sh): qsub completes a short job id with it,
  # and the server releases a dependency only on that exact id
  cat > /etc/pbs.conf <<EOF
PBS_SERVER=${IDEA_PBS_SERVER%%.*}
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
# Health checks connect to sshd without logging in and sshd reports each one on stderr, which
# the log shipper files as an error: about 1.2M lines a week, every error IDEA logged. Two shapes
# are dropped before they leave the container, and nothing else:
#   the task's ssh-keyscan on 127.0.0.1, which closes after the key exchange:
#     Connection closed by 127.0.0.1 port N [preauth]
#   the load balancer's TCP check from its private addresses, which closes before sending an SSH
#     identification string (no [preauth]: no session existed):
#     Connection closed by 10.x.y.z port N
# Logins, failed and invalid-user attempts, disconnects after authentication, any close from a
# public address and every other sshd line still reach the task log.
SSHD_HEALTH_CHECK_LINES='^Connection closed by (127\.0\.0\.1 port [0-9]+ \[preauth\]|(10(\.[0-9]{1,3}){3}|(172\.(1[6-9]|2[0-9]|3[01])|192\.168)(\.[0-9]{1,3}){2}) port [0-9]+)$'
sshd_log_filter() { grep --line-buffered -v -E "${SSHD_HEALTH_CHECK_LINES}" >&2 || true; }
/usr/sbin/sshd -D -e 2> >(sshd_log_filter) &
sshd_pid=$!
# Initialize and retry terminal trust without blocking ordinary SSH.
(
  if [[ ! -f /run/idea-terminal-trust-ready ]]; then
    until timeout 300 python3.13 /opt/idea/roles/terminal_host.py; do
      # 78: the site's sshd principal policy needs explicit integration; retrying cannot help.
      if (( $? == 78 )); then
        echo "Terminal trust not installed: site principal policy; browser terminal unavailable"
        exit 0
      fi
      echo "Terminal trust not installed; retrying in 30 seconds"
      sleep 30
    done
    kill -HUP "$sshd_pid"
  fi
) &
trust_pid=$!
trap 'kill "$trust_pid" "$sshd_pid" "$sssd_pid" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
# Losing directory authentication is a service failure, even if port 22 still answers.
wait -n "$sshd_pid" "$sssd_pid"
exit 1
