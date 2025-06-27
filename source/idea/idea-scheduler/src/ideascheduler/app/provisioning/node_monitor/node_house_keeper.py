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
    exceptions,
    SocaCapacityType,
    SocaComputeNode,
    SocaComputeNodeState,
    SocaJobState,
    ProvisioningStatus,
    EC2Instance,
)
from ideasdk.utils import Utils
from ideasdk.metrics import BaseMetrics

from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect

from typing import Dict, List, Optional, Set, Union, Tuple
from threading import Thread, Event
import arrow
import logging
import time

minutes = Utils.minutes

CurrentCapacity = int
DesiredCapacity = int
CapacityTuple = Tuple[CurrentCapacity, DesiredCapacity]

QueueType = str
LogInfo = str
InfoTuple = Tuple[QueueType, LogInfo]


class NodeHouseKeepingSession:
    """
    Perform Housekeeping activities for the Soca Cluster
    """

    def __init__(self, context: ideascheduler.AppContext, logger: logging.Logger):
        self._context = context
        self._logger = logger

        # pass 1
        self.job_groups: Dict[str, List[EC2Instance]] = {}
        self.spot_fleet_instances: Dict[str, List[EC2Instance]] = {}
        self.auto_scaling_group_instances: Dict[str, List[EC2Instance]] = {}

        # pass 2
        self.spot_fleet_present_capacities: Dict[str, int] = {}
        self.auto_scaling_group_present_capacities: Dict[str, int] = {}

        # pass 5
        self.spot_fleet_desired_capacities: Dict[str, CapacityTuple] = {}
        self.auto_scaling_group_desired_capacities: Dict[str, CapacityTuple] = {}

        # pass 6
        self.nodes_to_delete: Set[Union[SocaComputeNode, EC2Instance]] = set()
        self.instances_to_terminate: Set[EC2Instance] = set()
        self.stacks_to_delete: Set[str] = set()
        self.spot_fleet_update_capacities: Dict[str, CapacityTuple] = {}
        self.auto_scaling_group_update_capacities: Dict[str, CapacityTuple] = {}
        self.auto_scaling_group_detach_instances: Dict[str, List[str]] = {}

        # logging and metric helpers
        self.stack_info: Dict[str, InfoTuple] = {}
        self.spot_fleet_info: Dict[str, InfoTuple] = {}
        self.auto_scaling_group_info: Dict[str, InfoTuple] = {}

    @property
    def config(self):
        return self._context.config()

    @property
    def aws_util(self):
        return self._context.aws_util()

    @staticmethod
    def log_tag(instance: Union[EC2Instance, SocaComputeNode]):
        s = '('
        if isinstance(instance, EC2Instance):
            if instance.is_soca_ephemeral_capacity:
                s += f'JobId: {instance.soca_job_id}'
            else:
                s += f'JobGroup: {instance.soca_job_group}'
            s += f', Host: {instance.private_dns_name}'
            s += f', InstanceId: {instance.instance_id}'
            s += f', QueueType: {instance.soca_queue_type}'
        elif isinstance(instance, SocaComputeNode):
            if instance.is_shared_capacity():
                s += f'JobGroup: {instance.job_group}'
            else:
                s += f'JobId: {instance.job_id}'
            s += f', Host: {instance.host}'
            s += f', InstanceId: {instance.instance_id}'
            s += f', QueueType: {instance.queue_type}'
        s += ')'
        return s

    @staticmethod
    def log_info(instance: EC2Instance, key: str, value: str):
        s = '('
        if instance.is_soca_ephemeral_capacity:
            s += f'JobId: {instance.soca_job_id}'
        else:
            s += f'JobGroup: {instance.soca_job_group}'
        s += f', Host: {instance.private_dns_name}'
        s += f', InstanceId: {instance.instance_id}'
        s += f', QueueType: {instance.soca_queue_type}'
        s += f') {key}={value}'
        return s

    @staticmethod
    def _get_desired_capacity(
        current_capacity: int,
        weighted_capacities: Optional[Dict[str, int]],
        instances: List[EC2Instance],
    ) -> int:
        if weighted_capacities is None:
            desired_capacity = current_capacity - len(instances)
        else:
            weighted_capacity = 0
            for instance in instances:
                weighted_capacity += weighted_capacities[instance.instance_type]
            desired_capacity = current_capacity - weighted_capacity
        return max(0, int(desired_capacity))

    @staticmethod
    def _get_present_capacity(
        weighted_capacities: Optional[Dict[str, int]], instances: List[EC2Instance]
    ) -> int:
        if weighted_capacities is None:
            present_capacity = len(instances)
        else:
            weighted_capacity = 0
            for instance in instances:
                weighted_capacity += weighted_capacities[instance.instance_type]
            present_capacity = weighted_capacity
        return present_capacity

    def _compute_spot_fleet_capacities(
        self, spot_fleet_request_id: str, instances: List[EC2Instance], desired=True
    ):
        result = self.aws_util.ec2_describe_spot_fleet_request(
            spot_fleet_request_id=spot_fleet_request_id
        )

        if result is None:
            return

        fulfilled_capacity = result.fulfilled_capacity
        target_capacity = result.target_capacity
        current_capacity = min(fulfilled_capacity, target_capacity)

        weighted_capacities = result.weighted_capacities

        if desired:
            desired_capacity = self._get_desired_capacity(
                current_capacity=current_capacity,
                weighted_capacities=weighted_capacities,
                instances=instances,
            )
            self.spot_fleet_desired_capacities[spot_fleet_request_id] = (
                current_capacity,
                desired_capacity,
            )
        else:
            present_capacity = self._get_present_capacity(
                weighted_capacities=weighted_capacities, instances=instances
            )
            self.spot_fleet_present_capacities[spot_fleet_request_id] = present_capacity

    def _compute_auto_scaling_group_capacities(
        self, auto_scaling_group_name: str, instances: List[EC2Instance], desired=True
    ):
        result = self.aws_util.autoscaling_describe_auto_scaling_group(
            auto_scaling_group_name=auto_scaling_group_name
        )

        if result is None:
            return

        current_capacity = result.desired_capacity
        weighted_capacities = result.weighted_capacities

        if desired:
            desired_capacity = self._get_desired_capacity(
                current_capacity=current_capacity,
                weighted_capacities=weighted_capacities,
                instances=instances,
            )
            self.auto_scaling_group_desired_capacities[auto_scaling_group_name] = (
                current_capacity,
                desired_capacity,
            )
        else:
            present_capacity = self._get_present_capacity(
                weighted_capacities=weighted_capacities, instances=instances
            )
            self.auto_scaling_group_present_capacities[auto_scaling_group_name] = (
                present_capacity
            )

    def _add_to_job_group(self, instance: EC2Instance):
        job_group = instance.soca_job_group
        if job_group in self.job_groups:
            instances = self.job_groups[instance.soca_job_group]
        else:
            instances = []
            self.job_groups[instance.soca_job_group] = instances
        instances.append(instance)

    def _can_terminate(self, instance: EC2Instance, node: SocaComputeNode) -> bool:
        """
        Checks if an ec2 instance is eligible for termination and compute node can be deleted.

        for scaling_mode = single jobs or ephemeral capacity:
             - job tracker is a good place to check if the single job is active or not.

        for scaling_mode = batch or non-ephemeral capacity:
            - job tracker cannot be used, even if we can check metrics for a job group to find if there are any
              active jobs for the group.
            - multiple jobs share the same compute stack and the tags assigned to compute nodes do not represent
              the job running on compute node.
            - terminate_when_idle is a mandatory parameter when scaling mode == multiple_jobs.
              this means, if multiple jobs are running on the compute stack, individual instances can be terminated
              independently.

        :param instance: ECInstance
        :param node: SocaComputeNode
        :return: True if instance can be terminated, False otherwise
        """

        if not instance.is_valid_idea_compute_node():
            return False

        if instance.is_soca_ephemeral_capacity:
            job = self._context.job_cache.get_job(job_id=instance.soca_job_id)
            if job is not None:
                return False

        if instance.soca_keep_forever and instance.soca_terminate_when_idle == 0:
            return False

        terminate_when_idle = instance.soca_terminate_when_idle
        if terminate_when_idle > 0:
            last_used = node.last_used_time

            if last_used is None and node.has_state(SocaComputeNodeState.FREE):
                last_used = node.last_state_changed_time

            if last_used is None:
                # node is still being provisioned
                return False

            last_used = arrow.get(last_used)
            terminate_after = last_used.shift(minutes=terminate_when_idle)
            terminate_after_delta = arrow.utcnow() - terminate_after
            if terminate_after > arrow.utcnow():
                # log example:
                # (JobId: 1034, Host: ip-10-0-79-226) node was last used 5 seconds ago. terminate when idle: 3 minutes.
                if minutes(seconds=terminate_after_delta.seconds) % 5 == 0:
                    terminate = arrow.utcnow().shift(minutes=terminate_when_idle)
                    self._logger.info(
                        f'{self.log_tag(instance)} node was last used {last_used.humanize()}. '
                        f'terminate when idle: {terminate.humanize(only_distance=True)}.'
                    )
                return False

            pending_jobs = OpenPBSQSelect(
                context=self._context,
                logger=self._logger,
                job_group=node.job_group,
                job_state=[SocaJobState.QUEUED, SocaJobState.HELD],
            ).get_count()
            if pending_jobs > 0:
                self._logger.info(
                    f'{self.log_tag(instance)} {pending_jobs} jobs waiting to be executed. skip termination.'
                )
                return False

            if minutes(terminate_after_delta.seconds) > 5:
                if minutes(seconds=terminate_after_delta.seconds) % 5 == 0:
                    terminate = arrow.utcnow().shift(minutes=terminate_when_idle)
                    self._logger.info(
                        f'{self.log_tag(instance)} node was last used {last_used.humanize()}, '
                        f'but is not deleted yet.'
                        f'terminate when idle: {terminate.humanize(only_distance=True)}.'
                    )

        return True

    def _add_candidate_for_deletion(self, instance: EC2Instance):
        if instance.soca_capacity_type == SocaCapacityType.SPOT:
            spot_fleet_request_id = instance.aws_ec2spot_fleet_request_id
            if Utils.is_empty(spot_fleet_request_id):
                return
            if spot_fleet_request_id in self.spot_fleet_instances:
                spot_fleet = self.spot_fleet_instances[spot_fleet_request_id]
            else:
                spot_fleet = []
                self.spot_fleet_instances[spot_fleet_request_id] = spot_fleet
            spot_fleet.append(instance)
        else:
            auto_scale_group_name = instance.aws_autoscaling_group_name
            if Utils.is_empty(auto_scale_group_name):
                return
            if auto_scale_group_name in self.auto_scaling_group_instances:
                asg = self.auto_scaling_group_instances[auto_scale_group_name]
            else:
                asg = []
                self.auto_scaling_group_instances[auto_scale_group_name] = asg
            asg.append(instance)

    def _publish_node_metrics(self, node: SocaComputeNode):
        if node.has_state(SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY):
            self._context.metrics.nodes_busy(queue_type=node.queue_type)
        elif node.has_state(SocaComputeNodeState.FREE):
            self._context.metrics.nodes_idle(queue_type=node.queue_type)
        elif node.has_state(SocaComputeNodeState.OFFLINE):
            self._context.metrics.nodes_offline(queue_type=node.queue_type)
        else:
            self._context.metrics.nodes_down(queue_type=node.queue_type)

    def _publish_instance_metrics(self, instance: EC2Instance):
        if instance.state in ('running', 'pending'):
            self._context.metrics.instances_running(queue_type=instance.soca_queue_type)

    def pass1_identify_potential_candidates_for_deletion(self):
        """
        identify potential candidates for deletion

        it's important that no I/O calls should be implemented after fetching the node list from the scheduler, and
        while iterating through the loop. this is because, the SocaComputeNode.last_used_time is time sensitive and
        we want to get through this pass as soon as possible to identify the candidates for deletion.

        candidates identified for deletion are grouped as per their auto scaling group or spot fleet request.
        """

        cluster_name = self._context.cluster_name()

        nodes = self._context.scheduler.list_nodes()

        for node in nodes:
            if not node.is_provisioned():
                continue

            if not node.is_valid_idea_compute_node():
                continue

            if not node.is_current_cluster(cluster_name):
                continue

            self._publish_node_metrics(node=node)

            if node.has_state(SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY):
                continue

            # scheduler returns the state as free, even if 1 of 8 is available.
            # and returns BUSY only when all cores are full
            # check if any jobs are running on the node and then mark for deletion
            if Utils.is_not_empty(node.jobs):
                continue

            instance = self._context.instance_cache.get_instance(
                instance_id=node.instance_id
            )

            if instance is None:
                self.nodes_to_delete.add(node)
                continue

            # manually deleted CloudFormation Stacks
            # manually stopped or terminated from EC2 Console
            if instance.state in ('stopping', 'stopped', 'shutting-down', 'terminated'):
                self.nodes_to_delete.add(node)
                continue

            self._publish_instance_metrics(instance=instance)

            self._add_to_job_group(instance=instance)

            if not self._can_terminate(instance=instance, node=node):
                continue

            self._add_candidate_for_deletion(instance=instance)

    def pass2_compute_present_capacities(self):
        """
        compute present capacities

        for each auto scaling group or spot fleet request, compute the desired capacity. if instance weighting is used,
        the capacity of the instance it's weight is identified, and present capacity is computed.
        """
        for spot_fleet_request_id, instances in self.spot_fleet_instances.items():
            self._compute_spot_fleet_capacities(
                spot_fleet_request_id=spot_fleet_request_id,
                instances=instances,
                desired=False,
            )

        for (
            auto_scaling_group_name,
            instances,
        ) in self.auto_scaling_group_instances.items():
            self._compute_auto_scaling_group_capacities(
                auto_scaling_group_name=auto_scaling_group_name,
                instances=instances,
                desired=False,
            )

    def pass3_filter_deletion_candidates(self):
        """
        filter deletion candidates based on present capacity and required capacity

        For each job group if non-ephemeral capacity, check if the present capacity is less than required capacity
        for the group.
        This is to avoid scenarios where, instances are provisioned, become free and terminated as terminate when idle
        interval is too short.
        This can happen when capacity is still being provisioned.
        """
        for job_group, instances in self.job_groups.items():
            ref = instances[0]
            if ref.is_soca_ephemeral_capacity:
                continue

            required_capacity = self._context.job_cache.get_desired_capacity(
                job_group=ref.soca_job_group
            )

            if ref.soca_capacity_type == SocaCapacityType.SPOT:
                if (
                    ref.aws_ec2spot_fleet_request_id
                    not in self.spot_fleet_present_capacities
                ):
                    continue
                present_capacity = self.spot_fleet_present_capacities[
                    ref.aws_ec2spot_fleet_request_id
                ]
                if present_capacity < required_capacity:
                    del self.spot_fleet_instances[ref.aws_ec2spot_fleet_request_id]
            else:
                if (
                    ref.aws_autoscaling_group_name
                    not in self.auto_scaling_group_present_capacities
                ):
                    continue
                present_capacity = self.auto_scaling_group_present_capacities[
                    ref.aws_autoscaling_group_name
                ]
                if present_capacity < required_capacity:
                    del self.auto_scaling_group_instances[
                        ref.aws_autoscaling_group_name
                    ]

    def pass4_set_offline(self):
        """
        set offline

        after the candidates are identified in ``pass1``, we set these candidates as ``OFFLINE``, so that no jobs
        can be scheduled on these nodes, while we proceed further with housekeeping.
        """

        # if between pass1 and pass4, if there were any jobs that were provisioned on this node,
        # the node could become busy again.

        # fetch node from scheduler and filter again

        nodes_to_set_offline = set()
        for instances in self.spot_fleet_instances.values():
            for instance in instances:
                node = self._context.scheduler.get_node(host=instance.private_dns_name)
                if node is None:
                    continue
                if node.has_state(
                    SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY
                ):
                    continue
                nodes_to_set_offline.add(instance)

        for instances in self.auto_scaling_group_instances.values():
            for instance in instances:
                node = self._context.scheduler.get_node(host=instance.private_dns_name)
                if node is None:
                    continue
                if node.has_state(
                    SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY
                ):
                    continue
                nodes_to_set_offline.add(instance)

        for node in nodes_to_set_offline:
            self._logger.info(f'{self.log_tag(node)} set offline')
            self._context.scheduler.set_node_state(
                host=node.private_dns_name, state=SocaComputeNodeState.OFFLINE
            )

        # filter the updated instances from ASG and SpotFleet
        skip_spot_fleets = set()
        skip_asgs = set()

        for spot_fleet in self.spot_fleet_instances:
            instances = (
                set(self.spot_fleet_instances[spot_fleet]) & nodes_to_set_offline
            )
            if len(instances) == 0:
                skip_spot_fleets.add(spot_fleet)
            else:
                self.spot_fleet_instances[spot_fleet] = list(instances)

        for auto_scale_group_name in self.auto_scaling_group_instances:
            instances = (
                set(self.auto_scaling_group_instances[auto_scale_group_name])
                & nodes_to_set_offline
            )
            if len(instances) == 0:
                skip_asgs.add(auto_scale_group_name)
            else:
                self.auto_scaling_group_instances[auto_scale_group_name] = list(
                    instances
                )

        for spot_fleet in skip_spot_fleets:
            del self.spot_fleet_instances[spot_fleet]
        for auto_scale_group_name in skip_asgs:
            del self.auto_scaling_group_instances[auto_scale_group_name]

    def pass5_compute_desired_capacities(self):
        """
        compute desired capacities

        for each auto scaling group or spot fleet request, compute the desired capacity. if instance weighting is used,
        the capacity of the instance it's weight is identified, and desired capacity is computed.
        """
        for spot_fleet_request_id, instances in self.spot_fleet_instances.items():
            self._compute_spot_fleet_capacities(
                spot_fleet_request_id=spot_fleet_request_id,
                instances=instances,
                desired=True,
            )

        for (
            auto_scaling_group_name,
            instances,
        ) in self.auto_scaling_group_instances.items():
            self._compute_auto_scaling_group_capacities(
                auto_scaling_group_name=auto_scaling_group_name,
                instances=instances,
                desired=True,
            )

    def pass6_finalize_resources_to_delete(self):
        """
        finalize resources to delete

        based on desired capacities computed in pass3, identify if the cloud formation stack can be deleted, or
        capacity needs to be updated.
        """

        for (
            spot_fleet_request_id,
            capacity,
        ) in self.spot_fleet_desired_capacities.items():
            instances = self.spot_fleet_instances[spot_fleet_request_id]

            for instance in instances:
                self.instances_to_terminate.add(instance)

            ref = instances[0]

            current_capacity, desired_capacity = capacity

            if desired_capacity == 0:
                self.stack_info[ref.soca_compute_stack] = (
                    ref.soca_queue_type,
                    self.log_info(ref, 'Stack', ref.soca_compute_stack),
                )
                self.stacks_to_delete.add(ref.soca_compute_stack)
            else:
                self.spot_fleet_info[spot_fleet_request_id] = (
                    ref.soca_queue_type,
                    self.log_info(ref, 'SpotFleet', spot_fleet_request_id),
                )
                self.spot_fleet_update_capacities[spot_fleet_request_id] = capacity

        for (
            auto_scaling_group_name,
            capacity,
        ) in self.auto_scaling_group_desired_capacities.items():
            instances = self.auto_scaling_group_instances[auto_scaling_group_name]

            instance_ids = []
            for instance in instances:
                self.instances_to_terminate.add(instance)
                instance_ids.append(instance.instance_id)

            ref = instances[0]

            current_capacity, desired_capacity = capacity

            if desired_capacity == 0:
                self.stack_info[ref.soca_compute_stack] = (
                    ref.soca_queue_type,
                    self.log_info(ref, 'Stack', ref.soca_compute_stack),
                )
                self.stacks_to_delete.add(ref.soca_compute_stack)
            else:
                self.auto_scaling_group_info[auto_scaling_group_name] = (
                    ref.soca_queue_type,
                    self.log_info(ref, 'ASG', auto_scaling_group_name),
                )
                self.auto_scaling_group_update_capacities[auto_scaling_group_name] = (
                    capacity
                )
                self.auto_scaling_group_detach_instances[auto_scaling_group_name] = (
                    instance_ids
                )

    def cleanup(self):
        """
        perform cleanup
            - scheduler nodes
            - terminates ec2 instances
            - deletes cloudformation stacks
            - deletes AD computer objects
            - detach applicable instances and update auto scaling group capacities
            - updates spot fleet capacities
        """

        deletion_failed_instances = set()
        for node in self.nodes_to_delete:
            self._logger.info(f'{self.log_tag(node)} deleting node')

            if isinstance(node, SocaComputeNode):
                host = node.host
                queue_type = node.queue_type
            else:
                host = node.private_dns_name
                queue_type = node.soca_queue_type
            try:
                self._context.scheduler.delete_node(host=host)
                self._context.metrics.nodes_deleted(queue_type=queue_type)
            except exceptions.SocaException as e:
                self._logger.warning(f'{self.log_tag(node)} {e.message}')
                deletion_failed_instances.add(node.instance_id)

        for instance in self.instances_to_terminate:
            if instance.instance_id in deletion_failed_instances:
                self._logger.warning(
                    f'{self.log_tag(instance)} deletion failed. skip instance termination.'
                )
                continue

            self._logger.info(f'{self.log_tag(instance)} terminating ec2 instance')
            self.aws_util.ec2_terminate_instances(instance_ids=[instance.instance_id])

            self._context.metrics.instances_terminated(
                queue_type=instance.soca_queue_type
            )

            duration = instance.launch_time - arrow.utcnow()
            self._context.metrics.instances_running_duration(
                queue_type=instance.soca_queue_type, duration_secs=duration.seconds
            )

            # Send AD automation event to delete the computer object
            try:
                # First check if directory service is properly configured
                provider = self._context.config().get_string(
                    'directoryservice.provider', default=None
                )
                if not provider or provider not in [
                    'aws_managed_activedirectory',
                    'activedirectory',
                ]:
                    self._logger.debug(
                        f'Skipping AD computer deletion for {instance.instance_id}, directory service provider is {provider}'
                    )
                    continue

                # Get AD automation queue URL from config - fail early if not configured
                ad_automation_queue_url = self._context.config().get_string(
                    'directoryservice.ad_automation.sqs_queue_url', default=None
                )
                if not ad_automation_queue_url:
                    self._logger.warning(
                        'AD automation queue URL not configured. Skipping AD computer deletion.'
                    )
                    continue

                # Try to determine the computer name for this instance
                computer_name = None

                # First try standard methods to get hostname
                if hasattr(instance, 'private_dns_name') and instance.private_dns_name:
                    # Extract hostname from private DNS name (e.g., ip-10-0-0-1.ec2.internal -> ip-10-0-0-1)
                    computer_name = instance.private_dns_name.split('.')[0]
                    self._logger.debug(
                        f'Using private_dns_name derived hostname: {computer_name}'
                    )
                elif hasattr(instance, 'host') and instance.host:
                    computer_name = instance.host
                    self._logger.debug(f'Using host as hostname: {computer_name}')

                # For Windows instances or when hostname couldn't be determined, generate using standard algorithm
                if not computer_name:
                    try:
                        # Get cluster config values for hostname generation
                        aws_region = self._context.config().get_string(
                            'cluster.aws.region', required=True
                        )
                        aws_account = self._context.config().get_string(
                            'cluster.aws.account_id', required=True
                        )
                        cluster_name = self._context.config().get_string(
                            'cluster.cluster_name', required=True
                        )

                        hostname_data = f'{aws_region}|{aws_account}|{cluster_name}|{instance.instance_id}'
                        hostname_prefix = self._context.config().get_string(
                            'directoryservice.ad_automation.hostname_prefix',
                            default='IDEA-',
                        )

                        # Calculate available characters (max length of AD computer name is 15)
                        avail_chars = 15 - len(hostname_prefix)
                        if avail_chars < 4:
                            self._logger.warning(
                                f'Hostname prefix too long: {hostname_prefix}, using default IDEA-'
                            )
                            hostname_prefix = 'IDEA-'
                            avail_chars = 10  # 15 - 5

                        # Take the last n-chars from the resulting shake256 bucket of 256
                        shake_value = Utils.shake_256(hostname_data, 256)[
                            (avail_chars * -1) :
                        ]
                        computer_name = f'{hostname_prefix}{shake_value}'.upper()

                        self._logger.info(
                            f'Generated hostname for instance {instance.instance_id}: {computer_name}'
                        )
                    except Exception as e:
                        self._logger.error(
                            f'Failed to generate hostname for instance: {str(e)}'
                        )

                if not computer_name:
                    self._logger.warning(
                        f'Unable to determine computer name for instance {instance.instance_id}. Skipping AD computer deletion.'
                    )
                    continue

                self._logger.info(
                    f'Sending AD automation delete event for computer: {computer_name}, instance: {instance.instance_id}'
                )

                # Create the AD automation request
                request = {
                    'header': {'namespace': 'ADAutomation.DeleteComputer'},
                    'payload': {
                        'computer_name': computer_name,
                        'instance_id': instance.instance_id,
                    },
                }

                # Send the message to SQS - always using FIFO parameters since AD automation queue is always FIFO
                message_deduplication_id = (
                    f'{instance.instance_id}-{Utils.current_time_ms()}'
                )
                self._logger.debug(
                    f'Using message deduplication ID: {message_deduplication_id}'
                )

                sqs_response = (
                    self._context.aws()
                    .sqs()
                    .send_message(
                        QueueUrl=ad_automation_queue_url,
                        MessageBody=Utils.to_json(request),
                        MessageGroupId='ADAutomation.DeleteComputer',
                        MessageDeduplicationId=message_deduplication_id,
                    )
                )

                self._logger.info(
                    f'Successfully sent AD delete request for {computer_name}, SQS MessageId: {sqs_response.get("MessageId", "unknown")}'
                )
            except Exception as e:
                self._logger.warning(
                    f'Failed to send AD automation delete event: {str(e)}',
                    exc_info=True,
                )

        # todo - refactor this implementation to create a NodeHouseKeeperContext:
        #  - get rid of all the tuple returns and simplify the code.
        #  - if node deletion failed, find all the ASGs or SpotFleets which need not be updated.
        #  - find all the stacks that cannot be deleted.

        for (
            spot_fleet_request_id,
            capacity,
        ) in self.spot_fleet_update_capacities.items():
            current_capacity, target_capacity = capacity
            queue_type, info = self.spot_fleet_info[spot_fleet_request_id]
            self._logger.info(
                f'{info} updating capacity: {current_capacity} -> {target_capacity}'
            )
            self.aws_util.ec2_modify_spot_fleet_request(
                spot_fleet_request_id=spot_fleet_request_id,
                target_capacity=target_capacity,
            )
            self._context.metrics.spotfleet_capacity_decreased(
                queue_type, current_capacity - target_capacity
            )

        for (
            auto_scaling_group_name,
            instance_ids,
        ) in self.auto_scaling_group_detach_instances.items():
            queue_type, info = self.auto_scaling_group_info[auto_scaling_group_name]
            self._logger.info(f'{info} detaching {len(instance_ids)} instance(s)')
            self.aws_util.autoscaling_detach_instances(
                auto_scaling_group_name=auto_scaling_group_name,
                instance_ids=instance_ids,
            )

        for (
            auto_scaling_group_name,
            capacity,
        ) in self.auto_scaling_group_update_capacities.items():
            current_capacity, desired_capacity = capacity
            queue_type, info = self.auto_scaling_group_info[auto_scaling_group_name]
            self._logger.info(
                f'{info} updating capacity: {current_capacity} -> {desired_capacity}'
            )
            self.aws_util.autoscaling_update_auto_scaling_group(
                auto_scaling_group_name=auto_scaling_group_name,
                desired_capacity=desired_capacity,
            )
            self._context.metrics.asg_capacity_decreased(
                queue_type, current_capacity - desired_capacity
            )

        for stack_name in self.stacks_to_delete:
            queue_type, info = self.stack_info[stack_name]
            self._logger.info(f'{info} deleting stack')
            self.aws_util.cloudformation_delete_stack(stack_name=stack_name)
            time.sleep(0.3)
            self._context.metrics.stacks_deleted(queue_type)

    def retry_provisioning_cleanup(self):
        """
        retry provisioning clean-up

        for all active jobs with ephemeral capacity, where job is queued but not started more than:
            {scheduler.job_provisioning.stack_provisioning_timeout_seconds}
            1. delete stacks for those jobs
            2. mark job as not provisioned.
        """

        jobs_table = self._context.job_cache.get_jobs_table()
        queued_jobs = jobs_table.find(state=(SocaJobState.QUEUED, SocaJobState.HELD))
        stack_provisioning_timeout_secs = self.config.get_int(
            'scheduler.job_provisioning.stack_provisioning_timeout_seconds'
        )

        self._logger.debug(
            'retry_provisioning_cleanup: Processing queued/held jobs from database'
        )
        self._logger.debug(
            f'retry_provisioning_cleanup: Stack provisioning timeout: {stack_provisioning_timeout_secs} seconds'
        )

        compute_stacks = {}

        for entry in queued_jobs:
            job = None
            try:
                job = self._context.job_cache.convert_db_entry_to_job(entry)
                if job is None:
                    self._logger.debug(
                        'retry_provisioning_cleanup: Skipping entry - could not convert to job'
                    )
                    continue

                self._logger.debug(
                    f'{job.log_tag} Processing job in retry_provisioning_cleanup, state: {job.state}, provisioned: {job.provisioned}'
                )

                if job.state == SocaJobState.HELD:
                    # for jobs that were held previously, fetch the latest copy of the job from scheduler
                    self._logger.debug(
                        f'{job.log_tag} Job is HELD, fetching latest state from scheduler'
                    )
                    job = self._context.scheduler.get_job(job_id=job.job_id)
                    if job.state == SocaJobState.HELD:
                        self._logger.debug(
                            f'{job.log_tag} Job still HELD after refresh, skipping'
                        )
                        continue
                    self._logger.info(
                        f'queue previously held job: {job.log_tag}, state: {job.state}'
                    )
                    self._context.job_monitor.job_modified(job=job)
                    continue

                if not job.is_provisioned():
                    self._logger.debug(
                        f'{job.log_tag} Job not provisioned, re-queuing for provisioning'
                    )
                    provisioning_queue = (
                        self._context.queue_profiles.get_provisioning_queue(
                            queue_profile_name=job.queue_type
                        )
                    )
                    if provisioning_queue is None:
                        self._logger.warning(
                            f'{job.log_tag} No provisioning queue found for queue_type: {job.queue_type}'
                        )
                        continue
                    provisioning_queue.put(job=job)
                    self._logger.debug(f'{job.log_tag} Job re-queued for provisioning')
                    continue

                self._logger.debug(
                    f'{job.log_tag} Job is provisioned, checking stack status. Stack ID: {job.params.stack_id if job.params else "None"}'
                )

                provisioning_util = JobProvisioningUtil(
                    context=self._context, jobs=[job], logger=self._logger
                )

                # when batch jobs are being submitted and cloud formation stack is being provisioned, we need to ensure we don't create a flood of describe stack requests
                # as all batch jobs will point to same cloud formation stack.
                # implement a local cached copy of the status based on the name of the stack.
                compute_stack = job.get_compute_stack()
                self._logger.debug(f'{job.log_tag} Compute stack name: {compute_stack}')

                if compute_stack in compute_stacks:
                    provisioning_status = compute_stacks[compute_stack]
                    self._logger.debug(
                        f'{job.log_tag} Using cached provisioning status: {provisioning_status}'
                    )
                else:
                    self._logger.debug(
                        f'{job.log_tag} Checking stack status via AWS API'
                    )
                    provisioning_status = provisioning_util.check_status()
                    compute_stacks[compute_stack] = provisioning_status
                    self._logger.info(
                        f'{job.log_tag} Stack: {compute_stack}, ProvisioningStatus: {provisioning_status}'
                    )

                if provisioning_status in (
                    ProvisioningStatus.IN_PROGRESS,
                    ProvisioningStatus.DELETE_IN_PROGRESS,
                ):
                    self._logger.info(
                        f'{job.log_tag} '
                        f'Stack: {job.get_compute_stack()}, '
                        f'ProvisioningStatus: {provisioning_status} - Stack still in progress, skipping'
                    )
                    continue

                if (
                    job.is_shared_capacity()
                    and provisioning_status == ProvisioningStatus.COMPLETED
                ):
                    self._logger.debug(
                        f'{job.log_tag} Shared capacity job with completed stack, skipping cleanup'
                    )
                    continue

                delete_stack = False
                self._logger.debug(
                    f'{job.log_tag} Evaluating cleanup actions for provisioning status: {provisioning_status}'
                )

                if provisioning_status == ProvisioningStatus.COMPLETED:
                    self._logger.debug(
                        f'{job.log_tag} Stack completed successfully, checking timeout'
                    )
                    creation_time = provisioning_util.stack.creation_time
                    if creation_time is None:
                        self._logger.warning(
                            f'{job.log_tag} Stack creation time is None, skipping timeout check'
                        )
                        continue

                    now = arrow.utcnow()
                    delta = now - creation_time
                    self._logger.debug(
                        f'{job.log_tag} Stack age: {delta.seconds} seconds, timeout threshold: {stack_provisioning_timeout_secs} seconds'
                    )

                    if delta.seconds < stack_provisioning_timeout_secs:
                        # print log message only after 5 mins have elapsed.
                        if minutes(seconds=delta.seconds) % 5 == 0:
                            timeout_in_secs = (
                                stack_provisioning_timeout_secs - delta.seconds
                            )
                            timeout = arrow.utcnow().shift(seconds=timeout_in_secs)
                            self._logger.info(
                                f'{job.log_tag} ComputeStack: {job.get_compute_stack()} will be deleted '
                                f'and retried if job does not start {timeout.humanize()}'
                            )
                        self._logger.debug(
                            f'{job.log_tag} Stack not yet timed out, continuing to wait'
                        )
                        continue

                    self._logger.info(
                        f'{job.log_tag} Stack has timed out ({delta.seconds}s > {stack_provisioning_timeout_secs}s), marking for deletion'
                    )
                    delete_stack = True

                elif provisioning_status in (
                    ProvisioningStatus.FAILED,
                    ProvisioningStatus.TIMEOUT,
                ):
                    self._logger.info(
                        f'{job.log_tag} Stack provisioning failed or timed out (status: {provisioning_status}), marking for deletion'
                    )
                    delete_stack = True

                if delete_stack:
                    stack_name = job.get_compute_stack()

                    self._logger.info(
                        f'{job.log_tag} Deleting ComputeStack: {stack_name}, Retry provisioning ...'
                    )

                    try:
                        self.aws_util.cloudformation_delete_stack(stack_name=stack_name)
                        self._logger.info(
                            f'{job.log_tag} Successfully initiated stack deletion for: {stack_name}'
                        )
                    except Exception as stack_delete_error:
                        self._logger.error(
                            f'{job.log_tag} Failed to delete stack {stack_name}: {stack_delete_error}'
                        )
                        # Continue with job reset even if stack deletion fails
                else:
                    self._logger.debug(f'{job.log_tag} No stack deletion required')

                self._logger.debug(f'{job.log_tag} Resetting job in scheduler')
                try:
                    self._context.scheduler.reset_job(job_id=job.job_id)
                    self._logger.debug(
                        f'{job.log_tag} Successfully reset job in scheduler'
                    )
                except Exception as reset_error:
                    self._logger.error(
                        f'{job.log_tag} Failed to reset job in scheduler: {reset_error}'
                    )
                    raise  # Re-raise to trigger the outer exception handler

                self._logger.debug(
                    f'{job.log_tag} Notifying job monitor of job modification'
                )
                try:
                    self._context.job_monitor.job_modified(job=job)
                    self._logger.debug(
                        f'{job.log_tag} Successfully notified job monitor'
                    )
                except Exception as monitor_error:
                    self._logger.error(
                        f'{job.log_tag} Failed to notify job monitor: {monitor_error}'
                    )

                self._logger.warning(
                    f'{job.log_tag} CloudFormation Stack provisioning failed or timed-out. '
                    f'Job provisioning will be re-tried.'
                )
            except Exception as e:
                job_tag = job.log_tag if job else 'Unknown Job'
                self._logger.exception(
                    f'{job_tag} provisioning retry clean-up failed. error: {e}'
                )
                # Log additional context for debugging
                if job:
                    self._logger.error(
                        f'{job_tag} Job details - ID: {job.job_id}, State: {job.state}, Provisioned: {job.provisioned}, Stack ID: {job.params.stack_id if job.params else "None"}'
                    )

    def publish_cluster_metrics_and_index_in_opensearch(self):
        nodes = self._context.scheduler.list_nodes()
        if Utils.is_empty(nodes):
            return

        try:
            total_cpus = 0
            instance_type_counts = {}
            instance_type_vcpus = {}
            instance_type_ht_support_cpus = {}
            for node in nodes:
                if node.has_state(
                    SocaComputeNodeState.BUSY,
                    SocaComputeNodeState.JOB_BUSY,
                    SocaComputeNodeState.FREE,
                ) and not node.has_state(SocaComputeNodeState.DOWN):
                    resources_available = node.resources_available
                    cpus = 0
                    if resources_available is not None:
                        cpus += Utils.get_as_int(resources_available.cpus, 0)
                        total_cpus += cpus

                    # instance type counts
                    instance_type_count = Utils.get_value_as_int(
                        node.instance_type, instance_type_counts, 0
                    )
                    instance_type_count += 1
                    instance_type_counts[node.instance_type] = instance_type_count

                    # instance type vcpus
                    instance_type_vcpu_count = Utils.get_value_as_int(
                        node.instance_type, instance_type_vcpus, 0
                    )
                    ec2_instance_type = self._context.aws_util().get_ec2_instance_type(
                        node.instance_type
                    )
                    instance_type_vcpu_count += (
                        ec2_instance_type.vcpu_info_default_vcpus
                    )
                    instance_type_vcpus[node.instance_type] = instance_type_vcpu_count

                    # instance type, ht_support cpu count
                    instance_type_ht_support_cpu_count_key = f'{node.instance_type}:{Utils.get_as_bool(node.enable_ht_support, default=False)}'
                    instance_type_ht_support_cpu_count = Utils.get_value_as_int(
                        instance_type_ht_support_cpu_count_key,
                        instance_type_ht_support_cpus,
                        0,
                    )
                    instance_type_ht_support_cpu_count += cpus
                    instance_type_ht_support_cpus[
                        instance_type_ht_support_cpu_count_key
                    ] = instance_type_ht_support_cpu_count

            BaseMetrics(context=self._context, split_dimensions=False).count(
                Value=total_cpus, MetricName='cpus_total'
            )

            for instance_type, count in instance_type_counts.items():
                BaseMetrics(
                    context=self._context, split_dimensions=False
                ).with_dimension('instance_type', instance_type).count(
                    Value=count, MetricName='total'
                )

            for instance_type, vcpus in instance_type_vcpus.items():
                BaseMetrics(
                    context=self._context, split_dimensions=False
                ).with_dimension('instance_type', instance_type).count(
                    Value=vcpus, MetricName='vcpus_total'
                )

            for (
                instance_type_ht_support_count_key,
                cpus,
            ) in instance_type_ht_support_cpus.items():
                tokens = instance_type_ht_support_count_key.split(':')
                BaseMetrics(
                    context=self._context, split_dimensions=False
                ).with_dimension(
                    'instance_type',
                    tokens[0],
                ).with_dimension(
                    'ht_support',
                    tokens[1].lower(),
                ).count(Value=cpus, MetricName='cpus_total')

        except Exception as e:
            self._logger.exception(f'failed to publish hpc cluster metrics: {e}')

        try:
            self._context.document_store.add_nodes(nodes=nodes)
        except Exception as e:
            self._logger.exception(f'Failed to publish node to opensearch: {e}')

    def begin(self):
        success = False
        start_time_ms = Utils.current_time_ms()
        try:
            self._logger.debug('node housekeeping session started ...')

            # identify potential candidates for deletion
            self.pass1_identify_potential_candidates_for_deletion()

            # compute present capacities
            self.pass2_compute_present_capacities()

            # set offline
            self.pass4_set_offline()

            # compute desired capacities
            self.pass5_compute_desired_capacities()

            # finalize resources to delete
            self.pass6_finalize_resources_to_delete()

            # perform cleanup
            self.cleanup()

            # retry provisioning clean-up
            self.retry_provisioning_cleanup()

            # publish cluster metrics and index in opensearch
            self.publish_cluster_metrics_and_index_in_opensearch()

            success = True

        except Exception as e:
            self._logger.exception(f'node housekeeping session failed: {e}')
        finally:
            if success:
                self._logger.debug('node housekeeping session completed.')
            else:
                self._context.metrics.node_housekeeping_failed()

            total_time_ms = Utils.current_time_ms() - start_time_ms
            self._context.metrics.node_housekeeping_duration(duration_ms=total_time_ms)

    def kill(self):
        pass


class NodeHouseKeeper:
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = self._context.logger(name='node_housekeeper')
        self._is_running = Event()
        self._house_keeping_thread: Optional[Thread] = None

    def do_job(self):
        session = NodeHouseKeepingSession(context=self._context, logger=self._logger)
        session.begin()

    def invoke(self):
        # this is a fail safe in the event when instance cache or job cache is stale, or has not yet initialized.
        if not self._context.is_ready():
            self._logger.warning('job provisioning not ready. skip.')
            return

        # allow only one instance of housekeeper to run at any given time.
        if self._is_running.is_set():
            return

        try:
            self._is_running.set()

            self._house_keeping_thread = Thread(
                name='node-housekeeper', target=self.do_job
            )
            self._house_keeping_thread.start()

        finally:
            self._is_running.clear()

    def stop(self):
        if not self._is_running.is_set():
            return
        if self._house_keeping_thread is None:
            return
        self._house_keeping_thread.join()
