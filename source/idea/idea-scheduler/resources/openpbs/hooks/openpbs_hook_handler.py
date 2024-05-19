"""
soca - openpbs hooks handler script
a generic script to handle all hooks from openpbs

soca installation:

    # create job validation hooks - events on these hooks will be used for job acls, allowed list and any other validations
    > qmgr -c "create hook validate_job event='queuejob,modifyjob,movejob'"
    > qmgr -c "import hook validate_job application/x-python default ${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks/openpbs_hook_handler.py"

    # create job status hooks - these are be used for job status related triggers and email notifications
    > qmgr -c "create hook job_status event='runjob,execjob_begin,execjob_end'"
    > qmgr -c "import hook job_status application/x-python default ${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks/openpbs_hook_handler.py"

Supported Events:

    At this time, only below events are supported by this script:
        queuejob,
        modifyjob,
        movejob,
        runjob,
        execjob_begin,
        execjob_end

    Supported will be processed by the hook and posted to soca-daemon.
    Events that are not supported, will simply accept the job and exit.

Developer Notes:

    * openpbs_hookhandler.py
        - Do not include external package dependencies from other python installations on the system.
        - The goal is to make this work with pbs python modules only.
        - This allows us to de-couple the dependencies between soca python version and opebpbs python version.

    * delegated implementation in idea-scheduler
        - the implementation to handle the logic is delegated to idea-scheduler
        - delegated invocations should respond as quickly as possible, as user is waiting for the job to be submitted
        - this not only affects the UX, but also impacts stability and performance
        - delegated invocations must make use of caching or concurrent strategies to improve response times.

    * payload format

        When pbs triggers a hook, an event is posted to soca-daemon as below. The payload contains all information to validate
        or take relevant action on the job to implement the desired functionality.

        {
          "header": {
            "namespace": "OpenPBSHook.queuejob",
            "request_id": "305e0715-4be4-493d-aad0-6c90d7ff5c83"
          },
          "payload": {
            "scheduler": "openpbs",
            "event": {
              "type": "queuejob",
              "hook_name": "validate_job",
              "requestor": "admin",
              "requestor_host": "ip-10-0-0-9.us-west-2.compute.internal",
              "hook_type": "site",
              "job": {
                "Checkpoint": "u",
                "Hold_Types": "n",
                "Job_Name": "single-job",
                "Join_Path": "oe",
                "Keep_Files": "n",
                "Mail_Points": "a",
                "Output_Path": "ip-10-0-0-9.us-west-2.compute.internal:/data/home/admin/projects/sample-job/./logs/single_job.qlog",
                "Priority": "0",
                "project": "sample_project",
                "queue": "normal",
                "Rerunable": "1",
                "Resource_List": {
                  "nodes": "1",
                  "ht_support": "True",
                  "instance_type": "t3.xlarge+t3.2xlarge",
                  "placement_group": "False",
                  "spot_price": "auto"
                },
                "Variable_List": {
                  "PBS_O_HOME": "/data/home/admin",
                  "PBS_O_LANG": "en_US.UTF-8",
                  "PBS_O_LOGNAME": "admin"
                  ..
                  ..
                }
              }
            }
          }
        }

"""

import pbs
import sys
import os
import socket
import json
import uuid
import time
import base64
from urllib.request import urlopen, Request

DEFAULT_ENCODING = 'utf-8'
SOCKET_TIMEOUT_SECS = 10
SOCKET_RECV_BUFFER_SIZE = 1024
DEBUG_MODE = True

EC2_INSTANCE_METADATA_LATEST = 'http://169.254.169.254/latest'
EC2_INSTANCE_METADATA_URL_PREFIX = f'{EC2_INSTANCE_METADATA_LATEST}/meta-data'
EC2_INSTANCE_METADATA_API_URL = f'{EC2_INSTANCE_METADATA_LATEST}/api'
EC2_INSTANCE_METADATA_URL_INSTANCE_ID = f'{EC2_INSTANCE_METADATA_URL_PREFIX}/instance-id'
EC2_INSTANCE_METADATA_URL_INSTANCE_TYPE = f'{EC2_INSTANCE_METADATA_URL_PREFIX}/instance-type'

EC2_INSTANCE_METADATA_TOKEN_REQUEST_HEADER = 'X-aws-ec2-metadata-token-ttl-seconds'
EC2_INSTANCE_METADATA_TOKEN_HEADER = 'X-aws-ec2-metadata-token'
EC2_INSTANCE_METADATA_TOKEN_REQUEST_TTL = '900' # Value in seconds

start_time = round(time.time() * 1000)


class SocaException(Exception):
    def __init__(self, message):
        self.message = message


def _log(level, message: str):
    pbs.logmsg(level, message)


def log_fine(message: str):
    if not DEBUG_MODE:
        return
    _log(level=pbs.LOG_DEBUG, message=message)


def log_debug(message: str):
    _log(level=pbs.LOG_DEBUG, message=message)


def log_error(message: str):
    _log(level=pbs.LOG_ERROR, message=message)


def get_imds_auth_token() -> str:
    """
    Generate an IMDSv2 authentication header
    :returns: str - A suitable value for follow-up authenticated requests.
    """
    req = Request(url=f'{EC2_INSTANCE_METADATA_API_URL}/token', data=b'', method='PUT')
    req.add_header(EC2_INSTANCE_METADATA_TOKEN_REQUEST_HEADER, EC2_INSTANCE_METADATA_TOKEN_REQUEST_TTL)

    with urlopen(req) as conn:
        content = conn.read()
        if not content:
            raise SocaException(
                message=f'Failed to retrieve EC2 instance IMDSv2 authentication token. Empty Reply. Statuscode: ({conn.status})'
            )
        else:
            if conn.status != 200:
                raise SocaException(
                    message=f'Failed to retrieve EC2 instance IMDSv2 authentication token. Statuscode: ({conn.status})'
                )
            else:
                return content.decode(DEFAULT_ENCODING)


