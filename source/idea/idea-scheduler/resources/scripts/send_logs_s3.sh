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

# shellcheck disable=SC1091
source /etc/environment

DATA_RETENTION=10 # number of days logs stay in the server

S3_BUCKET_SCHEDULER_LOGS="s3://${IDEA_CLUSTER_S3_BUCKET}/logs/${IDEA_MODULE_ID}/"
SCHEDULER_DIRECTORY='/var/spool/pbs'
SCHEDULER_SERVER_LOGS="${SCHEDULER_DIRECTORY}/server_logs/"
SCHEDULER_SCHED_LOGS="${SCHEDULER_DIRECTORY}/sched_logs/"
SCHEDULER_ACCOUNTING="${SCHEDULER_DIRECTORY}/server_priv/accounting/"
COMPUTE_NODE_LOGS="${IDEA_CLUSTER_HOME}/${IDEA_MODULE_ID}/compute_node/logs"

/usr/bin/aws s3 sync ${SCHEDULER_ACCOUNTING} "${S3_BUCKET_SCHEDULER_LOGS}accounting/"
/usr/bin/aws s3 sync ${SCHEDULER_SERVER_LOGS} "${S3_BUCKET_SCHEDULER_LOGS}server_logs/"
/usr/bin/aws s3 sync ${SCHEDULER_SCHED_LOGS} "${S3_BUCKET_SCHEDULER_LOGS}sched_logs/"
/usr/bin/aws s3 sync "${COMPUTE_HOST_LOG}" "${S3_BUCKET_SCHEDULER_LOGS}compute_nodes/"

find "${COMPUTE_NODE_LOGS}"/* -type d -mtime +${DATA_RETENTION} -exec rm -rf {} +
find ${SCHEDULER_SERVER_LOGS} -type f -mtime +${DATA_RETENTION} -delete
find ${SCHEDULER_SCHED_LOGS} -type f -mtime +${DATA_RETENTION} -delete
find ${SCHEDULER_ACCOUNTING} -type f -mtime +${DATA_RETENTION} -delete
