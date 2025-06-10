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

from ideadatamodel import exceptions, errorcodes, constants
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookResult
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideasdk.utils import Utils
from ideasdk.api import BaseAPI, ApiInvocationContext

import os


class OpenPBSAPI(BaseAPI):
    """
    OpenPBS API - implementation can be OpenPBS specific.
    """

    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger('api_openpbs')
        self.check_openpbs()

    def check_openpbs(self):
        current_scheduler = self.context.config().get_string(
            'scheduler.provider', required=True
        )
        if constants.SCHEDULER_OPENPBS != current_scheduler:
            raise exceptions.SocaException(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'openpbs is not supported. Current scheduler is: ({current_scheduler})',
            )

    def hook_validate_job(self, pbs_context: OpenPBSAPIInvocationContext):
        try:
            if not self.context.is_ready():
                raise exceptions.soca_exception(
                    error_code=errorcodes.JOB_PROVISIONING_NOT_READY,
                    message='IDEA job provisioning is not yet ready. Please try again later.',
                )

            pbs_context.build_and_validate_job()

            if pbs_context.is_valid():
                pbs_context.check_incidentals()
                pbs_context.job_submission_result.incidentals = (
                    pbs_context.incidentals_validation_result
                )

            if not pbs_context.is_valid():
                formatted_user_message = f'Job submission failed and cannot be queued: {os.linesep}{os.linesep}'

                if not pbs_context.job_validation_result:
                    formatted_user_message += f'* Below parameters failed to be validated: {os.linesep}{os.linesep}'
                    formatted_user_message += f'{pbs_context.job_validation_result}'
                    formatted_user_message += f'{os.linesep}'

                if not pbs_context.incidentals_validation_result:
                    formatted_user_message += (
                        f'* Job will not be provisioned due to below errors: {os.linesep}'
                        f'{os.linesep}'
                    )
                    formatted_user_message += (
                        f'{pbs_context.incidentals_validation_result}'
                    )

                pbs_context.job_submission_result.accepted = False
                pbs_context.api_context.success(
                    OpenPBSHookResult(
                        accept=False, formatted_user_message=formatted_user_message
                    )
                )
                self.context.job_submission_tracker.ok(
                    pbs_context.job_submission_result
                )
                return

            job = pbs_context.job

            if Utils.is_true(job.provisioning_options.keep_forever):
                select = Utils.get_value_as_string(
                    'select', job.params.custom_params, ''
                )
                select = select.split(':compute_node')[0]
                if Utils.is_not_empty(select):
                    select += f':compute_node={job.get_compute_stack()}'
                else:
                    select = f'{job.desired_nodes()}:ncpus=1:compute_node={job.get_compute_stack()}'

                self.context.job_monitor.job_queued(job=job)

                pbs_context.api_context.success(
                    OpenPBSHookResult(
                        accept=True,
                        queue=job.queue,
                        project=job.project,
                        formatted_user_message=None,
                        resources_updated={
                            'select': select,
                            'job_uid': job.job_uid,
                            'job_group': job.get_job_group(),
                            'cluster_name': job.cluster_name,
                            'nodes': None,
                        },
                    )
                )

            else:
                if pbs_context.is_dry_run():
                    pbs_context.job_submission_result.accepted = False
                    pbs_context.api_context.success(
                        pbs_context.dry_run_post_processing()
                    )

                else:
                    job = pbs_context.job

                    self.context.job_monitor.job_queued(job=job)

                    select = Utils.get_value_as_string(
                        'select', job.params.custom_params, ''
                    )
                    select = select.split(':compute_node')[0]
                    if Utils.is_not_empty(select):
                        select += ':compute_node=tbd'
                    else:
                        select = f'{job.desired_nodes()}:ncpus=1:compute_node=tbd'

                    pbs_context.get_bom_cost()
                    pbs_context.get_budget_usage()

                    pbs_context.api_context.success(
                        OpenPBSHookResult(
                            accept=True,
                            queue=job.queue,
                            project=job.project,
                            formatted_user_message=None,
                            resources_updated={
                                'select': select,
                                'stack_id': 'tbd',
                                'job_uid': job.job_uid,
                                'job_group': job.get_job_group(),
                                'cluster_name': job.cluster_name,
                                'nodes': None,
                            },
                        )
                    )

            self.context.job_submission_tracker.ok(pbs_context.job_submission_result)

        except Exception as e:
            if isinstance(e, exceptions.SocaException):
                message = e.message
            else:
                self.logger.exception(f'job validation failed: {e}')
                message = str(
                    f'Something went wrong while validating the job. Please contact '
                    f'administrator to investigate the problem. {os.linesep}'
                    f'Error: {e}'
                )
            pbs_context.api_context.success(
                OpenPBSHookResult(accept=False, formatted_user_message=message)
            )
            self.context.job_submission_tracker.fail(pbs_context.job_uid, e)

    def hook_job_status(self, pbs_context: OpenPBSAPIInvocationContext):
        try:
            queue = pbs_context.event.job.queue
            queue_profile = self.context.queue_profiles.get_queue_profile(
                queue_name=queue
            )
            job = pbs_context.event.as_soca_job(
                context=self.context, queue_profile=queue_profile
            )

            if pbs_context.event.type == 'runjob':
                self.context.job_monitor.job_running(job=job)
            else:
                self.context.job_monitor.job_status_update(job=job)

        except Exception as e:
            self.logger.exception(f'failed to process job status hook: {e}')
        finally:
            # always return success to OpenPBS irrespective of the error.
            pbs_context.api_context.success(
                OpenPBSHookResult(accept=True, formatted_user_message=None)
            )

    def invoke(self, context: ApiInvocationContext):
        # this API can be invoked only via root users or administrators.
        # since openpbs runs as root, the hooks executed on scheduler can invoke the unix socket on /run/idea.sock
        # job_status events are executed via job_monitor and are published by mom hooks executed on compute nodes
        # the execution hooks are sent to the job status events SQS queue.

        if not context.is_administrator():
            raise exceptions.unauthorized_access()

        pbs_context = OpenPBSAPIInvocationContext(context=context)
        if pbs_context.event.hook_name == 'validate_job':
            return self.hook_validate_job(pbs_context)
        elif pbs_context.event.hook_name == 'job_status':
            return self.hook_job_status(pbs_context)
