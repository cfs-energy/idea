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
from ideadatamodel import exceptions, errorcodes
from ideadatamodel.scheduler import SocaJob, SocaJobState
from ideasdk.shell import ShellInvocationResult, ShellInvoker
from ideasdk.utils import Utils

from ideascheduler.app.scheduler.openpbs import OpenPBSConverter, OpenPBSJob
from ideascheduler.app.scheduler.openpbs import openpbs_constants
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect

from typing import Optional, List, Generator
import logging
import orjson


class OpenPBSQStat:
    """
    Wrapper class for qstat shell invocations
    """

    def __init__(
        self,
        context: ideascheduler.AppContext,
        logger: logging.Logger,
        shell: ShellInvoker,
        converter: OpenPBSConverter,
        **kwargs,
    ):
        self._context = context
        self._logger = logger
        self._shell = shell
        self._converter = converter
        self._kwargs = kwargs

        self._shell_result: Optional[ShellInvocationResult] = None

    @property
    def qstat_bin(self):
        return '/opt/pbs/bin/qstat'

    @property
    def queue(self) -> Optional[str]:
        return self._kwargs.get('queue', None)

    @property
    def job_ids(self) -> Optional[List[str]]:
        return self._kwargs.get('job_ids', None)

    @property
    def owners(self) -> Optional[List[str]]:
        return self._kwargs.get('owners', None)

    @property
    def job_state(self) -> Optional[SocaJobState]:
        return self._kwargs.get('job_state', None)

    @staticmethod
    def _compare_job_state(pbs_job_state: str, job_state: SocaJobState) -> bool:
        if Utils.is_empty(pbs_job_state):
            return False
        other_state = OpenPBSJob.to_soca_job_state(state=pbs_job_state)
        return other_state == job_state

    def _handle_owner_query_result(self) -> List[SocaJob]:
        """
        owner query does not support json format. parse the below output to extract job ids
        apply applicable filters and return the result of query by job ids.

        eg:

        $ qstat -u kulkary normal

        ip-10-0-0-9:
                                                                    Req'd  Req'd   Elap
        Job ID          Username Queue    Jobname    SessID NDS TSK Memory Time  S Time
        --------------- -------- -------- ---------- ------ --- --- ------ ----- - -----
        625.ip-10-0-0-9 kulkary  normal   single_job    --    2   2    --    --  Q   --
        846.ip-10-0-0-9 kulkary  job-sha* multiple_*    --    1   1    --    --  Q   --
        """
        owners = self.owners
        queue = self.queue
        job_state = self.job_state
        job_ids = []
        lines = str(self._shell_result.stdout).splitlines()
        for line in lines:
            tokens = line.split()
            if len(tokens) != 11:
                continue
            job_owner = tokens[1]
            if job_owner.endswith('*'):
                job_owner = job_owner[:-1]
                found = False
                for owner in owners:
                    if owner.startswith(job_owner):
                        found = True
                        break
                if not found:
                    continue
            else:
                if job_owner not in owners:
                    continue
            if queue is not None:
                job_queue = tokens[2]
                if job_queue.endswith('*'):
                    job_queue = job_queue[:-1]
                    if not queue.startswith(job_queue):
                        continue
                elif job_queue != queue:
                    continue
            if job_state is not None:
                pbs_state = tokens[9]
                if not self._compare_job_state(
                    pbs_job_state=pbs_state, job_state=job_state
                ):
                    continue
            job_id = tokens[0].split('.')[0]
            job_ids.append(job_id)
        if len(job_ids) == 0:
            return []

        # switch query params and call list jobs again.
        del self._kwargs['owners']
        self._kwargs['job_ids'] = job_ids

        return self._invoke_qstat()

    def _handle_qstat_result(self, qstat_result: str = None) -> List[SocaJob]:
        try:
            response = []

            if qstat_result is None:
                qstat_result = self._shell_result.stdout

            if Utils.is_empty(qstat_result):
                return []

            # single pass implementation to strip all Variable_List content since env variables are not used in any soca provisioning process.
            # env variables are skipped for 1\ pbs sometimes returns invalid json content in these variables, 2\memory usage optimization.
            # if any Env variables are required to be included as part of SocaJob, they need to be handled on a case-by-case basis.
            content = []
            variable_list = False
            lines = qstat_result.splitlines()
            for line in lines:
                current_line = line.strip()
                if current_line == '"Variable_List":{':
                    variable_list = True
                if variable_list:
                    if current_line == '},':
                        variable_list = False
                        continue
                if variable_list:
                    continue
                content.append(line)

            json_response = Utils.from_json(''.join(content))

            jobs = Utils.get_value_as_dict('Jobs', json_response)
            if jobs is None or len(jobs) == 0:
                return []

            for job_id, job in jobs.items():
                pbs_job = OpenPBSJob(id=job_id, **job)

                queue_profile = self._context.queue_profiles.get_queue_profile(
                    queue_name=pbs_job.queue
                )

                if queue_profile is None:
                    continue

                soca_job = pbs_job.as_soca_job(
                    context=self._context, queue_profile=queue_profile
                )

                if soca_job is None:
                    continue
                response.append(soca_job)

            return response
        except orjson.JSONDecodeError as e:
            self._logger.error(
                f'failed to parse json data during pbs qstat: {qstat_result}'
            )
            raise e

    def _handle_job_finished_error(self) -> List[SocaJob]:
        """
        Scenario happens when JobExecution completes, and JobMonitor tries to query using job_id
        But the job is moved to finished stage and needs additional args for invocation.

        This is a race condition scenario and depends on when qstat is called after the job finishes.
        80-90% of times, pbs returns the Job with status as E. Ideally, OpenPBS should provide a hook when
        JobStatus changes to F.

        Recency is also into play as OpenPBS does not return finished jobs to be queried using qstat after more than
        X hours.

        $ /opt/pbs/bin/qstat -f -F json 973 -> returncode: 35
        qstat: 973.ip-10-0-0-9 Job has finished, use -x or -H to obtain historical job information

        $ /opt/pbs/bin/qstat -f -F json 1276 >> returncode: 35
        qstat: 1276.ip-10-0-0-9 Job has finished, use -x or -H to obtain historical job information
        """
        raise exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_JOB_FINISHED,
            message=f'{self._shell_result}',
        )

    def _handle_unknown_job_id_error(self) -> List[SocaJob]:
        """
        one or more of job_ids not found
        open pbs still returns job information for other job_ids that were found after error messages

        e.g:

        $ qstat -f -F json 600 601 597
        qstat: Unknown Job Id 600.ip-10-0-0-9
        qstat: Unknown Job Id 601.ip-10-0-0-9
        {
            "timestamp":1634779109,
              ..
        """
        output = str(self._shell_result.stderr)

        lines = output.splitlines()
        applicable_lines = []
        for line in lines:
            if line.lower().startswith('qstat: unknown job'):
                continue
            applicable_lines.append(line)

        qstat_result = ''.join(applicable_lines)

        return self._handle_qstat_result(qstat_result=qstat_result)

    def _invoke_qstat(self) -> List[SocaJob]:
        cmd = [self.qstat_bin]

        owners = self.owners
        is_owner_query = owners is not None and len(owners) > 0
        job_state = self.job_state

        if is_owner_query:
            cmd += ['-u', ','.join(self.owners)]
        else:
            job_ids = self.job_ids
            queue = self.queue
            if job_ids is not None and len(job_ids) > 0:
                if job_state is not None and job_state == SocaJobState.FINISHED:
                    cmd += ['-E', '-x', '-f', '-F', 'json']
                else:
                    cmd += ['-E', '-f', '-F', 'json']
                cmd += job_ids
            elif queue is not None:
                cmd += ['-E', '-f', '-F', 'json']
                cmd.append(queue)

        result = self._shell.invoke(cmd, skip_error_logging=True)
        self._shell_result = result

        if result.returncode == 0:
            if is_owner_query:
                return self._handle_owner_query_result()
            else:
                return self._handle_qstat_result()

        if result.returncode == openpbs_constants.QSTAT_ERROR_CODE_JOB_FINISHED:
            return self._handle_job_finished_error()
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_UNKNOWN_JOB_ID:
            return self._handle_unknown_job_id_error()
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_ABORTED:
            self._logger.warning(f'{result}')
            return []
        else:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )

    def list_jobs(self) -> List[SocaJob]:
        job_iterator = self.job_iterator()
        results = []
        for job in job_iterator:
            results.append(job)
        return results

    def job_iterator(self) -> Generator[SocaJob, None, None]:
        """
        return a generator instead of holding all jobs in memory.
        this is required to support listing of 1000s of jobs
        """
        cmd = [self.qstat_bin]
        job_state = self.job_state
        if job_state is not None and job_state == SocaJobState.FINISHED:
            cmd += ['-E', '-x', '-f', '-F', 'json']
        else:
            cmd += ['-E', '-f', '-F', 'json']

        job_ids = self.job_ids
        if Utils.is_empty(job_ids):
            job_ids = OpenPBSQSelect(
                context=self._context,
                logger=self._logger,
                shell=self._shell,
                **self._kwargs,
            ).list_jobs_ids()

        if Utils.is_empty(job_ids):
            return

        page_size = 100
        start = 0
        remaining = len(job_ids)
        result = None
        while remaining > 0:
            job_ids_to_get = job_ids[start : start + page_size]

            get_jobs_cmd = cmd + job_ids_to_get
            result = self._shell.invoke(get_jobs_cmd, skip_error_logging=True)
            self._shell_result = result
            self._logger.info(f'{result}')

            if result.returncode == 0:
                jobs = self._handle_qstat_result()
                for job in jobs:
                    yield job
                start += page_size

            remaining -= page_size

        if result.returncode == 0:
            return

        jobs = []
        if result.returncode == openpbs_constants.QSTAT_ERROR_CODE_JOB_FINISHED:
            jobs = self._handle_job_finished_error()
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_UNKNOWN_JOB_ID:
            jobs = self._handle_unknown_job_id_error()
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_ABORTED:
            self._logger.warning(f'{result}')
        else:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )
        for job in jobs:
            yield job

    def get_job(self) -> Optional[SocaJob]:
        result = self._invoke_qstat()
        if len(result) > 0:
            return result[0]
