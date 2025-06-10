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

from ideadatamodel import constants, errorcodes, exceptions, SocaPaginator, SocaFilter
from ideadatamodel.scheduler import (
    ListJobsRequest,
    ListJobsResult,
    GetUserApplicationsRequest,
    DryRunOption,
    SubmitJobRequest,
    SubmitJobResult,
    GetInstanceTypeOptionsRequest,
    GetInstanceTypeOptionsResult,
    GetJobRequest,
    GetJobResult,
    DeleteJobRequest,
    DeleteJobResult,
)
from ideasdk.api import BaseAPI, ApiInvocationContext
from ideasdk.utils import Utils, GroupNameHelper
from ideascheduler.app.scheduler.job_param_builder import (
    JobParamsBuilderContext,
    InstanceTypesParamBuilder,
)

import ideascheduler
import os
import shutil


class SchedulerAPI(BaseAPI):
    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger('scheduler-api')
        self.group_name_helper = GroupNameHelper(context)

        self.SCOPE_WRITE = f'{self.context.module_id()}/write'
        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            'Scheduler.ListActiveJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_active_jobs,
            },
            'Scheduler.ListCompletedJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_completed_jobs,
            },
            'Scheduler.GetUserApplications': {
                'scope': self.SCOPE_READ,
                'method': self.get_user_applications,
            },
            'Scheduler.SubmitJob': {
                'scope': self.SCOPE_WRITE,
                'method': self.submit_job,
            },
            'Scheduler.DeleteJob': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_job,
            },
            'Scheduler.GetActiveJob': {
                'scope': self.SCOPE_READ,
                'method': self.get_active_job,
            },
            'Scheduler.GetCompletedJob': {
                'scope': self.SCOPE_READ,
                'method': self.get_completed_job,
            },
            'Scheduler.GetInstanceTypeOptions': {
                'scope': self.SCOPE_READ,
                'method': self.get_instance_type_options,
            },
        }

    def list_active_jobs(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListJobsRequest)
        page_size = payload.page_size
        page_start = payload.page_start
        entries = self.context.job_cache.list_jobs(
            owner=context.get_username(), _limit=page_size, _offset=page_start
        )
        total = self.context.job_cache.get_count(owner=context.get_username())

        context.success(
            ListJobsResult(
                paginator=SocaPaginator(
                    total=total, page_size=payload.page_size, start=payload.page_start
                ),
                listing=entries,
            )
        )

    def list_completed_jobs(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListJobsRequest)

        if self.context.document_store.is_enabled():
            # clean up filters and always add the current user to the filter
            filters = []
            for search_filter in payload.filters:
                if search_filter.key == 'owner':
                    continue
                filters.append(search_filter)

            owner_filter = SocaFilter(key='owner', value=context.get_username())
            filters.append(owner_filter)
            payload.filters = filters
            result = self.context.document_store.search_jobs(payload)
        else:
            payload = context.get_request_payload_as(ListJobsRequest)
            page_size = payload.page_size
            page_start = payload.page_start
            entries = self.context.job_cache.list_completed_jobs(
                owner=context.get_username(), _limit=page_size, _offset=page_start
            )
            total = self.context.job_cache.get_completed_jobs_count(
                owner=context.get_username()
            )
            result = ListJobsResult(
                paginator=SocaPaginator(
                    total=total, page_size=payload.page_size, start=payload.page_start
                ),
                listing=entries,
            )

        context.success(result)

    def get_user_applications(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserApplicationsRequest)
        request.username = context.get_username()
        result = self.context.applications.get_user_applications(request)
        return context.success(result)

    def submit_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(SubmitJobRequest)
        job_owner = request.job_owner
        context_user = context.get_username()

        if Utils.is_empty(job_owner):
            job_owner = context.get_username()

        if Utils.is_empty(context_user):
            raise exceptions.invalid_params('Empty context user')

        if context_user is not job_owner:
            raise exceptions.invalid_params(
                'Mismatched user information in job request'
            )

        job_script = request.job_script
        if Utils.is_empty(job_script):
            raise exceptions.invalid_params(
                'job_script is required in form of base64 encoded job script.'
            )

        job_script_interpreter = request.job_script_interpreter
        if Utils.is_empty(job_script_interpreter):
            raise exceptions.invalid_params('job_script_interpreter is required.')
        if job_script_interpreter not in ('pbs', 'bash'):
            raise exceptions.invalid_params(
                'job_script_interpreter must be one of [pbs, bash]'
            )

        dry_run = DryRunOption.resolve(request.dry_run)

        data_dir = self.context.config().get_string(
            'shared-storage.data.mount_dir', required=True
        )
        job_submission_dir = os.path.join(data_dir, 'home', job_owner, 'jobs')

        if Utils.is_symlink(job_submission_dir):
            raise exceptions.general_exception(
                f'a symbolic link exists at location: {job_submission_dir}. delete the symbolic link and try again.'
            )

        if not Utils.is_dir(job_submission_dir):
            os.makedirs(job_submission_dir)
            group_name = self.group_name_helper.get_user_group(job_owner)
            shutil.chown(job_submission_dir, user=job_owner, group=group_name)

        job_uid = Utils.short_uuid()
        # Try to extract job name from PBS script if present
        job_name = None
        script_content = Utils.base64_decode(job_script)
        for line in script_content.split('\n'):
            if line.startswith('#PBS -N '):
                job_name = line.replace('#PBS -N ', '').strip()
                break

        # Use job_name and uid without timestamp
        filename_base = f'{job_name}_{job_uid}' if job_name else job_uid

        if job_script_interpreter == 'pbs':
            job_submit_script = os.path.join(job_submission_dir, f'{filename_base}.que')
        else:
            job_submit_script = os.path.join(job_submission_dir, f'{filename_base}.sh')

        with open(job_submit_script, 'w') as f:
            f.write(Utils.base64_decode(job_script))
            f.write('\n')

        group_name = self.group_name_helper.get_user_group(job_owner)
        shutil.chown(job_submit_script, user=job_owner, group=group_name)

        if job_script_interpreter == 'pbs':
            job_submit_command = ['cd', job_submission_dir, '&&', 'qsub']
            if dry_run is not None:
                job_submit_command += ['-l', f'dry_run={dry_run}']
            if Utils.is_not_empty(request.project):
                job_submit_command += ['-P', f'"{request.project}"']

            job_submit_command += ['-l', f'job_uid={job_uid}', job_submit_script]
            job_submit_command_str = ' '.join(job_submit_command)
            command = ['su', job_owner, '-c', f'{job_submit_command_str}']

            self.logger.info(' '.join(command))
            result = self.context.shell.invoke(command)

            job_id = None
            if result.returncode == 0:
                job_id = str(result.stdout).split('.')[0]
            elif result.returncode != 0 and dry_run is None:
                self.logger.error(f'Failed to submit job: {result}')
                raise exceptions.soca_exception(
                    errorcodes.JOB_SUBMISSION_FAILED, f'Failed to submit job: {result}'
                )

            submission_result = self.context.job_submission_tracker.get(job_uid)
            if isinstance(submission_result, BaseException):
                raise submission_result

            if submission_result is None and result.returncode != 0:
                self.logger.error(f'Failed to submit job: {result}')
                raise exceptions.soca_exception(
                    errorcodes.JOB_SUBMISSION_FAILED, f'Failed to submit job: {result}'
                )

            if job_id is not None:
                submission_result.job.job_id = job_id

            # clean up - if dry run request or job was not accepted
            if Utils.get_as_bool(submission_result.accepted, False):
                os.remove(job_submit_script)

            context.success(submission_result)

        elif job_script_interpreter == 'bash':
            job_submit_command = [
                'cd',
                job_submission_dir,
                '&&',
                'bash',
                job_submit_script,
            ]

            job_submit_command_str = ' '.join(job_submit_command)
            command = ['su', job_owner, '-c', f'{job_submit_command_str}']

            self.logger.info(' '.join(command))
            result = self.context.shell.invoke(command)

            if result.returncode == 0:
                context.success(SubmitJobResult(accepted=True))
            else:
                raise exceptions.soca_exception(
                    errorcodes.JOB_SUBMISSION_FAILED, f'Failed to submit job: {result}'
                )

    def get_instance_type_options(self, context: ApiInvocationContext):
        """
        This API is used to get the instance type options during job submission.
        Based on ht support is enabled or disabled, threads_per_core is computed by InstanceTypesParamBuilder

        :param context:
        :return:
        """
        request = context.get_request_payload_as(GetInstanceTypeOptionsRequest)

        instance_types = request.instance_types
        queue_name = request.queue_name
        queue_profile_name = request.queue_profile_name
        enable_ht_support = request.enable_ht_support

        if Utils.are_empty(queue_name, queue_profile_name):
            raise exceptions.invalid_params(
                'One of [queue_name, queue_profile_name] is required.'
            )

        queue_profile = self.context.queue_profiles.get_queue_profile(
            queue_profile_name=queue_profile_name, queue_name=queue_name
        )

        if Utils.is_empty(instance_types):
            instance_types = queue_profile.default_job_params.instance_types
        if enable_ht_support is None:
            enable_ht_support = Utils.get_as_bool(
                queue_profile.default_job_params.enable_ht_support, False
            )

        param_builder_context = JobParamsBuilderContext(
            self.context, params={}, queue_profile=queue_profile
        )
        builder = InstanceTypesParamBuilder(
            param_builder_context, constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_type_options = builder.get_instance_type_options(
            instance_types, enable_ht_support
        )
        context.success(
            GetInstanceTypeOptionsResult(instance_types=instance_type_options)
        )

    def get_active_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetJobRequest)
        job_id = request.job_id
        if Utils.is_empty(job_id):
            raise exceptions.invalid_params('job_id is required')
        job = self.context.job_cache.get_job(job_id)
        if job is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.JOB_NOT_FOUND,
                message=f'Job not found for job id: {job_id}',
            )
        return context.success(GetJobResult(job=job))

    def get_completed_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetJobRequest)
        job_id = request.job_id
        if Utils.is_empty(job_id):
            raise exceptions.invalid_params('job_id is required')
        job = self.context.job_cache.get_completed_job(job_id)
        if job is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.JOB_NOT_FOUND,
                message=f'Job not found for job id: {job_id}',
            )
        return context.success(GetJobResult(job=job))

    def delete_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DeleteJobRequest)
        if Utils.is_empty(request.job_id):
            raise exceptions.invalid_params('job_id is required')

        username = context.get_username()

        job = self.context.scheduler.get_job(job_id=request.job_id)
        if job is None:
            raise exceptions.soca_exception(
                errorcodes.JOB_NOT_FOUND, f'Job not found for Job Id: {request.job_id}'
            )
        if job.owner != username:
            raise exceptions.unauthorized_access()

        self.context.scheduler.delete_job(job.job_id)

        context.success(DeleteJobResult())

    def invoke(self, context: ApiInvocationContext):
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace == 'Scheduler.ListActiveJobs':
            return self.list_active_jobs(context)
        elif namespace == 'Scheduler.ListCompletedJobs':
            return self.list_completed_jobs(context)
        elif namespace == 'Scheduler.GetUserApplications':
            return self.get_user_applications(context)
        elif namespace == 'Scheduler.SubmitJob':
            return self.submit_job(context)
        elif namespace == 'Scheduler.DeleteJob':
            return self.delete_job(context)
        elif namespace == 'Scheduler.GetActiveJob':
            return self.get_active_job(context)
        elif namespace == 'Scheduler.GetCompletedJob':
            return self.get_completed_job(context)
        elif namespace == 'Scheduler.GetInstanceTypeOptions':
            return self.get_instance_type_options(context)
