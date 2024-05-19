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
from pydantic import Field

import ideascheduler
from ideadatamodel import (
    constants,
    SocaBaseModel,
    SocaMemory,
    SocaJob, SocaJobState, SocaJobExecutionHost, SocaJobExecution, SocaJobExecutionRun,
    SocaJobExecutionResourcesUsed, SocaJobNotifications,
    HpcQueueProfile
)
from ideasdk.utils import Utils

from ideascheduler.app.scheduler import SocaJobBuilder

from typing import Optional, Union, Dict, List
import arrow
from datetime import datetime

DATE_FORMAT = 'ddd MMM D HH:mm:ss YYYY'
DEFAULT_QUEUE_NAME = 'normal'


class OpenPBSJob(SocaBaseModel):
    dry_run: Optional[str] = Field(default=None)
    id: Optional[str] = Field(default=None)
    accounting_id: Optional[str] = Field(default=None)
    Account_Name: Optional[str] = Field(default=None)
    accrue_type: Optional[str] = Field(default=None)
    alt_id: Optional[str] = Field(default=None)
    argument_list: Optional[str] = Field(default=None)
    array: Optional[str] = Field(default=None)
    array_id: Optional[str] = Field(default=None)
    array_index: Optional[str] = Field(default=None)
    array_indices_remaining: Optional[str] = Field(default=None)
    array_indices_submitted: Optional[str] = Field(default=None)
    array_state_count: Optional[str] = Field(default=None)
    block: Optional[str] = Field(default=None)
    Checkpoint: Optional[str] = Field(default=None)
    comment: Optional[str] = Field(default=None)
    create_resv_from_job: Optional[str] = Field(default=None)
    ctime: Optional[str] = Field(default=None)
    depend: Optional[str] = Field(default=None)
    egroup: Optional[str] = Field(default=None)
    eligible_time: Optional[str] = Field(default=None)
    Error_Path: Optional[str] = Field(default=None)
    estimated: Optional[str] = Field(default=None)
    etime: Optional[str] = Field(default=None)
    euser: Optional[str] = Field(default=None)
    Executable: Optional[str] = Field(default=None)
    Execution_Time: Optional[str] = Field(default=None)
    exec_host: Optional[str] = Field(default=None)
    exec_vnode: Optional[str] = Field(default=None)
    Exit_status: Optional[int] = Field(default=None, strict=False)
    group_list: Optional[dict] = Field(default=None)
    hashname: Optional[str] = Field(default=None)
    Hold_Types: Optional[str] = Field(default=None)
    interactive: Optional[str] = Field(default=None)
    jobdir: Optional[str] = Field(default=None)
    Job_Name: Optional[Union[int, str, float]] = Field(default=None)
    Job_Owner: Optional[str] = Field(default=None)
    job_state: Optional[str] = Field(default=None)
    Join_Path: Optional[str] = Field(default=None)
    Keep_Files: Optional[str] = Field(default=None)
    Mail_Points: Optional[str] = Field(default=None)
    Mail_Users: Optional[str] = Field(default=None)
    mtime: Optional[str] = Field(default=None)
    no_stdio_sockets: Optional[str] = Field(default=None)
    Output_Path: Optional[str] = Field(default=None)
    Priority: Optional[int] = Field(default=None, strict=False)
    project: Optional[str] = Field(default=None)
    pset: Optional[str] = Field(default=None)
    qtime: Optional[str] = Field(default=None)
    queue: Optional[str] = Field(default=None)
    queue_rank: Optional[int] = Field(default=None, strict=False)
    queue_type: Optional[str] = Field(default=None)
    release_nodes_on_stageout: Optional[str] = Field(default=None)
    Rerunable: Optional[str] = Field(default=None)
    resources_released: Optional[str] = Field(default=None)
    resources_released_list: Optional[dict] = Field(default=None)
    resources_used: Optional[Union[str, dict]] = Field(default=None)
    Resource_List: Optional[dict] = Field(default=None)
    run_count: Optional[int] = Field(default=None, strict=False)
    run_version: Optional[int] = Field(default=None, strict=False)
    sandbox: Optional[str] = Field(default=None)
    schedselect: Optional[str] = Field(default=None)
    sched_hint: Optional[str] = Field(default=None)
    server: Optional[str] = Field(default=None)
    session_id: Optional[int] = Field(default=None, strict=False)
    Shell_Path_List: Optional[str] = Field(default=None)
    stagein: Optional[str] = Field(default=None)
    stageout: Optional[str] = Field(default=None)
    Stageout_status: Optional[int] = Field(default=None, strict=False)
    stime: Optional[str] = Field(default=None)
    Submit_arguments: Optional[str] = Field(default=None)
    substate: Optional[int] = Field(default=None, strict=False)
    sw_index: Optional[str] = Field(default=None)
    umask: Optional[str] = Field(default=None)
    User_List: Optional[dict] = Field(default=None)
    Variable_List: Optional[dict] = Field(default=None)

    @staticmethod
    def to_soca_job_state(state: str) -> Optional[SocaJobState]:
        _token = state.lower()
        if _token == 't':
            # Job is being moved to new location
            return SocaJobState.TRANSITION
        elif _token == 'q':
            # Job is queued
            return SocaJobState.QUEUED
        elif _token == 'h':
            # Job is held
            return SocaJobState.HELD
        elif _token == 'w':
            # Job is waiting for its submitter-assigned start time to be reached
            return SocaJobState.WAITING
        elif _token == 'r':
            # job is running
            return SocaJobState.RUNNING
        elif _token == 'e':
            # job is exiting after having run
            return SocaJobState.EXIT
        elif _token == 'x':
            # Subjob has completed execution or has been deleted
            return SocaJobState.SUBJOB_EXPIRED
        elif _token == 'b':
            # Array job has at least one subjob running
            return SocaJobState.SUBJOB_BEGUN
        elif _token == 'm':
            # Job was moved to another server
            return SocaJobState.MOVED
        elif _token == 'f':
            # Job is finished
            return SocaJobState.FINISHED
        elif _token == 's':
            # Job is suspended
            return SocaJobState.SUSPENDED

        return None

    @property
    def soca_job_state(self) -> Optional[SocaJobState]:
        if self.job_state is None:
            return None
        return self.to_soca_job_state(state=self.job_state)

    @staticmethod
    def _parse_select(select: str) -> Dict[str, str]:
        tokens = select.split(':')
        params = {}
        for token in tokens:
            kv = token.split('=')
            if len(kv) == 1:
                key = kv[0]
                if Utils.is_int(key):
                    params['nodes'] = key
                    continue
            elif len(kv) == 2:
                key = kv[0]
                value = kv[1]
                params[key] = value
        return params

    @staticmethod
    def _build_soca_job_params(soca_params: Dict[str, str], pbs_params: Dict[str, str]):

        if Utils.is_empty(pbs_params):
            return

        for key in pbs_params:

            if key == 'select':
                continue

            if key == 'ncpus':
                soca_params[constants.JOB_PARAM_CPUS] = pbs_params['ncpus']
                continue
            if key == 'nodect':
                soca_params[constants.JOB_PARAM_NODES] = pbs_params['nodect']
                continue
            # todo - verify difference between nodes and nodect
            if key == 'nodes' and constants.JOB_PARAM_NODES not in soca_params:
                soca_params[constants.JOB_PARAM_NODES] = pbs_params['nodes']
                continue
            # todo - verify gpu format
            if key == 'ngpus':
                soca_params[constants.JOB_PARAM_GPUS] = pbs_params['ngpus']
                continue
            if key == 'mpiprocs':
                soca_params[constants.JOB_PARAM_MPIPROCS] = pbs_params['mpiprocs']
                continue
            if key == 'mem':
                soca_params[constants.JOB_PARAM_MEMORY] = pbs_params['mem']
                continue
            if key == 'compute_node':
                soca_params[constants.JOB_PARAM_COMPUTE_STACK] = pbs_params['compute_node']
                continue
            if key == 'stack_id':
                soca_params[constants.JOB_PARAM_STACK_ID] = pbs_params['stack_id']
                continue

            # todo: placement

            soca_params[key] = pbs_params[key]

    def get_soca_job_params(self) -> Dict[str, str]:
        job_params = {}
        resources = self.Resource_List

        if resources is None:
            return job_params

        if 'select' in resources:
            select_params = self._parse_select(select=resources['select'])
            self._build_soca_job_params(soca_params=job_params, pbs_params=select_params)

        self._build_soca_job_params(soca_params=job_params, pbs_params=resources)

        return job_params

    def is_dry_run(self) -> bool:
        resources = self.Resource_List
        if resources is None:
            return False
        return 'dry_run' in resources

    def get_job_group(self) -> Optional[str]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'job_group' in resources:
            return resources['job_group']
        return None

    def get_job_uid(self) -> Optional[str]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'job_uid' in resources:
            return resources['job_uid']
        return None

    def get_cluster_name(self) -> Optional[str]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'cluster_name' in resources:
            return resources['cluster_name']
        return None

    def get_cluster_version(self) -> Optional[str]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'cluster_version' in resources:
            return resources['cluster_version']
        return None

    def get_error_message(self) -> Optional[str]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'error_message' in resources:
            return resources['error_message']
        return None

    def get_notifications(self) -> Optional[SocaJobNotifications]:
        flags = Utils.get_as_string(self.Mail_Points)
        if flags is None:
            return None
        started = None
        if 'b' in flags:
            started = True

        completed = None
        if 'e' in flags:
            completed = True

        if started or completed:
            return SocaJobNotifications(
                started=started,
                completed=completed
            )
        return None

    def provisioning_time(self) -> Optional[datetime]:
        resources = self.Resource_List
        if resources is None:
            return None
        if 'provisioning_time' not in resources:
            return None
        return arrow.get(resources['provisioning_time']).datetime

    def capacity_added(self) -> Optional[bool]:
        resources = self.Resource_List
        if resources is None:
            return None
        return Utils.get_value_as_bool('capacity_added', resources, True)

    def get_soca_execution_hosts(self, event: 'OpenPBSEvent') -> Optional[List[SocaJobExecutionHost]]:

        if event is None:
            return None

        execution_hosts = None
        if self.exec_host:
            # exec_host=ip-10-0-79-226/0+ip-10-0-114-162/0
            tokens = self.exec_host.split('+')
            execution_hosts = []
            for token in tokens:
                execution_hosts.append(token.split('/')[0])
        elif self.exec_vnode:
            # exec_vnode=(ip-10-0-79-226:ncpus=1)+(ip-10-0-114-162:ncpus=1)
            tokens = self.exec_vnode.split('+')
            execution_hosts = []
            for token in tokens:
                execution_hosts.append(token.split(':')[0][1:])

        if execution_hosts is None:
            return None

        soca_execution_hosts = []
        if event.type == 'runjob':
            for host in execution_hosts:
                soca_execution_hosts.append(SocaJobExecutionHost(
                    host=host
                ))
        elif event.type.startswith('execjob_'):
            host = event.requestor_host.split('.')[0]
            execution_host = SocaJobExecutionHost(
                host=host,
                instance_id=event.instance_id,
                instance_type=event.instance_type,
                execution=SocaJobExecution(runs=[])
            )
            soca_execution_hosts.append(execution_host)

            # todo - array jobs
            run_id = '1'

            if event.type == 'execjob_begin':
                run = SocaJobExecutionRun(
                    run_id=run_id,
                    start=arrow.get(str(event.timestamp), 'x').datetime
                )
                execution_host.execution.runs.append(run)
            elif event.type == 'execjob_end':

                exit_code = Utils.get_as_int(self.Exit_status)

                if exit_code is not None and exit_code == 0:
                    status = 'success'
                else:
                    status = 'failed'

                cpu_time_secs = None
                memory = None
                virtual_memory = None
                cpus = None
                gpus = None
                cpu_percent = None

                resources_used = self.resources_used
                if resources_used is not None and isinstance(resources_used, str):
                    tokens = resources_used.split(',')
                    for token in tokens:
                        kv = token.split('=')
                        if len(kv) != 2:
                            continue
                        key = kv[0]
                        value = kv[1]
                        if key == 'cput':
                            cpu_time_secs = Utils.walltime_to_seconds(value)
                            continue
                        if key == 'mem':
                            memory = SocaMemory.resolve(value=value)
                            continue
                        if key == 'vmem':
                            virtual_memory = SocaMemory.resolve(value=value)
                            continue
                        if key == 'ncpus':
                            cpus = Utils.get_as_int(value)
                            continue
                        if key == 'cpupercent':
                            cpu_percent = Utils.get_as_int(value)
                            continue

                run = SocaJobExecutionRun(
                    run_id=run_id,
                    end=arrow.get(str(event.timestamp), 'x').datetime,
                    exit_code=exit_code,
                    status=status,
                    resources_used=SocaJobExecutionResourcesUsed(
                        cpu_time_secs=cpu_time_secs,
                        memory=memory,
                        virtual_memory=virtual_memory,
                        cpus=cpus,
                        gpus=gpus,
                        cpu_percent=cpu_percent
                    )
                )
                execution_host.execution.runs.append(run)
        return soca_execution_hosts

    def get_select(self) -> Optional[str]:

        if not Utils.is_empty(self.schedselect):
            select = self.schedselect
        else:
            select = Utils.get_value_as_string('select', self.Resource_List)

        if select is None:
            return None

        if 'compute_node=' in select:
            return select

        return select + ':compute_node=tbd'

    @staticmethod
    def parse_pbs_datetime(value: Optional[Union[int, str]]) -> Optional[datetime]:
        if value is None:
            return None

        if Utils.is_int(value):
            return arrow.get(value, 'X').datetime

        value = Utils.get_as_string(value)
        if value is None:
            return None

        # replace double spaces with single space (ugh! C printf)
        # "Mon Nov  1 14:30:40 2021"
        # "Wed Oct 20 01:41:21 2021"
        value = value.replace('  ', ' ')
        return arrow.get(value, DATE_FORMAT).datetime

    def get_resources_used_wall_time(self) -> Optional[str]:
        resources_used = self.resources_used
        if resources_used is None:
            return None
        return Utils.get_value_as_string('walltime', resources_used)

    def as_soca_job(self, context: ideascheduler.AppContext,
                    event: Optional['OpenPBSEvent'] = None,
                    queue_profile: Optional[HpcQueueProfile] = None,
                    job_builder: Optional[SocaJobBuilder] = None) -> Optional[SocaJob]:

        # "589.ip-10-0-0-9"
        job_id = None
        if self.id is not None:
            job_id = self.id.split('.')[0]

        # assigned during job submission by SOCA
        job_uid = self.get_job_uid()

        # "project":"sample_project"
        project = self.project

        # "Job_Name":"sample_job"
        name = self.Job_Name

        # "queue":"normal"
        queue = self.queue

        owner = self.Job_Owner
        if owner is not None:
            owner = owner.split('@')[0]
        if owner is None:
            owner = self.euser

        # "job_state":"Q"
        state = self.soca_job_state

        # exit status
        exit_status = self.Exit_status

        params = self.get_soca_job_params()
        execution_hosts = self.get_soca_execution_hosts(event=event)

        # "qtime":"Wed Oct 20 01:41:21 2021"
        # "qtime": "1635187450" from execution hosts
        queue_time = self.parse_pbs_datetime(self.qtime)

        # "stime":"Wed Oct 20 01:41:21 2021"
        # "stime": "1635187450" from execution hosts
        start_time = self.parse_pbs_datetime(self.stime)

        # end time computation using wall time from resources used.
        end_time = None
        if state == SocaJobState.FINISHED:
            resources_used_walltime = self.get_resources_used_wall_time()
            if Utils.is_not_empty(resources_used_walltime):
                total_seconds = Utils.walltime_to_seconds(resources_used_walltime)
                end_time = arrow.get(start_time).shift(seconds=total_seconds).datetime

        if job_builder is None:
            job_builder = SocaJobBuilder(
                context=context,
                params=params,
                queue_profile=queue_profile
            )

        job_params, provisioning_options = job_builder.build()

        # 2:ncpus=1:compute_node=tbd
        select = self.get_select()
        # put the select expression as is for later use during provisioning
        job_params.custom_params = {
            'select': select
        }

        provisioning_time = self.provisioning_time()

        provisioned = False
        if select is not None:
            provisioned = 'compute_node=tbd' not in select

        job_group = self.get_job_group()

        comment = self.get_error_message()
        if comment is None:
            comment = self.comment

        notifications = self.get_notifications()

        cluster_name = Utils.get_as_string(self.get_cluster_name(), context.cluster_name())

        # capacity_added
        capacity_added = self.capacity_added()

        queue_type = None
        scaling_mode = None

        if queue_profile is None and job_builder is not None:
            queue_profile = job_builder

        if queue_profile is not None:
            queue_type = queue_profile.name
            scaling_mode = queue_profile.scaling_mode

        return SocaJob(
            cluster_name=cluster_name,
            job_id=job_id,
            job_uid=job_uid,
            job_group=job_group,
            project=project,
            name=name,
            queue=queue,
            queue_type=queue_type,
            scaling_mode=scaling_mode,
            owner=owner,
            state=state,
            exit_status=exit_status,
            provisioned=provisioned,
            queue_time=queue_time,
            start_time=start_time,
            end_time=end_time,
            provisioning_time=provisioning_time,
            capacity_added=capacity_added,
            params=job_params,
            provisioning_options=provisioning_options,
            execution_hosts=execution_hosts,
            comment=comment,
            notifications=notifications
        )


