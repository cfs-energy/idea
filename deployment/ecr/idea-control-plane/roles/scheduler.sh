#!/bin/bash
#
# Start the PBS daemons and scheduler module.
#
# Required: IDEA_CLUSTER_NAME, IDEA_MODULE_ID, IDEA_MODULE_SET, AWS_DEFAULT_REGION,
#           IDEA_SCHEDULER_DNS_NAME (the stable name every execution host is configured
#           with, for example scheduler.<cluster>.<region>.local)
# Optional: IDEA_ROUTE53_ZONE_ID (when set, the record is pointed at this task's address)

set -Eeuo pipefail

log() { echo "[entrypoint] $*"; }
trap 'log "ERROR at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

: "${IDEA_CLUSTER_NAME:?IDEA_CLUSTER_NAME is required}"
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
if [[ "${PBS_CREATE}" == 1 ]]; then
  /opt/pbs/libexec/pbs_habitat
fi

# Missing settings use defaults; API errors must not silently install defaults.
setting() {
  local value
  value="$(aws dynamodb get-item --region "${AWS_DEFAULT_REGION}" \
    --table-name "${IDEA_CLUSTER_NAME}.cluster-settings" \
    --key "{\"key\":{\"S\":\"$1\"}}" \
    --query 'Item.value.S || Item.value.N || Item.value.BOOL' --output text)" || return "$?"
  if [[ "${value}" == None || -z "${value}" ]]; then
    printf '%s\n' "$2"
  else
    printf '%s\n' "${value}"
  fi
}
# Imported databases may have no container marker and still contain live policy.
# Only a datastore absent at startup permits installation defaults.
MARKER="${PBS_HOME}/.idea-server-configured"
if [[ "${PBS_CREATE}" == 1 && ! -f "${MARKER}" ]]; then
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
fi

# PBS reads this file during server initialization, so refresh it before starting.
log "refreshing pbs_environment and license resources from the image and settings"
if [[ -f "${PBS_HOME}/pbs_environment" ]]; then
  cp -p "${PBS_HOME}/pbs_environment" "${PBS_HOME}/pbs_environment.$(date -u +%Y%m%d%H%M%S)"
fi
printf 'PATH=/bin:/usr/bin\nIDEA_SCHEDULER_UNIX_SOCKET=/run/idea.sock\n' > "${PBS_HOME}/pbs_environment"
python3.13 -m ideascheduler.app.runtime_tasks licenses "${PBS_HOME}"

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

# Existing scheduler policy belongs to the datastore, including host and retry settings.
# Import preparation must supply any changes needed for the target environment.
if [[ "${PBS_CREATE}" == 1 ]]; then
  /opt/pbs/bin/qmgr -c "set sched default sched_host = ${IDEA_SCHEDULER_DNS_NAME}"
  /opt/pbs/bin/qmgr -c "set server node_fail_requeue = ${PBS_NODE_FAIL_REQUEUE:-600}"
fi

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

if [[ "${PBS_CREATE}" == 1 && ! -f "${MARKER}" ]]; then
  log "server attributes and default queue"
  VALUE="$(setting scheduler.openpbs.server.flatuid true)"
  /opt/pbs/bin/qmgr -c "set server flatuid = ${VALUE}"
  VALUE="$(setting scheduler.openpbs.server.job_history_enable 1)"
  /opt/pbs/bin/qmgr -c "set server job_history_enable = ${VALUE}"
  VALUE="$(setting scheduler.openpbs.server.job_history_duration 72:00:00)"
  /opt/pbs/bin/qmgr -c "set server job_history_duration = ${VALUE}"
  VALUE="$(setting scheduler.openpbs.server.scheduler_iteration 30)"
  /opt/pbs/bin/qmgr -c "set server scheduler_iteration = ${VALUE}"
  VALUE="$(setting scheduler.openpbs.server.max_concurrent_provision 5000)"
  /opt/pbs/bin/qmgr -c "set server max_concurrent_provision = ${VALUE}"
  /opt/pbs/bin/qmgr -c "create queue normal"
  /opt/pbs/bin/qmgr -c "set queue normal queue_type = Execution"
  /opt/pbs/bin/qmgr -c "set queue normal started = True"
  /opt/pbs/bin/qmgr -c "set queue normal enabled = True"
  /opt/pbs/bin/qmgr -c "set server default_queue = normal"
  date -u +%Y-%m-%dT%H:%M:%SZ > "${MARKER}"
  log "PBS server configured"
fi

