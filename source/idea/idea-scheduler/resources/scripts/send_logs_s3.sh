#!/bin/bash

#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.


# This file send the scheduler logs to S3 every day
# This is particularly useful is you are planning to do data mining with services such as Glue / Athena
# To prevent disk to fill up, we also remove files after 10 days (default). This value can be changed using DATA_RETENTION variable

set -euo pipefail

# Host cron gets these from /etc/environment; the container passes current settings.
if [[ -z "${IDEA_CLUSTER_S3_BUCKET:-}" || -z "${IDEA_CLUSTER_HOME:-}" || -z "${IDEA_MODULE_ID:-}" ]]; then
  # shellcheck disable=SC1091
  source /etc/environment
fi
: "${IDEA_CLUSTER_S3_BUCKET:?IDEA_CLUSTER_S3_BUCKET is required}"
: "${IDEA_CLUSTER_HOME:?IDEA_CLUSTER_HOME is required}"
: "${IDEA_MODULE_ID:?IDEA_MODULE_ID is required}"

DATA_RETENTION=10
S3_BUCKET_SCHEDULER_LOGS="s3://${IDEA_CLUSTER_S3_BUCKET}/logs/${IDEA_MODULE_ID}/"
SCHEDULER_DIRECTORY="${PBS_HOME:-/var/spool/pbs}"
SCHEDULER_SERVER_LOGS="${SCHEDULER_DIRECTORY}/server_logs/"
SCHEDULER_SCHED_LOGS="${SCHEDULER_DIRECTORY}/sched_logs/"
SCHEDULER_ACCOUNTING="${SCHEDULER_DIRECTORY}/server_priv/accounting/"
COMPUTE_NODE_LOGS="${IDEA_CLUSTER_HOME}/${IDEA_MODULE_ID}/compute_node/logs"

# A cron invocation and the container loop may overlap during migration.
exec 9>"${SCHEDULER_DIRECTORY}/.idea-log-archive.lock"
flock -n 9 || exit 0

# Each directory is uploaded, then pruned of what is older than the retention. A failed
# upload is logged and that directory keeps twice the retention instead, so one bad day
# cannot let the logs grow on PBS_HOME without bound. The run still fails when an upload did.
failed=0
archive_logs() {
  local directory="$1" name="$2" age="${DATA_RETENTION}"
  [[ -d "${directory}" ]] || return 0
  if ! aws s3 sync "${directory}" "${S3_BUCKET_SCHEDULER_LOGS}${name}/"; then
    age=$(( DATA_RETENTION * 2 ))
    echo "log archive: upload of ${directory} failed; pruning only what is older than ${age} days" >&2
    failed=1
  fi
  if [[ "${name}" == compute_nodes ]]; then
    find "${directory}" -mindepth 1 -maxdepth 1 -type d -mtime +"${age}" -exec rm -rf {} +
  else
    find "${directory}" -type f -mtime +"${age}" -delete
  fi
}
archive_logs "${SCHEDULER_ACCOUNTING}" accounting
archive_logs "${SCHEDULER_SERVER_LOGS}" server_logs
archive_logs "${SCHEDULER_SCHED_LOGS}" sched_logs
archive_logs "${COMPUTE_NODE_LOGS}" compute_nodes
exit "${failed}"