def get_imds_url(url: str) -> str:
    """
    Helper to get an AWS EC2 Instance Medata Server (IMDS) v2
    URL - taking care of the authentication for the caller.
    :param url: str - The URL to fetch from the localhost IMDS service
    :returns: str - String of the fetched URL
    """
    req = Request(url=url, method='GET')
    req.add_header(EC2_INSTANCE_METADATA_TOKEN_HEADER, get_imds_auth_token())

    with urlopen(req) as conn:
        content = conn.read()
        if not content:
            raise SocaException(
                message=f'Failed to retrieve EC2 instance IMDSv2 URL ({url}). Empty Reply. Statuscode: ({conn.status})'
            )
        else:
            if conn.status != 200:
                raise SocaException(
                    message=f'Failed to retrieve EC2 instance IMDSv2 URL ({url}). Statuscode: ({conn.status})'
                )
            else:
                return content.decode(DEFAULT_ENCODING)


def get_instance_id() -> str:
    return get_imds_url(url=EC2_INSTANCE_METADATA_URL_INSTANCE_ID)


def get_instance_type() -> str:
    return get_imds_url(url=EC2_INSTANCE_METADATA_URL_INSTANCE_TYPE)


# refer to PBSHooks2020.1.pdf (5.2.4.15 Table: Reading & Setting Job Attributes in Hooks)
# procedure to generate this file:
#   1. copy and paste the table in section 5.2.4.15 in a text file
#   2. format the file, replace all R,S to rs, replace all --- to -
#   3. runjob as 2 columns: (accept and reject), we are considering the accept column as we are not setting
#       any values at this point in time.
# legend (r=read, s=set, -=disabled)
# delta - tracking changes here due to documentation bugs:
#   Executable
#   > queuejob: rs -> s
#   > runjob: r -> -
#   resources_released_list
#   > queuejob, modifyjob, runjob: r -> -
#   sw_index
#   > modifyjob, runjob: r -> -
JOB_PARAM_EVENT_TYPE_MATRIX = """
Job Attribute,queuejob,modifyjob,movejob,runjob,resvsub,resv_end,periodic,execjob_begin,execjob_attach,execjob_prologue,execjob_launch,execjob_postsuspend,execjob_preresume,execjob_end,execjob_epilogue,execjob_preterm,exechost_startup,exechost_periodic,
accounting_id,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Account_Name,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
accrue_type,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
alt_id,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
argument_list,-,-,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
array,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
array_id,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
array_index,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
array_indices_remaining,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
array_indices_submitted,rs,-,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
array_state_count,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
block,-,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Checkpoint,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
comment,-,-,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
create_resv_from_job,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,rs,
ctime,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
depend,rs,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
egroup,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
eligible_time,-,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Error_Path,rs,rs,r,rs,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
estimated,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
etime,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
euser,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Executable,s,-,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Execution_Time,rs,rs,r,r,-,-,-,rs,r,rs,rs,r,r,r,rs,rs,-,rs,
exec_host,-,-,r,-,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
exec_vnode,-,-,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Exit_status,-,r,r,r,-,-,-,-,r,-,-,r,r,r,r,-,-,-,
group_list,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
hashname,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Hold_Types,rs,rs,r,r,-,-,-,rs,r,rs,rs,r,r,r,rs,rs,-,rs,
interactive,rs,r,o,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r
jobdir,-,r,r,-,-,-,-,-,-,-,-,-,-,-,r,-,-,-,
Job_Name,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Job_Owner,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
job_state,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Join_Path,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Keep_Files,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Mail_Points,rs,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Mail_Users,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
mtime,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
no_stdio_sockets,-,-,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Output_Path,rs,rs,r,rs,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Priority,rs,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
project,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
qtime,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
queue,rs,r,rs,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
queue_rank,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
queue_type,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
release_nodes_on_stageout,rs,rs,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Rerunable,rs,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
resources_released,r,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
resources_released_list,-,-,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
resources_used,-,r,r,r,-,-,-,rs,r,rs,rs,r,r,r,rs,rs,rs,rs,
Resource_List,rs,rs,r,rs,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
run_count,rs,rs,r,r,-,-,-,rs,r,rs,rs,r,r,r,rs,rs,-,rs,
run_version,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
sandbox,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
schedselect,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
sched_hint,-,-,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
server,-,r,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
session_id,-,-,-,-,-,-,-,-,-,-,-,r,r,r,r,r,-,-,
Shell_Path_List,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
stagein,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
stageout,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
Stageout_status,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
stime,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Submit_arguments,-,-,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
substate,-,r,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
sw_index,-,-,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
umask,rs,rs,r,r,-,-,-,r,r,r,r,r,r,r,r,r,-,r,
User_List,rs,rs,r,r,-,-,-,-,-,-,-,-,-,-,-,-,-,-,
Variable_List,rs,rs,r,rs,-,-,-,rs,r,r,r,r,r,r,r,r,-,rs,
"""

# event types - refer to https://github.com/openpbs/openpbs/blob/master/src/include/hook.h#L83

# server hooks
HOOK_EVENT_QUEUEJOB = 0x01
HOOK_EVENT_MODIFYJOB = 0x02
HOOK_EVENT_RESVSUB = 0x04
HOOK_EVENT_MOVEJOB = 0x08
HOOK_EVENT_RUNJOB = 0x10
HOOK_EVENT_PROVISION = 0x20
HOOK_EVENT_PERIODIC = 0x8000
HOOK_EVENT_RESV_END = 0x10000
HOOK_EVENT_MANAGEMENT = 0x200000
HOOK_EVENT_MODIFYVNODE = 0x400000
HOOK_EVENT_RESV_BEGIN = 0x1000000
HOOK_EVENT_RESV_CONFIRM = 0x2000000
HOOK_EVENT_MODIFYRESV = 0x4000000
# mom hooks
HOOK_EVENT_EXECJOB_BEGIN = 0x40
HOOK_EVENT_EXECJOB_PROLOGUE = 0x80
HOOK_EVENT_EXECJOB_EPILOGUE = 0x100
HOOK_EVENT_EXECJOB_END = 0x200
HOOK_EVENT_EXECJOB_PRETERM = 0x400
HOOK_EVENT_EXECJOB_LAUNCH = 0x800
HOOK_EVENT_EXECHOST_PERIODIC = 0x1000
HOOK_EVENT_EXECHOST_STARTUP = 0x2000
HOOK_EVENT_EXECJOB_ATTACH = 0x4000
HOOK_EVENT_EXECJOB_RESIZE = 0x20000
HOOK_EVENT_EXECJOB_ABORT = 0x40000
HOOK_EVENT_EXECJOB_POSTSUSPEND = 0x80000
HOOK_EVENT_EXECJOB_PRERESUME = 0x100000

