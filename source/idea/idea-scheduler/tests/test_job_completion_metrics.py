"""
job completion metrics: the outcome, duration and CPU efficiency derived from a finished
job, and the tags they carry.
"""

from ideadatamodel.scheduler.scheduler_model import (
    SocaJob,
    SocaJobParams,
    SocaJobExecutionHost,
    SocaJobExecution,
    SocaJobExecutionRun,
    SocaJobExecutionResourcesUsed,
    SocaCapacityType,
)
from ideascheduler.app.metrics.job_completion_metrics import JobCompletionMetrics
from ideadatamodel import SocaAmount
from ideadatamodel.scheduler.scheduler_model import SocaJobEstimatedBOMCost

import arrow


def _job(
    exit_status=0, cpus=4, wall_secs=3600, cpu_time_secs=None, started=True, **kwargs
):
    start = arrow.get('2026-09-09T10:00:00+00:00').datetime
    runs = []
    if cpu_time_secs is not None:
        runs.append(
            SocaJobExecutionRun(
                run_id='1',
                resources_used=SocaJobExecutionResourcesUsed(
                    cpu_time_secs=cpu_time_secs
                ),
            )
        )
    return SocaJob(
        job_id='42',
        project='fusion',
        owner='alice',
        queue='normal',
        queue_type='compute',
        exit_status=exit_status,
        start_time=start if started else None,
        end_time=arrow.get(start).shift(seconds=wall_secs).datetime
        if started
        else None,
        params=SocaJobParams(
            cpus=cpus, gpus=0, base_os='rhel9', instance_types=['c7g.2xlarge']
        ),
        execution_hosts=[
            SocaJobExecutionHost(
                instance_type='c7g.2xlarge',
                capacity_type=SocaCapacityType.SPOT,
                execution=SocaJobExecution(run_count=len(runs), runs=runs),
            )
        ],
        **kwargs,
    )


def test_outcome_follows_the_pbs_exit_status():
    assert JobCompletionMetrics.outcome(_job(exit_status=0)) == 'success'
    assert JobCompletionMetrics.outcome(_job(exit_status=1)) == 'failure'
    assert JobCompletionMetrics.outcome(_job(exit_status=-3)) == 'requeued'
    assert JobCompletionMetrics.outcome(_job(exit_status=137)) == 'killed'
    assert JobCompletionMetrics.outcome(_job(exit_status=None)) == 'unknown'
    assert JobCompletionMetrics.outcome(_job(started=False)) == 'unprovisioned'


def test_duration_is_wall_clock_from_the_job_stamps():
    assert JobCompletionMetrics.wall_seconds(_job(wall_secs=86514)) == 86514
    assert JobCompletionMetrics.wall_seconds(_job(started=False)) is None


def test_cpu_efficiency_is_used_over_allocated_and_drops_nonsense():
    # 4 cpus for an hour, 3 cpu-hours used
    assert (
        JobCompletionMetrics.cpu_efficiency(
            _job(cpus=4, wall_secs=3600, cpu_time_secs=10800)
        )
        == 0.75
    )
    # hyperthread accounting can read a little over; capped, not dropped
    assert (
        JobCompletionMetrics.cpu_efficiency(
            _job(cpus=4, wall_secs=3600, cpu_time_secs=14500)
        )
        == 1.0
    )
    # ratios in the hundreds are an accounting error, so they are not reported at all
    assert (
        JobCompletionMetrics.cpu_efficiency(
            _job(cpus=4, wall_secs=3600, cpu_time_secs=5_000_000)
        )
        is None
    )
    assert JobCompletionMetrics.cpu_efficiency(_job(cpus=4, wall_secs=3600)) is None


def test_dimensions_are_the_cost_dashboard_slices(context):
    metrics = JobCompletionMetrics(context=context, job=_job(exit_status=0))
    assert {d['Name']: d['Value'] for d in metrics.dimensions} == {
        'project': 'fusion',
        'owner': 'alice',
        'queue': 'normal',
        'queue_type': 'compute',
        'instance_family': 'c7g',
        'capacity_type': 'spot',
        'base_os': 'rhel9',
        'job_outcome': 'success',
        'gpu': 'false',
    }


class _Recorder:
    def __init__(self):
        self.entries = []

    def publish(self, metric_data):
        self.entries.extend(metric_data)


def _published(context, monkeypatch, provider, job):
    recorder = _Recorder()
    monkeypatch.setattr(
        context.service_registry(),
        'get_service',
        lambda name: recorder if name == 'metrics-service' else None,
    )
    monkeypatch.setattr(
        JobCompletionMetrics, 'metrics_provider', property(lambda self: provider)
    )
    JobCompletionMetrics(context=context, job=job).publish()
    return recorder.entries


def test_detail_cost_carries_the_job_identity_and_only_off_cloudwatch(
    context, monkeypatch
):
    job = _job(
        exit_status=0,
        job_uid='42-r1',
        estimated_bom_cost=SocaJobEstimatedBOMCost(total=SocaAmount(amount=1.25)),
    )
    entries = _published(context, monkeypatch, 'dogstatsd', job)
    by_name = {e['MetricName']: e for e in entries}
    detail = {d['Name']: d['Value'] for d in by_name['job.detail.cost']['Dimensions']}
    assert detail['job_id'] == '42'
    assert detail['job_uid'] == '42-r1'
    assert detail['instance_type'] == 'c7g.2xlarge'
    assert detail['project'] == 'fusion', 'the aggregate slices ride along'
    assert by_name['job.detail.cost']['Value'] == 1.25
    # the aggregate families stay bounded: no job identity on them
    aggregate = {d['Name'] for d in by_name['job.cost']['Dimensions']}
    assert 'job_id' not in aggregate and 'job_uid' not in aggregate

    cloudwatch = {
        e['MetricName'] for e in _published(context, monkeypatch, 'cloudwatch', job)
    }
    assert 'job.cost' in cloudwatch and 'job.detail.cost' not in cloudwatch
