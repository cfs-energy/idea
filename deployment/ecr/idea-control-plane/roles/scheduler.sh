#!/bin/bash
#
# Start the PBS daemons and scheduler module.
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

# Use task metadata when available and reject link-local addresses.
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

# Put the scheduler name first in /etc/hosts for reverse lookup.
if ! grep -q " ${SERVER_NAME}\$" /etc/hosts 2>/dev/null; then
  { echo "${TASK_IP} ${IDEA_SCHEDULER_DNS_NAME} ${SERVER_NAME}"; cat /etc/hosts; } > /etc/hosts.new
  cat /etc/hosts.new > /etc/hosts
  rm -f /etc/hosts.new
fi

# Set the datastore user for a new PBS_HOME.
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

# Create the datastore only when PBS_HOME is new.
if [[ ! -d "${PBS_HOME}/datastore" ]]; then
  log "first start: pbs_habitat will create PBS_HOME at ${PBS_HOME}"
  PBS_CREATE=1
else
  log "reusing existing PBS_HOME at ${PBS_HOME}"
  PBS_CREATE=0
fi

# Create the PostgreSQL runtime directory.
install -d -m 755 -o postgres -g postgres /run/postgresql

# Initialize PBS_HOME and its datastore.
/opt/pbs/libexec/pbs_habitat || true

# Configure a new PBS server before its daemons start.
setting() {
  aws dynamodb get-item --region "${AWS_DEFAULT_REGION}" \
    --table-name "${IDEA_CLUSTER_NAME}.cluster-settings" \
    --key "{\"key\":{\"S\":\"$1\"}}" \
    --query "Item.value.S || Item.value.N" --output text 2>/dev/null | grep -v '^None$' || echo "$2"
}
# Use a marker so incomplete configuration runs again.
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

# Fail if the PBS server does not start.
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

# Keep the scheduler host aligned with the stable name.
/opt/pbs/bin/qmgr -c "set sched default sched_host = ${IDEA_SCHEDULER_DNS_NAME}"

# Allow execution hosts time to reconnect after a replacement.
/opt/pbs/bin/qmgr -c "set server node_fail_requeue = ${PBS_NODE_FAIL_REQUEUE:-600}"

# A person, not this tool, closes admission before a migration and reopens it afterwards. Those
# are qmgr operations and qmgr needs the caller in the server's managers list, which is otherwise
# the software default of root on this server's own host -- and this server's host is now a task
# with no shell into it. The bastion host keeps its batch client and stays an instance, so putting
# it in the list gives those commands an authorised client over the batch protocol.
#
# It must be the EC2 private DNS name: the server matches the name it reverse-resolves the
# caller's address to, not the private-zone alias in bastion-host.hostname. An absent row means no
# bastion module, so no grant. `|| true` because re-adding an existing entry must not fail a start.
#
# Known ceiling: the entry names an address-derived host name, so after the bastion is replaced
# the grant is stale until this task next starts, and the superseded entry is left behind. Prune
# or re-resolve here if either becomes a problem.
BASTION_PRIVATE_DNS_NAME="$(setting bastion-host.private_dns_name '')"
if [[ -n "${BASTION_PRIVATE_DNS_NAME}" ]]; then
  log "granting qmgr manager rights to root@${BASTION_PRIVATE_DNS_NAME}"
  /opt/pbs/bin/qmgr -c "set server managers += root@${BASTION_PRIVATE_DNS_NAME}" || true
fi

log "starting pbs_sched"
/opt/pbs/sbin/pbs_sched

# Ask execution hosts to refresh the scheduler address.
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
  # Register the scheduler event hooks.
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

shutdown() {
  log "SIGTERM: qterm -t quick"
  /opt/pbs/bin/qterm -t quick || true
  exit 0
}
trap shutdown SIGTERM SIGINT

# Synchronize cluster users before starting the scheduler module. The sync itself defers a fresh
# cluster's not-yet-created tables to its background pass and fails on anything else.
log "syncing cluster users and groups into the resolver"
python3.13 /opt/idea/sync_users.py --once
python3.13 /opt/idea/sync_users.py &

log "starting scheduler module"
python3.13 -c "import sys; sys.argv=['ideaserver']; from ideascheduler.app.app_main import main; sys.exit(main())" &
IDEA_PID=$!
wait "${IDEA_PID}"