HOOK_EVENT_TO_NAME_MAP = {
    0x01: 'queuejob',
    0x02: 'modifyjob',
    0x04: 'resvsub',
    0x08: 'movejob',
    0x10: 'runjob',
    0x20: 'provision',
    0x8000: 'periodic',
    0x10000: 'resv_end',
    0x200000: 'management',
    0x400000: 'modifyvnode',
    0x1000000: 'resv_begin',
    0x2000000: 'resv_confirm',
    0x4000000: 'modify_resv',
    0x40: 'execjob_begin',
    0x80: 'execjob_prologue',
    0x100: 'execjob_epilogue',
    0x200: 'execjob_end',
    0x400: 'execjob_preterm',
    0x800: 'execjob_launch',
    0x1000: 'exechost_periodic',
    0x2000: 'exechost_startup',
    0x4000: 'execjob_attach',
    0x20000: 'execjob_resize',
    0x40000: 'execjob_abort',
    0x80000: 'execjob_postsuspend',
    0x100000: 'execjob_preresume'
}

# SUPPORTED_EVENTS are the only event types currently supported for this script.
# additional testing is needed to try and get all data from pbs hooks for all types of events
# there are some documentation bugs, due to which the statemachine for eventtype -> parameters available is not
# working for all events.
# if the event is not supported, we will skip the hook execution and accept the hook.

SUPPORTED_EVENTS = [
    HOOK_EVENT_QUEUEJOB,
    HOOK_EVENT_MODIFYJOB,
    HOOK_EVENT_MOVEJOB,
    HOOK_EVENT_RUNJOB,
    HOOK_EVENT_EXECJOB_BEGIN,
    HOOK_EVENT_EXECJOB_END
]

# we are not performing any critical activities in soca-daemon for these events
# and the job should be accepted irrespective of the result from soca-daemon
ALWAYS_SUCCESS_EVENTS = [
    HOOK_EVENT_RUNJOB,
    HOOK_EVENT_EXECJOB_BEGIN,
    HOOK_EVENT_EXECJOB_END
]

PRE_EXECUTION_HOOKS = [
    HOOK_EVENT_QUEUEJOB,
    HOOK_EVENT_MODIFYJOB,
    HOOK_EVENT_RESVSUB,
    HOOK_EVENT_MOVEJOB,
    HOOK_EVENT_RUNJOB,
    HOOK_EVENT_PROVISION,
    HOOK_EVENT_PERIODIC,
    HOOK_EVENT_RESV_END,
    HOOK_EVENT_MANAGEMENT,
    HOOK_EVENT_MODIFYVNODE,
    HOOK_EVENT_RESV_BEGIN,
    HOOK_EVENT_RESV_CONFIRM,
    HOOK_EVENT_MODIFYRESV
]
EXECUTION_HOOKS = [
    HOOK_EVENT_EXECJOB_BEGIN,
    HOOK_EVENT_EXECJOB_PROLOGUE,
    HOOK_EVENT_EXECJOB_EPILOGUE,
    HOOK_EVENT_EXECJOB_END,
    HOOK_EVENT_EXECJOB_PRETERM,
    HOOK_EVENT_EXECJOB_LAUNCH,
    HOOK_EVENT_EXECHOST_PERIODIC,
    HOOK_EVENT_EXECHOST_STARTUP,
    HOOK_EVENT_EXECJOB_ATTACH,
    HOOK_EVENT_EXECJOB_RESIZE,
    HOOK_EVENT_EXECJOB_ABORT,
    HOOK_EVENT_EXECJOB_POSTSUSPEND,
    HOOK_EVENT_EXECJOB_PRERESUME
]


