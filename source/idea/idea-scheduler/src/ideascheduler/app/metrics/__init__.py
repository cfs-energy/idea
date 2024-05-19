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


from ideasdk.context import SocaContext
from ideasdk.metrics import BaseMetrics, BaseAccumulator

from threading import RLock


class ProvisioningQueueMetrics(BaseMetrics):
    def __init__(self, context: SocaContext, queue_type: str):
        super().__init__(context)
        self.with_dimension('queue_profile', queue_type)
        self.split_dimensions = False

        self.jobs_pending = self.build_counter(name='jobs_pending')
        self.jobs_provisioned = self.build_counter(name='jobs_provisioned')
        self.jobs_running = self.build_counter(name='jobs_running')
        self.jobs_finished = self.build_counter(name='jobs_finished')
        self.nodes_added = self.build_counter(name='nodes_added')
        self.nodes_ready = self.build_counter(name='nodes_ready')
        self.nodes_idle = self.build_counter(name='nodes_idle')
        self.nodes_busy = self.build_counter(name='nodes_busy')
        self.nodes_down = self.build_counter(name='nodes_down')
        self.nodes_offline = self.build_counter(name='nodes_offline')
        self.nodes_idle = self.build_counter(name='nodes_idle')
        self.nodes_deleted = self.build_counter(name='nodes_deleted')
        self.instances_running = self.build_counter(name='instances_running')
        self.instances_terminated = self.build_counter(name='instances_terminated')
        self.spotfleet_capacity_increased = self.build_counter(name='spotfleet_capacity_increased')
        self.spotfleet_capacity_decreased = self.build_counter(name='spotfleet_capacity_decreased')
        self.asg_capacity_increased = self.build_counter(name='asg_capacity_increased')
        self.asg_capacity_decreased = self.build_counter(name='asg_capacity_decreased')
        self.spotfleet_created = self.build_counter(name='spotfleet_created')
        self.spotfleet_deleted = self.build_counter(name='spotfleet_deleted')
        self.asg_deleted = self.build_counter(name='asg_created')
        self.asg_created = self.build_counter(name='asg_deleted')
        self.stacks_created = self.build_counter(name='stack_created')
        self.stacks_deleted = self.build_counter(name='stack_deleted')
        self.reserved_capacity_unavailable = self.build_counter(name='reserved_capacity_unavailable')
        self.service_quota_unavailable = self.build_counter(name='service_quota_unavailable')