# Reconcile image-owned hooks on every start, including existing/imported datastores.
# A hook is created when absent, and (re)imported, given its events and enabled when the
# image's file differs from the one recorded at the last import. An unchanged hook is not
# touched, so an operator's enabled/disabled choice stands until a new image replaces it.
log "refreshing PBS hooks"
HOOKS="${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks"
HOOK_STATE="${PBS_HOME}/.idea-hooks"
mkdir -p "${HOOK_STATE}"
# Listing all hooks distinguishes an absent hook from a failed server connection.
EXISTING_HOOKS="$(/opt/pbs/bin/qmgr -c 'list hook')"
install_hook() {
  local name="$1" events="$2" file="$3" digest recorded=""
  digest="$(sha256sum "${HOOKS}/${file}" | cut -d' ' -f1)"
  [[ ! -f "${HOOK_STATE}/${name}.sha256" ]] || recorded="$(cat "${HOOK_STATE}/${name}.sha256")"
  if grep -Eq "^Hook[[:space:]]+${name}([[:space:]]|$)" <<< "${EXISTING_HOOKS}"; then
    if [[ "${digest}" == "${recorded}" ]]; then
      log "hook ${name} is unchanged; leaving it as configured"
      return 0
    fi
  else
    /opt/pbs/bin/qmgr -c "create hook ${name}"
  fi
  log "installing hook ${name} (${events})"
  /opt/pbs/bin/qmgr -c "set hook ${name} event='${events}'"
  /opt/pbs/bin/qmgr -c "import hook ${name} application/x-python default ${HOOKS}/${file}"
  /opt/pbs/bin/qmgr -c "set hook ${name} enabled=true"
  printf '%s\n' "${digest}" > "${HOOK_STATE}/${name}.sha256"
}
install_hook validate_job 'queuejob,modifyjob,movejob' openpbs_hook_handler.py
install_hook job_status 'runjob,execjob_begin,execjob_end' openpbs_hook_handler.py
# an earlier release's hook rewrote ncpus at queuejob; the scheduler app sizes ncpus now, and
# the copy of the hook body PBS keeps would go on running until deleted
if grep -Eq '^Hook[[:space:]]+calculate_ncpus([[:space:]]|$)' <<< "${EXISTING_HOOKS}"; then
  log "deleting the retired hook calculate_ncpus"
  /opt/pbs/bin/qmgr -c "delete hook calculate_ncpus"
  rm -f "${HOOK_STATE}/calculate_ncpus.sha256"
fi

# The NLB name is not a host PBS can match, and a container bastion's address is per task.
# A container bastion gets no grant: an administrator runs qmgr from inside the scheduler
# task, where root on the server's own host needs none, and a grant by a task's address
# would go stale, then reach whichever desktop is given the address next (users have sudo
# there). A host bastion keeps its grant by its EC2 name, as before. Grants IDEA made or
# inherited from the host era, root@ip-… in the EC2 domain, are removed on every start;
# anything an operator added by hand stays. Nothing here stops the start.
reconcile_managers() {
  local bastion_host="" existing entry grant_pattern
  grant_pattern='^root@ip-[0-9]+(-[0-9]+){3}\.([a-z0-9-]+\.)?(compute|ec2)\.internal$'
  if [[ -z "$(setting bastion-host.service_name '')" ]]; then
    bastion_host="$(setting bastion-host.private_dns_name '')"
    if [[ -n "${bastion_host}" && ! "${bastion_host}" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]; then
      log "ignoring invalid bastion hostname: ${bastion_host}"
      bastion_host=""
    fi
  fi
  existing="$(/opt/pbs/bin/qmgr -c 'list server managers')" || { log "could not list managers; leaving them"; return 0; }
  while IFS= read -r entry; do
    [[ -n "${entry}" && "${entry}" != "root@${bastion_host}" ]] || continue
    if [[ "${entry}" =~ ${grant_pattern} ]]; then
      log "removing stale manager grant ${entry}"
      /opt/pbs/bin/qmgr -c "set server managers -= ${entry}" || log "could not remove ${entry}"
    fi
  done < <(sed -n 's/^[[:space:]]*managers = //p' <<< "${existing}" | tr ', \t' '\n')
  if [[ -n "${bastion_host}" ]] && ! tr ', \t' '\n' <<< "${existing}" | grep -Fxq "root@${bastion_host}"; then
    log "granting qmgr manager rights to root@${bastion_host}"
    /opt/pbs/bin/qmgr -c "set server managers += root@${bastion_host}" || log "could not grant root@${bastion_host}"
  fi
}
reconcile_managers || log "manager reconciliation failed; continuing"

log "starting pbs_sched"
/opt/pbs/sbin/pbs_sched

shutdown() {
  trap - SIGTERM SIGINT
  log "stopping scheduler module, log archiver and user sync"
  kill "${IDEA_PID}" "${ARCHIVE_PID}" "${SYNC_PID}" 2>/dev/null || true
  wait "${IDEA_PID}" "${ARCHIVE_PID}" "${SYNC_PID}" 2>/dev/null || true
  log "SIGTERM: qterm -t quick"
  /opt/pbs/bin/qterm -t quick || true
  exit "${1:-0}"
}

# Synchronize cluster users before starting the scheduler module. The sync itself defers a fresh
# cluster's not-yet-created tables to its background pass and fails on anything else.
log "syncing cluster users and groups into the resolver"
python3.13 /opt/idea/sync_users.py --once
python3.13 /opt/idea/sync_users.py &
SYNC_PID=$!

# Containers have no cron; supervise the same daily archive alongside the app.
export IDEA_CLUSTER_HOME IDEA_CLUSTER_S3_BUCKET PBS_HOME
IDEA_CLUSTER_HOME="$(setting cluster.home_dir '')"
IDEA_CLUSTER_S3_BUCKET="$(setting cluster.cluster_s3_bucket '')"
: "${IDEA_CLUSTER_HOME:?cluster.home_dir is required}"
: "${IDEA_CLUSTER_S3_BUCKET:?cluster.cluster_s3_bucket is required}"
python3.13 -m ideascheduler.app.runtime_tasks archive-logs \
  "${IDEA_APP_DEPLOY_DIR}/scheduler/resources/scripts/send_logs_s3.sh" &
ARCHIVE_PID=$!

log "starting scheduler module"
python3.13 -c "import sys; sys.argv=['ideaserver']; from ideascheduler.app.app_main import main; sys.exit(main())" &
IDEA_PID=$!
trap shutdown SIGTERM SIGINT
# Any supervised process exiting unexpectedly must replace this task.
wait -n "${IDEA_PID}" "${ARCHIVE_PID}" "${SYNC_PID}" || true
log "a scheduler process exited unexpectedly"
shutdown 1
