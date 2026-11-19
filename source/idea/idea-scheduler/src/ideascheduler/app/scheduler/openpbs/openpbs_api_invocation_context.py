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

from ideadatamodel import (
    exceptions,
    constants,
    errorcodes,
    GetHpcLicenseResourceRequest,
    SocaMemory,
    JobValidationResult,
    JobValidationResultEntry,
    SocaJob,
    SocaQueueMode,
    SocaSpotAllocationStrategy,
    SocaJobEstimatedBOMCost,
    SocaJobNotifications,
    SocaJobState,
    CheckServiceQuotaResult,
    ServiceQuota,
    DryRunOption,
    SocaJobEstimatedBudgetUsage,
    SubmitJobResult,
    SocaAmount,
)
from ideascheduler.app.scheduler.openpbs.openpbs_api import (
    OpenPBSHookRequest,
    OpenPBSHookResult,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent
from ideascheduler.app.scheduler import SocaJobBuilder
from ideascheduler.app.scheduler.job_param_builder import (
    COMPUTED_JOB_GROUP_PATTERN,
    JOB_UID_PATTERN,
)
from ideascheduler.app.scheduler.pbs_select import (
    select_chunks,
    chunk_resources,
    select_compute_node,
)
from ideasdk.utils import Utils
from ideasdk.api import ApiInvocationContext

import ideascheduler
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.provisioning.job_provisioner.job_provisioning_util import (
    projects_allowed_on_queue,
)
from ideascheduler.app.aws import PricingHelper, AwsBudgetsHelper

import os
from typing import Optional, List
from prettytable import PrettyTable
import arrow

DRY_RUN_HEADER = (
    f' ---- THIS IS A DRY RUN, JOB WILL NOT BE QUEUED ----- {os.linesep}{os.linesep}'
)


class OpenPBSAPIInvocationContext:
    def __init__(self, context: ApiInvocationContext):
        self.api_context = context
        self.app_context: ideascheduler.AppContext = context.context

        self.request = context.get_request_payload_as(OpenPBSHookRequest)
        self.event: Optional[OpenPBSEvent] = self.request.event

        self._job_builder: Optional[SocaJobBuilder] = None
        self._queue_profile = None

        self._job_validation_result: Optional[JobValidationResult] = None
        self._incidentals_validation_result: Optional[JobValidationResult] = None
        self._job: Optional[SocaJob] = None

        self._reserved_instances_unavailable = False

        self._service_quota_result: Optional[CheckServiceQuotaResult] = None
        self._service_quota_unavailable = False

        self._bom_cost: Optional[SocaJobEstimatedBOMCost] = None
        self._budget_usage: Optional[SocaJobEstimatedBudgetUsage] = None

        self._job_submission_result = SubmitJobResult()

    @staticmethod
    def _default_existing_fsx(queue_profile) -> Optional[str]:
        default_params = queue_profile.default_job_params if queue_profile else None
        default_fsx = default_params.fsx_lustre if default_params else None
        return default_fsx.existing_fsx if default_fsx else None

    def _default_existing_fsx_of(self, queue_name: Optional[str]) -> Optional[str]:
        if Utils.is_empty(queue_name):
            return None
        try:
            return self._default_existing_fsx(
                self.app_context.queue_profiles.get_queue_profile(queue_name=queue_name)
            )
        except exceptions.SocaException:
            # a source queue with no profile pinned nothing
            return None

    def _refuse_move_keeping_another_queues_fsx(
        self, queue_profile, queue_name: str, moved_fsx: Optional[str]
    ):
        """
        Resource_List is read-only to a movejob hook (the PBS hooks matrix, copied in
        openpbs_hook_handler.JOB_PARAM_EVENT_TYPE_MATRIX), so the target queue's value
        cannot be relied on to replace the one the job carries. a job carrying a queue's
        pinned file system into a queue that restricts fsx_lustre and resolves to another
        value would run with it: the move is refused instead (rereview3 S2)
        """
        if not moved_fsx:
            return
        params = queue_profile.queue_management_params if queue_profile else None
        restricted = Utils.get_as_list(
            params.restricted_parameters if params else None, []
        )
        if constants.JOB_PARAM_FSX_LUSTRE not in restricted:
            return
        fsx = self._job.params.fsx_lustre if self._job.params else None
        resolved = fsx.existing_fsx if fsx is not None and fsx.enabled else None
        if resolved == moved_fsx:
            return
        self.add_job_validation_entry(
            error_code=errorcodes.INVALID_PARAMS,
            message=f'This job uses the FSx for Lustre file system {moved_fsx}, the '
            f'default of queue {self.event.src_queue}. Queue {queue_name} does not let '
            f'jobs set fsx_lustre, so the job cannot keep it there. Submit the job to '
            f'queue {queue_name} instead of moving it.',
        )

    def _refuse_move_of_bound_job(self, queue_name: str):
        """
        a movejob hook may only read the job, so a job whose selector already names a
        node of its queue's stack (held jobs keep it) cannot be unbound: accepted, it sat
        in the target queue bound to the source queue's capacity. it is refused instead
        """
        if not self._moved_job_is_bound():
            return
        self.add_job_validation_entry(
            error_code=errorcodes.INVALID_PARAMS,
            message=f'This job already has a node from queue {self.event.src_queue}, '
            f'so it cannot be moved to queue {queue_name}. Delete it and submit it to '
            f'queue {queue_name}, or move a job before it is given a node.',
        )

    def _moved_job_is_bound(self) -> bool:
        """a move of a job whose node selector names its stack: PBS keeps a provisioned
        job's node in schedselect, which the select of its request may not show"""
        if self.event.type != 'movejob' or self.event.job is None:
            return False
        select = self.event.job.get_select() or ''
        return 'compute_node=' in select and 'compute_node=tbd' not in select

    def _bound_to_capacity(self) -> bool:
        """an edit of a job whose node selector names its stack"""
        if self.event.type != 'modifyjob' or self.event.job_o is None:
            return False
        select = (self.event.job_o.Resource_List or {}).get('select') or ''
        return 'compute_node=' in select and 'compute_node=tbd' not in select

    @staticmethod
    def _without_computed_group(params: dict) -> tuple:
        """
        a group of the computed form (g + hex) is the scheduler's, never a submitter's
        (only custom-... passes validation), so one carried on an edited or moved job is
        not checked as the submitter's and is computed again from the request as it is
        now: carried over, an edit kept the group of its old request and was sent to that
        group's stack. a custom group stays.
        """
        group = params.get('job_group')
        if group and COMPUTED_JOB_GROUP_PATTERN.fullmatch(str(group)):
            return {k: v for k, v in params.items() if k != 'job_group'}, str(group)
        return params, None

    def _cached_job_uid(self) -> Optional[str]:
        for pbs_job in (self.event.job_o, self.event.job):
            job_id = (
                str(pbs_job.id).split('.')[0]
                if pbs_job is not None and pbs_job.id
                else ''
            )
            if job_id and job_id.lower() != 'tbd':
                try:
                    cached = self.app_context.job_cache.get_job(job_id)
                except Exception:
                    return None
                return cached.job_uid if cached is not None else None
        return None

    def build_and_validate_job(self):
        try:
            queue_name = None
            job_params = {}
            old_job_params = {}
            job_uid = None
            project_name = None

            if self.event.job_o:
                old_job_params = self.event.job_o.get_soca_job_params()
                job_uid = self.event.job_o.get_job_uid()
                queue_name = self.event.job_o.queue
                project_name = self.event.job_o.project

            if self.event.job:
                job_params = self.event.job.get_soca_job_params()
                queue_name = self.event.job.queue or queue_name
                job_uid = self.event.job.get_job_uid() or job_uid
                if Utils.is_not_empty(self.event.job.project):
                    project_name = self.event.job.project

            # an existing job keeps its uid: its package and completion record are under
            # it. an edit whose job_o carried none minted a new one and wrote it back
            self.existing_uid_unknown = False
            if Utils.is_empty(job_uid) and self.event.type == 'modifyjob':
                job_uid = self._cached_job_uid()
                self.existing_uid_unknown = Utils.is_empty(job_uid)
            if Utils.is_empty(job_uid):
                job_uid = Utils.short_uuid()

            # create temporary job, in case of queue profile not found or disabled errors
            # and correlate with job submission handler
            self._job = SocaJob(job_uid=job_uid, owner=self._owner_username())

            # job_uid names the bootstrap package a compute node downloads as root, so a
            # value carried on the job must keep the letters, digits, underscores and dashes it is made of.
            if not JOB_UID_PATTERN.fullmatch(str(job_uid)):
                raise exceptions.invalid_params(
                    f'job_uid can only contain letters, digits, underscores and dashes: ({job_uid})'
                )
            # the completion record is stored under job_uid, so a value the submitter
            # chose must not be another job's: that job's record would be replaced
            supplied_uid = (
                self.event.type == 'queuejob'
                and self.event.job is not None
                and Utils.is_not_empty(self.event.job.get_job_uid())
            )
            if supplied_uid and self.app_context.job_cache.is_job_uid_taken(job_uid):
                raise exceptions.invalid_params(
                    f'job_uid {job_uid} already belongs to another job. Leave job_uid '
                    f'out and IDEA assigns one.'
                )

            # if no queue or project is specified during job submission, default queue name to 'normal'
            if Utils.is_empty(queue_name):
                queue_name = 'normal'

            queue_profile = self.app_context.queue_profiles.get_queue_profile(
                queue_name=queue_name
            )
            self._queue_profile = queue_profile
            if not queue_profile.is_enabled():
                raise exceptions.soca_exception(
                    error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_DISABLED,
                    message=f'queue: {queue_name} has been disabled and cannot accept new job submissions.',
                )

            # the project is resolved before the builder is created: it decides the
            # instance profile the job's compute nodes run under. an omitted -P used
            # to take projects[0], which is often a project the user is not in.
            if self.event.type != 'modifyjob' and Utils.is_empty(project_name):
                project_name = self._project_when_omitted(queue_profile, queue_name)
                self.app_context.logger().info(
                    f'no project given, using {project_name}'
                )

            if (
                self.event.type != 'modifyjob'
                and self.event.job is not None
                and Utils.is_not_empty(project_name)
            ):
                self.event.job.project = project_name

            # the queue's default existing FSx was written onto the job at submission; on
            # an edit or move that does not touch fsx_lustre it is the queue's value, not
            # the job's, so it is left to the default again: read back as the job's own it
            # was refused on queues that restrict fsx_lustre (A-S3). a move carries the
            # whole job in event.job, so there the value equal to the source or target
            # queue's default is the pinned one (S1)
            pinned_values = {self._default_existing_fsx(queue_profile)}
            if self.event.type == 'movejob':
                pinned_values.add(self._default_existing_fsx_of(self.event.src_queue))
            pinned_values.discard(None)
            pinned = old_job_params.get('fsx_lustre')
            if 'fsx_lustre' not in job_params and pinned and pinned in pinned_values:
                old_job_params = {
                    k: v for k, v in old_job_params.items() if k != 'fsx_lustre'
                }
            moved_fsx = None
            if (
                self.event.type == 'movejob'
                and job_params.get('fsx_lustre')
                and job_params['fsx_lustre'] in pinned_values
            ):
                moved_fsx = job_params['fsx_lustre']
                job_params = {k: v for k, v in job_params.items() if k != 'fsx_lustre'}
            carried_group = None
            if self.event.type in ('modifyjob', 'movejob'):
                old_job_params, old_group = self._without_computed_group(old_job_params)
                job_params, new_group = self._without_computed_group(job_params)
                carried_group = new_group or old_group
            job_params = {**old_job_params, **job_params}
            # the subnet a multi-node job draws is seeded by its uid, which the job keeps
            # from submission on, so the group stored now is the one a later read computes
            job_params.setdefault('job_uid', job_uid)
            self._job_builder = SocaJobBuilder(
                context=self.app_context,
                params=job_params,
                queue_name=queue_name,
                queue_profile=queue_profile,
                project=project_name,
            )

            self._job = self.event.as_soca_job(
                context=self.app_context,
                queue_profile=queue_profile,
                job_builder=self.job_builder,
                owner=self._owner_username(),
            )
            self._job.queue = queue_name
            self._job.project = project_name

            dry_run = self.dry_run_option()
            if dry_run is not None and dry_run == DryRunOption.DEBUG:
                self._job.debug = True

            self._job.job_id = 'tbd'
            self._job.job_uid = job_uid

            self._job_validation_result: Optional[JobValidationResult] = None
            self._job_validation_result = self.job_builder.validate()
            if carried_group and (
                self._bound_to_capacity() or self._moved_job_is_bound()
            ):
                # a job on a stack keeps the group that names the stack
                self._job.job_group = carried_group
                self._job.params.job_group = carried_group
            self._refuse_move_of_bound_job(queue_name)
            self._refuse_move_keeping_another_queues_fsx(
                queue_profile, queue_name, moved_fsx
            )

        except exceptions.SocaException as e:
            if e.error_code in (
                errorcodes.SCHEDULER_QUEUE_PROFILE_DISABLED,
                errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND,
                errorcodes.PROJECT_NOT_FOUND,
                errorcodes.CONFIG_ERROR,
            ):
                raise e
            else:
                self.add_job_validation_entry(
                    error_code=e.error_code, message=e.message
                )
        finally:
            self._job_submission_result.job = self.job
            self._job_submission_result.validations = self.job_validation_result
            self._job_submission_result.dry_run = self.dry_run_option()

    def job_select(self, compute_node: str) -> str:
        """Size omitted CPUs after queue defaults resolve, only at submission."""
        job = self.job
        resources = self.event.job.Resource_List or {}
        select = Utils.get_value_as_string('select', job.params.custom_params, '')
        shared = job.queue == 'job-shared' or job.is_shared_capacity()
        if not select:
            select = str(job.desired_nodes())
            if resources.get('ncpus') is not None:
                select += f':ncpus={resources["ncpus"]}'
            elif shared:
                select += f':ncpus={job.params.cpus}'

        if self.event.type == 'queuejob' and not shared:
            select = self.size_select_cpus(select, resources)
        return select_compute_node(select, compute_node)

    @staticmethod
    def _ht_support(values: dict, default: bool) -> bool:
        # the two names EnableHtSupportParamBuilder accepts; the node is provisioned by
        # either, so the chunk must be sized by either
        ht_support = Utils.get_value_as_bool('ht_support', values)
        if ht_support is None:
            ht_support = Utils.get_value_as_bool('enable_ht_support', values)
        return default if ht_support is None else ht_support

    def size_select_cpus(self, select: str, resources: dict) -> str:
        """Leave the entire select unchanged if any missing CPU count is unresolved."""
        defaults = self._queue_profile.default_job_params
        default_ht = constants.DEFAULT_ENABLE_HT_SUPPORT
        if defaults and defaults.enable_ht_support is not None:
            default_ht = defaults.enable_ht_support
        default_ht = self._ht_support(resources, default_ht)
        sized = []
        try:
            for chunk in select_chunks(select):
                params = chunk_resources(chunk)
                if 'ncpus' in params:
                    sized.append(chunk)
                    continue
                instance_types = params.get('instance_type') or resources.get(
                    'instance_type'
                )
                if instance_types:
                    instance_types = instance_types.split('+')
                else:
                    instance_types = defaults.instance_types if defaults else None
                if not instance_types:
                    raise ValueError('no instance_type resolved')
                ht_support = self._ht_support(params, default_ht)
                counts = []
                for instance_type in instance_types:
                    info = self.app_context.aws_util().get_ec2_instance_type(
                        instance_type
                    )
                    count = (
                        info.vcpu_info_default_vcpus
                        if ht_support
                        else info.vcpu_info_default_cores
                    )
                    if count is None or count <= 0:
                        raise ValueError(f'no CPU count for {instance_type}')
                    counts.append(count)
                sized.append(f'{chunk}:ncpus={min(counts)}')
        except Exception as e:
            self.app_context.logger().warning(f'select CPU sizing skipped: {e}')
            return select
        return '+'.join(sized)

    def _owner_username(self) -> Optional[str]:
        owner = None
        if self.event is not None:
            for job in (self.event.job_o, self.event.job):
                if job is not None:
                    owner = job.Job_Owner or job.euser
                    if Utils.is_not_empty(owner):
                        break
        if Utils.is_empty(owner) and self.event is not None:
            owner = self.event.requestor
        if Utils.is_empty(owner):
            return None
        return str(owner).split('@')[0]

    def _project_when_omitted(self, queue_profile, queue_name: str) -> str:
        """
        First queue project the submitter belongs to. Same membership list check_acls uses.
        """
        owner = self._owner_username()
        user_projects = []
        if Utils.is_not_empty(owner):
            try:
                user_projects = self.app_context.projects_client.get_user_projects(
                    username=owner
                )
            except Exception as e:
                raise exceptions.soca_exception(
                    error_code=errorcodes.GENERAL_ERROR,
                    message='Could not read projects for the job owner. Please try again or contact your administrator.',
                ) from e
        allowed = projects_allowed_on_queue(user_projects, queue_profile.projects)
        if len(allowed) == 0:
            names = []
            for project in (queue_profile.projects or [])[:10]:
                if project.enabled is False:
                    continue
                project_id = project.project_id
                if not project.name and project_id:
                    try:
                        # ProjectsClient caches these lookups across submissions.
                        project = (
                            self.app_context.projects_client.get_project_by_id(
                                project_id=project_id
                            )
                            or project
                        )
                    except Exception:
                        # A display lookup must never replace the admission rejection.
                        pass
                if project.enabled is False:
                    continue
                name = project.name or project_id
                if name:
                    names.append(name)
            if not names:
                raise exceptions.soca_exception(
                    error_code=errorcodes.UNAUTHORIZED_ACCESS,
                    message=f'Queue {queue_name} has no enabled projects; ask an admin to configure a project for this queue.',
                )
            listed = ', '.join(names)
            raise exceptions.soca_exception(
                error_code=errorcodes.UNAUTHORIZED_ACCESS,
                message=(
                    f'No project given (add `#PBS -P <project>` or `-P <project>`). '
                    f'Your projects allowed on queue {queue_name}: none; '
                    f'ask an admin to add you to one of: {listed}'
                ),
            )
        return allowed[0]

    @property
    def job_uid(self) -> str:
        return self._job.job_uid

    @property
    def job_submission_result(self) -> SubmitJobResult:
        return self._job_submission_result

    @property
    def job_validation_result(self) -> JobValidationResult:
        if self._job_validation_result is None:
            self._job_validation_result = JobValidationResult(results=[])
        return self._job_validation_result

    @property
    def incidentals_validation_result(self) -> JobValidationResult:
        if self._incidentals_validation_result is None:
            self._incidentals_validation_result = JobValidationResult(results=[])
        return self._incidentals_validation_result

    def is_valid(self) -> bool:
        return (
            self.job_validation_result.is_valid()
            and self.incidentals_validation_result.is_valid()
        )

    def add_job_validation_entry(self, error_code: str, message: str):
        self.job_validation_result.results.append(
            JobValidationResultEntry(error_code=error_code, message=message)
        )

    def add_incidentals_validation_entry(
        self, error_code: str, message: str, line_break=False
    ):
        if line_break:
            self.incidentals_validation_result.results.append(
                JobValidationResultEntry()
            )
        self.incidentals_validation_result.results.append(
            JobValidationResultEntry(error_code=error_code, message=message)
        )

    def is_dry_run(self) -> bool:
        return self.dry_run_option() is not None

    def dry_run_option(self) -> Optional[DryRunOption]:
        if self.event is None:
            return None
        if self.event.job is None:
            return None
        return DryRunOption.resolve(self.event.job.dry_run)

    @property
    def job_builder(self) -> Optional[SocaJobBuilder]:
        return self._job_builder

    @property
    def job(self) -> Optional[SocaJob]:
        return self._job

    def get_existing_fsx(self) -> Optional[str]:
        fsx_lustre = self.job.params.fsx_lustre
        if fsx_lustre is None:
            return None
        if fsx_lustre.existing_fsx is None:
            return None
        return fsx_lustre.existing_fsx.split('.')[0]

    @staticmethod
    def get_quotas_as_table(quotas: List[ServiceQuota]):
        table = PrettyTable(
            ['QuotaName', 'Available vCPUs', 'Desired vCPUs', 'Consumed vCPUs']
        )
        for quota in quotas:
            table.add_row(
                [quota.quota_name, quota.available, quota.desired, quota.consumed]
            )
        return str(table)

    def check_incidentals(self):
        if self.job is None:
            return

        provisioning_util = JobProvisioningUtil(
            context=self.app_context, jobs=[self.job]
        )

        has_access = False
        try:
            provisioning_util.check_acls()
            has_access = True
        except exceptions.SocaException as e:
            self.add_incidentals_validation_entry(
                error_code='NotAuthorized', message=e.message
            )

        if has_access:
            # check budgets
            try:
                provisioning_util.check_budgets()
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.BUDGETS_PROJECT_IS_REQUIRED:
                    error_code = 'Budgets.ProjectNameRequired'
                elif e.error_code == errorcodes.BUDGETS_USER_NOT_CONFIGURED:
                    error_code = 'Budgets.UserNotConfigured'
                elif e.error_code == errorcodes.PROJECT_NOT_FOUND:
                    error_code = 'Budgets.ProjectNotFound'
                else:
                    error_code = 'Budgets.LimitExceeded'
                self.add_incidentals_validation_entry(
                    error_code=error_code, message=e.message
                )

            # bedrock access needing an administrator is rejected here; an unresolved
            # check is accepted, and provisioning holds the job until it resolves.
            try:
                provisioning_util.check_bedrock(reject_when_check_fails=False)
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.BEDROCK_ACCESS_NOT_AUTHORIZED:
                    self.add_incidentals_validation_entry(
                        error_code='Bedrock.NotAuthorized', message=e.message
                    )
                else:
                    raise e

            # check reserved instance usage
            try:
                provisioning_util.check_reserved_instance_usage()
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.EC2_RESERVED_INSTANCES_NOT_PURCHASED:
                    self.add_incidentals_validation_entry(
                        error_code='ReservedInstancesNotPurchased', message=e.message
                    )
                elif e.error_code == errorcodes.EC2_RESERVED_INSTANCES_NOT_AVAILABLE:
                    self._reserved_instances_unavailable = True

            # check service quota
            if self.app_context.config().get_bool(
                'scheduler.job_provisioning.service_quotas', default=True
            ):
                try:
                    result = provisioning_util.check_service_quota()
                    self._service_quota_result = result
                    self._job_submission_result.service_quotas = result.quotas
                except exceptions.SocaException as e:
                    if e.error_code == errorcodes.SERVICE_QUOTA_NOT_AVAILABLE:
                        result: CheckServiceQuotaResult = e.ref
                        not_requested = result.find_insufficient_quotas()

                        if len(not_requested) > 0:
                            table = self.get_quotas_as_table(not_requested)
                            self.app_context.logger().warning(
                                f'{self.job.log_tag} - quota too low:{os.linesep}{table}'
                            )
                            message = (
                                'This job needs more AWS capacity than this cluster is '
                                'allowed. Ask an administrator to request a higher quota.'
                            )
                            self.add_incidentals_validation_entry(
                                error_code='ServiceQuota', message=message
                            )
                        else:
                            self._service_quota_unavailable = True
            else:
                self.app_context.logger(
                    'Bypassing Service Quota checks due to scheduler.job_provisioning.service_quotas'
                )
                self._service_quota_unavailable = False
                self._service_quota_result = None
                self._job_submission_result.service_quotas = None

            # an unknown license resource can never provision, so it's checked at submission -
            # except on license-optimized queues, which are expected to queue until available.
            self.check_licenses(provisioning_util=provisioning_util)

            # ephemeral capacity gets a live dry run per submission; batch/job-shared queues
            # cache one per job group signature, since a dry run per job floods the EC2 API.
            try:
                if self.job.is_ephemeral_capacity():
                    provisioning_util.ec2_dry_run()
                else:
                    provisioning_util.ec2_dry_run_cached()
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.EC2_DRY_RUN_FAILED:
                    self.add_incidentals_validation_entry(
                        error_code='EC2DryRunFailed', message=e.message
                    )

    def check_licenses(self, provisioning_util: JobProvisioningUtil):
        licenses = self.job.params.licenses
        if licenses is None or len(licenses) == 0:
            return

        licenses_resolved = True
        for license_ask in licenses:
            try:
                self.app_context.license_service.get_license_resource(
                    GetHpcLicenseResourceRequest(name=license_ask.name)
                )
            except exceptions.SocaException as e:
                self.app_context.logger().warning(
                    f'{self.job.log_tag} - license {license_ask.name}: {e.message}'
                )
                licenses_resolved = False
                self.add_incidentals_validation_entry(
                    error_code='Licenses.NotFound',
                    message=f"License {license_ask.name} isn't set up on this cluster, so this "
                    f'job could never start. Remove it from your job script, or ask an '
                    f'administrator.',
                )

        if not licenses_resolved:
            return

        queue_profile = self.app_context.queue_profiles.get_queue_profile(
            queue_name=self.job.queue
        )
        if queue_profile.queue_mode == SocaQueueMode.LICENSE_OPTIMIZED:
            return

        try:
            # only a reachable license server can reject a submission; an unreachable one
            # accepts the job (provisioning re-checks), so it can't reject every job for the resource.
            provisioning_util.check_licenses(reject_when_check_fails=False)
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.NOT_ENOUGH_LICENSES:
                self.add_incidentals_validation_entry(
                    error_code='Licenses.NotAvailable', message=e.message
                )
            else:
                raise e

    def get_job_time_seconds(self) -> int:
        if self.job.params.walltime:
            return Utils.walltime_to_seconds(self.job.params.walltime)
        return 60 * 60

    def get_friendly_job_time(self) -> str:
        total_time_seconds = self.get_job_time_seconds()
        job_time = arrow.utcnow().shift(seconds=total_time_seconds)
        s = job_time.humanize(only_distance=True, granularity=['hour', 'minute'])
        s = s.replace('0 hours and ', '').replace(' and 0 minutes', '')
        return s

    def get_bom_cost(self) -> Optional[SocaJobEstimatedBOMCost]:
        if self._bom_cost is not None:
            return self._bom_cost

        helper = PricingHelper(
            context=self.app_context,
            job=self.job,
            total_time_secs=self.get_job_time_seconds(),
        )

        self._bom_cost = helper.compute_estimated_bom_cost()
        self._job_submission_result.estimated_bom_cost = self._bom_cost
        return self._bom_cost

    def get_budget_usage(self) -> Optional[SocaJobEstimatedBudgetUsage]:
        if self._budget_usage is not None:
            return self._budget_usage

        budget_helper = AwsBudgetsHelper(context=self.app_context, job=self.job)

        bom_cost = self.get_bom_cost()

        try:
            self._budget_usage = budget_helper.compute_budget_usage(bom_cost=bom_cost)
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.BUDGET_NOT_FOUND:
                # Create a budget usage with is_missing flag to indicate missing budget
                budget_name = budget_helper.budget_name
                self._budget_usage = SocaJobEstimatedBudgetUsage(
                    budget_name=budget_name,
                    budget_limit=SocaAmount(amount=100.0),  # Set a default budget limit
                    actual_spend=SocaAmount(
                        amount=200.0
                    ),  # Set actual spend higher than limit to show as exhausted
                    forecasted_spend=SocaAmount(
                        amount=200.0
                    ),  # Set forecasted spend higher than limit
                    job_usage_percent=100.0,  # Set a high job usage percent
                    job_usage_percent_with_savings=100.0,  # Set a high job usage percent with savings
                    is_missing=True,  # Flag to indicate budget is missing
                )
                # Also add an incidental warning for the UI
                self.add_incidentals_validation_entry(
                    error_code='Budgets.LimitExceeded',
                    message=f'Budget not found: {budget_name}',
                )
            else:
                raise e

        self._job_submission_result.budget_usage = self._budget_usage

        return self._budget_usage

    def get_service_quotas(self) -> Optional[CheckServiceQuotaResult]:
        if self._service_quota_result is not None:
            return self._service_quota_result

    def get_estimated_price_as_table(self) -> str:
        bom_cost = self.get_bom_cost()

        costs = PrettyTable(['Item', 'Unit Price', 'Unit', 'Qty', 'Total'])
        costs.align = 'l'
        for item in bom_cost.line_items:
            costs.add_row(
                [
                    item.title,
                    item.unit_price.formatted(),
                    item.unit,
                    round(item.quantity, 2),
                    item.total_price.formatted(),
                ]
            )
        costs.add_row(
            ['Estimated Job Cost', '', '', '', bom_cost.line_items_total.formatted()]
        )

        savings = None
        if bom_cost.savings:
            savings = PrettyTable(['Item', 'Unit Price', 'Unit', 'Qty', 'Total'])
            savings.align = 'l'
            for item in bom_cost.savings:
                savings.add_row(
                    [
                        item.title,
                        item.unit_price.formatted(),
                        item.unit,
                        round(item.quantity, 2),
                        item.total_price.formatted(),
                    ]
                )
            savings.add_row(
                [
                    'Estimated Job Cost (with Savings)',
                    '',
                    '',
                    '',
                    f'{bom_cost.total.formatted()}',
                ]
            )

        result = f'{costs}'
        if savings:
            result += f'{os.linesep}{os.linesep}'
            result += (
                f'Potential Savings: {bom_cost.savings_total.formatted()} ({bom_cost.savings_percent()}%)'
                f'{os.linesep}'
                f'{savings}'
            )
        return result

    def get_budget_usage_as_table(self) -> Optional[str]:
        usage = self.get_budget_usage()
        if usage is None:
            return None

        table = PrettyTable(['Item', 'Value'])
        table.align = 'l'
        table.add_row(['Limit', usage.budget_limit.formatted()])
        table.add_row(['Actual', usage.actual_spend.formatted()])
        table.add_row(['Forecasted', usage.forecasted_spend.formatted()])

        result = f"{usage.budget_name}'s Budget (Job Usage: {usage.job_usage_percent}%"

        bom_cost = self.get_bom_cost()
        if bom_cost.savings is not None:
            result += (
                f', Job usage with Savings: {usage.job_usage_percent_with_savings}%'
            )
        result += f'){os.linesep}{table}'
        return result

    @staticmethod
    def sanitize(value):
        if isinstance(value, SocaMemory):
            return str(value)
        if isinstance(value, SocaSpotAllocationStrategy):
            return str(value)
        return value

    def get_job_params_as_yaml(self) -> str:
        result = self.job_builder.debug()
        params = {}
        response = {'Job Parameters': params}
        for entry in result:
            if entry.job_value is None:
                continue
            params[entry.title] = self.sanitize(entry.job_value)
        return Utils.to_yaml(response)

    def get_job_params_as_table(self) -> str:
        table = PrettyTable(['Title', 'Job Value'])
        table.align = 'l'
        result = self.job_builder.debug()

        for entry in result:
            if entry.job_value is None:
                continue
            table.add_row([entry.title, self.sanitize(entry.job_value)])
        return str(table)

    def dry_run_post_processing(self) -> OpenPBSHookResult:
        dry_run = self.dry_run_option()
        if dry_run == DryRunOption.JSON_JOB:
            json_content = Utils.to_json(self.job, indent=2)
            formatted_user_message = f'Job Info: {os.linesep}{json_content}'
        elif dry_run == DryRunOption.NOTIFICATION_EMAIL:
            job = self.job
            job.notifications = SocaJobNotifications(started=True, completed=True)

            job.state = SocaJobState.RUNNING
            job.queue_time = arrow.utcnow().shift(seconds=-10).datetime
            job.provisioning_time = arrow.utcnow().shift(seconds=-5).datetime
            self.app_context.job_notifications.job_started(job=job)

            job.state = SocaJobState.FINISHED
            job.start_time = arrow.utcnow().datetime
            job.end_time = (
                arrow.utcnow().shift(seconds=self.get_job_time_seconds()).datetime
            )
            job.estimated_bom_cost = self.get_bom_cost()
            job.estimated_budget_usage = self.get_budget_usage()
            self.app_context.job_notifications.job_completed(job=self.job)

            formatted_user_message = 'DryRun: Email notifications sent successfully.'

        else:
            formatted_user_message = DRY_RUN_HEADER

            formatted_user_message += (
                f'If submitted, job will be accepted and queued. IDEA will provision AWS '
                f'resources required for this job. {os.linesep}{os.linesep}'
            )

            formatted_user_message += (
                f'Estimated Job Costs for {self.get_friendly_job_time()}: {os.linesep}:'
            )
            formatted_user_message += (
                f'{self.get_estimated_price_as_table()}{os.linesep}{os.linesep}'
            )

            budget_usage = self.get_budget_usage_as_table()
            if budget_usage:
                formatted_user_message += f'{budget_usage}{os.linesep}{os.linesep}'

            if self._reserved_instances_unavailable:
                formatted_user_message += (
                    'You have requested for reserved instances, but purchased reserved '
                    'instance capacity is currently being used. Your job will be provisioned '
                    'after reserved capacity is available. Please purchase more '
                    f'reserved instances to speed up provisioning for this job.'
                    f'{os.linesep}{os.linesep}'
                )

            if self._service_quota_result is not None:
                table = self.get_quotas_as_table(self._service_quota_result.quotas)
                if self._service_quota_unavailable:
                    formatted_user_message += (
                        'The service quota for the requested class of instances is at capacity. '
                        'Job may be provisioned after capacity becomes available. Below are the'
                        f'available AWS Service Quotas: {os.linesep}{os.linesep}'
                        f'{table}{os.linesep}'
                    )
                else:
                    formatted_user_message += (
                        f'Applicable AWS Service Quotas: {os.linesep}'
                        f'{table}{os.linesep}{os.linesep}'
                    )

            # todo - pending messages in queue, limits etc.

        return OpenPBSHookResult(
            accept=False, formatted_user_message=formatted_user_message
        )
