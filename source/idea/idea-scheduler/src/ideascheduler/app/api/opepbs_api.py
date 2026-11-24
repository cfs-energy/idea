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

from ideascheduler.app.accounts_guard import require_enabled
from ideascheduler.app.api.scheduler_api import maintenance_message, maintenance_exempt
from ideascheduler.app.job_messages import JOB_INTERNAL_ERROR_MESSAGE
import ideascheduler

from ideadatamodel import exceptions, errorcodes, constants
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookResult
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideasdk.utils import Utils
from ideasdk.api import BaseAPI, ApiInvocationContext

import os
from typing import Dict, Optional

from ideascheduler.app.scheduler.pbs_select import select_compute_node


def bound_to_capacity(pbs_context) -> bool:
    """a modifyjob of a job whose node selector already names its capacity"""
    if pbs_context.event.type != 'modifyjob' or pbs_context.event.job_o is None:
        return False
    select = (pbs_context.event.job_o.Resource_List or {}).get('select') or ''
    return 'compute_node=' in select and 'compute_node=tbd' not in select


def bound_compute_node(pbs_context) -> Optional[str]:
    select = (pbs_context.event.job_o.Resource_List or {}).get('select') or ''
    for token in select.replace('+', ':').split(':'):
        if token.startswith('compute_node='):
            return token.split('=', 1)[1]
    return None


def edited_job_id(pbs_context) -> Optional[str]:
    """the PBS id of the job an edit event is for, as the job cache keys it"""
    for pbs_job in (pbs_context.event.job_o, pbs_context.event.job):
        job_id = (
            str(pbs_job.id).split('.')[0] if pbs_job is not None and pbs_job.id else ''
        )
        if job_id and job_id.lower() != 'tbd':
            return job_id
    return None


def edit_select(pbs_context, compute_node: str) -> Optional[str]:
    """
    the selector to write, or None to leave the stored one exactly as it is. a modifyjob
    writes one only when the edit sets select, as given, naming compute_node: the edit
    object holds only what changed, and a selector rebuilt from it from the node count
    dropped every chunk's CPUs, memory and MPI ranks (rc19: '1:ncpus=1' became '1')
    """
    if pbs_context.event.type != 'modifyjob':
        return pbs_context.job_select(compute_node)
    new_select = (pbs_context.event.job.Resource_List or {}).get('select')
    if not new_select:
        return None
    return select_compute_node(new_select, compute_node)


