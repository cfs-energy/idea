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

from ideadatamodel import (
    constants,
    exceptions,
    errorcodes,
    SocaBaseModel,
    SocaJob,
    SocaJobState,
    SocaScalingMode,
    HpcQueueProfile,
    ProvisioningStatus,
    ProvisioningCapacityInfo,
    CloudFormationStack,
    CloudFormationStackResources,
    EC2SpotFleetRequestConfig,
    AutoScalingGroup,
    CheckServiceQuotaResult,
)
from ideasdk.utils import Utils
from ideasdk.aws.ec2_instance_types_db import TRANSIENT_EC2_ERROR_CODES
from ideasdk.utils.error_redaction import redact_aws_identifiers

from ideascheduler.app.aws import EC2ServiceQuotaHelper, AwsBudgetsHelper
from ideascheduler.app.bedrock_job_access import (
    BedrockJobAccessResolver,
    BedrockJobAccessState,
    is_enabled_for_jobs,
)
from ideascheduler.app.provisioning.job_provisioner.cloudformation_stack_builder import (
    build_efa_network_interface_shapes,
)
from ideascheduler.app.provisioning.job_provisioner.batch_capacity_helper import (
    BatchCapacityHelper,
)
from pydantic import Field
from botocore.exceptions import ClientError
from typing import Dict, Optional, List
import arrow
import os
import logging


def _is_transient_ec2_error(exc: BaseException = None) -> bool:
    """True when EC2 refused the call itself - a throttle, an outage, an auth failure."""
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get('Error', {}).get('Code') in TRANSIENT_EC2_ERROR_CODES


# auto-scaling activity status codes that mean the group could not launch the node.
ASG_FAILED_ACTIVITY_STATUS_CODES = ('Failed', 'Cancelled')


class NotEnoughReservedInstances(SocaBaseModel):
    instance_type: Optional[str] = Field(default=None)
    purchased_count: Optional[int] = Field(default=None)
    instances_count: Optional[int] = Field(default=None)
    desired_count: Optional[int] = Field(default=None)


class ProvisionCapacityResult(SocaBaseModel):
    provisioned_jobs: Optional[List[SocaJob]] = Field(default=None)
    unprovisioned_jobs: Optional[List[SocaJob]] = Field(default=None)
    capacity_info: Optional[ProvisioningCapacityInfo] = Field(default=None)


