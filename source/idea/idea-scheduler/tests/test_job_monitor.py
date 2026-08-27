"""
Test Cases for JobMonitor job intake reconciliation

covers the lost queuejob event scenario: the queuejob hook fires before the
scheduler commits the job, so the scan triggered by the event can run too early
and find nothing. the queued sweep and the periodic job reconciler must adopt
such orphaned jobs.
"""

import logging
from threading import Event
from types import SimpleNamespace

import arrow
import pytest

from ideadatamodel import SocaJob, SocaJobState, errorcodes, exceptions
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions

from ideascheduler.app.provisioning.job_monitor import job_monitor as job_monitor_module
from ideascheduler.app.provisioning.job_monitor.job_monitor import JobMonitor


class FakeConfig:
    def __init__(self, values=None):
        self.values = values if values is not None else {}

    def get_string(self, key, required=False, default=None):
        return self.values.get(key, default if default is not None else 'mock-value')

    def get_int(self, key, default=None, required=False):
        return self.values.get(key, default)


class FakeServiceRegistry:
    def register(self, service):
        pass


class FakeJobCache:
    def __init__(self):
        self.jobs = {}
        self.synced_job_ids = []
        self.retry_counts = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def sync(self, jobs):
        for job in jobs:
            self.synced_job_ids.append(job.job_id)
            self.jobs[job.job_id] = job

    def get_job_provisioning_retry_count(self, job_id):
        return self.retry_counts.get(job_id, 0)


class FakeProvisioningQueue:
    def __init__(self):
        self.put_job_ids = []
        self.deleted_job_ids = []

    def put(self, job):
        self.put_job_ids.append(job.job_id)

    def delete(self, job_id):
        self.deleted_job_ids.append(job_id)


class FakeQueueProfiles:
    def __init__(self, provisioning_queue, queue_profiles=None):
        self.provisioning_queue = provisioning_queue
        self.queue_profiles = queue_profiles if queue_profiles is not None else []

    def get_provisioning_queue(self, queue_profile_name):
        return self.provisioning_queue

    def list_queue_profiles(self):
        return self.queue_profiles


class FakeScheduler:
    def __init__(self, jobs_by_id=None, failing_job_ids=None):
        self.jobs_by_id = jobs_by_id if jobs_by_id is not None else {}
        self.failing_job_ids = failing_job_ids if failing_job_ids is not None else set()
        self.list_jobs_calls = []

    def list_jobs(self, queue=None, job_ids=None, **kwargs):
        self.list_jobs_calls.append(job_ids)
        for job_id in job_ids:
            if job_id in self.failing_job_ids:
                raise Exception(f'qstat failed for job: {job_id}')
        return [
            self.jobs_by_id[job_id] for job_id in job_ids if job_id in self.jobs_by_id
        ]


class FakeContext:
    def __init__(self, config_values=None):
        self.config_obj = FakeConfig(values=config_values)
        self.job_cache = FakeJobCache()
        self.provisioning_queue = FakeProvisioningQueue()
        self.queue_profiles = FakeQueueProfiles(
            provisioning_queue=self.provisioning_queue
        )
        self.scheduler = FakeScheduler()

    def logger(self, name=None):
        return logging.getLogger(name if name is not None else 'test')

    def config(self):
        return self.config_obj

    def service_registry(self):
        return FakeServiceRegistry()


class FakeQSelect:
    """
    scripted OpenPBSQSelect replacement. each constructed instance pops the next
    result from `results`; when the script is exhausted, returns an empty list.
    """

    results = []
    calls = []
    raise_on_error_args = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def list_jobs_ids(self, raise_on_error: bool = False):
        FakeQSelect.calls.append(self.kwargs)
        FakeQSelect.raise_on_error_args.append(raise_on_error)
        if len(FakeQSelect.results) > 0:
            result = FakeQSelect.results.pop(0)
            if isinstance(result, Exception):
                if raise_on_error:
                    raise result
                return []
            return result
        return []

    @classmethod
    def reset(cls, results=None):
        cls.results = list(results) if results is not None else []
        cls.calls = []
        cls.raise_on_error_args = []


def make_job(
    job_id: str,
    queue: str = 'normal',
    queue_type: str = 'compute',
    state: SocaJobState = SocaJobState.QUEUED,
) -> SocaJob:
    return SocaJob(
        cluster_name='idea-test',
        job_id=job_id,
        job_group=job_id,
        queue=queue,
        queue_type=queue_type,
        owner='testuser',
        state=state,
        provisioned=False,
        provisioning_options=SocaJobProvisioningOptions(terminate_when_idle=5),
    )


@pytest.fixture()
def fake_context():
    return FakeContext()


@pytest.fixture()
def monitor(fake_context, monkeypatch):
    monkeypatch.setattr(job_monitor_module, 'OpenPBSQSelect', FakeQSelect)
    FakeQSelect.reset()
    job_monitor = JobMonitor(context=fake_context)
    job_monitor._exit = Event()
    return job_monitor


