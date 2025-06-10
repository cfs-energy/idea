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

from ideadatamodel import exceptions, errorcodes, SocaPaginator
from ideadatamodel.scheduler import (
    ListNodesRequest,
    ListJobsRequest,
    ListJobsResult,
    CreateQueueProfileRequest,
    CreateQueueProfileResult,
    GetQueueProfileRequest,
    GetQueueProfileResult,
    UpdateQueueProfileRequest,
    UpdateQueueProfileResult,
    EnableQueueProfileRequest,
    EnableQueueProfileResult,
    DisableQueueProfileRequest,
    DisableQueueProfileResult,
    DeleteQueueProfileRequest,
    DeleteQueueProfileResult,
    ListQueueProfilesRequest,
    ListQueueProfilesResult,
    CreateHpcApplicationRequest,
    UpdateHpcApplicationRequest,
    GetHpcApplicationRequest,
    DeleteHpcApplicationRequest,
    ListHpcApplicationsRequest,
    GetUserApplicationsRequest,
    ProvisionAlwaysOnNodesRequest,
    ProvisionAlwaysOnNodesResult,
    CreateHpcLicenseResourceRequest,
    GetHpcLicenseResourceRequest,
    UpdateHpcLicenseResourceRequest,
    DeleteHpcLicenseResourceRequest,
    ListHpcLicenseResourcesRequest,
    CheckHpcLicenseResourceAvailabilityRequest,
    DeleteJobRequest,
    DeleteJobResult,
    HpcQueueProfile,
    SocaJob,
    SocaScalingMode,
)
from ideasdk.api import BaseAPI, ApiInvocationContext
from ideasdk.utils import Utils

import ideascheduler

from ideascheduler.app.provisioning.job_provisioning_queue.job_provisioning_queue import (
    JobProvisioningQueue,
)
from ideascheduler.app.scheduler.job_param_builder import SocaJobBuilder
from ideascheduler.app.provisioning.job_provisioner.cloudformation_stack_builder import (
    CloudFormationStackBuilder,
)

from typing import Optional