class JobProvisioningUtil:
    """
    This class is not thread safe.
    """

    def __init__(
        self,
        context: ideascheduler.AppContext,
        jobs: List[SocaJob],
        logger: logging.Logger = None,
    ):
        self.context = context
        if logger is None:
            self._logger = context.logger()
        else:
            self._logger = logger

        self._jobs = jobs

        self._stack: Optional[CloudFormationStack] = None
        self._stack_resources: Optional[CloudFormationStackResources] = None
        self._spot_fleet: Optional[EC2SpotFleetRequestConfig] = None
        self._auto_scaling_group: Optional[AutoScalingGroup] = None

    @property
    def config(self):
        return self.context.config()

    @property
    def job(self) -> SocaJob:
        return self._jobs[0]

    @property
    def jobs(self) -> List[SocaJob]:
        return self._jobs

    @property
    def is_batch(self) -> bool:
        return self.job.scaling_mode == SocaScalingMode.BATCH

    @property
    def queue(self) -> str:
        return self.job.queue

    @property
    def queue_profile(self) -> HpcQueueProfile:
        return self.context.queue_profiles.get_queue_profile(queue_name=self.job.queue)

    @property
    def max_provisioned_capacity(self) -> int:
        queue_params = self.queue_profile.queue_management_params
        return Utils.get_as_int(queue_params.max_provisioned_capacity, 0)

    @property
    def max_provisioned_instances(self) -> int:
        queue_params = self.queue_profile.queue_management_params
        return Utils.get_as_int(queue_params.max_provisioned_instances, 0)

    @property
    def stack_provisioning_timeout_secs(self) -> int:
        return self.config.get_int(
            'scheduler.job_provisioning.stack_provisioning_timeout_seconds',
            default=1800,
        )

    @property
    def max_provisioning_retries(self) -> int:
        return self.config.get_int(
            'scheduler.job_provisioning.max_provisioning_retries',
            default=3,
        )

    def track_provisioning_failure(self, job: SocaJob, message: str) -> bool:
        """
        increment the job's persistent provisioning retry count.

        when the count reaches scheduler.job_provisioning.max_provisioning_retries, the job
        is held with the failure reason recorded and True is returned. a held job must not be
        re-queued for provisioning; the owner can qdel and resubmit, or qrls to retry.
        a cap of 0 or less disables the hold: the job is retried indefinitely.
        """
        retry_count = self.context.job_cache.increment_job_provisioning_retry(
            job_id=job.job_id
        )
        max_retries = self.max_provisioning_retries
        capped = max_retries is not None and max_retries > 0
        if not capped or retry_count < max_retries:
            budget = f' of {max_retries}' if capped else ''
            self._logger.info(
                f'{job.log_tag} provisioning failed '
                f'(attempt {retry_count}{budget}): {message}'
            )
            return False

        reason = (
            f'Job held: provisioning failed {retry_count} times. '
            f'Last error: {message} '
            f'Use qdel to delete the job and resubmit with corrected parameters, '
            f'or contact your administrator.'
        )
        # set_job_provisioning_error publishes the reason via the gated path; no direct qalter
        # here (a raw message with commas breaks qalter's resource-list parsing).
        self.context.job_cache.set_job_provisioning_error(
            job_id=job.job_id,
            error_code=errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED,
            message=reason,
        )

        lifecycle_events = self.context.lifecycle_events
        if lifecycle_events is not None:
            lifecycle_events.stack_failed(
                job=job,
                error_code=errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED,
                message=reason,
                attempt_number=retry_count,
            )

        held = False
        try:
            self.context.scheduler.hold_job(job_id=job.job_id)
            # update the cache immediately so housekeeping and provisioning cycles
            # do not re-queue the job before the next scheduler refresh.
            job.state = SocaJobState.HELD
            self.context.job_cache.sync(jobs=[job])
            held = True
        except exceptions.SocaException as e:
            self._logger.error(f'{job.log_tag} failed to hold job: {e.message}')

        # emitted even when the hold failed: the job is excluded from re-queue either way, so
        # a missing event would leave it invisible. hold_failed flags it wasn't actually held.
        if lifecycle_events is not None:
            lifecycle_events.job_held(
                job=job,
                attempt_number=retry_count,
                error_code=errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED,
                message=reason,
                hold_failed=not held,
            )

        # replace the stale 'provisioning (attempt N)' comment so qstat -s doesn't advertise
        # progress on a job now held for good; sanitized and never aborts the flow (no commas).
        try:
            self.context.scheduler.set_job_comment(
                job_id=job.job_id,
                comment=f'IDEA: provisioning failed {retry_count} times - job held. '
                f'See error_message in qstat -f or contact your administrator',
            )
        except Exception as e:
            self._logger.warning(f'{job.log_tag} failed to set held-job comment: {e}')

        self._logger.warning(
            f'{job.log_tag} provisioning retries exhausted after {retry_count} attempts. '
            f'job held and will not be re-queued. last error: {message}'
        )
        return True

    @property
    def aws_util(self):
        return self.context.aws_util()

    @property
    def instance_cache(self):
        return self.context.instance_cache

    @property
    def stack_name(self) -> str:
        return self.job.get_compute_stack()

    @property
    def stack(self) -> CloudFormationStack:
        if self._stack is None:
            self._stack = self.aws_util.cloudformation_describe_stack(
                stack_name=self.stack_name
            )
        return self._stack

    @property
    def stack_resources(self) -> CloudFormationStackResources:
        if self._stack_resources is None:
            self._stack_resources = (
                self.aws_util.cloudformation_describe_stack_resources(
                    stack_name=self.stack_name
                )
            )
        return self._stack_resources

    @property
    def spot_fleet(self) -> Optional[EC2SpotFleetRequestConfig]:
        if self._spot_fleet is not None:
            return self._spot_fleet

        spot_fleet_request_id = self.stack_resources.get_spot_fleet_request_id()

        if spot_fleet_request_id is None:
            return None

        self._spot_fleet = self.aws_util.ec2_describe_spot_fleet_request(
            spot_fleet_request_id=spot_fleet_request_id
        )
        return self._spot_fleet

    @property
    def auto_scaling_group(self) -> Optional[AutoScalingGroup]:
        if self._auto_scaling_group is not None:
            return self._auto_scaling_group

        auto_scaling_group_name = self.stack_resources.get_auto_scaling_group_name()

        if auto_scaling_group_name is None:
            return None

        self._auto_scaling_group = (
            self.aws_util.autoscaling_describe_auto_scaling_group(
                auto_scaling_group_name=auto_scaling_group_name
            )
        )
        return self._auto_scaling_group

    @property
    def is_spot_fleet(self) -> bool:
        return self.job.is_spot_capacity()

    def can_update_spot_fleet_capacity(self) -> bool:
        status = self.spot_fleet.activity_status
        if status not in ('fulfilled', 'pending_fulfillment'):
            return False

        state = self.spot_fleet.spot_fleet_request_state
        if state != 'active':
            return False

        return True

    @property
    def provisioned_capacity(self) -> int:
        if self.is_spot_fleet:
            if self.stack is not None:
                return self.spot_fleet.target_capacity
        else:
            if self.stack is not None:
                return self.auto_scaling_group.desired_capacity
        return 0

    def update_capacity(self) -> ProvisionCapacityResult:
        job = self.job

        result = BatchCapacityHelper(
            context=self.context,
            jobs=self.jobs,
            provisioned_capacity=self.provisioned_capacity,
        ).invoke()
        provisioned_jobs = result.provisioned_jobs
        unprovisioned_jobs = result.unprovisioned_jobs
        capacity = result.capacity_info

        if capacity.target_capacity <= 0:
            # no change in target capacity, re-use existing capacity
            return ProvisionCapacityResult(
                provisioned_jobs=provisioned_jobs,
                unprovisioned_jobs=unprovisioned_jobs,
                capacity_info=capacity,
            )

        if job.is_spot_capacity():
            if not self.can_update_spot_fleet_capacity():
                raise exceptions.SocaException(
                    error_code=errorcodes.SPOT_FLEET_CAPACITY_UPDATE_IN_PROGRESS,
                    message='Spot fleet capacity cannot can not be updated at this time. '
                    'Provisioning will be retried.',
                )

            self.aws_util.ec2_modify_spot_fleet_request(
                spot_fleet_request_id=self.stack_resources.get_spot_fleet_request_id(),
                target_capacity=capacity.target_capacity,
            )

        else:
            # todo: handle scenario when a new shared job requested for spot capacity to be increased.
            self.aws_util.autoscaling_update_auto_scaling_group(
                auto_scaling_group_name=self.stack_resources.get_auto_scaling_group_name(),
                desired_capacity=capacity.target_capacity,
            )

        return ProvisionCapacityResult(
            provisioned_jobs=provisioned_jobs,
            unprovisioned_jobs=unprovisioned_jobs,
            capacity_info=capacity,
        )

    def is_provisioning_timeout(self) -> bool:
        creation_time = self.stack.creation_time
        if creation_time is None:
            return False
        delta = arrow.utcnow() - self.stack.creation_time
        return delta.total_seconds() > self.stack_provisioning_timeout_secs

    def is_stack_a_shared_resource(self):
        if self.stack.soca_keep_forever:
            return True
        if self.stack.soca_terminate_when_idle > 0:
            return True
        return False

    def get_capacity_failure_reason(self) -> Optional[str]:
        """
        why the compute stack has no node yet, taken from the most recent failed
        auto-scaling activity.

        an insufficient-capacity rejection is reported only on the activity: the group
        keeps retrying and cloudformation waits out its whole stabilization window
        before failing, so this is the only source for a job that is still waiting.

        enrichment only - returns None when the reason cannot be read.
        """
        try:
            auto_scaling_group_name = self.stack_resources.get_auto_scaling_group_name()
            if auto_scaling_group_name is None:
                return None

            activities = self.aws_util.autoscaling_describe_scaling_activities(
                auto_scaling_group_name=auto_scaling_group_name
            )
            for activity in activities:
                status_code = Utils.get_value_as_string('StatusCode', activity)
                if status_code not in ASG_FAILED_ACTIVITY_STATUS_CODES:
                    continue
                reason = Utils.get_value_as_string('StatusMessage', activity)
                if Utils.is_empty(reason):
                    reason = Utils.get_value_as_string('Description', activity)
                if Utils.is_empty(reason):
                    continue
                return reason
            return None
        except Exception as e:
            self._logger.warning(
                f'{self.job.log_tag} failed to read scaling activities for '
                f'ComputeStack: {self.stack_name}: {e}'
            )
            return None

    def check_status(self) -> Optional[ProvisioningStatus]:
        """
        check and return provisioning status

        :return: int status
        0: PROVISIONING_STATUS_NOT_PROVISIONED
        1: PROVISIONING_STATUS_COMPLETED
        2: PROVISIONING_STATUS_IN_PROGRESS
        3: PROVISIONING_STATUS_FAILED
        4: PROVISIONING_STATUS_TIMEOUT

        None is returned for a stack status this method does not map. callers must treat
        it as unknown and leave the stack and the job alone.
        """

        try:
            stack = self.stack
            if stack is None:
                return ProvisioningStatus.NOT_PROVISIONED

            stack_status = self.stack.stack_status

            if stack_status == 'CREATE_COMPLETE':
                # if is keep forever or terminate when idle, stack becomes a shared resource,
                # and provisioning should not timeout.
                if self.is_stack_a_shared_resource():
                    return ProvisioningStatus.COMPLETED

                else:
                    if self.is_provisioning_timeout():
                        return ProvisioningStatus.TIMEOUT

                    else:
                        return ProvisioningStatus.COMPLETED

            elif stack_status == 'CREATE_IN_PROGRESS':
                return ProvisioningStatus.IN_PROGRESS

            elif stack_status in ['DELETE_IN_PROGRESS', 'ROLLBACK_IN_PROGRESS']:
                # a rollback is the failed create tearing itself down. wait for it to
                # settle on ROLLBACK_COMPLETE, which reports FAILED below.
                return ProvisioningStatus.DELETE_IN_PROGRESS

            elif stack_status in [
                'CREATE_FAILED',
                'ROLLBACK_COMPLETE',
                'ROLLBACK_FAILED',
                'DELETE_FAILED',
            ]:
                # terminal and unusable: these can only be deleted, never updated, so
                # the job cannot provision again until the stack is gone.
                return ProvisioningStatus.FAILED

        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ValidationError':
                return ProvisioningStatus.NOT_PROVISIONED
            else:
                raise exc

    def ec2_dry_run_request(self, instance_type: str) -> Dict:
        """
        the RunInstances request used for the pre-provisioning dry run.

        the network shape must mirror what CloudFormationStackBuilder.build_launch_template()
        emits, otherwise the dry run validates a launch that is never attempted. with EFA the
        launch template replaces SecurityGroupIds with InterfaceType='efa' network
        interfaces built from the shared build_efa_network_interface_shapes(), so a
        multi-rail launch (scheduler.efa.multi_rail_enabled) is dry-run with the same
        interface list; RunInstances rejects a top level SubnetId/SecurityGroupIds alongside
        NetworkInterfaces, so the subnet and groups move onto the interfaces.
        """
        request = {
            'ImageId': self.job.params.instance_ami,
            'InstanceType': instance_type,
            'MaxCount': self.job.params.nodes,
            'MinCount': self.job.params.nodes,
            'BlockDeviceMappings': [
                {
                    'DeviceName': Utils.get_ec2_block_device_name(
                        base_os=self.job.params.base_os
                    ),
                    'Ebs': {
                        'Encrypted': constants.DEFAULT_VOLUME_ENCRYPTION_COMPUTE,
                        'VolumeType': constants.DEFAULT_VOLUME_TYPE_COMPUTE,
                        'DeleteOnTermination': constants.DEFAULT_KEEP_EBS_VOLUMES,
                    },
                }
            ],
            'DryRun': True,
        }

        if Utils.get_as_bool(self.job.params.enable_efa_support, False):
            network_interfaces = build_efa_network_interface_shapes(
                context=self.context,
                instance_type_names=self.job.params.instance_types,
                security_groups=self.job.params.security_groups,
                log_tag=self.job.log_tag,
            )
            for network_interface in network_interfaces:
                network_interface['SubnetId'] = self.job.params.subnet_ids[0]
            request['NetworkInterfaces'] = network_interfaces
        else:
            request['SubnetId'] = self.job.params.subnet_ids[0]
            request['SecurityGroupIds'] = self.job.params.security_groups

        return request

    def ec2_dry_run(self):
        for instance_type in self.job.params.instance_types:
            try:
                self.context.aws().ec2().run_instances(
                    **self.ec2_dry_run_request(instance_type=instance_type)
                )
            except ClientError as e:
                if e.response['Error'].get('Code') == 'DryRunOperation':
                    pass
                else:
                    raise exceptions.SocaException(
                        error_code=errorcodes.EC2_DRY_RUN_FAILED,
                        message=f'EC2 dry run failed for instance_type: {instance_type}, '
                        f'Err: {redact_aws_identifiers(e)}',
                        ref=e,
                    )

    def ec2_dry_run_cached(self):
        """
        ec2 dry run for job-shared/batch queues.
        a live dry run is executed once per job group signature per TTL window and the
        outcome is cached, so batches of submissions do not flood the EC2 API.
        """
        cache_ttl_seconds = self.config.get_int(
            'scheduler.job_provisioning.dry_run_cache_ttl_seconds',
            default=300,
        )
        if cache_ttl_seconds <= 0:
            self.ec2_dry_run()
            return

        # key on capacity signature, not get_job_group(): a custom job_group can collide across
        # jobs with different AMI/subnet/instance types and would share one cached verdict.
        cache_key = f'scheduler.job_provisioning.ec2_dry_run.{self.job.get_capacity_signature()}'
        cache = self.context.cache().short_term()
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            error_message = Utils.get_value_as_string('error_message', cached_result)
            if error_message is None:
                return
            raise exceptions.SocaException(
                error_code=errorcodes.EC2_DRY_RUN_FAILED,
                message=error_message,
            )

        try:
            self.ec2_dry_run()
        except exceptions.SocaException as e:
            # a throttle or an auth blip describes this call, not the capacity. the key
            # carries no owner, so caching one would reject every other submitter too.
            if (
                e.error_code == errorcodes.EC2_DRY_RUN_FAILED
                and not _is_transient_ec2_error(e.ref)
            ):
                cache.set(
                    key=cache_key,
                    value={'error_message': e.message},
                    ttl=cache_ttl_seconds,
                )
            raise
        cache.set(key=cache_key, value={}, ttl=cache_ttl_seconds)

    def check_service_quota(self) -> CheckServiceQuotaResult:
        result = CheckServiceQuotaResult(quotas=[])
        enable_service_quota_check = self.context.config().get_bool(
            'scheduler.job_provisioning.service_quotas', default=True
        )
        if not enable_service_quota_check:
            return result

        if self.job.is_spot_capacity():
            desired_capacity = 0
            for job in self.jobs:
                desired_capacity += job.spot_nodes() * job.default_vcpus()

            quota = self.get_service_quota(
                instance_types=self.job.params.instance_types,
                quota_type=constants.EC2_SERVICE_QUOTA_SPOT,
                desired_capacity=desired_capacity,
            )
            result.quotas += quota.quotas

        elif self.job.is_mixed_capacity():
            desired_ondemand_capacity = 0
            for job in self.jobs:
                desired_ondemand_capacity += job.ondemand_nodes() * job.default_vcpus()

            quota = self.get_service_quota(
                instance_types=self.job.params.instance_types,
                quota_type=constants.EC2_SERVICE_QUOTA_ONDEMAND,
                desired_capacity=desired_ondemand_capacity,
            )
            result.quotas += quota.quotas

            desired_spot_capacity = 0
            for job in self.jobs:
                desired_spot_capacity += job.spot_nodes() * job.default_vcpus()
            quota = self.get_service_quota(
                instance_types=self.job.params.instance_types,
                quota_type=constants.EC2_SERVICE_QUOTA_SPOT,
                desired_capacity=desired_spot_capacity,
            )
            result.quotas += quota.quotas
        else:
            desired_capacity = 0
            for job in self.jobs:
                desired_capacity += job.ondemand_nodes() * job.default_vcpus()

            quota = self.get_service_quota(
                instance_types=self.job.params.instance_types,
                quota_type=constants.EC2_SERVICE_QUOTA_ONDEMAND,
                desired_capacity=desired_capacity,
            )
            result.quotas += quota.quotas

        if not result:
            raise exceptions.SocaException(
                error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE,
                message=f'service quota not available for instance_types: {self.job.params.instance_types}',
                ref=result,
            )

        return result

    def check_reserved_instance_usage(self):
        """
        for force reserved instances enforcement:
            - if multiple instances types are provided, all instance types should have the desired capacity purchased.

        IMPORTANT NOTE: When multiple jobs are being provisioned back to back, there is a possibility that this
        implementation will not enforce reservations. To enforce reservations, CapacityReservationResourceGroups must
        be used. See to do note below.

        See below link for more information:
        https://aws.amazon.com/about-aws/whats-new/2020/07/amazon-ec2-on-demand-capacity-reservations-now-support-group-targeting/

        TODO: Changes required in next release:
         > Automatically create a Capacity Reservation Group for SOCA Cluster during installation
         > Add the CapacityReservationResourceGroupArn to soca cluster config in AWS Secrets
         > Document steps and instruct cluster admins or AWS admins to add individual capacity reservations in the
            group
         > Update CloudFormationStackBuilder to include CapacityReservationResourceGroupArn if
            force reserved instances is enabled
        """

        force_ri = False
        for job in self.jobs:
            force_ri = job.params.force_reserved_instances
            if force_ri is None or force_ri is False:
                continue
            force_ri = True
            break

        if not force_ri:
            return

        ondemand_nodes = 0
        for job in self.jobs:
            ondemand_nodes += job.ondemand_nodes()

        if ondemand_nodes == 0:
            return

        instance_types = self.job.params.instance_types

        # TODO:
        #   > this implementation is incorrect as it does not check for InstanceTenancy, State, StartDate and
        #   Duration
        #   > update this implementation in the next release when support for reservation group targeting is added.
        #   > purchased RIs should be calculated only against SOCA cluster reservation group.
        purchased_ri_result = self.aws_util.ec2_describe_reserved_instances(
            instance_types=instance_types
        )

        not_purchased = []
        for instance_type in instance_types:
            if instance_type not in purchased_ri_result:
                not_purchased.append(instance_type)

        # enforces all instance types specified must have reserved capacity purchased
        if len(not_purchased) > 0:
            raise exceptions.SocaException(
                error_code=errorcodes.EC2_RESERVED_INSTANCES_NOT_PURCHASED,
                message=f'could not find any reservations purchased for instance types: [{", ".join(not_purchased)}]',
            )

        base_weight = self.job.default_instance_type_option.weighted_capacity

        not_enough_ri = []
        for option in self.job.provisioning_options.instance_types:
            instance_type = option.name
            purchased_count = purchased_ri_result[instance_type]

            # todo - weighted capacity can be used only if flexible ri is purchased.
            weighted_capacity = option.weighted_capacity

            instances = self.instance_cache.list_instances(
                instance_types=[instance_type], state=['pending', 'running']
            )

            total_running_instances = len(instances)

            desired_count = int(ondemand_nodes * (base_weight / weighted_capacity))
            desired_count = max(1, desired_count)

            if total_running_instances + desired_count > purchased_count:
                not_enough_ri.append(
                    NotEnoughReservedInstances(
                        instance_type=instance_type,
                        purchased_count=purchased_count,
                        instances_count=total_running_instances,
                        desired_count=desired_count,
                    )
                )

        if len(not_enough_ri) > 0:
            message = f'Not enough reserved capacity available: {os.linesep}'
            for entry in not_enough_ri:
                message += (
                    f'InstanceType: {entry.instance_type}, Purchased: {entry.purchased_count}, '
                    f'RunningInstances: {entry.instances_count}, Desired: {entry.desired_count}'
                )
            raise exceptions.SocaException(
                error_code=errorcodes.EC2_RESERVED_INSTANCES_NOT_AVAILABLE,
                message=message,
            )

    def get_service_quota(
        self, instance_types: List[str], quota_type: int, desired_capacity: int
    ) -> CheckServiceQuotaResult:
        """
        > for SPOT + ONDEMAND or ONDEMAND + DEDICATED or SPOT + DEDICATED capacity, this API should be invoked twice.
        > SPOT + ONDEMAND + DEDICATED, this API should be invoked three times
        :param instance_types: list of applicable instance types in the capacity to be provisioned
        :param quota_type: one of:
            1: on-demand -> constants.EC2_SERVICE_QUOTA_ONDEMAND
            2: spot -> constants.EC2_SERVICE_QUOTA_SPOT
            3: dedicated -> constants.EC2_SERVICE_QUOTA_DEDICATED
        :param desired_capacity: desired capacity required for the job
        :return: True, if service quota is available, False otherwise
        :raises SocaException
            error codes:
            > EC2_SERVICE_QUOTA_NOT_FOUND
        """
        return EC2ServiceQuotaHelper(
            context=self.context,
            instance_types=instance_types,
            quota_type=quota_type,
            desired_capacity=desired_capacity,
        ).check_service_quota()

    @staticmethod
    def fail_message(key: str = None, message: str = None):
        result = ''
        if key:
            result += f'({key}) '
        result += f'{message}'
        return result

    def check_budgets(self):
        for job in self.jobs:
            return AwsBudgetsHelper(
                context=self.context, job=job
            ).check_budget_availability()

    def check_bedrock(self, reject_when_check_fails: bool = True):
        """
        a job whose project has Bedrock enabled must run under that project's instance
        profile. the profile is re-resolved here rather than trusted from the job, so a
        change since submission is caught: without it the job would launch under the
        standard compute node profile and fail its first model call, having already
        consumed compute, while the portal lists the models as available.

        :param reject_when_check_fails: what an unresolved project means. True
        (provisioning) keeps the job queued for a retry. False (submission) accepts the
        job, so a projects api blip or a reconcile still in flight cannot reject it -
        the provisioning check runs again before any capacity is created.
        :raises SocaException
            error codes:
            > BEDROCK_ACCESS_NOT_READY - expected to resolve on its own, job waits
            > BEDROCK_ACCESS_NOT_AUTHORIZED - needs an administrator, job is not retried
        """
        if not is_enabled_for_jobs(self.context):
            # checked before anything else is read, so a cluster that has not enabled
            # this reaches none of it.
            return

        queue_management = self.queue_profile.queue_management_params
        for job in self.jobs:
            access = BedrockJobAccessResolver(
                context=self.context,
                project_name=job.project,
                queue_management=queue_management,
            ).resolve()

            if access.state == BedrockJobAccessState.NOT_AUTHORIZED:
                raise exceptions.SocaException(
                    error_code=errorcodes.BEDROCK_ACCESS_NOT_AUTHORIZED,
                    message=self.fail_message(
                        key=constants.JOB_PARAM_INSTANCE_PROFILE, message=access.message
                    ),
                )

            if access.state == BedrockJobAccessState.NOT_READY:
                if not reject_when_check_fails:
                    self._logger.warning(
                        f'{job.log_tag} - Bedrock access is not resolved yet, skipping '
                        f'the check: {access.message}'
                    )
                    continue
                raise exceptions.SocaException(
                    error_code=errorcodes.BEDROCK_ACCESS_NOT_READY,
                    message=self.fail_message(
                        key=constants.JOB_PARAM_INSTANCE_PROFILE, message=access.message
                    ),
                )

            if (
                access.is_available
                and job.params.instance_profile != access.instance_profile_arn
            ):
                # the identity the job would launch with is not the project's, so its
                # model access would be missing at runtime.
                raise exceptions.SocaException(
                    error_code=errorcodes.BEDROCK_ACCESS_NOT_READY,
                    message=self.fail_message(
                        key=constants.JOB_PARAM_INSTANCE_PROFILE,
                        message=f'job is provisioned with instance profile '
                        f'({job.params.instance_profile}), which does not carry the '
                        f'Bedrock access of project ({job.project}).',
                    ),
                )

    def check_licenses(self, reject_when_check_fails: bool = True):
        """
        :param reject_when_check_fails: what an unusable license server check means. True
        (provisioning) counts zero available, so the job stays queued and is retried. False
        (submission) skips the license check, so an unhealthy server cannot reject the job.
        """
        for job in self.jobs:
            if job.params.licenses is None or len(job.params.licenses) == 0:
                continue
            for license_ask in job.params.licenses:
                availability = self.context.license_service.get_license_availability(
                    license_resource_name=license_ask.name
                )
                if not availability.check_ok and not reject_when_check_fails:
                    self._logger.warning(
                        f'{job.log_tag} - {license_ask.name} - license availability is '
                        f'unknown, skipping the license check: {availability.error}'
                    )
                    continue
                available = availability.available_count
                active_license_usage_count = (
                    self.context.job_cache.get_active_license_count(
                        license_name=license_ask.name
                    )
                )
                total_required = active_license_usage_count + license_ask.count
                if available < total_required:
                    raise exceptions.SocaException(
                        error_code=errorcodes.NOT_ENOUGH_LICENSES,
                        message=f'Not enough licenses available for: {license_ask.name}. '
                        f'licenses available: {available}, '
                        f'total required: {total_required}, '
                        f'job required: {license_ask.count}',
                    )
                else:
                    self._logger.info(
                        f'{job.log_tag} - {license_ask.name} - '
                        f'licenses available: {available}, '
                        f'total required: {total_required}, '
                        f'job required: {license_ask.count}'
                    )

    def check_acls(self) -> bool:
        user_projects = self.context.projects_client.get_user_projects(
            username=self.job.owner
        )
        project_name = self.job.project

        current_project = None
        for project in user_projects:
            if project.name == project_name:
                current_project = project
                break

        if current_project is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.UNAUTHORIZED_ACCESS,
                message=f'User: {self.job.owner} is not authorized to submit jobs for project: {project_name} on queue: {self.job.queue}.',
            )

        return True
