#!/bin/bash
#
# Starts the PBS daemons and then runs the scheduler module in the foreground.
#
# PBS is started by its own init script rather than a process supervisor: the daemons
# expect to fork, and ECS is already a supervisor. The container health check runs
# qstat, so a server that dies takes the task with it and ECS replaces it.
#
# Required: IDEA_CLUSTER_NAME, IDEA_MODULE_ID, IDEA_MODULE_SET, AWS_DEFAULT_REGION,
#           IDEA_SCHEDULER_DNS_NAME (the stable name every execution host is configured
#           with, for example scheduler.<cluster>.<region>.local)
# Optional: IDEA_ROUTE53_ZONE_ID (when set, the record is pointed at this task's address)

set -euo pipefail

log() { echo "[entrypoint] $*"; }

: "${IDEA_SCHEDULER_DNS_NAME:?IDEA_SCHEDULER_DNS_NAME is required}"
PBS_HOME="${PBS_HOME:-/var/spool/pbs}"
SERVER_NAME="${IDEA_SCHEDULER_DNS_NAME%%.*}"

# The task address. On Fargate the task metadata endpoint is authoritative; the
# interface list is not, because the agent's 169.254.172.x link-local interface also
# reports global scope and sorts first. Fall back to the interfaces only off ECS, and
# never accept a link-local address.
TASK_IP=""
if [[ -n "${ECS_CONTAINER_METADATA_URI_V4:-}" ]]; then
  TASK_IP="$(curl -fsS --max-time 3 "${ECS_CONTAINER_METADATA_URI_V4}/task" \
    | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["Containers"][0]["Networks"][0]["IPv4Addresses"][0])' 2>/dev/null || true)"
fi
if [[ -z "${TASK_IP}" ]]; then
  TASK_IP="$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | grep -v '^169\.254\.' | head -1)"
fi
: "${TASK_IP:?could not determine the task address}"
case "${TASK_IP}" in 169.254.*) echo "[entrypoint] refusing link-local task address ${TASK_IP}" >&2; exit 1;; esac
log "task ip ${TASK_IP}, pbs server name ${SERVER_NAME}"

# PBS resolves its own name and the execution hosts resolve the same one. Mapping it
# locally means the server agrees with what the execution hosts were told.
# First, not appended: the runtime already wrote a line mapping this address to the
# task's own hostname, and the sched identifies itself by reverse lookup of its source
# address. If that comes back as anything but the server name, pbs_server refuses the
# sched's registration (PBSE_BADHOST on RegisterSched) and nothing ever runs.
if ! grep -q " ${SERVER_NAME}\$" /etc/hosts 2>/dev/null; then
  { echo "${TASK_IP} ${IDEA_SCHEDULER_DNS_NAME} ${SERVER_NAME}"; cat /etc/hosts; } > /etc/hosts.new
  cat /etc/hosts.new > /etc/hosts
  rm -f /etc/hosts.new
fi

# PBS_DATA_SERVICE_USER has no default in pbs_db_utility: it is written into
# /etc/pbs.conf by pbs_postinstall, and a module host then replaces that file with one
# that omits it. That is harmless on a host whose datastore already exists, but a
# container starting from an empty PBS_HOME needs it or the datastore is never created.
PBS_DATA_SERVICE_USER="${PBS_DATA_SERVICE_USER:-postgres}"

cat > /etc/pbs.conf <<EOF
PBS_SERVER=${SERVER_NAME}
PBS_START_SERVER=1
PBS_START_SCHED=0
PBS_START_COMM=1
PBS_START_MOM=0
PBS_EXEC=/opt/pbs
PBS_HOME=${PBS_HOME}
PBS_CORE_LIMIT=unlimited
PBS_SCP=/usr/bin/scp
PBS_DATA_SERVICE_USER=${PBS_DATA_SERVICE_USER}
PBS_LEAF_NAME=${SERVER_NAME}
EOF

# Point the record at this task before the daemons come up, so an execution host that
# reconnects during the replacement finds the new address.
if [[ -n "${IDEA_ROUTE53_ZONE_ID:-}" ]]; then
  log "upserting ${IDEA_SCHEDULER_DNS_NAME} -> ${TASK_IP}"
  cat > /tmp/rr.json <<EOF
{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{
  "Name":"${IDEA_SCHEDULER_DNS_NAME}","Type":"A","TTL":60,
  "ResourceRecords":[{"Value":"${TASK_IP}"}]}}]}
EOF
  aws route53 change-resource-record-sets \
    --hosted-zone-id "${IDEA_ROUTE53_ZONE_ID}" \
    --change-batch file:///tmp/rr.json >/dev/null
fi

# First start on an empty shared PBS_HOME lays down the directory tree and the
# datastore. Afterwards the datastore is what carries the running jobs across a
# replacement, so it is never recreated.
if [[ ! -d "${PBS_HOME}/datastore" ]]; then
  log "first start: pbs_habitat will create PBS_HOME at ${PBS_HOME}"
  PBS_CREATE=1
else
  log "reusing existing PBS_HOME at ${PBS_HOME}"
  PBS_CREATE=0
