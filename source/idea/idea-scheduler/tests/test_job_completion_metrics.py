"""
job completion metrics: the outcome, duration and CPU efficiency derived from a finished
job, and the tags they carry.
"""

from ideadatamodel.scheduler.scheduler_model import (
    SocaJob,
    SocaJobState,
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
import pytest
import time


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
    metrics = JobCompletionMetrics(
        context=context, job=_job(exit_status=0, state=SocaJobState.FINISHED)
    )
    assert {d['Name']: d['Value'] for d in metrics.dimensions} == {
        'project': 'fusion',
        'owner': 'alice',
        'queue': 'normal',
        'queue_type': 'compute',
        'state': JobCompletionMetrics.tag(SocaJobState.FINISHED),
        'cluster': context.cluster_name(),
        'idea_cluster': context.cluster_name(),
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


def test_detail_cost_carries_the_job_identity_on_dogstatsd_only(context, monkeypatch):
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

    for provider in ['cloudwatch', 'prometheus', 'unknown']:
        names = {
            e['MetricName'] for e in _published(context, monkeypatch, provider, job)
        }
        assert 'job.cost' in names and 'job.detail.cost' not in names


def test_every_point_uses_job_end_time(context, monkeypatch):
    from unittest.mock import Mock

    job = _job(
        cpu_time_secs=10800,
        estimated_bom_cost=SocaJobEstimatedBOMCost(
            total=SocaAmount(amount=1.25),
            line_items_total=SocaAmount(amount=2),
            savings_total=SocaAmount(amount=0.75),
        ),
    )
    entries = _published(context, monkeypatch, 'dogstatsd', job)
    assert {entry['MetricName'] for entry in entries} == {
        'job.count',
        'job.duration_seconds',
        'job.cost',
        'job.cost_ondemand',
        'job.savings',
        'job.cpu_efficiency',
        'job.detail.cost',
    }
    assert all(entry['Timestamp'] == int(job.end_time.timestamp()) for entry in entries)
    assert _published(context, monkeypatch, 'dogstatsd', job) == entries
    metrics = JobCompletionMetrics(context, job)
    metrics.count = Mock()
    metrics.publish()
    metrics.count.assert_any_call(
        MetricName='job.count', Value=1, Timestamp=int(job.end_time.timestamp())
    )


def test_unpriced_job_emits_unavailable_instead_of_cost(context, monkeypatch):
    entries = []
    for unavailable in (False, True):
        job = _job(
            estimated_bom_cost=SocaJobEstimatedBOMCost(
                total=SocaAmount(amount=1.25),
                line_items_total=SocaAmount(amount=2),
                savings_total=SocaAmount(amount=0.75),
                price_unavailable=unavailable,
            )
        )
        entries.extend(_published(context, monkeypatch, 'dogstatsd', job))
    for name in ('job.cost', 'job.cost_ondemand', 'job.savings', 'job.detail.cost'):
        assert len([entry for entry in entries if entry['MetricName'] == name]) == 1
    cost = next(entry for entry in entries if entry['MetricName'] == 'job.cost')
    missing = [
        entry for entry in entries if entry['MetricName'] == 'job.price_unavailable'
    ]
    assert len(missing) == 1
    assert missing[0]['Value'] == 1
    assert missing[0]['MetricType'] == 'Counter'
    assert missing[0]['Timestamp'] == int(job.end_time.timestamp())
    assert missing[0]['Dimensions'] == cost['Dimensions']


def test_job_without_end_time_is_stamped_now(context, monkeypatch):
    before = int(time.time())
    entries = _published(context, monkeypatch, 'dogstatsd', _job(started=False))
    assert [entry['MetricName'] for entry in entries][:1] == ['job.count']
    assert all(before <= entry['Timestamp'] <= int(time.time()) for entry in entries)


@pytest.mark.parametrize('unavailable', [False, True])
def test_batch_sums_jobs_finishing_in_the_same_second(
    context, monkeypatch, unavailable
):
    from ideascheduler.app.metrics.job_completion_metrics import JobCompletionBatch

    recorder = _Recorder()
    monkeypatch.setattr(context.service_registry(), 'get_service', lambda _: recorder)
    monkeypatch.setattr(
        JobCompletionMetrics, 'metrics_provider', property(lambda _: 'dogstatsd')
    )
    batch = JobCompletionBatch(context)
    for index in range(2):
        job = _job(
            job_uid=str(index),
            cpu_time_secs=3600,
            estimated_bom_cost=SocaJobEstimatedBOMCost(
                total=SocaAmount(amount=1.25),
                line_items_total=SocaAmount(amount=2),
                savings_total=SocaAmount(amount=0.75),
                price_unavailable=unavailable,
            ),
        )
        JobCompletionMetrics(batch, job).publish()
    batch.flush()
    counts = [point for point in recorder.entries if point['MetricName'] == 'job.count']
    assert len(counts) == 1
    assert counts[0]['Value'] == 2
    durations = [
        point
        for point in recorder.entries
        if point['MetricName'] == 'job.duration_seconds'
    ]
    assert len(durations) == 2
    assert (
        len(
            [
                point
                for point in recorder.entries
                if point['MetricName'] == 'job.cpu_efficiency'
            ]
        )
        == 2
    )
    expected = (
        {'job.price_unavailable': 2}
        if unavailable
        else {'job.cost': 2.5, 'job.cost_ondemand': 4, 'job.savings': 1.5}
    )
    for name, value in expected.items():
        points = [point for point in recorder.entries if point['MetricName'] == name]
        assert len(points) == 1
        assert points[0]['Value'] == value


def test_finished_job_processor_publishes_one_counter_per_second(context, monkeypatch):
    from unittest.mock import Mock
    from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
        FinishedJobProcessor,
        ProcessFinishedJob,
    )

    recorder = _Recorder()
    monkeypatch.setattr(context.service_registry(), 'get_service', lambda _: recorder)
    monkeypatch.setattr(
        JobCompletionMetrics, 'metrics_provider', property(lambda _: 'dogstatsd')
    )
    jobs = [_job(job_uid='first'), _job(job_uid='second')]
    jobs[1].job_id = '43'
    scheduler = Mock()
    monkeypatch.setattr(context, 'scheduler', scheduler)
    scheduler.list_jobs.return_value = jobs
    monkeypatch.setattr(context, 'document_store', Mock())
    monkeypatch.setattr(
        context, 'job_cache', Mock(get_completed_job_by_uid=Mock(return_value=None))
    )

    def finish(process):
        JobCompletionMetrics(process._metrics_context, process.job).publish()
        return process.job

    monkeypatch.setattr(ProcessFinishedJob, 'invoke', finish)
    processor = FinishedJobProcessor.__new__(FinishedJobProcessor)
    processor._context = context
    processor._logger = processor._jobs_export_logger = Mock()
    processor._process_finished_jobs(jobs)
    counts = [point for point in recorder.entries if point['MetricName'] == 'job.count']
    assert len(counts) == 1
    assert counts[0]['Value'] == 2