class SchedulerAdminAPI(BaseAPI):
    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger('scheduler-admin-api')

        self.SCOPE_WRITE = f'{self.context.module_id()}/write'
        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            'SchedulerAdmin.ListActiveJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_active_jobs,
            },
            'SchedulerAdmin.ListCompletedJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_completed_jobs,
            },
            'SchedulerAdmin.ListNodes': {
                'scope': self.SCOPE_READ,
                'method': self.list_nodes,
            },
            'SchedulerAdmin.CreateQueueProfile': {
                'scope': self.SCOPE_WRITE,
                'method': self.create_queue_profile,
            },
            'SchedulerAdmin.GetQueueProfile': {
                'scope': self.SCOPE_READ,
                'method': self.get_queue_profile,
            },
            'SchedulerAdmin.UpdateQueueProfile': {
                'scope': self.SCOPE_WRITE,
                'method': self.update_queue_profile,
            },
            'SchedulerAdmin.EnableQueueProfile': {
                'scope': self.SCOPE_WRITE,
                'method': self.enable_queue_profile,
            },
            'SchedulerAdmin.DisableQueueProfile': {
                'scope': self.SCOPE_WRITE,
                'method': self.disable_queue_profile,
            },
            'SchedulerAdmin.DeleteQueueProfile': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_queue_profile,
            },
            'SchedulerAdmin.ListQueueProfiles': {
                'scope': self.SCOPE_READ,
                'method': self.list_queue_profiles,
            },
            'SchedulerAdmin.CreateHpcApplication': {
                'scope': self.SCOPE_WRITE,
                'method': self.create_hpc_application,
            },
            'SchedulerAdmin.GetHpcApplication': {
                'scope': self.SCOPE_READ,
                'method': self.get_hpc_application,
            },
            'SchedulerAdmin.UpdateHpcApplication': {
                'scope': self.SCOPE_WRITE,
                'method': self.update_hpc_application,
            },
            'SchedulerAdmin.DeleteHpcApplication': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_hpc_application,
            },
            'SchedulerAdmin.ListHpcApplications': {
                'scope': self.SCOPE_READ,
                'method': self.list_hpc_applications,
            },
            'SchedulerAdmin.GetUserApplications': {
                'scope': self.SCOPE_READ,
                'method': self.get_user_applications,
            },
            'SchedulerAdmin.ProvisionAlwaysOnNodes': {
                'scope': self.SCOPE_WRITE,
                'method': self.provision_always_on_nodes,
            },
            'SchedulerAdmin.CreateHpcLicenseResource': {
                'scope': self.SCOPE_READ,
                'method': self.create_license_resource,
            },
            'SchedulerAdmin.GetHpcLicenseResource': {
                'scope': self.SCOPE_READ,
                'method': self.get_license_resource,
            },
            'SchedulerAdmin.UpdateHpcLicenseResource': {
                'scope': self.SCOPE_WRITE,
                'method': self.update_license_resource,
            },
            'SchedulerAdmin.DeleteHpcLicenseResource': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_license_resource,
            },
            'SchedulerAdmin.ListHpcLicenseResources': {
                'scope': self.SCOPE_READ,
                'method': self.list_license_resources,
            },
            'SchedulerAdmin.CheckHpcLicenseResourceAvailability': {
                'scope': self.SCOPE_READ,
                'method': self.check_license_resource_availability,
            },
            'SchedulerAdmin.DeleteJob': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_job,
            },
        }

    def list_active_jobs(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListJobsRequest)
        page_size = payload.page_size
        page_start = payload.page_start

        entries = self.context.job_cache.list_jobs(_limit=page_size, _offset=page_start)
        total = self.context.job_cache.get_count()

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
            result = self.context.document_store.search_jobs(payload)
            context.success(result)
        else:
            page_size = payload.page_size
            page_start = payload.page_start

            entries = self.context.job_cache.list_completed_jobs(
                _limit=page_size, _offset=page_start
            )
            total = self.context.job_cache.get_completed_jobs_count()

            context.success(
                ListJobsResult(
                    paginator=SocaPaginator(
                        total=total,
                        page_size=payload.page_size,
                        start=payload.page_start,
                    ),
                    listing=entries,
                )
            )

    # nodes
    def list_nodes(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListNodesRequest)
        result = self.context.document_store.search_nodes(payload)
        context.success(result)

    # queue profiles

    def create_queue_profile(self, context: ApiInvocationContext):
        try:
            payload = context.get_request_payload_as(CreateQueueProfileRequest)
            queue_profile = payload.queue_profile
            created = self.context.queue_profiles.create_queue_profile(queue_profile)
            context.success(CreateQueueProfileResult(queue_profile=created))
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.VALIDATION_FAILED:
                context.fail(
                    e.error_code,
                    e.message,
                    CreateQueueProfileResult(validation_errors=e.ref),
                )
            else:
                raise e

    def update_queue_profile(self, context: ApiInvocationContext):
        try:
            payload = context.get_request_payload_as(UpdateQueueProfileRequest)
            queue_profile = payload.queue_profile
            created = self.context.queue_profiles.update_queue_profile(queue_profile)
            context.success(UpdateQueueProfileResult(queue_profile=created))
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.VALIDATION_FAILED:
                context.fail(
                    e.error_code,
                    e.message,
                    CreateQueueProfileResult(validation_errors=e.ref),
                )
            else:
                raise e

    def get_queue_profile(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(GetQueueProfileRequest)
        queue_profile = self.context.queue_profiles.get_queue_profile(
            queue_profile_name=payload.queue_profile_name,
            queue_profile_id=payload.queue_profile_id,
            queue_name=payload.queue_name,
        )
        context.success(GetQueueProfileResult(queue_profile=queue_profile))

    def list_queue_profiles(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListQueueProfilesRequest)
        lite = Utils.get_as_bool(request.lite, False)
        queue_profiles = self.context.queue_profiles.list_queue_profiles()
        result = []
        for queue_profile in queue_profiles:
            if lite:
                result.append(
                    HpcQueueProfile(
                        queue_profile_id=queue_profile.queue_profile_id,
                        name=queue_profile.name,
                        enabled=queue_profile.enabled,
                        title=queue_profile.title,
                    )
                )
                continue

            # get projects
            for current_project in queue_profile.projects:
                project = self.context.projects_client.get_project_by_id(
                    current_project.project_id
                )
                current_project.title = project.title
                current_project.name = project.name

            # real time fields and metrics
            provisioning_queue: Optional[JobProvisioningQueue] = (
                self.context.queue_profiles.get_provisioning_queue(
                    queue_profile_name=queue_profile.name
                )
            )

            if provisioning_queue is not None:
                queue_size = provisioning_queue.get_queue_size()
                if provisioning_queue.is_queue_blocked_by_limits():
                    queue_profile.status = 'blocked'
                    queue_profile.limit_info = provisioning_queue.get_limit_info()
                else:
                    if queue_size > 0:
                        queue_profile.status = 'active'
                    else:
                        queue_profile.status = 'idle'

                queue_profile.queue_size = provisioning_queue.get_queue_size()

            result.append(queue_profile)

        context.success(ListQueueProfilesResult(listing=result))

    def enable_queue_profile(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(EnableQueueProfileRequest)
        self.context.queue_profiles.enable_queue_profile(
            queue_profile_id=payload.queue_profile_id,
            queue_profile_name=payload.queue_profile_name,
        )
        context.success(EnableQueueProfileResult())

    def disable_queue_profile(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(DisableQueueProfileRequest)
        self.context.queue_profiles.disable_queue_profile(
            queue_profile_id=payload.queue_profile_id,
            queue_profile_name=payload.queue_profile_name,
        )
        context.success(DisableQueueProfileResult())

    def delete_queue_profile(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(DeleteQueueProfileRequest)
        self.context.queue_profiles.delete_queue_profile(
            queue_profile_id=payload.queue_profile_id,
            queue_profile_name=payload.queue_profile_name,
            delete_queues=payload.delete_queues,
        )
        context.success(DeleteQueueProfileResult())

    # applications

    def create_hpc_application(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateHpcApplicationRequest)
        result = self.context.applications.create_application(request)
        context.success(result)

    def get_hpc_application(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetHpcApplicationRequest)
        result = self.context.applications.get_application(request)
        context.success(result)

    def update_hpc_application(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateHpcApplicationRequest)
        result = self.context.applications.update_application(request)
        context.success(result)

    def delete_hpc_application(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DeleteHpcApplicationRequest)
        result = self.context.applications.delete_application(request)
        context.success(result)

    def list_hpc_applications(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListHpcApplicationsRequest)
        result = self.context.applications.list_applications(request)
        context.success(result)

    def get_user_applications(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserApplicationsRequest)
        if request.username is None:
            request.username = context.get_username()
        result = self.context.applications.get_user_applications(request)
        return context.success(result)

    def provision_always_on_nodes(self, context: ApiInvocationContext):
        # validate job parameters
        request = context.get_request_payload_as(ProvisionAlwaysOnNodesRequest)
        project_name = request.project_name

        owner = request.owner
        if Utils.is_empty(owner):
            owner = context.get_username()

        if Utils.is_empty(owner):
            raise exceptions.invalid_params('owner is required')

        queue_profile_name = request.queue_profile_name
        if Utils.is_empty(queue_profile_name):
            raise exceptions.invalid_params('queue_profile_name is required')

        queue_profile = self.context.queue_profiles.get_queue_profile(
            queue_profile_name=queue_profile_name
        )
        if not Utils.get_as_bool(queue_profile.keep_forever, False):
            raise exceptions.invalid_params(
                f'queue profile: {queue_profile_name} does not support always on provisioning.'
            )
        if Utils.is_empty(queue_profile.stack_uuid):
            raise exceptions.invalid_params(
                f'queue profile: {queue_profile_name} is not valid. stack_uuid is not configured.'
            )

        queue_name = request.queue_name
        if Utils.is_empty(queue_name):
            queue_name = queue_profile.queues[0]

        if Utils.is_empty(queue_name):
            raise exceptions.invalid_params(
                'unable to find a queue name in queue profile or request parameters.'
            )

        job_params = request.params
        if Utils.is_empty(job_params):
            raise exceptions.invalid_params('job_params is required')

        builder = SocaJobBuilder(
            context=self.context,
            params=Utils.to_dict(job_params),
            queue_profile=queue_profile,
            stack_uuid=queue_profile.stack_uuid,
        )
        params, provisioning_options = builder.build()

        job = SocaJob(
            cluster_name=self.context.cluster_name(),
            name=queue_profile.name,
            project=project_name,
            job_id=queue_profile.stack_uuid,
            job_uid=queue_profile.stack_uuid,
            queue_type=queue_profile_name,
            queue=queue_name,
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            owner=owner,
            params=params,
            provisioning_options=provisioning_options,
        )

        self.logger.info(
            f'provisioning always on nodes: job configuration - {Utils.to_json(job, indent=True)}'
        )

        stack = self.context.aws_util().cloudformation_describe_stack(
            job.get_compute_stack()
        )
        if stack is not None:
            raise exceptions.general_exception(
                f'cloud formation stack: {job.get_compute_stack()} already exists for queue profile: '
                f'{queue_profile_name}'
            )

        try:
            stack_id = CloudFormationStackBuilder(context=self.context, job=job).build()

            context.success(
                ProvisionAlwaysOnNodesResult(
                    stack_name=job.get_compute_stack(), stack_id=stack_id
                )
            )
        except Exception as e:
            self.logger.exception(f'failed to provision cloudformation stack: {e}')
            raise e

    def create_license_resource(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateHpcLicenseResourceRequest)
        result = self.context.license_service.create_license_resource(request)
        context.success(result)

    def get_license_resource(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetHpcLicenseResourceRequest)
        result = self.context.license_service.get_license_resource(request)
        context.success(result)

    def update_license_resource(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateHpcLicenseResourceRequest)
        result = self.context.license_service.update_license_resource(request)
        context.success(result)

    def delete_license_resource(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DeleteHpcLicenseResourceRequest)
        result = self.context.license_service.delete_license_resource(request)
        context.success(result)

    def list_license_resources(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListHpcLicenseResourcesRequest)
        result = self.context.license_service.list_license_resources(request)
        context.success(result)

    def check_license_resource_availability(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(
            CheckHpcLicenseResourceAvailabilityRequest
        )
        result = self.context.license_service.check_license_resource_availability(
            request
        )
        context.success(result)

    def delete_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DeleteJobRequest)
        if Utils.is_empty(request.job_id):
            raise exceptions.invalid_params('job_id is required')

        job = self.context.scheduler.get_job(job_id=request.job_id)
        if job is None:
            raise exceptions.soca_exception(
                errorcodes.JOB_NOT_FOUND, f'Job not found for Job Id: {request.job_id}'
            )

        self.context.scheduler.delete_job(job.job_id)

        context.success(DeleteJobResult())

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace

        acl_entry = Utils.get_value_as_dict(namespace, self.acl)
        if acl_entry is None:
            raise exceptions.unauthorized_access()

        acl_entry_scope = Utils.get_value_as_string('scope', acl_entry)
        is_authorized = context.is_authorized(
            elevated_access=True, scopes=[acl_entry_scope]
        )

        if is_authorized:
            acl_entry['method'](context)
        else:
            raise exceptions.unauthorized_access()
