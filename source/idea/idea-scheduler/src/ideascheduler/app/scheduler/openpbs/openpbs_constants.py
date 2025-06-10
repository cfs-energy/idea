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

QSTAT_ERROR_CODE_ABORTED = -2
QSTAT_ERROR_CODE_JOB_FINISHED = 35
QSTAT_ERROR_CODE_UNKNOWN_JOB_ID = 153
QSTAT_ERROR_CODE_UNKNOWN_QUEUE = 170

QALTER_ERROR_CODE_JOB_IS_RUNNING = 167

QMGR_ERROR_CODE_OBJECT_BUSY = 179

QSTAT = '/opt/pbs/bin/qstat'
QMGR = '/opt/pbs/bin/qmgr'
PBSNODES = '/opt/pbs/bin/pbsnodes'
QALTER = '/opt/pbs/bin/qalter'
QDEL = '/opt/pbs/bin/qdel'

CONFIG_FILE_RESOURCE_DEF = '/var/spool/pbs/server_priv/resourcedef'
CONFIG_FILE_SCHED_CONFIG = '/var/spool/pbs/sched_priv/sched_config'
