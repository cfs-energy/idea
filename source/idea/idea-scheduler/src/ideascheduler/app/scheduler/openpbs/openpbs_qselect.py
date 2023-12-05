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

import ideascheduler
from ideadatamodel.scheduler import SocaJobState
from ideasdk.shell import ShellInvocationResult, ShellInvoker
from ideasdk.utils import Utils

from typing import Optional, List
import arrow
import logging


class OpenPBSQSelect:

    def __init__(self, context: ideascheduler.AppContext,
                 shell: ShellInvoker = None,
                 log_tag: str = None,
                 logger: logging.Logger = None,
                 **kwargs):
        self._context = context
        if shell is None:
            shell = ShellInvoker(logger=logger)
        self._shell = shell
        self._kwargs = kwargs
        self._log_tag = log_tag
        self._logger = logger

        self._shell_result: Optional[ShellInvocationResult] = None

    @property
    def qselect_bin(self):
        return '/opt/pbs/bin/qselect'

    @property
    def queue(self) -> Optional[str]:
        return self._kwargs.get('queue', None)

    def is_queue_query(self) -> bool:
        queue = self.queue
        return Utils.is_not_empty(queue)

    @property
    def owners(self) -> Optional[List[str]]:
        return self._kwargs.get('owners', None)

    def is_owner_query(self) -> bool:
        owners = self.owners
        return owners is not None and len(owners) > 0

    @property
    def job_state(self) -> Optional[List[SocaJobState]]:
        return self._kwargs.get('job_state', None)

    def is_job_state_query(self) -> bool:
        job_state = self.job_state
        return job_state is not None and len(job_state) > 0

    @property
    def queued_after(self) -> Optional[arrow.Arrow]:
        return self._kwargs.get('queued_after', None)

    def get_queue_select_state_filter(self) -> str:
        result = ''
        for state in self.job_state:
            result = f'{result}{state.value[0].upper()}'
        return result

    @property
    def job_group(self) -> Optional[str]:
        return self._kwargs.get('job_group', None)

    def is_job_group_query(self) -> bool:
        return Utils.is_not_empty(self.job_group)

    @property
    def stack_id(self) -> Optional[str]:
        return self._kwargs.get('stack_id', None)

    def is_stack_id_query(self) -> bool:
        return Utils.is_not_empty(self.stack_id)

    @property
    def max_jobs(self) -> int:
        return Utils.get_value_as_int('max_jobs', self._kwargs, -1)

    def list_jobs_ids(self) -> List[str]:
        cmd = [self.qselect_bin]

        if self.is_owner_query():
            cmd += ['-u', ','.join(self.owners)]
        if self.is_queue_query():
            cmd += ['-q', self.queue]
        if self.is_job_state_query():
            cmd += ['-s', self.get_queue_select_state_filter()]
            if self.queued_after is not None and self.job_state == SocaJobState.QUEUED:
                # noinspection StrFormat
                date_format = self.queued_after.format('MMDDHHmm.ss')
                cmd += [f'-tq.gt.{date_format}']
            if self.job_state == SocaJobState.FINISHED:
                cmd += ['-x']
        if self.is_job_group_query():
            cmd += ['-l', f'job_group.eq.{self.job_group}']
        if self.is_stack_id_query():
            cmd += ['-l', f'stack_id.eq.{self.stack_id}']

        result = self._shell.invoke(cmd, skip_error_logging=True)

        job_ids = []
        if result.returncode == 0:
            lines = str(result.stdout).splitlines()

            for line in lines:
                job_ids.append(line.split('.')[0])

        if self._logger is not None:
            log_msg = f'{" ".join(cmd)} -> num jobs: {len(job_ids)}'
            if Utils.is_not_empty(self._log_tag):
                log_msg = f'({self._log_tag}) {log_msg}'
            self._logger.info(log_msg)

        if self.max_jobs > 0:
            return job_ids[0:self.max_jobs]
        else:
            return job_ids

    def get_count(self) -> int:
        return len(self.list_jobs_ids())