class OpenPBSJob:
    def __init__(self, event_type: str, job):
        self.event_type = event_type
        self.job = job
        self.matrix = self.build_matrix()

    def build_matrix(self):
        matrix = {}
        header = []
        for line in JOB_PARAM_EVENT_TYPE_MATRIX.splitlines():

            if len(line.strip()) == 0:
                continue

            if len(header) == 0:
                header = line.split(',')
                continue

            tokens = line.split(',')
            event_types = []

            index = 0
            for event_type in header:
                if index == 0:
                    index += 1
                    continue
                if self.event_type != event_type:
                    index += 1
                    continue
                if DEBUG_MODE:
                    log_fine(f'eventtype: {event_type} - {tokens[0]} = {tokens[index]}')
                if tokens[index] in ['r', 'rs']:
                    event_types.append(event_type)
                index += 1

            matrix[tokens[0]] = event_types

        if DEBUG_MODE:
            log_fine('matrix: ' + json.dumps(matrix))
        return matrix

    @property
    def id(self):
        if self.job.id is not None:
            return str(self.job.id)
        return None

    @property
    def accounting_id(self):
        if self.event_type in self.matrix['accounting_id']:
            if self.job.accounting_id is not None:
                return str(self.job.accounting_id)
        return None

    @property
    def Account_Name(self):
        if self.event_type in self.matrix['Account_Name']:
            if self.job.Account_Name is not None:
                return str(self.job.Account_Name)
        return None

    @property
    def accrue_type(self):
        if self.event_type in self.matrix['accrue_type']:
            if self.job.accrue_type is not None:
                return str(self.job.accrue_type)
        return None

    @property
    def alt_id(self):
        if self.event_type in self.matrix['alt_id']:
            if self.job.alt_id is not None:
                return self.job.alt_id
        return None

    @property
    def argument_list(self):
        if self.event_type in self.matrix['argument_list']:
            if self.job.argument_list is None:
                return None
            return str(self.job.argument_list)
        return None

    @property
    def array(self):
        if self.event_type in self.matrix['array']:
            if self.job.array is not None:
                return str(self.job.array)
        return None

    @property
    def array_id(self):
        if self.event_type in self.matrix['array_id']:
            if self.job.array_id is not None:
                return str(self.job.array_id)
        return None

    @property
    def array_index(self):
        if self.event_type in self.matrix['array_index']:
            if self.job.array_index is not None:
                return str(self.job.array_index)
        return None

    @property
    def array_indices_remaining(self):
        if self.event_type in self.matrix['array_indices_remaining']:
            if self.job.array_indices_remaining is not None:
                return str(self.job.array_indices_remaining)
        return None

    @property
    def array_indices_submitted(self):
        if self.event_type in self.matrix['array_indices_submitted']:
            if self.job.array_indices_submitted is not None:
                return str(self.job.array_indices_submitted)
        return None

    @property
    def array_state_count(self):
        if self.event_type in self.matrix['array_state_count']:
            if self.job.array_state_count is not None:
                return str(self.job.array_state_count)
        return None

    @property
    def block(self):
        if self.event_type in self.matrix['block']:
            if self.job.block is not None:
                return str(self.job.block)
        return None

    @property
    def Checkpoint(self):
        if self.event_type in self.matrix['Checkpoint']:
            if self.job.Checkpoint is not None:
                return str(self.job.Checkpoint)
        return None

    @property
    def comment(self):
        if self.event_type in self.matrix['comment']:
            if self.job.comment is not None:
                return str(self.job.comment)
        return None

    @property
    def create_resv_from_job(self):
        if self.event_type in self.matrix['create_resv_from_job']:
            if self.job.create_resv_from_job is not None:
                return str(self.job.create_resv_from_job)
        return None

    @property
    def ctime(self):
        if self.event_type in self.matrix['ctime']:
            if self.job.ctime is not None:
                return str(self.job.ctime)
        return None

    @property
    def depend(self):
        if self.event_type in self.matrix['depend']:
            if self.job.depend is not None:
                return str(self.job.depend)
        return None

    @property
    def egroup(self):
        if self.event_type in self.matrix['egroup']:
            if self.job.egroup is not None:
                return str(self.job.egroup)
        return None

    @property
    def eligible_time(self):
        if self.event_type in self.matrix['eligible_time']:
            if self.job.eligible_time is not None:
                return str(self.job.eligible_time)
        return None

    @property
    def Error_Path(self):
        if self.event_type in self.matrix['Error_Path']:
            if self.job.Error_Path is not None:
                return str(self.job.Error_Path)
        return None

    @property
    def estimated(self):
        if self.event_type in self.matrix['estimated']:
            if self.job.estimated is not None:
                return str(self.job.estimated)
        return None

    @property
    def etime(self):
        if self.event_type in self.matrix['etime']:
            if self.job.etime is not None:
                return str(self.job.etime)
        return None

    @property
    def euser(self):
        if self.event_type in self.matrix['euser']:
            if self.job.euser is not None:
                return str(self.job.euser)
        return None

    @property
    def Executable(self):
        if self.event_type in self.matrix['Executable']:
            if self.job.Executable is not None:
                return str(self.job.Executable)
        return None

    @property
    def Execution_Time(self):
        if self.event_type in self.matrix['Execution_Time']:
            if self.job.Execution_Time is not None:
                return str(self.job.Execution_Time)
        return None

    @property
    def exec_host(self):
        if self.event_type in self.matrix['exec_host']:
            if self.job.exec_host is not None:
                return str(self.job.exec_host)
        return None

    @property
    def exec_vnode(self):
        if self.event_type in self.matrix['exec_vnode']:
            if self.job.exec_vnode is not None:
                return str(self.job.exec_vnode)
        return None

    @property
    def Exit_status(self):
        if self.event_type in self.matrix['Exit_status']:
            if self.job.Exit_status is not None:
                return str(self.job.Exit_status)
        return None

    @property
    def group_list(self):
        if self.event_type in self.matrix['group_list']:
            if self.job.group_list is None:
                return None
            response = {}
            for key in self.job.group_list.keys():
                response[key] = str(self.job.group_list[key])
            return response
        return None

    @property
    def hashname(self):
        if self.event_type in self.matrix['hashname']:
            if self.job.hashname is not None:
                return str(self.job.hashname)
        return None

    @property
    def Hold_Types(self):
        if self.event_type in self.matrix['Hold_Types']:
            if self.job.Hold_Types is not None:
                return str(self.job.Hold_Types)
        return None

    @property
    def interactive(self):
        if self.event_type in self.matrix['interactive']:
            if self.job.interactive is not None:
                return str(self.job.interactive)
        return None

    @property
    def jobdir(self):
        if self.event_type in self.matrix['jobdir']:
            if self.job.jobdir is not None:
                return str(self.job.jobdir)
        return None

    @property
    def Job_Name(self):
        if self.event_type in self.matrix['Job_Name']:
            if self.job.Job_Name is not None:
                return str(self.job.Job_Name)
        return None

    @property
    def Job_Owner(self):
        if self.event_type in self.matrix['Job_Owner']:
            if self.job.Job_Owner is not None:
                return str(self.job.Job_Owner)
        return None

    @property
    def job_state(self):
        if self.event_type in self.matrix['job_state']:
            if self.job.job_state is not None:
                state = self.job.job_state
                if state == pbs.JOB_STATE_EXITING:
                    return 'E'
                elif state == pbs.JOB_STATE_EXPIRED:
                    return 'X'
                elif state == pbs.JOB_STATE_BEGUN:
                    return 'B'
                elif state == pbs.JOB_STATE_FINISHED:
                    return 'F'
                elif state == pbs.JOB_STATE_HELD:
                    return 'H'
                elif state == pbs.JOB_STATE_MOVED:
                    return 'M'
                elif state == pbs.JOB_STATE_QUEUED:
                    return 'Q'
                elif state == pbs.JOB_STATE_RUNNING:
                    return 'R'
                elif state == pbs.JOB_STATE_SUSPEND:
                    return 'S'
                elif state == pbs.JOB_STATE_SUSPEND_USERACTIVE:
                    return 'S'
                else:
                    return str(state)
        return None

    @property
    def Join_Path(self):
        if self.event_type in self.matrix['Join_Path']:
            if self.job.Join_Path is not None:
                return str(self.job.Join_Path)
        return None

    @property
    def Keep_Files(self):
        if self.event_type in self.matrix['Keep_Files']:
            if self.job.Keep_Files is not None:
                return str(self.job.Keep_Files)
        return None

    @property
    def Mail_Points(self):
        if self.event_type in self.matrix['Mail_Points']:
            if self.job.Mail_Points is not None:
                return str(self.job.Mail_Points)
        return None

    @property
    def Mail_Users(self):
        if self.event_type in self.matrix['Mail_Users']:
            if self.job.Mail_Users is not None:
                return str(self.job.Mail_Users)
        return None

    @property
    def mtime(self):
        if self.event_type in self.matrix['mtime']:
            if self.job.mtime is not None:
                return str(self.job.mtime)
        return None

    @property
    def no_stdio_sockets(self):
        if self.event_type in self.matrix['no_stdio_sockets']:
            if self.job.no_stdio_sockets is not None:
                return str(self.job.no_stdio_sockets)
        return None

    @property
    def Output_Path(self):
        if self.event_type in self.matrix['Output_Path']:
            if self.job.Output_Path is not None:
                return str(self.job.Output_Path)
        return None

    @property
    def Priority(self):
        if self.event_type in self.matrix['Priority']:
            if self.job.Priority is not None:
                return str(self.job.Priority)
        return None

    @property
    def project(self):
        if self.event_type in self.matrix['project']:
            if self.job.project is not None:
                return str(self.job.project)
        return None

    @property
    def qtime(self):
        if self.event_type in self.matrix['qtime']:
            if self.job.qtime is not None:
                return str(self.job.qtime)
        return None

    @property
    def queue(self):
        if self.event_type in self.matrix['queue']:
            if self.job.queue is not None:
                return str(self.job.queue)
        return None

    @property
    def queue_rank(self):
        if self.event_type in self.matrix['queue_rank']:
            if self.job.queue_rank is not None:
                return str(self.job.queue_rank)
        return None

    @property
    def queue_type(self):
        if self.event_type in self.matrix['queue_type']:
            if self.job.queue_type is not None:
                return str(self.job.queue_type)
        return None

    @property
    def release_nodes_on_stageout(self):
        if self.event_type in self.matrix['release_nodes_on_stageout']:
            if self.job.release_nodes_on_stageout is not None:
                return str(self.job.release_nodes_on_stageout)
        return None

    @property
    def Rerunable(self):
        if self.event_type in self.matrix['Rerunable']:
            if self.job.Rerunable is not None:
                return str(self.job.Rerunable)
        return None

    @property
    def resources_released(self):
        if self.event_type in self.matrix['resources_released']:
            if self.job.resources_released is not None:
                return str(self.job.resources_released)
        return None

    @property
    def resources_released_list(self):
        if self.event_type in self.matrix['resources_released_list']:
            if self.job.resources_released_list is None:
                return None
            response = {}
            for key in self.job.resources_released_list.keys():
                response[key] = str(self.job.resources_released_list[key])
            return response
        return None

    @property
    def resources_used(self):
        if self.event_type in self.matrix['resources_used']:
            if self.job.resources_used is not None:
                return str(self.job.resources_used)
        return None

    @property
    def Resource_List(self):
        if self.event_type in self.matrix['Resource_List']:
            if self.job.Resource_List is None:
                return None
            response = {}
            for key in self.job.Resource_List.keys():
                response[key] = str(self.job.Resource_List[key])
            return response
        return None

    @property
    def run_count(self):
        if self.event_type in self.matrix['run_count']:
            if self.job.run_count is not None:
                return str(self.job.run_count)
        return None

    @property
    def run_version(self):
        if self.event_type in self.matrix['run_version']:
            if self.job.run_version is not None:
                return str(self.job.run_version)
        return None

    @property
    def sandbox(self):
        if self.event_type in self.matrix['sandbox']:
            if self.job.sandbox is not None:
                return str(self.job.sandbox)
        return None

    @property
    def schedselect(self):
        if self.event_type in self.matrix['schedselect']:
            if self.job.schedselect is not None:
                return str(self.job.schedselect)
        return None

    @property
    def sched_hint(self):
        if self.event_type in self.matrix['sched_hint']:
            if self.job.sched_hint is not None:
                return str(self.job.sched_hint)
        return None

    @property
    def server(self):
        if self.event_type in self.matrix['server']:
            if self.job.server is not None:
                return str(self.job.server)
        return None

    @property
    def session_id(self):
        if self.event_type in self.matrix['session_id']:
            if self.job.session_id is not None:
                return str(self.job.session_id)
        return None

    @property
    def Shell_Path_List(self):
        if self.event_type in self.matrix['Shell_Path_List']:
            if self.job.Shell_Path_List is None:
                return None
            return str(self.job.Shell_Path_List)
        return None

    @property
    def stagein(self):
        if self.event_type in self.matrix['stagein']:
            if self.job.stagein is not None:
                return str(self.job.stagein)
        return None

    @property
    def stageout(self):
        if self.event_type in self.matrix['stageout']:
            if self.job.stageout is not None:
                return str(self.job.stageout)
        return None

    @property
    def Stageout_status(self):
        if self.event_type in self.matrix['Stageout_status']:
            if self.job.Stageout_status is not None:
                return str(self.job.Stageout_status)
        return None

    @property
    def stime(self):
        if self.event_type in self.matrix['stime']:
            if self.job.stime is not None:
                return str(self.job.stime)
        return None

    @property
    def Submit_arguments(self):
        if self.event_type in self.matrix['Submit_arguments']:
            if self.job.Submit_arguments is not None:
                return str(self.job.Submit_arguments)
        return None

    @property
    def substate(self):
        if self.event_type in self.matrix['substate']:
            if self.job.substate is not None:
                return str(self.job.substate)
        return None

    @property
    def sw_index(self):
        if self.event_type in self.matrix['sw_index']:
            if self.job.sw_index is not None:
                return str(self.job.sw_index)
        return None

    @property
    def umask(self):
        if self.event_type in self.matrix['umask']:
            if self.job.umask is not None:
                return str(self.job.umask)
        return None

    @property
    def User_List(self):
        if self.event_type in self.matrix['User_List']:
            if self.job.User_List is None:
                return None
            response = {}
            for key in self.job.User_List.keys():
                response[key] = str(self.job.User_List[key])
            return response
        return None

    @property
    def Variable_List(self):
        if self.event_type in self.matrix['Variable_List']:
            if self.job.Variable_List is None:
                return None
            response = {}
            for key in self.job.Variable_List.keys():
                response[key] = str(self.job.Variable_List[key])
            return response
        return None

    def build(self):
        pbs_job = {}
        pbs_job['id'] = self.id
        pbs_job['accounting_id'] = self.accounting_id
        pbs_job['Account_Name'] = self.Account_Name
        pbs_job['accrue_type'] = self.accrue_type
        pbs_job['alt_id'] = self.alt_id
        pbs_job['argument_list'] = self.argument_list
        pbs_job['array'] = self.array
        pbs_job['array_id'] = self.array_id
        pbs_job['array_index'] = self.array_index
        pbs_job['array_indices_remaining'] = self.array_indices_remaining
        pbs_job['array_indices_submitted'] = self.array_indices_submitted
        pbs_job['array_state_count'] = self.array_state_count
        pbs_job['block'] = self.block
        pbs_job['Checkpoint'] = self.Checkpoint
        pbs_job['comment'] = self.comment
        pbs_job['create_resv_from_job'] = self.create_resv_from_job
        pbs_job['ctime'] = self.ctime
        pbs_job['depend'] = self.depend
        pbs_job['egroup'] = self.egroup
        pbs_job['eligible_time'] = self.eligible_time
        pbs_job['Error_Path'] = self.Error_Path
        pbs_job['estimated'] = self.estimated
        pbs_job['etime'] = self.etime
        pbs_job['euser'] = self.euser
        pbs_job['Executable'] = self.Executable
        pbs_job['Execution_Time'] = self.Execution_Time
        pbs_job['exec_host'] = self.exec_host
        pbs_job['exec_vnode'] = self.exec_vnode
        pbs_job['Exit_status'] = self.Exit_status
        pbs_job['group_list'] = self.group_list
        pbs_job['hashname'] = self.hashname
        pbs_job['Hold_Types'] = self.Hold_Types
        pbs_job['interactive'] = self.interactive
        pbs_job['jobdir'] = self.jobdir
        pbs_job['Job_Name'] = self.Job_Name
        pbs_job['Job_Owner'] = self.Job_Owner
        pbs_job['job_state'] = self.job_state
        pbs_job['Join_Path'] = self.Join_Path
        pbs_job['Keep_Files'] = self.Keep_Files
        pbs_job['Mail_Points'] = self.Mail_Points
        pbs_job['Mail_Users'] = self.Mail_Users
        pbs_job['mtime'] = self.mtime
        pbs_job['no_stdio_sockets'] = self.no_stdio_sockets
        pbs_job['Output_Path'] = self.Output_Path
        pbs_job['Priority'] = self.Priority
        pbs_job['project'] = self.project
        pbs_job['qtime'] = self.qtime
        pbs_job['queue'] = self.queue
        pbs_job['queue_rank'] = self.queue_rank
        pbs_job['queue_type'] = self.queue_type
        pbs_job['release_nodes_on_stageout'] = self.release_nodes_on_stageout
        pbs_job['Rerunable'] = self.Rerunable
        pbs_job['resources_released'] = self.resources_released
        pbs_job['resources_released_list'] = self.resources_released_list
        pbs_job['resources_used'] = self.resources_used
        pbs_job['Resource_List'] = self.Resource_List
        pbs_job['run_count'] = self.run_count
        pbs_job['run_version'] = self.run_version
        pbs_job['sandbox'] = self.sandbox
        pbs_job['schedselect'] = self.schedselect
        pbs_job['sched_hint'] = self.sched_hint
        pbs_job['server'] = self.server
        pbs_job['session_id'] = self.session_id
        pbs_job['Shell_Path_List'] = self.Shell_Path_List
        pbs_job['stagein'] = self.stagein
        pbs_job['stageout'] = self.stageout
        pbs_job['Stageout_status'] = self.Stageout_status
        pbs_job['stime'] = self.stime
        pbs_job['Submit_arguments'] = self.Submit_arguments
        pbs_job['substate'] = self.substate
        pbs_job['sw_index'] = self.sw_index
        pbs_job['umask'] = self.umask
        pbs_job['User_List'] = self.User_List
        # pbs_job['Variable_List'] = self.Variable_List

        result = {}
        for key in pbs_job:
            if pbs_job[key] is None:
                continue
            if DEBUG_MODE:
                log_fine('pbs_job: %s = %s' % (key, type(pbs_job[key])))
            result[key] = pbs_job[key]
            if key == 'Resource_List':
                value = result[key]
                if 'dry_run' in value:
                    result['dry_run'] = value['dry_run']

        return result


