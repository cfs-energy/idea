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
    constants, EC2Instance, EC2InstanceMonitorEvent, SocaComputeNode
)
from ideasdk.service import SocaService
from ideasdk.pubsub import SocaPubSub
from ideasdk.utils import Utils
from ideascheduler.app.app_protocols import NodeMonitorProtocol
from ideascheduler.app.provisioning import NodeHouseKeeper

from typing import Optional, Set
from threading import Thread, Event, RLock, Condition


class NodeMonitorState:
    """
    Thread safe implementation for node monitoring state

    synchronize invocations from multiple sources:
        > Node Monitor Thread
        > SQS Events from Nodes
        > EC2 Instance Monitoring Thread

    doc reference:
    HOUSEKEEPING_INTERVAL_SECS = config key: scheduler.job_provisioning.node_housekeeping_interval_seconds
    """

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context

        self._running_instances_lock = RLock()
        self._running_instances: Set[str] = set()
        self.running_instances_event = Event()

        self.instance_cache_refresh_event = Event()

        self._last_housekeeper_run = None

    def get_running_instances(self) -> Set[str]:
        """
        return a copy of current state and clear in one atomic operation
        :return: Set of running InstanceIds
        """
        with self._running_instances_lock:
            running_instances = set(self._running_instances)
            self._running_instances.clear()
        self.running_instances_event.clear()
        return running_instances

    def clear_all(self):
        with self._running_instances_lock:
            self._running_instances.clear()

        self.instance_cache_refresh_event.clear()

    def add_running_instance(self, instance: EC2Instance):
        with self._running_instances_lock:
            self._running_instances.add(instance.instance_id)
        self.running_instances_event.set()

    def should_perform_housekeeping(self):
        is_expired = Utils.is_interval_expired(
            last_run_ms=self._last_housekeeper_run,
            now_ms=Utils.current_time_ms(),
            interval_secs=self._context.config().get_int('scheduler.job_provisioning.node_housekeeping_interval_seconds', default=60)
        )

        if is_expired:
            self._last_housekeeper_run = Utils.current_time_ms()

        return is_expired