fi

# The datastore's PostgreSQL puts its socket and lock file in /run/postgresql. On a
# host systemd-tmpfiles creates that directory at boot from the package's rule; nothing
# does in a container, and without it postgres exits right after pg_ctl reports
# "server starting", which PBS cannot distinguish from success.
install -d -m 755 -o postgres -g postgres /run/postgresql

# pbs_habitat runs pbs_postinstall, which lays down the whole PBS_HOME tree (spool,
# server_priv/accounting, sched_priv/sched_config, pbs_environment, db_user) but only
# when PBS_HOME does not exist yet, then creates the datastore. Nothing here pre-creates
# any of it: a partial tree makes postinstall skip population and the server then fails
# chk_file_sec on the pieces that are missing. On an existing PBS_HOME it is a no-op.
/opt/pbs/libexec/pbs_habitat || true

# First start only: the configuration a module host applies to a new PBS server in
# configure_openpbs_server.jinja2, minus the host-only parts (systemd, the login alias).
# The server reads resourcedef, sched_config and pbs_environment at startup, so these go
# in before the daemons; the qmgr settings need a running server and follow. All of it
# lives in PBS_HOME or the datastore, so a replacement finds it in place.
setting() {
  aws dynamodb get-item --region "${AWS_DEFAULT_REGION}" \
    --table-name "${IDEA_CLUSTER_NAME}.cluster-settings" \
    --key "{\"key\":{\"S\":\"$1\"}}" \
    --query "Item.value.S || Item.value.N" --output text 2>/dev/null | grep -v '^None$' || echo "$2"
}
# Gated on a marker rather than on the datastore's absence: a PBS_HOME that exists but was
# never configured (or a task that died mid-way) is configured on the next start.
MARKER="${PBS_HOME}/.idea-server-configured"
if [[ ! -f "${MARKER}" ]]; then
  log "applying the scheduler's PBS server configuration"
  cat > "${PBS_HOME}/server_priv/resourcedef" <<'RESOURCEDEF'
anonymous_metrics type=string
availability_zone type=string
availability_zone_id type=string
base_os type=string
compute_node type=string flag=h
efa_support type=string
error_message type=string
force_ri type=string
fsx_lustre type=string
fsx_lustre_deployment_type type=string
fsx_lustre_per_unit_throughput type=string
fsx_lustre_size type=string
ht_support type=string
instance_profile type=string
instance_ami type=string
instance_id type=string
instance_type type=string
instance_type_used type=string
keep_ebs type=string
placement_group type=string
root_size type=string
scratch_iops type=string
scratch_size type=string
security_groups type=string
spot_allocation_count type=string
spot_allocation_strategy type=string
spot_price type=string
stack_id type=string
subnet_id type=string
system_metrics type=string
queue_type type=string
job_id type=string
job_group type=string
job_uid type=string
provisioning_time type=string
dry_run type=string
cluster_name type=string
cluster_version type=string
scaling_mode type=string
lifecycle type=string
tenancy type=string
spot_fleet_request type=string
auto_scaling_group type=string
keep_forever type=string
terminate_when_idle type=string
launch_time type=string
capacity_added type=string
job_started_email_template type=string
job_completed_email_template type=string
RESOURCEDEF
  grep -q "compute_node" "${PBS_HOME}/sched_priv/sched_config" || \
    sed -i 's/resources: "ncpus, mem, arch, host, vnode, aoe, eoe"/resources: "ncpus, mem, arch, host, vnode, aoe, eoe, compute_node"/' "${PBS_HOME}/sched_priv/sched_config"
  printf 'PATH=/bin:/usr/bin\nIDEA_SCHEDULER_UNIX_SOCKET=/run/idea.sock\n' > "${PBS_HOME}/pbs_environment"
fi

log "starting pbs daemons (create=${PBS_CREATE})"
/opt/pbs/libexec/pbs_init.d start

# A replacement must not out-race the datastore: fail fast rather than serve a server
# that never came up.
for _ in $(seq 1 30); do
  if /opt/pbs/bin/qstat -B >/dev/null 2>&1; then
    log "pbs server is answering"
    break
  fi
  sleep 5
done
if ! /opt/pbs/bin/qstat -B >/dev/null 2>&1; then
  log "pbs server did not come up"
  exit 1
fi

# Every start, not only the first: the sched object's host is persisted in the datastore
# and defaults to whatever host created it (sched_func.c sets it only when unset). The
# sched identifies itself by the stable name, and the server refuses its registration
# with PBSE_BADHOST unless this matches, so pin it to the stable name here.
/opt/pbs/bin/qmgr -c "set sched default sched_host = ${IDEA_SCHEDULER_DNS_NAME}"

# How long the server tolerates an unreachable execution host before requeuing its jobs.
# The default, 310 s, is shorter than a replacement plus the hosts' re-read, so a slow
# heal would rerun every running job from the start. Ten minutes covers a replacement
# with margin; a host that is really gone just waits that long before its jobs move.
/opt/pbs/bin/qmgr -c "set server node_fail_requeue = ${PBS_NODE_FAIL_REQUEUE:-600}"