def pinned_fsx_lustre(job) -> Dict[str, str]:
    """
    a queue's default existing FSx is resolved here, at submission, and written to the
    job, so later reads show and price the file system the job really mounts.
    """
    fsx_lustre = job.params.fsx_lustre
    if fsx_lustre is not None and fsx_lustre.enabled and fsx_lustre.existing_fsx:
        return {'fsx_lustre': fsx_lustre.existing_fsx}
    return {}


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

    def _fits_persistent_pool(self, job) -> bool:
        """
        a keep-forever job is placed on the queue's persistent pool at once, and the pool's
        nodes hold the instance profile it was provisioned with: a job of another project
        would run under that project's role (SCH-03). a pool built before the tag was written
        (an earlier release) is read from the job recorded in its template, once per
        stack. a pool not built yet, or one whose recorded job cannot be read, is accepted
        as before.
        """
        stack_name = job.get_compute_stack()
        stack = self.context.aws_util().cloudformation_describe_stack(
            stack_name=stack_name
        )
        if stack is None:
            return True
        pool_profile = stack.get_tag(constants.IDEA_TAG_INSTANCE_PROFILE)
        if pool_profile is None:
            pool_profile = self._recorded_pool_profile(stack_name)
        return pool_profile is None or pool_profile == job.params.instance_profile

    def _recorded_pool_profile(self, stack_name: str) -> Optional[str]:
        # shortcut: cached for the process; a pool rebuilt keeps its name but gets the tag,
        # so the cache is only read for untagged pools
        cache = self.__dict__.setdefault('_pool_profiles', {})
        if stack_name not in cache:
            try:
                recorded = self.context.aws_util().get_soca_job_from_stack(
                    stack_name=stack_name
                )
            except Exception as e:
                self.logger.warning(f'pool {stack_name}: recorded job not read: {e}')
                return None
            cache[stack_name] = (
                recorded.params.instance_profile
                if recorded is not None and recorded.params is not None
                else None
            )
        return cache[stack_name]

    def hook_validate_job(self, pbs_context: OpenPBSAPIInvocationContext):
        # Read the subscribed config on every submission; modify/move events keep draining.
        if pbs_context.event.type == 'queuejob' and self.context.config().get_bool(
            'cluster-manager.maintenance.enabled', default=False
        ):
            # The socket caller is always root. Exempt the PBS submitter, not the transport.
            requestor = pbs_context.event.requestor
            if not maintenance_exempt(self.context, requestor, self.logger):
                return pbs_context.api_context.success(
                    OpenPBSHookResult(
                        accept=False,
                        formatted_user_message=maintenance_message(
                            self.context.config()
                        ),
                    )
                )
        try:
            if not self.context.is_ready():
                raise exceptions.soca_exception(
                    error_code=errorcodes.JOB_PROVISIONING_NOT_READY,
                    message='IDEA job provisioning is not yet ready. Please try again later.',
                )

            pbs_context.build_and_validate_job()
            require_enabled(self.context, pbs_context.job.owner)

            if pbs_context.is_valid():
                pbs_context.check_incidentals()
                pbs_context.job_submission_result.incidentals = (
                    pbs_context.incidentals_validation_result
                )

            if not pbs_context.is_valid():
                formatted_user_message = f"This job can't be queued:{os.linesep}"

                if not pbs_context.job_validation_result:
                    formatted_user_message += f'{pbs_context.job_validation_result}'

                if not pbs_context.incidentals_validation_result:
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

            if pbs_context.is_dry_run():
                # a dry run reports and queues nothing, on persistent capacity too
                pbs_context.job_submission_result.accepted = False
                pbs_context.api_context.success(pbs_context.dry_run_post_processing())

            elif Utils.is_true(
                job.provisioning_options.keep_forever
            ) and not self._fits_persistent_pool(job):
                message = (
                    f"This job can't be queued: the persistent nodes of queue {job.queue} "
                    f'run under another instance profile than this job would, so they '
                    f'belong to another project. Ask an administrator for a queue for '
                    f'your project.'
                )
                pbs_context.job_submission_result.accepted = False
                pbs_context.api_context.success(
                    OpenPBSHookResult(accept=False, formatted_user_message=message)
                )

            elif Utils.is_true(job.provisioning_options.keep_forever):
                select = edit_select(pbs_context, job.get_compute_stack())

                # accepted must be True here, as below: an unset value rolls back the
                # dedupe key and a retried submission queues a second copy
                pbs_context.job_submission_result.accepted = True

                self.context.job_monitor.job_queued(job=job)

                pbs_context.api_context.success(
                    OpenPBSHookResult(
                        accept=True,
                        queue=job.queue,
                        project=job.project,
                        formatted_user_message=None,
                        resources_updated={
                            **({'select': select} if select else {}),
                            'job_uid': job.job_uid,
                            'job_group': job.get_job_group(),
                            'cluster_name': job.cluster_name,
                            'nodes': None,
                            **pinned_fsx_lustre(job),
                        },
                    )
                )

            else:
                pbs_context.get_bom_cost()
                pbs_context.get_budget_usage()

                # accepted must be True here: a None left commit() a no-op,
                # defeating client_submission_id dedupe, and misreported the result.
                pbs_context.job_submission_result.accepted = True

                resources_updated = {
                    'job_uid': job.job_uid,
                    'job_group': job.get_job_group(),
                    'cluster_name': job.cluster_name,
                    'nodes': None,
                    **pinned_fsx_lustre(job),
                }
                if getattr(pbs_context, 'existing_uid_unknown', False):
                    # never a new uid for an existing job
                    resources_updated.pop('job_uid')
                if bound_to_capacity(pbs_context):
                    # an edit of a job already given capacity keeps its node selector and
                    # stack: writing tbd unbound it from a stack that stayed up (PBS-13).
                    # a new select keeps the stack's node (A-S1), and the cache is re-read
                    # under the job's real id, not the hook's placeholder (SJ-36, A-S2)
                    new_select = (pbs_context.event.job.Resource_List or {}).get(
                        'select'
                    )
                    if new_select:
                        resources_updated['select'] = select_compute_node(
                            new_select, bound_compute_node(pbs_context)
                        )
                    self.context.job_monitor.job_modified(
                        job=job.model_copy(
                            update={'job_id': edited_job_id(pbs_context)}
                        )
                    )
                else:
                    select = edit_select(pbs_context, 'tbd')
                    if select:
                        resources_updated['select'] = select
                    resources_updated['stack_id'] = 'tbd'
                    if pbs_context.event.type == 'movejob':
                        try:
                            self._cache_moved_job(pbs_context, job)
                        except Exception as e:
                            # the move stands: the next read of the job corrects the cache,
                            # and the source profile's queue skips a job no longer its own
                            self.logger.warning(
                                f'moved job not re-cached under queue {job.queue}: {e}'
                            )
                    # signal the job monitor as late as possible: the job is not
                    # visible in the scheduler until this hook response is accepted
                    self.context.job_monitor.job_queued(job=job)

                pbs_context.api_context.success(
                    OpenPBSHookResult(
                        accept=True,
                        queue=job.queue,
                        project=job.project,
                        formatted_user_message=None,
                        resources_updated=resources_updated,
                    )
                )

            self.context.job_submission_tracker.ok(pbs_context.job_submission_result)

        except Exception as e:
            if isinstance(e, exceptions.SocaException):
                message = e.message
            else:
                self.logger.exception(f'job validation failed: {e}')
                message = JOB_INTERNAL_ERROR_MESSAGE
            pbs_context.api_context.success(
                OpenPBSHookResult(accept=False, formatted_user_message=message)
            )
            self.context.job_submission_tracker.fail(pbs_context.job_uid, e)

    def _cache_moved_job(self, pbs_context: OpenPBSAPIInvocationContext, job):
        """
        a moved job is cached under its new queue and leaves the old queue's provisioning
        queue: the cache kept the source queue, so the job was queued for provisioning under
        the source profile again, its limits and scaling mode applied, and once read under
        its new queue it could be provisioned a second time by the destination's
        """
        job_id = edited_job_id(pbs_context)
        if not job_id:
            return
        cached = self.context.job_cache.get_job(job_id)
        moved = job.model_copy(update={'job_id': job_id})
        if cached is not None and cached.queue_type not in (None, moved.queue_type):
            source = self.context.queue_profiles.get_provisioning_queue(
                queue_profile_name=cached.queue_type
            )
            if source is not None:
                source.delete(job_id=job_id)
        self.context.job_cache.sync(jobs=[moved])

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

    def instance_type_info(self, context: ApiInvocationContext):
        """
        Compatibility endpoint for older installed hooks; reads the instance type cache.
        """
        payload = Utils.get_value_as_dict('payload', context.get_request(), {})
        instance_type = Utils.get_value_as_string('instance_type', payload)
        if Utils.is_empty(instance_type):
            raise exceptions.invalid_params('instance_type is required')
        ec2_instance_type = self.context.aws_util().get_ec2_instance_type(instance_type)
        info = {
            'instance_type': instance_type,
            'vcpus': ec2_instance_type.vcpu_info_default_vcpus,
            'cores': ec2_instance_type.vcpu_info_default_cores,
            'threads_per_core': ec2_instance_type.vcpu_info_default_threads_per_core,
        }
        if not all(info.values()):
            raise exceptions.general_exception(
                f'no CPU information for instance type {instance_type}'
            )
        context.success(info)

    def invoke(self, context: ApiInvocationContext):
        # only root/administrators invoke this: openpbs (as root) calls /run/idea.sock.
        # job_status events reach us from mom hooks on compute nodes via the SQS queue.

        if not context.is_administrator():
            raise exceptions.unauthorized_access()

        if context.namespace == 'OpenPBSHook.InstanceTypeInfo':
            return self.instance_type_info(context)

        pbs_context = OpenPBSAPIInvocationContext(context=context)
        if pbs_context.event.hook_name == 'validate_job':
            return self.hook_validate_job(pbs_context)
        elif pbs_context.event.hook_name == 'job_status':
            return self.hook_job_status(pbs_context)