class OpenPBSEvent(SocaBaseModel):
    timestamp: Optional[int] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    type: Optional[str] = Field(default=None)
    hook_name: Optional[str] = Field(default=None)
    requestor: Optional[str] = Field(default=None)
    requestor_host: Optional[str] = Field(default=None)
    hook_type: Optional[str] = Field(default=None)
    user: Optional[str] = Field(default=None)
    vnode_list: Optional[dict] = Field(default=None)
    job_list: Optional[dict] = Field(default=None)
    argv: Optional[str] = Field(default=None)
    env: Optional[str] = Field(default=None)
    prog: Optional[str] = Field(default=None)
    vnode_list_fail: Optional[dict] = Field(default=None)
    pid: Optional[str] = Field(default=None)
    src_queue: Optional[str] = Field(default=None)
    resv: Optional[str] = Field(default=None)
    job: Optional[OpenPBSJob] = Field(default=None)
    job_o: Optional[OpenPBSJob] = Field(default=None)

    def __str__(self):
        # all required parameters
        return f'scheduler: openpbs, name: {self.hook_name}, type: {self.type}, ' \
               f'requestor: {self.requestor}, requestor_host: {self.requestor_host}'

    def get_queue(self) -> str:
        pass

    def as_soca_job(self, context: ideascheduler.AppContext,
                    queue_profile: HpcQueueProfile = None,
                    job_builder: Optional[SocaJobBuilder] = None) -> SocaJob:
        job = self.job
        if job is None:
            job = self.job_o

        # when job is queued, job_id is not available
        if job.id is None:
            job.id = 'TBD'

        if job.Job_Owner is None:
            job.Job_Owner = self.requestor

        return job.as_soca_job(context=context,
                               event=self,
                               queue_profile=queue_profile,
                               job_builder=job_builder)