# The sched registers two connections with the server and the server checks the
# sched's address against sched_host on each. Starting it only after that attribute is
# pinned means its first registration is clean; starting it alongside the server (the
# init script default) leaves the server holding a half-registered sched it then rejects
# with PBSE_IVALREQ on every retry.
log "starting pbs_sched"
/opt/pbs/sbin/pbs_sched

# A mom resolves the server address once, at start, for both the address it connects to
# and the list it authorises server messages against. A replacement task has a new
# address, so every execution host re-reads its configuration or stays down under the
# new server while its jobs keep running. SIGHUP is the mom re-read; running jobs are
# untouched. One tag-targeted command reaches the fleet; on a first start it reaches nothing.
# The re-read resolves the server name through the node's resolver cache, which the mom's own
# retries keep warm with the old address, so the command flushes that cache and waits until
# the name resolves to this task before signalling; otherwise the mom re-authorises the old
# address and rejects the new server for a TTL.
if [[ -n "${IDEA_ROUTE53_ZONE_ID:-}" ]]; then
  log "asking execution hosts to re-read their configuration for the new server address"
  aws ssm send-command --region "${AWS_DEFAULT_REGION}" \
    --targets "Key=tag:idea:ClusterName,Values=${IDEA_CLUSTER_NAME}" "Key=tag:idea:NodeType,Values=compute-node" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"resolvectl flush-caches 2>/dev/null || true\",\"for i in \$(seq 1 18); do getent hosts ${IDEA_SCHEDULER_DNS_NAME} | grep -q '^${TASK_IP} ' && break; sleep 5; done\",\"pkill -HUP -x pbs_mom || true\"]" \
    --comment "scheduler address changed to ${TASK_IP}" \
    --query Command.CommandId --output text 2>&1 | sed "s/^/[entrypoint] ssm command: /" || true
fi

if [[ ! -f "${MARKER}" ]]; then
  log "server attributes, default queue and hooks"
  /opt/pbs/bin/qmgr -c "set server flatuid = $(setting scheduler.openpbs.server.flatuid true)"
  /opt/pbs/bin/qmgr -c "set server job_history_enable = $(setting scheduler.openpbs.server.job_history_enable 1)"
  /opt/pbs/bin/qmgr -c "set server job_history_duration = $(setting scheduler.openpbs.server.job_history_duration 72:00:00)"
  /opt/pbs/bin/qmgr -c "set server scheduler_iteration = $(setting scheduler.openpbs.server.scheduler_iteration 30)"
  /opt/pbs/bin/qmgr -c "set server max_concurrent_provision = $(setting scheduler.openpbs.server.max_concurrent_provision 5000)"
  /opt/pbs/bin/qmgr -c "create queue normal" 2>/dev/null || true
  /opt/pbs/bin/qmgr -c "set queue normal queue_type = Execution"
  /opt/pbs/bin/qmgr -c "set queue normal started = True"
  /opt/pbs/bin/qmgr -c "set queue normal enabled = True"
  /opt/pbs/bin/qmgr -c "set server default_queue = normal"
  # The hooks are how PBS hands job validation and run/finish events to the module. Same
  # three as install_app.sh.jinja2 installs on a module host, from the same shipped files.
  HOOKS="${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks"
  /opt/pbs/bin/qmgr -c "create hook validate_job event='queuejob,modifyjob,movejob'" 2>/dev/null || true
  /opt/pbs/bin/qmgr -c "import hook validate_job application/x-python default ${HOOKS}/openpbs_hook_handler.py"
  /opt/pbs/bin/qmgr -c "create hook job_status event='runjob,execjob_begin,execjob_end'" 2>/dev/null || true
  /opt/pbs/bin/qmgr -c "import hook job_status application/x-python default ${HOOKS}/openpbs_hook_handler.py"
  /opt/pbs/bin/qmgr -c "create hook calculate_ncpus event='queuejob'" 2>/dev/null || true
  /opt/pbs/bin/qmgr -c "import hook calculate_ncpus application/x-python default ${HOOKS}/calculate_ncpus_hook.py"
  /opt/pbs/bin/qmgr -c "set hook calculate_ncpus order=2"
  date -u +%Y-%m-%dT%H:%M:%SZ > "${MARKER}"
  log "PBS server configured"
fi

# Terminating the server leaves running jobs on their execution hosts; they are picked
# up again when the replacement connects. Anything harsher risks the datastore.
shutdown() {
  log "SIGTERM: qterm -t quick"
  /opt/pbs/bin/qterm -t quick || true
  exit 0
}
trap shutdown SIGTERM SIGINT

# The module submits every job as its owner ("su <owner> -c qsub"), so cluster users
# must resolve here. There is no directory join in a task; the identities come from the
# cluster's own tables instead. Once before the module starts, then refreshed in the
# background so users created later resolve within a minute.
log "syncing cluster users and groups into the resolver"
python3.13 /opt/idea/sync_users.py --once
python3.13 /opt/idea/sync_users.py &

log "starting scheduler module"
ideaserver &
IDEA_PID=$!
wait "${IDEA_PID}"