class OpenPBSNode:
    def __init__(self, node):
        self.node = node

    @property
    def pcpus(self):
        if self.node.pcpus is None:
            return None
        return str(self.node.pcpus)

    @property
    def pbs_version(self):
        if self.node.pbs_version is None:
            return None
        return str(self.node.pbs_version)

    @property
    def resources_assigned(self):
        if self.node.resources_assigned is None:
            return None
        response = {}
        for key in self.node.resources_assigned.keys():
            if self.node.resources_assigned[key] is None:
                continue
            response[key] = str(self.node.resources_assigned[key])
        return response

    @property
    def resources_available(self):
        if self.node.resources_available is None:
            return None
        response = {}
        for key in self.node.resources_available.keys():
            if self.node.resources_available[key] is None:
                continue
            response[key] = str(self.node.resources_available[key])
        return response

    def build(self):

        pbs_node = {}
        pbs_node['resources_assigned'] = self.resources_assigned
        pbs_node['resources_available'] = self.resources_available
        pbs_node['pcpus'] = self.pcpus
        pbs_node['pbs_version'] = self.pbs_version

        result = {}
        for key in pbs_node:
            if pbs_node[key] is None:
                continue
            if DEBUG_MODE:
                log_fine('pbs_node: %s = %s' % (key, type(pbs_node[key])))
            result[key] = pbs_node[key]

        return result