def test_reconcile_queue_adopts_unknown_jobs(monitor, fake_context):
    """
    a job visible in the scheduler but missing from the job cache (lost queuejob
    event) must be fetched, cached and submitted to the provisioning queue.
    """
    job = make_job('100')
    fake_context.scheduler.jobs_by_id['100'] = job
    FakeQSelect.reset([['100'], ['100']])

    adopted = monitor._reconcile_queue(queue='normal', log_tag='test')

    assert adopted == 1
    assert fake_context.job_cache.get_job('100') is job
    assert fake_context.provisioning_queue.put_job_ids == ['100']


def test_reconcile_queue_resubmits_cached_jobs(monitor, fake_context):
    """
    a job already in the cache but still pending capacity must be re-submitted to
    the provisioning queue (put is idempotent), not silently skipped.
    """
    job = make_job('200')
    fake_context.job_cache.jobs['200'] = job
    FakeQSelect.reset([['200']])

    adopted = monitor._reconcile_queue(queue='normal', log_tag='test')

    assert adopted == 0
    assert fake_context.provisioning_queue.put_job_ids == ['200']
    # no qstat calls: the job was served from the cache
    assert fake_context.scheduler.list_jobs_calls == []


def test_reconcile_queue_no_jobs(monitor, fake_context):
    FakeQSelect.reset([[]])

    adopted = monitor._reconcile_queue(queue='normal', log_tag='test')

    assert adopted == 0
    assert fake_context.provisioning_queue.put_job_ids == []
    assert len(FakeQSelect.calls) == 1


def test_reconcile_queue_queries_pending_capacity_only(monitor):
    FakeQSelect.reset([[]])

    monitor._reconcile_queue(queue='normal', log_tag='test')

    call = FakeQSelect.calls[0]
    assert call['stack_id'] == 'tbd'
    assert call['queue'] == 'normal'
    assert call['job_state'] == [SocaJobState.QUEUED, SocaJobState.HELD]


def test_reconcile_queue_relists_after_adoption(monitor, fake_context):
    """
    a job that becomes visible while an earlier batch is being fetched must be
    picked up by the re-list before the loop exits.
    """
    job_1 = make_job('301')
    job_2 = make_job('302')
    fake_context.scheduler.jobs_by_id['301'] = job_1
    fake_context.scheduler.jobs_by_id['302'] = job_2
    FakeQSelect.reset([['301'], ['301', '302'], ['301', '302']])

    adopted = monitor._reconcile_queue(queue='normal', log_tag='test')

    assert adopted == 2
    assert fake_context.provisioning_queue.put_job_ids == ['301', '302']


def test_queued_sweep_adopts_job_after_initial_miss(monitor, fake_context):
    """
    the lost event scenario end to end: the scan at event time sees nothing
    (job not committed yet), the armed sweep adopts the job once visible.
    """
    # initial scan triggered by the queuejob event: job not visible yet
    FakeQSelect.reset([[]])
    monitor._arm_queued_sweep(queue='normal')
    monitor._reconcile_queue(queue='normal', log_tag='queued-jobs')
    assert fake_context.provisioning_queue.put_job_ids == []

    # job commits in the scheduler; the sweep adopts it
    job = make_job('400')
    fake_context.scheduler.jobs_by_id['400'] = job
    FakeQSelect.reset([['400'], ['400']])
    monitor._run_queued_sweeps()

    assert fake_context.provisioning_queue.put_job_ids == ['400']
    assert fake_context.job_cache.get_job('400') is job


def test_queued_sweep_respects_retry_interval(monitor):
    monitor._arm_queued_sweep(queue='normal')

    FakeQSelect.reset([[]])
    monitor._run_queued_sweeps()
    assert len(FakeQSelect.calls) == 1

    # immediate re-run is throttled by the retry interval
    monitor._run_queued_sweeps()
    assert len(FakeQSelect.calls) == 1

    # after the retry interval has elapsed, the sweep runs again
    monitor._queued_sweep_last_attempt['normal'] = arrow.utcnow().shift(seconds=-3)
    monitor._run_queued_sweeps()
    assert len(FakeQSelect.calls) == 2


def test_queued_sweep_expires(monitor):
    monitor._arm_queued_sweep(queue='normal')
    monitor._queued_sweep_expiry['normal'] = arrow.utcnow().shift(seconds=-1)

    FakeQSelect.reset([[]])
    monitor._run_queued_sweeps()

    assert len(FakeQSelect.calls) == 0
    assert 'normal' not in monitor._queued_sweep_expiry
    assert 'normal' not in monitor._queued_sweep_last_attempt


def test_queued_sweep_rearming_extends_window(monitor):
    monitor._arm_queued_sweep(queue='normal')
    first_expiry = monitor._queued_sweep_expiry['normal']

    monitor._queued_sweep_expiry['normal'] = arrow.utcnow().shift(seconds=1)
    monitor._arm_queued_sweep(queue='normal')

    assert monitor._queued_sweep_expiry['normal'] >= first_expiry


