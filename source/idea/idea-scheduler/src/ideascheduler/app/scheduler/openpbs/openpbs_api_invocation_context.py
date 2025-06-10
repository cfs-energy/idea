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
    errorcodes,
    SocaMemory,
    JobValidationResult,
    JobValidationResultEntry,
    SocaJob,
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
from ideasdk.utils import Utils
from ideasdk.api import ApiInvocationContext

import ideascheduler
from ideascheduler.app.provisioning import JobProvisioningUtil
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

        self._job_validation_result: Optional[JobValidationResult] = None
        self._incidentals_validation_result: Optional[JobValidationResult] = None
        self._job: Optional[SocaJob] = None

        self._reserved_instances_unavailable = False

        self._service_quota_result: Optional[CheckServiceQuotaResult] = None
        self._service_quota_unavailable = False

        self._bom_cost: Optional[SocaJobEstimatedBOMCost] = None
        self._budget_usage: Optional[SocaJobEstimatedBudgetUsage] = None

        self._job_submission_result = SubmitJobResult()

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

            if self.event.job:
                job_params = self.event.job.get_soca_job_params()
                queue_name = self.event.job.queue
                job_uid = self.event.job.get_job_uid()
                project_name = self.event.job.project

            if Utils.is_empty(job_uid):
                job_uid = Utils.short_uuid()

            # create temporary job, in case of queue profile not found or disabled errors
            # and correlate with job submission handler
            self._job = SocaJob(job_uid=job_uid)

            # if no queue or project is specified during job submission, default queue name to 'normal'
            if Utils.is_empty(queue_name):
                queue_name = 'normal'

            queue_profile = self.app_context.queue_profiles.get_queue_profile(
                queue_name=queue_name
            )
            if not queue_profile.is_enabled():
                raise exceptions.soca_exception(
                    error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_DISABLED,
                    message=f'queue: {queue_name} has been disabled and cannot accept new job submissions.',
                )

            job_params = {**old_job_params, **job_params}
            self._job_builder = SocaJobBuilder(
                context=self.app_context, params=job_params, queue_profile=queue_profile
            )

            self._job = self.event.as_soca_job(
                context=self.app_context,
                queue_profile=queue_profile,
                job_builder=self.job_builder,
            )
            self._job.queue = queue_name

            if Utils.is_empty(project_name):
                project = self.app_context.projects_client.get_project_by_id(
                    queue_profile.projects[0].project_id
                )
                project_name = project.name

            self._job.project = project_name

            dry_run = self.dry_run_option()
            if dry_run is not None and dry_run == DryRunOption.DEBUG:
                self._job.debug = True

            self._job.job_id = 'tbd'
            self._job.job_uid = job_uid

            self._job_validation_result: Optional[JobValidationResult] = None
            self._job_validation_result = self.job_builder.validate()

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
                            message = f'Following AWS Service Quota needs to be requested from AWS: {os.linesep}'
                            table = self.get_quotas_as_table(not_requested)
                            message += str(table)
                            message += f'{os.linesep} Please contact administrator to request these AWS Service Quotas.'
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

            # check ec2 instance dry run - only if ephemeral capacity.
            # do not check dry run for batch/job-shared or always on nodes
            # job-shared is skipped as ec2 dry run can cause significant performance impact when 100s of jobs are submitted in batch
            try:
                if self.job.is_ephemeral_capacity():
                    provisioning_util.ec2_dry_run()
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.EC2_DRY_RUN_FAILED:
                    self.add_incidentals_validation_entry(
                        error_code='EC2DryRunFailed', message=e.message
                    )

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