class OpenPBSEvent:
    """
    OpenPBSEvent
    Refer To: PBSHooks2020.1 - Figure 6-2:Expanded view of event object members and methods
    """

    def __init__(self, event):
        self.event = event

    @property
    def type(self):
        return HOOK_EVENT_TO_NAME_MAP[self.event.type]

    @property
    def hook_name(self):
        return str(self.event.hook_name)

    @property
    def requestor(self):
        return str(self.event.requestor)

    @property
    def requestor_host(self):
        return str(self.event.requestor_host)

    @property
    def hook_type(self):
        return str(self.event.hook_type)

    @property
    def user(self):
        return str(self.event.user)

    @property
    def vnode_list(self):
        if self.type in [
            'exechost_startup',
            'exechost_periodic',
            'execjob_launch',
            'execjob_prologue',
            'execjob_begin',
            'execjob_end',
            'execjob_preterm',
            'execjob_epilogue',
            'execjob_preresume',
            'execjob_postsuspend'
            'execjob_attach'
        ]:
            if self.event.vnode_list is None:
                return None
            response = {}
            for key in self.event.vnode_list.keys():
                pbs_node = self.event.vnode_list[key]
                node = OpenPBSNode(node=pbs_node).build()
                response[key] = node
            return response
        return None

    @property
    def job_list(self):
        if self.type in [
            'exechost_periodic'
        ]:
            if self.event.job_list is None:
                return None
            response = []
            for key in self.event.job_list.keys():
                pbs_job = self.event.job_list[key]
                job = OpenPBSJob(event_type=self.type, job=pbs_job).build()
                response.append(job)
            return response
        return None

    @property
    def argv(self):
        if self.type in [
            'execjob_launch'
        ]:
            if self.event.argv is not None:
                return str(self.event.argv)
        return None

    @property
    def env(self):
        if self.type in [
            'execjob_launch'
        ]:
            if self.event.env is not None:
                return str(self.event.env)
        return None

    @property
    def job(self):
        if self.type in [
            'execjob_launch',
            'execjob_prologue',
            'execjob_begin',
            'execjob_end',
            'execjob_preterm',
            'execjob_epilogue',
            'execjob_preresume',
            'execjob_postsuspend',
            'execjob_attach',
            'queuejob',
            'runjob',
            'modifyjob',
            'movejob'
        ]:
            return self.event.job
        return None

    @property
    def progname(self):
        if self.type in [
            'execjob_launch'
        ]:
            if self.event.progname is not None:
                return str(self.event.progname)
        return None

    @property
    def vnode_list_fail(self):
        if self.type in [
            'execjob_launch',
            'execjob_prologue'
        ]:
            if self.event.vnode_list_fail is None:
                return None
            response = {}
            for key in self.event.vnode_list_fail.keys():
                pbs_node = self.event.vnode_list_fail[key]
                node = OpenPBSNode(node=pbs_node).build()
                response[key] = node
            return response
        return None

    @property
    def pid(self):
        if self.type in [
            'execjob_attach'
        ]:
            return self.event.pid
        return None

    @property
    def job_o(self):
        if self.type in [
            'modifyjob'
        ]:
            return self.event.job_o
        return None

    @property
    def src_queue(self):
        if self.type in [
            'movejob'
        ]:
            return self.event.src_queue
        return None

    @property
    def resv(self):
        if self.type in [
            'resvsub',
            'resv_end'
        ]:
            return self.event.resv
        return None

    def build(self) -> dict:

        pbs_event = {}
        pbs_event['timestamp'] = start_time
        if self.type.startswith('execjob_'):
            pbs_event['instance_id'] = get_instance_id()
            pbs_event['instance_type'] = get_instance_type()
        pbs_event['type'] = self.type
        pbs_event['hook_name'] = self.hook_name
        pbs_event['requestor'] = self.requestor
        pbs_event['requestor_host'] = self.requestor_host
        pbs_event['hook_type'] = self.hook_type
        # pbs_event['user'] = self.user
        pbs_event['vnode_list'] = self.vnode_list
        pbs_event['job_list'] = self.job_list
        pbs_event['argv'] = self.argv
        pbs_event['env'] = self.env
        pbs_event['prog'] = self.progname
        pbs_event['vnode_list_fail'] = self.vnode_list_fail
        pbs_event['pid'] = self.pid
        pbs_event['src_queue'] = self.src_queue
        pbs_event['resv'] = self.resv

        job = self.job
        if job:
            pbs_event['job'] = OpenPBSJob(event_type=self.type, job=job).build()
        job_o = self.job_o
        if job_o:
            pbs_event['job_o'] = OpenPBSJob(event_type=self.type, job=job_o).build()

        result = {}
        for key in pbs_event:
            if pbs_event[key] is None:
                continue
            if DEBUG_MODE:
                log_fine('pbs_event: %s = %s' % (key, type(pbs_event[key])))
            result[key] = pbs_event[key]
        return result