class NodeMonitor(SocaService, NodeMonitorProtocol):
    """
    monitors compute nodes in soca cluster

    nodes are added or provisioned near-realtime (as soon as instance is in RUNNING state)

    node resources are cleaned up periodically by ``NodeHouseKeeper``, with an interval of HOUSEKEEPING_INTERVAL_SECS.
    """

    def __init__(self, context: ideascheduler.AppContext):
        super().__init__(context)
        self._context = context
        self._logger = context.logger('node_monitor')

        self._topic: Optional[SocaPubSub] = None
        self._instance_monitor_topic: Optional[SocaPubSub] = None
        self._node_monitor_thread: Optional[Thread] = None
        self._housekeeper: Optional[NodeHouseKeeper] = None
        self._monitor: Optional[Condition] = None
        self._state: Optional[NodeMonitorState] = None
        self._instance_updates_available_event: Optional[Event] = None
        self._exit: Optional[Event] = None
        self._is_running = False

    def _initialize(self):
        self._topic = SocaPubSub(constants.TOPIC_NODE_MONITOR_EVENTS)
        self._instance_monitor_topic = SocaPubSub(constants.TOPIC_EC2_INSTANCE_MONITOR_EVENTS)

        self._node_monitor_thread = Thread(
            name='node-monitor',
            target=self._monitor_nodes
        )
        self._housekeeper = NodeHouseKeeper(self._context)
        self._monitor = Condition()
        self._state = NodeMonitorState(context=self._context)
        self._instance_updates_available_event = Event()
        self._exit = Event()

    def _instance_monitor_subscriber(self, _, message: EC2InstanceMonitorEvent):
        if message.type == constants.EC2_INSTANCE_MONITOR_EVENT_CACHE_REFRESH:
            self._state.instance_cache_refresh_event.set()
        elif message.type == constants.EC2_INSTANCE_MONITOR_EVENT_INSTANCE_STATE_RUNNING:
            self._state.add_running_instance(instance=message.instance)
        self._instance_updates_available_event.set()

    def _check_and_provision_node(self, instance: EC2Instance):
        """
        given an ec2 instance, checks and provisions the node.
        :param instance:
        :return:
        """
        try:

            if not instance.is_running:
                return

            if not instance.is_current_cluster(self._context.cluster_name()):
                return

            create_or_provision = SocaComputeNode.from_ec2_instance(instance)
            if create_or_provision is None:
                return

            existing_node = self._context.scheduler.get_node(host=create_or_provision.host)

            if existing_node is not None:
                return

            job = self._context.aws_util().get_soca_job_from_stack(
                stack_name=create_or_provision.compute_stack
            )

            if job is not None:
                create_or_provision.job_id = job.job_id
                create_or_provision.job_group = job.get_job_group()
                create_or_provision.keep_ebs_volumes = job.params.keep_ebs_volumes
                create_or_provision.root_storage_size = job.params.root_storage_size
                create_or_provision.scratch_storage_size = job.params.scratch_storage_size
                create_or_provision.scratch_storage_iops = job.params.scratch_storage_iops
                create_or_provision.enable_efa_support = job.params.enable_efa_support
                create_or_provision.force_reserved_instances = job.params.force_reserved_instances
                create_or_provision.enable_system_metrics = job.params.enable_system_metrics
                create_or_provision.enable_anonymous_metrics = job.params.enable_anonymous_metrics
                create_or_provision.base_os = job.params.base_os
                create_or_provision.spot = job.params.spot
                create_or_provision.spot_price = job.params.spot_price
                create_or_provision.fsx_lustre = job.params.fsx_lustre
                create_or_provision.enable_ht_support = job.params.enable_ht_support
                create_or_provision.enable_placement_group = job.params.enable_placement_group

            self._context.scheduler.create_node(
                node=create_or_provision
            )
            created_node = self._context.scheduler.get_node(host=create_or_provision.host)

            self._logger.info(f'({str(created_node)}) node created - waiting to be ready.')
            self._context.metrics.nodes_added(queue_type=instance.soca_queue_type)

        except Exception as e:
            self._logger.exception(f'failed to provision compute node - '
                                   f'queue_type: {instance.soca_queue_type}, '
                                   f'queue: {instance.soca_job_queue}, '
                                   f'host: {instance.private_dns_name}, '
                                   f'instance_id: {instance.instance_id}, '
                                   f'Error: {e}')

    def _monitor_nodes(self):
        while not self._exit.is_set():
            try:

                self._instance_updates_available_event.clear()

                # (full scan) this will be executed every time InstanceMonitor performs a full refresh
                if self._state.instance_cache_refresh_event.is_set():

                    self._state.instance_cache_refresh_event.clear()

                    compute_instances = self._context.instance_cache.list_compute_instances(
                        cluster_name=self._context.cluster_name()
                    )
                    for instance in compute_instances:
                        self._check_and_provision_node(instance=instance)

                # (instance delta updates) one or more instances are updated, let's apply these changes
                if self._state.running_instances_event.is_set():
                    updated_instances = self._state.get_running_instances()
                    for instance_id in updated_instances:
                        instance = self._context.instance_cache.get_instance(
                            instance_id=instance_id
                        )
                        if instance is None:
                            continue
                        self._check_and_provision_node(instance=instance)

                # (housekeeping) will be performed every HOUSEKEEPING_INTERVAL_SECS
                #   - housekeeping is an async operation, and should not block node provisioning.
                #   - provisioning and housekeeping is decoupled.
                #   - ony one session is active at any given time.
                if self._state.should_perform_housekeeping():
                    self._housekeeper.invoke()

            except Exception as e:
                self._logger.exception(f'scheduler node monitor iteration failed: {e}')
            finally:
                try:
                    self._monitor.acquire()
                    # instance updates if any, will be processed at an interval of 1 second
                    self._monitor.wait_for(self._instance_updates_available_event.is_set, 1)
                finally:
                    self._monitor.release()

    def start(self):
        if self._is_running:
            return
        self._initialize()
        self._is_running = True
        self._node_monitor_thread.start()
        self._instance_monitor_topic.subscribe(self._instance_monitor_subscriber)

    def stop(self):
        if not self._is_running:
            return
        self._instance_monitor_topic.unsubscribe(self._instance_monitor_subscriber)
        self._exit.set()
        self._is_running = False
        self._node_monitor_thread.join()
        self._housekeeper.stop()