def test_job_reconciler_sweeps_enabled_queues_only(monitor, fake_context):
    fake_context.queue_profiles.queue_profiles = [
        SimpleNamespace(name='compute', enabled=True, queues=['normal', 'high']),
        SimpleNamespace(name='disabled-profile', enabled=False, queues=['other']),
    ]
    job = make_job('500')
    fake_context.scheduler.jobs_by_id['500'] = job
    FakeQSelect.reset([['500'], ['500'], []])

    monitor._job_reconciler()

    swept_queues = [call['queue'] for call in FakeQSelect.calls]
    assert 'other' not in swept_queues
    assert swept_queues.count('normal') == 2
    assert swept_queues.count('high') == 1
    assert fake_context.provisioning_queue.put_job_ids == ['500']


def test_job_reconciler_is_idempotent(monitor, fake_context):
    """
    repeated reconciler runs against the same pending job must not fail and must
    not fetch the job from the scheduler again once cached.
    """
    fake_context.queue_profiles.queue_profiles = [
        SimpleNamespace(name='compute', enabled=True, queues=['normal']),
    ]
    job = make_job('600')
    fake_context.scheduler.jobs_by_id['600'] = job

    FakeQSelect.reset([['600'], ['600']])
    monitor._job_reconciler()
    assert fake_context.scheduler.list_jobs_calls == [['600']]

    FakeQSelect.reset([['600']])
    monitor._job_reconciler()

    # second run resubmits from the cache without another qstat
    assert fake_context.scheduler.list_jobs_calls == [['600']]
    assert fake_context.provisioning_queue.put_job_ids == ['600', '600']


def test_job_reconciler_queue_failure_does_not_block_other_queues(
    monitor, fake_context
):
    fake_context.queue_profiles.queue_profiles = [
        SimpleNamespace(name='compute', enabled=True, queues=['bad', 'good']),
    ]
    fake_context.scheduler.failing_job_ids.add('700')
    job = make_job('701', queue='good')
    fake_context.scheduler.jobs_by_id['701'] = job
    FakeQSelect.reset([['700'], ['701'], ['701']])

    monitor._job_reconciler()

    assert fake_context.provisioning_queue.put_job_ids == ['701']


def test_submit_to_provisioning_queue_deletes_running_jobs(monitor, fake_context):
    running_job = make_job('800', state=SocaJobState.RUNNING)
    queued_job = make_job('801')

    monitor._submit_to_provisioning_queue(jobs=[running_job, queued_job])

    assert fake_context.provisioning_queue.deleted_job_ids == ['800']
    assert fake_context.provisioning_queue.put_job_ids == ['801']


def test_submit_to_provisioning_queue_skips_retry_exhausted_jobs(monitor, fake_context):
    """
    a job held after exhausting its provisioning retry budget must not be re-queued.
    """
    capped_job = make_job('900')
    ok_job = make_job('901')
    # default cap is 3; a count at/above the cap means the job is held-at-cap
    fake_context.job_cache.retry_counts['900'] = 3

    monitor._submit_to_provisioning_queue(jobs=[capped_job, ok_job])

    assert fake_context.provisioning_queue.put_job_ids == ['901']


def test_reconcile_queue_skips_retry_exhausted_job(monitor, fake_context):
    """
    the reconciler adopts HELD jobs (stack_id=tbd) too; one held at the retry cap must
    not be re-submitted to the provisioning queue by the reconcile sweep.
    """
    capped_job = make_job('910', state=SocaJobState.HELD)
    fake_context.job_cache.jobs['910'] = capped_job
    fake_context.job_cache.retry_counts['910'] = 3
    FakeQSelect.reset([['910']])

    adopted = monitor._reconcile_queue(queue='normal', log_tag='test')

    assert adopted == 0
    assert fake_context.provisioning_queue.put_job_ids == []


def test_reconcile_queue_raises_on_qselect_failure(monitor, fake_context):
    """
    a failed qselect must not be indistinguishable from an empty queue. returning
    [] on failure makes every reconcile pass a silent no-op while pbs is degraded,
    which is precisely when jobs go missing.
    """
    fake_context.job_cache.jobs['920'] = make_job('920')
    FakeQSelect.reset(
        [
            exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message='qselect failed (rc=1)'
            )
        ]
    )

    with pytest.raises(exceptions.SocaException):
        monitor._reconcile_queue(queue='normal', log_tag='test')

    assert FakeQSelect.raise_on_error_args == [True]
    assert fake_context.provisioning_queue.put_job_ids == []


def test_job_reconciler_isolates_a_failing_queue(monitor, fake_context):
    """
    one queue whose qselect fails must not stop the remaining queues from being
    reconciled.
    """
    fake_context.queue_profiles.queue_profiles = [
        SimpleNamespace(enabled=True, queues=['normal', 'high'])
    ]
    fake_context.job_cache.jobs['930'] = make_job('930', queue='high')
    FakeQSelect.reset(
        [
            exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message='qselect failed (rc=1)'
            ),
            ['930'],
        ]
    )

    monitor._job_reconciler()

    assert fake_context.provisioning_queue.put_job_ids == ['930']