def get_env(key, env_type='str', default=None):
    if key in os.environ:
        value = os.environ[key]
        value = str(value).strip()
        if (len(value)) == 0:
            return default
        if env_type == 'str':
            return value
        if env_type == 'int':
            if not value.isnumeric():
                return default
            return int(value)
    return default


def get_socket():
    soca_daemon_unix_socket = get_env('IDEA_SCHEDULER_UNIX_SOCKET', default='/run/idea.sock')

    log_debug(f'IDEA_SCHEDULER_UNIX_SOCKET={soca_daemon_unix_socket}')

    if not os.path.exists(soca_daemon_unix_socket):
        raise SocaException('Error: idea-scheduler is not running. Contact administrator to resolve the problem.')

    socket_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    return socket_client, soca_daemon_unix_socket


def sanitize_error_message(message: str):
    # remove all double quotes - json parsing fails as pbs does not escape the quotes
    return message.replace('"', '')


def is_always_success_event(e):
    return e.type in ALWAYS_SUCCESS_EVENTS


def is_applicable(e) -> bool:
    if e.type not in SUPPORTED_EVENTS:
        return False

    if e.type == HOOK_EVENT_MODIFYJOB and e.requestor == 'root':
        return False

    return True


def invoke_soca_scheduler(e) -> dict:
    client = None
    address = None
    try:
        event = OpenPBSEvent(event=e)
        event_payload = event.build()

        client, address = get_socket()
        client.connect(address)
        client.settimeout(SOCKET_TIMEOUT_SECS)
        request = {
            'header': {
                'namespace': f'OpenPBSHook.{event.type}',
                'request_id': str(uuid.uuid4())
            },
            'payload': {
                'event': event_payload
            }
        }
        request_payload = json.dumps(request)
        request_payload_bytes = request_payload.encode(DEFAULT_ENCODING)
        if DEBUG_MODE:
            log_fine('Request: ' + request_payload)

        # create RAW HTTP request to avoid adding any external library dependencies to pbs python
        http_header = 'POST /scheduler/api/v1 HTTP/1.1\r\n'
        http_header += 'Content-Type: application/json\r\n'
        http_header += 'Accept-Charset: utf-8\r\n'
        http_header += 'Connection: close\r\n'
        http_header += f'Content-Length: {len(request_payload_bytes)}\r\n\r\n'

        http_message = http_header.encode(DEFAULT_ENCODING) + request_payload_bytes
        client.sendall(http_message)
        buffer = bytearray()
        while True:
            data = client.recv(SOCKET_RECV_BUFFER_SIZE)
            if len(data) == 0:
                break
            buffer += data

        http_response = buffer.decode(DEFAULT_ENCODING)
        lines = http_response.splitlines()
        headers = []
        content_available = False
        response_payload = None
        for line in lines:
            if not content_available:
                headers.append(line)
                if line in ('\r\n', '\n', ''):
                    content_available = True
                    continue
            else:
                response_payload = line

        if DEBUG_MODE:
            log_fine('Response: ' + response_payload)

        return json.loads(response_payload)

    except socket.timeout:
        raise SocaException(f'idea-scheduler is taking a long time ( more than {SOCKET_TIMEOUT_SECS} secs) to respond. '
                            f'Please try again later or contact your administrator to investigate the problem.')
    except (ConnectionRefusedError, ConnectionError, ConnectionResetError, ConnectionAbortedError) as exc:
        raise SocaException(f'Could not connect to idea-scheduler at {address}. '
                            f'Please contact administrator to investigate the problem. Err: {exc}')
    finally:
        if client:
            client.close()


