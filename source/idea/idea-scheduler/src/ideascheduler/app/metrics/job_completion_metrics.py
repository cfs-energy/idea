from ideadatamodel import SocaJob
from ideasdk.context import SocaContext
from ideasdk.metrics import BaseMetrics
from ideasdk.utils import Utils
from ideasdk.metrics.history_backfill import CapturingPublisher

from typing import Optional
import time

# used CPU time over allocated CPU time can read slightly over one when hyperthreads
# are counted; past this it is an accounting error and is not reported at all.
CPU_EFFICIENCY_MAX = 1.05


class JobCompletionMetrics(BaseMetrics):
    """
    One set of measurements per finished job, tagged the way spend gets sliced: what the
    job was estimated to cost, how long it ran on the wall clock and how much of the CPU it
    asked for it used.

    Published once from the finished-job processor. The duration comes from the job's own
    start and end stamps; total_time_secs is what the price estimate used and can differ.
    """

    def __init__(self, context: SocaContext, job: SocaJob):
        super().__init__(context, split_dimensions=False)
        self.job = job
        params = job.params
        instance_type = self.instance_type(job)
        self.with_dimension('project', self.tag(job.project))
        self.with_dimension('owner', self.tag(job.owner))
        self.with_dimension('queue', self.tag(job.queue))
        self.with_dimension('queue_type', self.tag(job.queue_type))
        self.with_dimension('state', self.tag(job.state))
        self.with_dimension('idea_cluster', self.tag(context.cluster_name()))
        # Existing dashboards still select the legacy cluster dimension.
        # Keep both for this release; cluster goes away after 26.10.
        self.with_dimension('cluster', self.tag(context.cluster_name()))
        self.with_dimension(
            'instance_family', self.tag(self.instance_family(instance_type))
        )
        self.with_dimension('capacity_type', self.capacity_type(job))
        self.with_dimension('base_os', self.tag(params.base_os if params else None))
        self.with_dimension('job_outcome', self.outcome(job))
        self.with_dimension(
            'gpu', 'true' if params and (params.gpus or 0) > 0 else 'false'
        )

    @staticmethod
    def tag(value) -> str:
        return str(value) if Utils.is_not_empty(value) else 'unknown'

    @staticmethod
    def instance_type(job: SocaJob) -> Optional[str]:
        for host in job.execution_hosts or []:
            if Utils.is_not_empty(host.instance_type):
                return host.instance_type
        if job.params and job.params.instance_types:
            return job.params.instance_types[0]
        return None

    @staticmethod
    def instance_family(instance_type: Optional[str]) -> Optional[str]:
        if Utils.is_empty(instance_type):
            return None
        return instance_type.split('.', 1)[0]

    @staticmethod
    def capacity_type(job: SocaJob) -> str:
        for host in job.execution_hosts or []:
            if host.capacity_type is not None:
                return str(host.capacity_type.value)
        if job.params and job.params.spot:
            return 'spot'
        return 'on-demand'

    @staticmethod
    def outcome(job: SocaJob) -> str:
        """
        PBS exit status: 0 ran to completion, negative never ran on the node (requeue,
        pre-execution failure), above 128 was killed by a signal, otherwise the
        application failed.
        """
        if job.start_time is None:
            return 'unprovisioned'
        status = job.exit_status
        if status is None:
            return 'unknown'
        if status == 0:
            return 'success'
        if status < 0:
            return 'requeued'
        if status > 128:
            return 'killed'
        return 'failure'

    @staticmethod
    def wall_seconds(job: SocaJob) -> Optional[float]:
        if job.start_time is None or job.end_time is None:
            return None
        seconds = (job.end_time - job.start_time).total_seconds()
        return seconds if seconds > 0 else None

    @classmethod
    def cpu_efficiency(cls, job: SocaJob) -> Optional[float]:
        cpus = job.params.cpus if job.params else None
        wall = cls.wall_seconds(job)
        if not cpus or cpus <= 0 or wall is None:
            return None
        used = 0.0
        for host in job.execution_hosts or []:
            runs = host.execution.runs if host.execution else None
            for run in runs or []:
                if run.resources_used and run.resources_used.cpu_time_secs:
                    used += run.resources_used.cpu_time_secs
        if used <= 0:
            return None
        efficiency = used / (cpus * wall)
        if efficiency > CPU_EFFICIENCY_MAX:
            return None
        return min(efficiency, 1.0)

    def publish(self):
        job = self.job
        # A completion record without an end stamp is still a completed job; its points
        # are stamped now rather than dropped, so every job stays in the counts.
        timestamp = int(job.end_time.timestamp()) if job.end_time else int(time.time())
        self.count(MetricName='job.count', Value=1, Timestamp=timestamp)

        wall = self.wall_seconds(job)
        if wall is not None:
            self.seconds(
                MetricName='job.duration_seconds', Value=wall, Timestamp=timestamp
            )

        cost = job.estimated_bom_cost
        if cost is not None and cost.price_unavailable:
            self.count(MetricName='job.price_unavailable', Value=1, Timestamp=timestamp)
        elif cost is not None:
            if cost.total is not None and cost.total.amount is not None:
                self.count(
                    MetricName='job.cost', Value=cost.total.amount, Timestamp=timestamp
                )
            if (
                cost.line_items_total is not None
                and cost.line_items_total.amount is not None
            ):
                self.count(
                    MetricName='job.cost_ondemand',
                    Value=cost.line_items_total.amount,
                    Timestamp=timestamp,
                )
            if cost.savings_total is not None and cost.savings_total.amount is not None:
                self.count(
                    MetricName='job.savings',
                    Value=cost.savings_total.amount,
                    Timestamp=timestamp,
                )

        efficiency = self.cpu_efficiency(job)
        if efficiency is not None:
            self._log(
                MetricName='job.cpu_efficiency',
                Timestamp=timestamp,
                Value=efficiency,
                MetricType='Summary',
                Unit='None',
            )

        # Unique job labels persist indefinitely in pull-based exporters.
        # Only the event transport can carry detail without retaining every job locally.
        if (
            cost is not None
            and not cost.price_unavailable
            and cost.total is not None
            and cost.total.amount is not None
            and self.metrics_provider == 'dogstatsd'
        ):
            detail = ('job_id', 'job_uid', 'instance_type')
            self.with_dimension('job_id', self.tag(job.job_id))
            self.with_dimension('job_uid', self.tag(job.job_uid))
            self.with_dimension('instance_type', self.tag(self.instance_type(job)))
            try:
                self.count(
                    MetricName='job.detail.cost',
                    Value=cost.total.amount,
                    Timestamp=timestamp,
                )
            finally:
                for name in detail:
                    self.without_dimension(name)


class JobCompletionBatch(CapturingPublisher):
    def config(self):
        return self.context.config()

    def flush(self):
        if not self.entries:
            return
        totals = {}
        points = []
        for entry in self.entries:
            if entry['MetricName'] not in {
                'job.count',
                'job.cost',
                'job.cost_ondemand',
                'job.savings',
                'job.price_unavailable',
            }:
                points.append(entry)
                continue
            identity = (
                entry.get('Namespace'),
                entry['MetricName'],
                int(entry['Timestamp']),
                tuple(sorted((d['Name'], d['Value']) for d in entry['Dimensions'])),
            )
            if identity in totals:
                totals[identity]['Value'] += entry['Value']
            else:
                totals[identity] = dict(entry)
                points.append(totals[identity])
        publisher = self.context.service_registry().get_service('metrics-service')
        for point in points:
            publisher.publish([point])
        self.entries.clear()