class JobProvisioningMetrics(BaseMetrics, BaseAccumulator):
    def __init__(self, context: SocaContext):
        super().__init__(context)

        self.context = context
        self.split_dimensions = False

        self._jobs_pending = self.build_counter(name='jobs_pending')
        self._jobs_provisioned = self.build_counter(name='jobs_provisioned')
        self._jobs_running = self.build_counter(name='jobs_running')
        self._jobs_finished = self.build_counter(name='jobs_finished')
        self._nodes_added = self.build_counter(name='nodes_added')
        self._nodes_ready = self.build_counter(name='nodes_ready')
        self._nodes_idle = self.build_counter(name='nodes_idle')
        self._nodes_busy = self.build_counter(name='nodes_busy')
        self._nodes_down = self.build_counter(name='nodes_down')
        self._nodes_offline = self.build_counter(name='nodes_offline')
        self._nodes_deleted = self.build_counter(name='nodes_deleted')
        self._instances_running = self.build_counter(name='instances_running')
        self._instances_terminated = self.build_counter(name='instances_terminated')
        self._spotfleet_capacity_increased = self.build_counter(name='spotfleet_capacity_increased')
        self._spotfleet_capacity_decreased = self.build_counter(name='spotfleet_capacity_decreased')
        self._asg_capacity_increased = self.build_counter(name='asg_capacity_increased')
        self._asg_capacity_decreased = self.build_counter(name='asg_capacity_decreased')
        self._spotfleet_created = self.build_counter(name='spotfleet_created')
        self._spotfleet_deleted = self.build_counter(name='spotfleet_deleted')
        self._asg_deleted = self.build_counter(name='asg_created')
        self._asg_created = self.build_counter(name='asg_deleted')
        self._stacks_created = self.build_counter(name='stack_created')
        self._stacks_deleted = self.build_counter(name='stack_deleted')
        self._reserved_capacity_unavailable = self.build_counter(name='reserved_capacity_unavailable')
        self._service_quota_unavailable = self.build_counter(name='service_quota_unavailable')

        self._queue_type_metrics = {}
        self._queue_type_metrics_lock = RLock()

    @property
    def accumulator_id(self):
        return 'job-provisioning-metrics'

    def get_queue_metrics(self, queue_type: str) -> ProvisioningQueueMetrics:
        if queue_type in self._queue_type_metrics:
            return self._queue_type_metrics[queue_type]

        with self._queue_type_metrics_lock:
            # check again
            if queue_type in self._queue_type_metrics:
                return self._queue_type_metrics[queue_type]

            queue_type_metrics = ProvisioningQueueMetrics(
                context=self.context,
                queue_type=queue_type
            )
            self._queue_type_metrics[queue_type] = queue_type_metrics
            return queue_type_metrics

    def publish_metrics(self):
        for counter in self.counters:
            value = counter.get()
            if value is None:
                continue
            self.count(MetricName=counter.name, Value=value)

        for queue_metrics in self._queue_type_metrics.values():
            for counter in queue_metrics.counters:
                value = counter.get()
                if value is None:
                    continue
                queue_metrics.count(MetricName=counter.name, Value=value)

    def nodes_added(self, queue_type: str, count=1):
        self._nodes_added.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_added.increment(count)

    def nodes_ready(self, queue_type: str, count=1):
        self._nodes_ready.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_ready.increment(count)

    def nodes_ready_duration(self, queue_type: str, duration_secs: int):
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='nodes_ready_duration', Value=duration_secs)

    def nodes_idle(self, queue_type: str, count=1):
        self._nodes_idle.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_idle.increment(count)

    def nodes_busy(self, queue_type: str, count=1):
        self._nodes_busy.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_busy.increment(count)

    def nodes_down(self, queue_type: str, count=1):
        self._nodes_down.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_down.increment(count)

    def nodes_offline(self, queue_type: str, count=1):
        self._nodes_offline.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_offline.increment(count)

    def nodes_deleted(self, queue_type: str, count=1):
        self._nodes_deleted.increment(count)
        self.get_queue_metrics(queue_type=queue_type).nodes_deleted.increment(count)

    def instances_running(self, queue_type: str, count=1):
        self._instances_running.increment(count)
        self.get_queue_metrics(queue_type=queue_type).instances_running.increment(count)

    def instances_terminated(self, queue_type: str, count=1):
        self._instances_terminated.increment(count)
        self.get_queue_metrics(queue_type=queue_type).instances_terminated.increment(count)

    def instances_running_duration(self, queue_type: str, duration_secs: int):
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='instances_running_duration', Value=duration_secs)

    def spotfleet_capacity_increased(self, queue_type: str, count=1):
        self._spotfleet_capacity_increased.increment(count)
        self.get_queue_metrics(queue_type=queue_type).spotfleet_capacity_increased.increment(count)

    def spotfleet_capacity_decreased(self, queue_type: str, count=1):
        self._spotfleet_capacity_decreased.increment(count)
        self.get_queue_metrics(queue_type=queue_type).spotfleet_capacity_decreased.increment(count)

    def asg_capacity_increased(self, queue_type: str, count=1):
        self._asg_capacity_increased.increment(count)
        self.get_queue_metrics(queue_type=queue_type).asg_capacity_increased.increment(count)

    def asg_capacity_decreased(self, queue_type: str, count=1):
        self._asg_capacity_decreased.increment(count)
        self.get_queue_metrics(queue_type=queue_type).asg_capacity_decreased.increment(count)

    def spotfleet_created(self, queue_type: str, count=1):
        self._spotfleet_created.increment(count)
        self.get_queue_metrics(queue_type=queue_type).spotfleet_created.increment(count)

    def spotfleet_deleted(self, queue_type: str, count=1):
        self._spotfleet_deleted.increment(count)
        self.get_queue_metrics(queue_type=queue_type).spotfleet_deleted.increment(count)

    def asg_deleted(self, queue_type: str, count=1):
        self._asg_deleted.increment(count)
        self.get_queue_metrics(queue_type=queue_type).asg_deleted.increment(count)

    def asg_created(self, queue_type: str, count=1):
        self._asg_created.increment(count)
        self.get_queue_metrics(queue_type=queue_type).asg_created.increment(count)

    def stacks_created(self, queue_type: str, count=1):
        self._stacks_created.increment(count)
        self.get_queue_metrics(queue_type=queue_type).stacks_created.increment(count)

    def stacks_deleted(self, queue_type: str, count=1):
        self._stacks_deleted.increment(count)
        self.get_queue_metrics(queue_type=queue_type).jobs_pending.increment(count)

    def jobs_pending(self, queue_type: str, count=1):
        """
        pending jobs are those jobs in job provisioning queue that are not yet processed by
        JobProvisioner.
        these are jobs that could be pending due to max instance or running job limits or any
        other errors preventing provisioning to complete.
        :param queue_type:
        :param count:
        :return:
        """
        self._jobs_pending.increment(count)
        self.get_queue_metrics(queue_type=queue_type).jobs_pending.increment(count)

    def jobs_provisioned(self, queue_type: str, count=1):
        self._jobs_provisioned.increment(count)
        self.get_queue_metrics(queue_type=queue_type).jobs_provisioned.increment(count)

    def jobs_running(self, queue_type: str, count=1):
        self._jobs_running.increment(count)
        self.get_queue_metrics(queue_type=queue_type).jobs_running.increment(count)

    def jobs_finished(self, queue_type: str, count=1):
        self._jobs_finished.increment(count)
        self.get_queue_metrics(queue_type=queue_type).jobs_finished.increment(count)

    def jobs_pending_duration(self, queue_type: str, duration_secs: int):
        """
        total amount of time taken for job from being queued to being processed by JobProvisioner
        :param queue_type: soca queue type
        :param duration_secs: duration in seconds
        """
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='jobs_pending_duration', Value=duration_secs)

    def jobs_provisioning_duration(self, queue_type: str, duration_secs: int):
        """
        total amount of time taken for job from being processed by JobProvisioner to running state
        :param queue_type: soca queue type
        :param duration_secs: duration in seconds
        """
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='jobs_provisioning_duration', Value=duration_secs)

    def jobs_running_duration(self, queue_type: str, duration_secs: int):
        """
        total amount of time taken for job from running state to finished state
        :param queue_type: soca queue type
        :param duration_secs: duration in seconds
        """
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='jobs_running_duration', Value=duration_secs)

    def jobs_total_duration(self, queue_type: str, duration_secs: int):
        """
        total amount of time taken for job from queued state to finished state
        :param queue_type: soca queue type
        :param duration_secs: duration in seconds
        """
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.seconds(MetricName='jobs_total_duration', Value=duration_secs)

    def job_provisioning_failed(self, queue_type: str, error_code: int):
        metrics = BaseMetrics(self._context, split_dimensions=True)
        metrics.with_required_dimension('error_code', error_code)
        metrics.with_dimension('queue_profile', queue_type)
        metrics.count(MetricName='count')

    def node_housekeeping_failed(self):
        self.count(MetricName='node_housekeeping_failed')

    def job_cache_sync_failed(self):
        self.count(MetricName='job_cache_sync_failed')

    def instance_cache_sync_failed(self):
        self.count(MetricName='instance_cache_sync_failed')

    def node_housekeeping_duration(self, duration_ms: int):
        self.milliseconds(MetricName='node_housekeeping_duration', Value=duration_ms)