e = pbs.event()

try:

    if not is_applicable(e):
        raise SystemExit

    event_type = e.type

    if event_type in PRE_EXECUTION_HOOKS:

        # if event is generated due to qsub, qalter etc, and scheduler is invoked via unix socket

        response = invoke_soca_scheduler(e)

        success = False
        if 'success' in response:
            success = response['success']

        if not success:
            raise SocaException(response['message'])

        if success:
            payload = response.get('payload', None)
            if payload is not None:
                payload = response['payload']
                accept = payload['accept']

                formatted_user_message = ''
                if 'formatted_user_message' in payload:
                    formatted_user_message = payload['formatted_user_message']

                if not accept:
                    raise SocaException(formatted_user_message)

                queue_name = payload.get('queue', None)
                if queue_name is not None and event_type == HOOK_EVENT_QUEUEJOB:
                    e.job.queue = pbs.server().queue(queue_name)

                project = payload.get('project', None)
                if project is not None:
                    e.job.project = project

                resources_updated = payload.get('resources_updated', None)
                if resources_updated is not None:
                    job = e.job
                    for name, value in resources_updated.items():
                        if name == 'select':
                            job.Resource_List['select'] = pbs.select(value)
                        else:
                            job.Resource_List[name] = value

                e.accept(formatted_user_message)

    elif event_type in EXECUTION_HOOKS:
        # these hooks are executed from compute node and job status events are posted to an SQS queue

        event = OpenPBSEvent(event=e)
        event_payload = event.build()
        message_id = str(uuid.uuid4())
        request = {
            'header': {
                'namespace': f'OpenPBSHook.{event.type}',
                'request_id': message_id
            },
            'payload': {
                'event': event_payload
            }
        }
        request_payload = json.dumps(request)
        message_body = base64.b64encode(request_payload.encode(DEFAULT_ENCODING)).decode(DEFAULT_ENCODING)
        queue_url = get_env('IDEA_JOB_STATUS_SQS_QUEUE_URL')
        aws_region = get_env('AWS_DEFAULT_REGION')
        send_message_command = str(f'AWS_DEFAULT_REGION={aws_region} /bin/aws sqs send-message '
                                   f'--queue-url {queue_url} '
                                   f'--message-body "{message_body}" ')
        os.system(send_message_command)

    pbs.logmsg(pbs.LOG_DEBUG, 'HookExecution TotalTime: %s ms' % (round(time.time() * 1000) - start_time))

except SystemExit:
    pass
except Exception as exc:
    error_message = None
    try:
        if isinstance(exc, SocaException):
            error_message = sanitize_error_message(exc.message)
        else:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            error_message = f'{pbs.event().hook_name} failed with {sys.exc_info()[:2]}, lineno: {exc_tb.tb_lineno}'
    finally:
        if error_message is None:
            error_message = 'Unknown Error'

        error_message = sanitize_error_message(error_message)
        pbs.logmsg(pbs.LOG_ERROR, error_message)

        if is_always_success_event(e):
            e.accept()
        else:
            e.reject(error_message)
