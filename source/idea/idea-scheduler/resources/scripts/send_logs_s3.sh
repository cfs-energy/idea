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

sync_logs() {
  if [[ -d "$1" ]]; then
    aws s3 sync "$1" "${S3_BUCKET_SCHEDULER_LOGS}$2/"
  fi
}
sync_logs "${SCHEDULER_ACCOUNTING}" accounting
sync_logs "${SCHEDULER_SERVER_LOGS}" server_logs
sync_logs "${SCHEDULER_SCHED_LOGS}" sched_logs
sync_logs "${COMPUTE_NODE_LOGS}" compute_nodes

# Never prune anything when an upload failed.
if [[ -d "${COMPUTE_NODE_LOGS}" ]]; then
  find "${COMPUTE_NODE_LOGS}" -mindepth 1 -maxdepth 1 -type d -mtime +${DATA_RETENTION} -exec rm -rf {} +
fi
for directory in "${SCHEDULER_SERVER_LOGS}" "${SCHEDULER_SCHED_LOGS}" "${SCHEDULER_ACCOUNTING}"; do
  if [[ -d "${directory}" ]]; then
    find "${directory}" -type f -mtime +${DATA_RETENTION} -delete
  fi
done
