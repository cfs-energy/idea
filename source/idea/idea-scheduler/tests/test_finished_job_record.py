"""
Test Cases for retaining the record of a job that never ran.

covers the four terminal paths a job can take: never provisioned, provisioned then
failed, clean run, and deleted by its owner. a never-provisioned job must reach the
finished jobs table with its provisioning error rather than be deleted from the cache.
"""

from ideasdk.config.soca_config import SocaConfig
import logging
from typing import Dict, List, Optional
from unittest.mock import Mock

import arrow
import pytest

from ideadatamodel import (
    SocaAnyPayload,
    SocaJob,
    SocaJobEstimatedBOMCost,
    SocaJobExecutionHost,
    SocaJobParams,
    SocaJobState,
)
from ideascheduler.app.aws import PricingHelper
from ideascheduler.app.provisioning.job_monitor import finished_job_processor
from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
    FinishedJobProcessor,
    ProcessFinishedJob,
)
from ideascheduler.app.provisioning.lifecycle_events import (
    DISPOSITION_DELETED,
    DISPOSITION_HELD,
    DISPOSITION_RAN,
    ProvisioningLifecycleEvents,
)

WALLTIME = '96:00:00'
WALLTIME_SECONDS = 96 * 60 * 60
PROVISIONING_ERROR = (
    'SERVICE_QUOTA_NOT_AVAILABLE: service_quota_not_available_for_instance_types'
)
QUEUE_LIMIT_ERROR = 'max_provisioned_instances: queue is at its configured limit'

LOGGER = logging.getLogger('test_finished_job_record')


class FakeJobCache:
    def __init__(self):
        self.finished_jobs: List[SocaJob] = []
        self.pending_jobs: List[SocaJob] = []

    @staticmethod
    def get_job_execution_hosts(job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        return []

    def delete_job_execution_hosts(self, job_id: str):
        pass

    def get_completed_job_by_uid(self, job_uid):
        return next((job for job in self.finished_jobs if job.job_uid == job_uid), None)

    def add_finished_job(self, job: SocaJob):
        self.finished_jobs.append(job)
        self.pending_jobs.append(job)

    def list_pending_index_jobs(self):
        return list(self.pending_jobs)

    def mark_job_indexed(self, job):
        self.pending_jobs.remove(job)


class FakeLifecycleEvents:
    def __init__(self):
        self.dispositions: List[tuple] = []

    def job_disposition(
        self,
        job: SocaJob,
        disposition: Optional[str] = None,
        attempt_number: Optional[int] = None,
    ):
        if disposition is None:
            disposition = ProvisioningLifecycleEvents.get_disposition(job)
        self.dispositions.append((job.job_id, disposition))

    def attempts_consumed(self, job: SocaJob) -> Optional[int]:
        return None


class FakeDocumentStore:
    def is_enabled(self):
        return True

    def __init__(self):
        self.indexed: List[SocaJob] = []
        self.jobs_by_uid: Dict[str, SocaJob] = {}
        self.lookup_error: Optional[Exception] = None

    def get_job(self, job_uid=None, **_):
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.jobs_by_uid.get(job_uid)

    def add_jobs(self, jobs: List[SocaJob], **_):
        self.indexed.extend(jobs)
        return True


class FakeScheduler:
    """returns the jobs PBS still knows about, keyed by job id."""

    def __init__(self, jobs_by_id=None):
        self.jobs_by_id = jobs_by_id if jobs_by_id is not None else {}
        # who the PBS server log says asked for a job's deletion
        self.deleted_by = {}

    def get_job_deleted_by(self, job_id):
        return self.deleted_by.get(job_id)

    def list_jobs(self, job_ids=None, **_):
        return [
            self.jobs_by_id[job_id] for job_id in job_ids if job_id in self.jobs_by_id
        ]

    def get_job_history_state(self, job_id):
        job = self.jobs_by_id.get(job_id)
        return job.state if job is not None else None

    @staticmethod
    def get_array_subjobs(job_id):
        return None, []


class FakeMetrics:
    def __getattr__(self, name):
        def record(**_):
            pass

        return record


class FakeJobNotifications:
    def __init__(self):
        self.completed: List[str] = []

    def job_completed(self, job: SocaJob):
        self.completed.append(job.job_id)


class PricingSpy:
    """
    stands in for PricingHelper. records every job it was asked to price, so a test can
    assert that pricing did not run at all.
    """

    calls: List[tuple] = []

    def __init__(self, context=None, job=None, total_time_secs=None):
        PricingSpy.calls.append((job.job_id, total_time_secs))

    @staticmethod
    def compute_estimated_bom_cost() -> SocaJobEstimatedBOMCost:
        # SocaAmount needs an initialized locale; the tests only assert that pricing ran
        return SocaJobEstimatedBOMCost()

    @classmethod
    def reset(cls):
        cls.calls = []


class BudgetsSpy:
    calls: List[str] = []

    def __init__(self, context=None, job=None):
        BudgetsSpy.calls.append(job.job_id)

    @staticmethod
    def compute_budget_usage():
        return None

    @classmethod
    def reset(cls):
        cls.calls = []


@pytest.fixture()
def pricing_spy(monkeypatch):
    PricingSpy.reset()
    BudgetsSpy.reset()
    monkeypatch.setattr(finished_job_processor, 'PricingHelper', PricingSpy)
    monkeypatch.setattr(finished_job_processor, 'AwsBudgetsHelper', BudgetsSpy)
    return PricingSpy


@pytest.fixture()
def fake_context():
    return SocaAnyPayload(
        job_cache=FakeJobCache(),
        lifecycle_events=FakeLifecycleEvents(),
        document_store=FakeDocumentStore(),
        scheduler=FakeScheduler(),
        metrics=FakeMetrics(),
        job_notifications=FakeJobNotifications(),
    )


def build_processor(context, job: SocaJob) -> ProcessFinishedJob:
    return ProcessFinishedJob(
        context=context, logger=LOGGER, job=job, job_export_logger=LOGGER
    )


def build_poller(context) -> FinishedJobProcessor:
    """
    FinishedJobProcessor starts its polling thread in __init__. build the object without
    it, so the per-cycle methods can be driven directly.
    """
    poller = FinishedJobProcessor.__new__(FinishedJobProcessor)
    poller._context = context
    poller._logger = LOGGER
    poller._jobs_export_logger = LOGGER
    return poller


def make_job(**kwargs) -> SocaJob:
    attributes = {
        'job_id': '14906',
        'name': 'mock-job',
        'owner': 'mockuser',
        'project': 'default',
        'queue': 'normal',
        'params': SocaJobParams(walltime=WALLTIME, nodes=1, cpus=1),
    }
    attributes.update(kwargs)
    return SocaJob(**attributes)


# --------------------------------------------------------------- disposition taxonomy


def test_disposition_unprovisioned_without_error_is_deleted():
    """
    Owner cancellation leaves a record even without a provisioning error.
    """
    job = make_job(provisioned=False)

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_DELETED


@pytest.mark.parametrize('error', [QUEUE_LIMIT_ERROR, PROVISIONING_ERROR])
def test_disposition_unprovisioned_with_a_wait_error_is_still_deleted(error):
    """
    error_message is written every time a queue limit or a capacity wait blocks the job
    and is cleared only on a successful provision. an owner who deletes a job the queue
    is still working on has not been failed by the platform.
    """
    job = make_job(provisioned=False, error_message=error)

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_DELETED


def test_disposition_held_at_retry_cap():
    """
    the retry cap is where provisioning stops trying, so it is the one unprovisioned
    outcome the platform owns and the one that becomes a record. The cache reads the
    cap's error code onto the job as its reason class.
    """
    job = make_job(
        provisioned=False,
        state=SocaJobState.HELD,
        error_message=PROVISIONING_ERROR,
        reason_class='retries_exhausted',
    )

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_HELD


def test_disposition_held_after_a_stack_timeout():
    job = make_job(
        provisioned=False, state=SocaJobState.HELD, reason_class='stack_timeout'
    )

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_HELD


@pytest.mark.parametrize(
    'recorded_error',
    [
        None,  # qsub -h, or a dependency not yet met
        ('CAPACITY_UNAVAILABLE', QUEUE_LIMIT_ERROR),  # a wait, not the platform's hold
        ('JOB_PROVISIONING_RETRIES_EXHAUSTED', PROVISIONING_ERROR),
        (
            'JOB_HELD_STACK_TIMEOUT',
            'Job held: nodes launched but the job did not start',
        ),
    ],
)
def test_a_held_job_that_leaves_pbs_is_recorded_by_who_held_it(
    fake_context, monkeypatch, tmp_path, recorded_error
):
    """
    PBS shows a held state for an owner's hold and a dependency too. Only a hold the
    platform placed, and recorded, is the platform giving up on the job; any other held
    job that leaves PBS was deleted: cancelled, or the wait it was in when one was
    recorded. Driven through the poll cycle, which writes the waiting signals ('Job
    held') onto a held job before the record is made.
    """
    from threading import Event
    from unittest.mock import Mock
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache

    class CycleExit(Event):
        def wait(self, timeout=None):
            self.set()

    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    fake_context.job_cache = cache
    job = make_job(job_uid='held-1', state=SocaJobState.HELD, provisioned=False)
    cache.sync([job])
    if recorded_error is not None:
        cache.set_job_provisioning_error(job.job_id, *recorded_error)
    fake_context.config = lambda: Mock(get_int=lambda key, default=None: default)
    monkeypatch.setattr(
        finished_job_processor,
        'OpenPBSQSelect',
        lambda context: Mock(list_jobs_ids=Mock(return_value=[])),
    )
    fake_context.document_store.is_enabled = Mock(return_value=True)
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()

    [record] = cache.list_completed_jobs()
    if recorded_error is None:
        assert (record.disposition, record.reason_class, record.status_reason) == (
            DISPOSITION_DELETED,
            'cancelled',
            'Cancelled.',
        )
    elif recorded_error[0] == 'CAPACITY_UNAVAILABLE':
        # deleted while it waited: the wait it was in stays on the record
        assert (record.disposition, record.reason_class, record.status_reason) == (
            DISPOSITION_DELETED,
            'capacity',
            QUEUE_LIMIT_ERROR,
        )
    else:
        expected = {
            'JOB_PROVISIONING_RETRIES_EXHAUSTED': 'retries_exhausted',
            'JOB_HELD_STACK_TIMEOUT': 'stack_timeout',
        }[recorded_error[0]]
        assert (record.disposition, record.reason_class) == (DISPOSITION_HELD, expected)


# --------------------------------------------------------- path 1: never provisioned


def test_unprovisioned_job_is_recorded_with_its_error(fake_context, pricing_spy):
    job = make_job(
        provisioned=False, state=SocaJobState.HELD, error_message=PROVISIONING_ERROR
    )

    recorded = build_processor(fake_context, job).invoke_unprovisioned()

    assert recorded is not None
    assert fake_context.job_cache.finished_jobs == [job]
    assert job.error_message == PROVISIONING_ERROR
    assert job.state == SocaJobState.FINISHED


def test_unprovisioned_record_is_given_an_end_time(fake_context, pricing_spy):
    """
    the record needs an end. without one the portal has no point to measure the queue
    wait to and reports a wait that grows every time the panel is opened.
    """
    job = make_job(
        provisioned=False,
        state=SocaJobState.HELD,
        queue_time=arrow.get('2026-08-01T10:00:00Z').datetime,
    )

    build_processor(fake_context, job).invoke_unprovisioned()

    assert job.start_time is None
    assert job.end_time is not None
    assert job.end_time >= job.queue_time


def test_unprovisioned_record_keeps_an_end_time_it_already_has(
    fake_context, pricing_spy
):
    end_time = arrow.get('2026-08-01T10:12:00Z').datetime
    job = make_job(provisioned=False, state=SocaJobState.HELD, end_time=end_time)

    build_processor(fake_context, job).invoke_unprovisioned()

    assert job.end_time == end_time


def test_unprovisioned_job_produces_no_charge(fake_context, pricing_spy):
    """
    the elapsed-time fallback for a job with no timestamps is the requested walltime.
    pricing must not run at all on a job that never started.
    """
    job = make_job(provisioned=False, error_message=PROVISIONING_ERROR)

    build_processor(fake_context, job).invoke_unprovisioned()

    assert pricing_spy.calls == []
    assert BudgetsSpy.calls == []
    assert job.estimated_bom_cost is None
    assert job.estimated_budget_usage is None
    # 0, not the 345600s requested walltime
    assert job.total_time_secs == 0
    assert job.get_total_time_seconds() == 0


def test_unprovisioned_job_disposition_is_read_before_state_is_rewritten(fake_context):
    """
    the record is written as FINISHED. the disposition event must still report the state
    the job was actually in, so a job parked at the retry cap reports 'held'.
    """
    job = make_job(
        provisioned=False,
        state=SocaJobState.HELD,
        error_message=PROVISIONING_ERROR,
        reason_class='retries_exhausted',
    )

    build_processor(fake_context, job).invoke_unprovisioned()

    assert fake_context.lifecycle_events.dispositions == [('14906', DISPOSITION_HELD)]
    assert job.state == SocaJobState.FINISHED


def test_unprovisioned_jobs_are_indexed(fake_context, pricing_spy):
    job = make_job(
        provisioned=False, state=SocaJobState.HELD, error_message=PROVISIONING_ERROR
    )

    build_poller(fake_context)._process_unprovisioned_jobs([job])

    assert fake_context.job_cache.finished_jobs == [job]
    build_poller(fake_context)._index_pending_jobs()
    assert fake_context.document_store.indexed == [job]


def test_unprovisioned_job_write_failure_is_not_indexed(fake_context, pricing_spy):
    """
    the whole poll cycle is driven here, not publish_to_finished_jobs_db alone: that
    method swallows its own exception, so only the caller can decide not to index.
    an indexed job absent from the finished jobs table splits the two read paths.
    """

    class FailingJobCache(FakeJobCache):
        def add_finished_job(self, job: SocaJob):
            raise RuntimeError('db unavailable')

    fake_context.job_cache = FailingJobCache()
    job = make_job(
        provisioned=False, state=SocaJobState.HELD, error_message=PROVISIONING_ERROR
    )

    build_poller(fake_context)._process_unprovisioned_jobs([job])

    assert fake_context.job_cache.finished_jobs == []
    build_poller(fake_context)._index_pending_jobs()
    assert fake_context.document_store.indexed == []


# ------------------------------------------------------ path 2: provisioned, failed


def test_provisioned_job_that_never_started_keeps_its_error(fake_context, pricing_spy):
    """
    the stack was created, so the job counts as provisioned and is re-read from the
    scheduler. the provisioning error row is deleted with the cache entry before that
    read, so the reason must be carried onto the finished record.
    """
    cached_job = make_job(provisioned=True, error_message=PROVISIONING_ERROR)
    # the scheduler read carries no error_message: PBS exposes it as a resource that the
    # job model maps onto comment, not onto error_message.
    scheduler_job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'14906': scheduler_job}

    build_poller(fake_context)._process_finished_jobs([cached_job])

    assert fake_context.job_cache.finished_jobs == [scheduler_job]
    assert scheduler_job.error_message == PROVISIONING_ERROR


def test_provisioned_job_that_never_started_is_not_priced(fake_context, pricing_spy):
    """
    a job whose stack was created but which never executed has no start time, so there is
    no instance time to price. the 60s floor plus the 300s boot penalty would record 0.1h
    of the requested instance type, a figure that corresponds to nothing that happened.
    """
    job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.total_time_secs == 0
    assert pricing_spy.calls == []
    assert job.estimated_bom_cost is None
    assert job.estimated_budget_usage is None


def test_provisioned_job_that_never_started_is_distinguishable(
    fake_context, pricing_spy
):
    """
    the fields the portal reads to tell a job that never ran from a clean exit 0: no
    start time, no exit status, and a reason.
    """
    job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs(
        [make_job(provisioned=True, error_message=PROVISIONING_ERROR)]
    )

    assert job.start_time is None
    assert job.exit_status is None
    assert job.error_message == PROVISIONING_ERROR


# ------------------------------------------------------------------ path 3: clean run


def test_clean_run_is_still_priced_from_elapsed_time(fake_context, pricing_spy):
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        exit_status=0,
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=7200).datetime,
    )
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.total_time_secs == 7200
    assert pricing_spy.calls == [('14906', 7200)]
    assert job.estimated_bom_cost is not None
    assert fake_context.job_cache.finished_jobs == [job]
    build_poller(fake_context)._index_pending_jobs()
    assert fake_context.document_store.indexed == [job]
    assert fake_context.lifecycle_events.dispositions == [('14906', DISPOSITION_RAN)]
    assert fake_context.job_notifications.completed == ['14906']


def test_clean_run_error_message_stays_unset(fake_context, pricing_spy):
    """
    a job that ran has no provisioning error to carry: the error is cleared when the job
    is successfully provisioned.
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        exit_status=0,
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=60).datetime,
    )
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.error_message is None


# --------------------------------------------------------------- path 4: user deleted


@pytest.mark.parametrize('error', [None, QUEUE_LIMIT_ERROR])
def test_user_deleted_unprovisioned_job_keeps_record(fake_context, pricing_spy, error):
    job = make_job(provisioned=False, error_message=error)
    fake_context.scheduler.deleted_by[job.job_id] = job.owner
    build_poller(fake_context)._process_unprovisioned_jobs([job])
    assert fake_context.job_cache.finished_jobs == [job]
    build_poller(fake_context)._index_pending_jobs()
    assert fake_context.document_store.indexed == [job]
    assert job.disposition == 'deleted'
    assert job.reason_class == 'cancelled'
    assert job.status_reason == 'Cancelled by the owner.'
    assert pricing_spy.calls == []


@pytest.mark.parametrize(
    'deleted_by, reason_class, status_reason',
    [
        ('mockuser', 'cancelled', 'Cancelled by the owner.'),
        # seen on a cluster: root's qdel of a queued job was recorded as the owner's
        ('root', 'administrator', 'Deleted by root.'),
        # PBS did not say: the record does not guess
        (None, 'cancelled', 'Cancelled.'),
        (OSError('server_logs: permission denied'), 'cancelled', 'Cancelled.'),
    ],
)
@pytest.mark.parametrize('provisioned', [False, True])
def test_a_deleted_job_is_recorded_under_who_pbs_says_deleted_it(
    fake_context, pricing_spy, provisioned, deleted_by, reason_class, status_reason
):
    job = make_job(provisioned=provisioned)
    if isinstance(deleted_by, Exception):
        fake_context.scheduler.get_job_deleted_by = Mock(side_effect=deleted_by)
    else:
        fake_context.scheduler.deleted_by[job.job_id] = deleted_by
    poller = build_poller(fake_context)
    if provisioned:
        poller._process_finished_jobs([job])
    else:
        poller._process_unprovisioned_jobs([job])
    [record] = fake_context.job_cache.finished_jobs
    assert (record.disposition, record.reason_class, record.status_reason) == (
        'deleted',
        reason_class,
        status_reason,
    )


@pytest.mark.parametrize(
    'deleted_by, reason_class, status_reason',
    [
        ('mockuser', 'cancelled', 'Cancelled by the owner.'),
        ('root', 'administrator', 'Deleted by root.'),
        # PBS did not say who: the record stays as it was
        (None, 'unknown', None),
    ],
)
def test_a_job_deleted_while_it_ran_says_who_deleted_it(
    fake_context, pricing_spy, deleted_by, reason_class, status_reason
):
    """
    seen on a cluster: a job its owner cancelled while it ran read "Failed (exit 143)"
    with nothing beside it. It is still failed with that status, and scratch still keeps
    its folder for the failed time; the record now also says who stopped it.
    """
    from ideascheduler.app.scratch_cleanup.job_state import job_succeeded

    start_time = arrow.get('2026-11-10T20:00:43Z')
    fake_context.scheduler.jobs_by_id = {
        '14906': make_job(
            provisioned=True,
            state=SocaJobState.FINISHED,
            exit_status=143,
            terminated=True,
            start_time=start_time.datetime,
            end_time=start_time.shift(seconds=217).datetime,
        )
    }
    fake_context.scheduler.deleted_by['14906'] = deleted_by

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    [record] = fake_context.job_cache.finished_jobs
    assert (record.disposition, record.exit_status) == ('failed', 143)
    assert (record.reason_class, record.status_reason) == (reason_class, status_reason)
    assert not job_succeeded(record)


def test_a_job_that_failed_on_its_own_is_not_asked_who_deleted_it(
    fake_context, pricing_spy
):
    start_time = arrow.get('2026-11-10T20:00:43Z')
    fake_context.scheduler.jobs_by_id = {
        '14906': make_job(
            provisioned=True,
            state=SocaJobState.FINISHED,
            exit_status=1,
            terminated=False,
            start_time=start_time.datetime,
            end_time=start_time.shift(seconds=217).datetime,
        )
    }
    fake_context.scheduler.get_job_deleted_by = Mock(side_effect=AssertionError)

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    [record] = fake_context.job_cache.finished_jobs
    assert (record.reason_class, record.status_reason) == ('unknown', None)


def test_a_deletion_idea_recorded_is_not_asked_about(fake_context, pricing_spy):
    """the portal's own deletes run qdel as the owner; what they recorded stands"""
    job = make_job(
        provisioned=False,
        disposition='deleted',
        reason_class='administrator',
        status_reason='Deleted by an administrator.',
    )
    fake_context.scheduler.deleted_by[job.job_id] = job.owner
    build_poller(fake_context)._process_unprovisioned_jobs([job])
    assert job.reason_class == 'administrator'
    assert job.status_reason == 'Deleted by an administrator.'


SERVER_LOG = """\
11/09/2026 15:00:27;0008;Server@scheduler;Job;39.scheduler;Job to be deleted at request of eve@scheduler.cluster.local
11/09/2026 15:00:27;0008;Server@scheduler;Job;307.scheduler;Job to be deleted at request of alice.liddell@scheduler.cluster.local
11/09/2026 15:00:28;0008;Server@scheduler;Job;311[].scheduler;Job to be deleted at request of alice.liddell@scheduler.cluster.local
11/09/2026 15:00:29;0100;Server@scheduler;Job;391.scheduler;enqueuing into test, state Q hop 1
11/09/2026 22:46:30;0008;Server@scheduler;Job;391.scheduler;Job to be deleted at request of root@scheduler.cluster.local
11/09/2026 14:58:25;0004;Server@scheduler;Que;iv-28463b57b918;deleted at request of root@scheduler.cluster.local
"""


def test_who_deleted_a_job_is_read_from_the_pbs_server_log(tmp_path, monkeypatch):
    """the log lines are a cluster's own, OpenPBS 23.06.06"""
    from ideascheduler.app.scheduler.openpbs import OpenPBSScheduler
    from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import DeletionLog

    logs = tmp_path / 'server_logs'
    logs.mkdir()
    (logs / '20261107').write_text(SERVER_LOG.replace('root@', 'old@'))
    (logs / '20261108').write_text(
        '11/08/2026 09:00:00;0008;Server@scheduler;Job;200.scheduler;'
        'Job to be deleted at request of carol@scheduler\n'
    )
    (logs / '20261109').write_text(SERVER_LOG)
    monkeypatch.setenv('PBS_HOME', str(tmp_path))
    scheduler = OpenPBSScheduler.__new__(OpenPBSScheduler)
    scheduler._deletion_log = DeletionLog()
    assert scheduler.get_job_deleted_by('391') == 'root'
    assert scheduler.get_job_deleted_by('307') == 'alice.liddell'
    assert scheduler.get_job_deleted_by('311[]') == 'alice.liddell'
    assert scheduler.get_job_deleted_by('39') == 'eve'
    # yesterday's log is read too
    assert scheduler.get_job_deleted_by('200') == 'carol'
    # a job nobody deleted, and a queue's delete line, name nobody
    assert scheduler.get_job_deleted_by('392') is None
    assert scheduler.get_job_deleted_by('iv-28463b57b918') is None

    # lines PBS adds later are picked up, a half-written one only once it is whole
    line = (
        '11/09/2026 23:09:30;0008;Server@scheduler;Job;394.scheduler;'
        'Job to be deleted at request of bob.stone@scheduler\n'
    )
    with open(logs / '20261109', 'a') as log:
        log.write(line[:-20])
    assert scheduler.get_job_deleted_by('394') is None
    with open(logs / '20261109', 'a') as log:
        log.write(line[-20:])
    assert scheduler.get_job_deleted_by('394') == 'bob.stone'
    assert scheduler.get_job_deleted_by('391') == 'root'

    # a new day: nothing older than yesterday's log is kept
    (logs / '20261110').write_text('')
    assert scheduler.get_job_deleted_by('394') == 'bob.stone'
    assert scheduler.get_job_deleted_by('200') is None

    with pytest.raises(OSError):
        DeletionLog(str(tmp_path / 'missing')).deleted_by('391')


def test_a_burst_of_deleted_jobs_reads_the_server_log_once(tmp_path, monkeypatch):
    """qdel of a few thousand queued jobs must not cost a pass over the log for each"""
    from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import DeletionLog

    logs = tmp_path / 'server_logs'
    logs.mkdir()
    (logs / '20261109').write_text(SERVER_LOG * 200)
    size = (logs / '20261109').stat().st_size
    log = DeletionLog(str(logs))
    read_bytes = []
    read = log._read

    def measured(name):
        before = log._offsets.get(name, 0)
        read(name)
        read_bytes.append(log._offsets[name] - before)

    monkeypatch.setattr(log, '_read', measured)
    for _ in range(50):
        assert log.deleted_by('391') == 'root'
    assert read_bytes == [size] + [0] * 49


def test_user_deleted_job_after_provisioning_is_not_priced(fake_context, pricing_spy):
    """
    qdel after capacity was provisioned but before the job started. recorded like any
    other terminal job, at 0 elapsed seconds and with no estimate.
    """
    job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.total_time_secs == 0
    assert job.get_total_time_seconds() != WALLTIME_SECONDS
    assert pricing_spy.calls == []
    assert job.estimated_bom_cost is None


# ------------------------------------------------ what a job that never ran is worth

BOOT_PENALTY_SECONDS = 300


def test_job_held_at_retry_cap_then_deleted_is_not_priced(fake_context, pricing_spy):
    """
    the retry cap holds the job, its owner runs the qdel the held-job message asks for,
    and PBS lists it as finished. that route reaches the ordinary pricing path, not the
    unprovisioned one, and must not manufacture a charge there either.
    """
    scheduler_job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'14906': scheduler_job}

    build_poller(fake_context)._process_finished_jobs(
        [make_job(state=SocaJobState.HELD, error_message=PROVISIONING_ERROR)]
    )

    assert scheduler_job.start_time is None
    assert pricing_spy.calls == []
    assert scheduler_job.estimated_bom_cost is None
    # the record survives with its reason; only the invented figure is gone
    assert fake_context.job_cache.finished_jobs == [scheduler_job]
    assert scheduler_job.error_message == PROVISIONING_ERROR


def test_never_started_job_is_unpriced_on_both_terminal_paths(
    fake_context, pricing_spy
):
    """
    the unprovisioned path already skipped pricing; the ordinary path did not. a job that
    never started must come out the same whichever path claims it.
    """
    ordinary = make_job(job_id='1', provisioned=True, state=SocaJobState.FINISHED)
    fake_context.scheduler.jobs_by_id = {'1': ordinary}
    build_poller(fake_context)._process_finished_jobs(
        [make_job(job_id='1', provisioned=True)]
    )

    unprovisioned = make_job(job_id='2', provisioned=False, state=SocaJobState.HELD)
    build_processor(fake_context, unprovisioned).invoke_unprovisioned()

    assert ordinary.estimated_bom_cost is None
    assert unprovisioned.estimated_bom_cost is None
    assert ordinary.total_time_secs == 0
    assert unprovisioned.total_time_secs == 0
    assert pricing_spy.calls == []


def test_briefly_run_job_keeps_its_elapsed_time(fake_context, pricing_spy):
    """
    a job killed 342s in ran on a node that really existed. it keeps its measured elapsed
    time; only jobs with no measured time at all stop being priced.
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        exit_status=271,
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=342).datetime,
    )
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert ProcessFinishedJob.never_started(job) is False
    assert job.total_time_secs == 342
    assert pricing_spy.calls == [('14906', 342)]
    assert job.estimated_bom_cost is not None


def test_briefly_run_job_still_gets_the_boot_penalty():
    """
    the boot penalty accounts for instance time before the job starts and is added to the
    measured elapsed time, not substituted for it: 342s is priced as 642s.
    """
    job = make_job(params=SocaJobParams(walltime=WALLTIME, nodes=1, cpus=1))
    helper = PricingHelper(
        context=SocaAnyPayload(config=lambda: SocaConfig({})),
        job=job,
        total_time_secs=342,
    )

    assert helper.ec2_boot_penalty_seconds == BOOT_PENALTY_SECONDS
    assert helper.total_time_seconds == 342 + BOOT_PENALTY_SECONDS


def test_sub_second_job_that_did_start_is_still_priced(fake_context, pricing_spy):
    """
    a node ran this one, it just finished inside a second. zero elapsed time is not the
    same signal as no start time, and the 60s floor still applies to it.
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        exit_status=0,
        start_time=start_time.datetime,
        end_time=start_time.datetime,
    )
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.total_time_secs == 0
    assert ProcessFinishedJob.never_started(job) is False
    assert pricing_spy.calls == [('14906', 60)]
    assert job.estimated_bom_cost is not None


def test_unmeasurable_run_time_is_not_priced_at_walltime(fake_context, pricing_spy):
    """
    a terminated job with a start time the duration computation cannot use (here a start
    time carrying no timezone, against a scheduler that reports none) leaves the run time
    unmeasured. the requested walltime must not stand in for it: that charges the job for
    every hour it was allowed, which is the whole reason a terminated job looked expensive.
    """
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        exit_status=271,
        start_time=arrow.get('2026-08-01T00:00:00Z').naive,
    )
    fake_context.scheduler.jobs_by_id = {'14906': job}

    build_poller(fake_context)._process_finished_jobs([make_job(provisioned=True)])

    assert job.total_time_secs is None
    assert job.get_total_time_seconds() == WALLTIME_SECONDS
    assert pricing_spy.calls == []
    assert job.estimated_bom_cost is None
    # the record itself survives, without a figure
    assert fake_context.job_cache.finished_jobs == [job]


def test_system_deletion_keeps_reason_and_disposition(fake_context, pricing_spy):
    job = make_job(
        state=SocaJobState.HELD,
        provisioned=False,
        disposition='deleted',
        reason_class='access',
        error_message='Deleted because the owner is disabled.',
    )
    build_poller(fake_context)._process_unprovisioned_jobs([job])
    assert fake_context.job_cache.finished_jobs == [job]
    assert job.state == SocaJobState.FINISHED
    assert job.disposition == 'deleted'
    assert job.reason_class == 'access'
    assert job.status_reason == job.error_message
    assert pricing_spy.calls == []


def test_missing_scheduler_history_keeps_cached_job(fake_context, pricing_spy):
    job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    build_poller(fake_context)._process_finished_jobs([job])
    assert fake_context.job_cache.finished_jobs == [job]
    build_poller(fake_context)._index_pending_jobs()
    assert fake_context.document_store.indexed == [job]
    assert job.disposition == 'deleted'
    assert job.end_time is not None
    assert pricing_spy.calls == []


@pytest.mark.parametrize('write_fails', [False, True])
def test_poll_retains_cache_until_record_is_written(
    fake_context, monkeypatch, write_fails
):
    from threading import Event
    from unittest.mock import Mock

    class CycleExit(Event):
        def wait(self, timeout=None):
            self.set()

    job = make_job(state=SocaJobState.QUEUED, provisioned=False)
    cache = fake_context.job_cache
    cache.get_jobs_table = Mock()
    cache.get_jobs_table.return_value.all.return_value = [{'job_id': job.job_id}]
    cache.convert_db_entry_to_job = Mock(return_value=job)
    cache.delete_jobs = Mock()
    if write_fails:
        cache.add_finished_job = Mock(side_effect=RuntimeError('store unavailable'))
    fake_context.config = lambda: Mock()
    scheduler = Mock()
    scheduler.list_jobs_ids.return_value = []
    monkeypatch.setattr(
        finished_job_processor, 'OpenPBSQSelect', lambda context: scheduler
    )
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()
    if write_fails:
        cache.delete_jobs.assert_not_called()
    else:
        cache.delete_jobs.assert_called_once_with(job_ids=[job.job_id])
        assert cache.finished_jobs[0].disposition == 'deleted'


@pytest.mark.parametrize('raises', [False, True])
def test_index_failure_leaves_completed_record_for_retry(fake_context, raises):
    from unittest.mock import Mock

    fake_context.document_store.is_enabled = Mock(return_value=True)
    fake_context.document_store.add_jobs = Mock(return_value=False)
    if raises:
        fake_context.document_store.add_jobs.side_effect = RuntimeError(
            'index unavailable'
        )
    job = make_job(provisioned=False, state=SocaJobState.QUEUED)
    poller = build_poller(fake_context)
    assert poller._process_unprovisioned_jobs([job]) == [job.job_id]
    poller._index_pending_jobs()
    assert fake_context.job_cache.list_pending_index_jobs() == [job]
    assert fake_context.job_cache.finished_jobs == [job]


@pytest.mark.parametrize('provisioned', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_index_retry_reuses_finished_record(
    fake_context, pricing_spy, provisioned, raises
):
    from unittest.mock import Mock

    cache = fake_context.job_cache
    cache.get_job_execution_hosts = Mock(
        side_effect=[[SocaJobExecutionHost(host='host-a')], []]
    )
    cache.delete_job_execution_hosts = Mock()
    fake_context.document_store.is_enabled = Mock(return_value=True)
    fake_context.document_store.add_jobs = Mock(
        side_effect=[RuntimeError('index unavailable') if raises else False, True]
    )
    job = make_job(
        job_uid='job-a',
        provisioned=provisioned,
        state=SocaJobState.FINISHED,
        start_time=arrow.utcnow().shift(minutes=-2).datetime if provisioned else None,
        end_time=arrow.utcnow().datetime,
    )
    poller = build_poller(fake_context)
    process = (
        poller._process_finished_jobs
        if provisioned
        else poller._process_unprovisioned_jobs
    )
    assert process([job.model_copy(deep=True)]) == [job.job_id]
    poller._index_pending_jobs()
    record = cache.finished_jobs[0].model_dump()
    poller._index_pending_jobs()
    assert cache.list_pending_index_jobs() == []
    assert len(cache.finished_jobs) == 1
    assert cache.finished_jobs[0].model_dump() == record
    assert (
        fake_context.document_store.add_jobs.call_args.kwargs['jobs'][0].model_dump()
        == record
    )
    assert fake_context.job_notifications.completed == (
        [job.job_id] if provisioned else []
    )
    assert len(pricing_spy.calls) == int(provisioned)
    assert len(fake_context.lifecycle_events.dispositions) == 1
    assert cache.get_job_execution_hosts.call_count == 1


@pytest.fixture()
def currency(monkeypatch):
    from ideadatamodel import locale

    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'USD')


@pytest.mark.parametrize('explicit_cost', [False, True])
def test_budget_estimate_requires_available_price(explicit_cost, currency):
    from unittest.mock import Mock
    from ideadatamodel import SocaAmount
    from ideascheduler.app.aws import AwsBudgetsHelper

    cost = SocaJobEstimatedBOMCost(
        price_unavailable=True,
        line_items_total=SocaAmount(amount=4),
        total=SocaAmount(amount=4),
    )
    job = make_job(estimated_bom_cost=None if explicit_cost else cost)
    project = Mock()
    project.budget.budget_name = 'budget'
    helper = AwsBudgetsHelper(Mock(), job=job, project=project)
    helper.get_budget = Mock()
    helper.get_budget.return_value.budget_limit = SocaAmount(amount=100)
    helper.get_budget.return_value.actual_spend = SocaAmount(amount=20)
    helper.get_budget.return_value.forecasted_spend = SocaAmount(amount=30)
    assert helper.compute_budget_usage(bom_cost=cost if explicit_cost else None) is None
    helper.get_budget.assert_not_called()


@pytest.mark.parametrize('provisioned', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_index_outage_releases_cache_and_retries_after_restart(
    fake_context, pricing_spy, monkeypatch, tmp_path, provisioned, raises
):
    from threading import Event
    from unittest.mock import Mock
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache

    class CycleExit(Event):
        def wait(self, timeout=None):
            self.set()

    db_context = FakeSchedulerContext(str(tmp_path))
    cache = JobCache(db_context)
    fake_context.job_cache = cache
    job = make_job(
        job_uid='finished-job', provisioned=provisioned, state=SocaJobState.QUEUED
    )
    cache.sync([job])
    fake_context.config = lambda: Mock()
    monkeypatch.setattr(
        finished_job_processor,
        'OpenPBSQSelect',
        lambda context: Mock(list_jobs_ids=Mock(return_value=[])),
    )
    store = fake_context.document_store
    store.is_enabled = Mock(return_value=True)
    store.add_jobs = Mock(return_value=False)
    if raises:
        store.add_jobs.side_effect = RuntimeError('index unavailable')
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()

    assert cache.get_job(job.job_id) is None
    record = cache.get_completed_job_by_uid(job.job_uid).model_dump()
    assert [job.job_uid for job in cache.list_pending_index_jobs()] == [job.job_uid]
    notifications = list(fake_context.job_notifications.completed)
    dispositions = list(fake_context.lifecycle_events.dispositions)
    cache.get_connection().close()

    fake_context.job_cache = JobCache(db_context)
    store.add_jobs = Mock(return_value=True)
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()

    assert fake_context.job_cache.list_pending_index_jobs() == []
    store.add_jobs.assert_called_once()
    assert store.add_jobs.call_args.kwargs['jobs'][0].model_dump() == record
    assert fake_context.job_notifications.completed == notifications
    assert fake_context.lifecycle_events.dispositions == dispositions


def test_an_index_outage_ends_the_single_writes_after_three_failures(
    fake_context, monkeypatch, tmp_path
):
    from threading import Event
    from unittest.mock import Mock
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache

    class CycleExit(Event):
        def wait(self, timeout=None):
            self.set()

    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    fake_context.job_cache = cache
    jobs = [
        make_job(job_id=str(i), job_uid=f'finished-{i}', state=SocaJobState.QUEUED)
        for i in range(10)
    ]
    cache.sync(jobs)
    fake_context.config = lambda: Mock()
    monkeypatch.setattr(
        finished_job_processor,
        'OpenPBSQSelect',
        lambda context: Mock(list_jobs_ids=Mock(return_value=[])),
    )
    fake_context.document_store.is_enabled = Mock(return_value=True)
    fake_context.document_store.add_jobs = Mock(return_value=False)
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()

    # one batch, then three single writes, then the cycle gives up
    assert fake_context.document_store.add_jobs.call_count == 4
    assert len(cache.list_pending_index_jobs()) == 10
    assert cache.get_job('0') is None  # the active cache is released regardless


@pytest.mark.parametrize('raises', [False, True])
def test_poisoned_document_does_not_block_other_finished_jobs(
    fake_context, monkeypatch, tmp_path, caplog, raises
):
    from threading import Event
    from unittest.mock import Mock
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache

    class CycleExit(Event):
        def wait(self, timeout=None):
            self.set()

    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    fake_context.job_cache = cache
    jobs = [
        make_job(job_id=str(i), job_uid=f'finished-{i}', state=SocaJobState.QUEUED)
        for i in range(3)
    ]
    cache.sync(jobs)
    fake_context.config = lambda: Mock()
    monkeypatch.setattr(
        finished_job_processor,
        'OpenPBSQSelect',
        lambda context: Mock(list_jobs_ids=Mock(return_value=[])),
    )
    accepted = set()
    caplog.set_level(logging.WARNING, logger=LOGGER.name)

    def index(jobs):
        if any(job.job_id == '1' for job in jobs):
            if raises:
                raise ValueError('invalid document')
            return False
        accepted.update(job.job_id for job in jobs)
        return True

    fake_context.document_store.is_enabled = Mock(return_value=True)
    fake_context.document_store.add_jobs = Mock(side_effect=index)
    poller = build_poller(fake_context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()

    assert cache.list_jobs() == []
    assert accepted == {'0', '2'}
    assert [job.job_id for job in cache.list_pending_index_jobs()] == ['1']
    assert len([r for r in caplog.records if 'pending' in r.message]) == 1
    fake_context.document_store.add_jobs.reset_mock()
    poller._exit = CycleExit()
    poller._poll_finished_jobs()
    fake_context.document_store.add_jobs.assert_called_once()
    assert (
        fake_context.document_store.add_jobs.call_args.kwargs['jobs'][0].job_id == '1'
    )


@pytest.mark.parametrize(
    'reason_class,message',
    [
        ('administrator', 'Deleted by an administrator.'),
        ('cancelled', 'Cancelled by the owner.'),
    ],
)
def test_apply_disposition_preserves_recorded_deletion_reason(
    fake_context, reason_class, message
):
    job = make_job(
        disposition='deleted', reason_class=reason_class, status_reason=message
    )
    build_processor(fake_context, job).apply_disposition()
    assert job.reason_class == reason_class
    assert job.status_reason == message


@pytest.mark.parametrize('provisioned', [False, True])
def test_sqlite_failure_defers_completion_side_effects(
    fake_context, pricing_spy, monkeypatch, provisioned
):
    import sqlite3
    from unittest.mock import Mock

    cache = fake_context.job_cache
    write = cache.add_finished_job
    cache.add_finished_job = Mock(side_effect=sqlite3.OperationalError('unavailable'))
    fake_context.metrics = Mock()
    export = Mock()
    monkeypatch.setattr(ProcessFinishedJob, 'publish_to_job_export_log', export)
    job = make_job(provisioned=provisioned, state=SocaJobState.FINISHED)
    poller = build_poller(fake_context)
    process = (
        poller._process_finished_jobs
        if provisioned
        else poller._process_unprovisioned_jobs
    )
    for _ in range(2):
        assert process([job.model_copy(deep=True)]) == []
    assert fake_context.job_notifications.completed == []
    assert fake_context.lifecycle_events.dispositions == []
    fake_context.metrics.jobs_finished.assert_not_called()
    export.assert_not_called()

    cache.add_finished_job = write
    for _ in range(2):
        assert process([job.model_copy(deep=True)]) == [job.job_id]
    assert fake_context.job_notifications.completed == (
        [job.job_id] if provisioned else []
    )
    assert len(fake_context.lifecycle_events.dispositions) == 1
    fake_context.metrics.jobs_finished.assert_called_once()
    export.assert_called_once()


@pytest.mark.parametrize('scheduler_returns_job', [False, True])
@pytest.mark.parametrize('has_cached_end', [False, True])
def test_cached_completion_never_prices_to_now(
    fake_context, pricing_spy, scheduler_returns_job, has_cached_end
):
    start = arrow.get('2026-08-01T00:00:00Z')
    cached = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        start_time=start.datetime,
        end_time=start.shift(seconds=120).datetime if has_cached_end else None,
    )
    if scheduler_returns_job:
        fake_context.scheduler.jobs_by_id[cached.job_id] = cached.model_copy(
            update={'end_time': None}
        )
    build_poller(fake_context)._process_finished_jobs([cached])
    recorded = fake_context.job_cache.finished_jobs[0]
    measured = has_cached_end and not scheduler_returns_job
    assert recorded.total_time_secs == (120 if measured else None)
    assert pricing_spy.calls == ([(cached.job_id, 120)] if measured else [])
    if not measured:
        assert recorded.estimated_bom_cost is None


@pytest.mark.parametrize('source', ['cache', 'hook', 'history'])
@pytest.mark.parametrize('job_id', ['42', '42[]'])
def test_completion_recovers_duration_before_persisting_and_publishing(
    fake_context, pricing_spy, monkeypatch, source, job_id
):
    from unittest.mock import Mock
    from ideadatamodel import SocaJobExecution, SocaJobExecutionRun
    from ideascheduler.app.metrics.job_completion_metrics import JobCompletionMetrics

    start = arrow.get('2026-08-01T00:00:00Z')
    end = start.shift(seconds=120).datetime
    cached = make_job(
        job_id=job_id, job_uid=job_id, provisioned=True, state=SocaJobState.FINISHED
    )
    fresh = cached.model_copy(deep=True)
    if source == 'cache':
        cached.start_time, cached.end_time = start.datetime, end
    elif source == 'history':
        fresh.start_time, fresh.end_time = start.datetime, end
    else:
        fake_context.job_cache.get_job_execution_hosts = Mock(
            return_value=[
                SocaJobExecutionHost(
                    execution=SocaJobExecution(
                        runs=[SocaJobExecutionRun(start=start.datetime, end=end)]
                    )
                )
            ]
        )
    if source == 'cache':
        fresh = cached
    else:
        fake_context.scheduler.jobs_by_id[job_id] = fresh
    published = []
    monkeypatch.setattr(
        ProcessFinishedJob,
        'publish_job_metrics',
        lambda self: published.append(JobCompletionMetrics.wall_seconds(self.job)),
    )
    build_poller(fake_context)._process_finished_jobs([cached])
    assert fresh.start_time == start.datetime
    assert fresh.end_time == end
    assert fresh.total_time_secs == 120
    assert published == [120]
    assert fake_context.job_cache.finished_jobs == [fresh]


@pytest.mark.parametrize('state', [SocaJobState.HELD, SocaJobState.QUEUED])
def test_previously_run_job_is_not_unprovisioned(fake_context, pricing_spy, state):
    start = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=False,
        state=state,
        start_time=start.datetime,
        end_time=start.shift(seconds=120).datetime,
    )
    build_poller(fake_context)._process_unprovisioned_jobs([job])
    assert job.total_time_secs == 120
    assert pricing_spy.calls == [(job.job_id, 120)]


def test_hook_recovery_survives_scheduler_restart(fake_context, pricing_spy, tmp_path):
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache
    from ideadatamodel import SocaJobExecution, SocaJobExecutionRun

    start = arrow.get('2026-08-01T00:00:00Z')
    db_context = FakeSchedulerContext(str(tmp_path))
    cache = JobCache(db_context)
    job = make_job(job_uid='finished', provisioned=True, state=SocaJobState.FINISHED)
    cache.sync([job])
    cache.log_job_execution(
        job.job_id,
        SocaJobExecutionHost(
            host='compute',
            execution=SocaJobExecution(
                runs=[
                    SocaJobExecutionRun(
                        start=start.datetime, end=start.shift(seconds=120).datetime
                    )
                ]
            ),
        ),
    )
    cache.get_connection().close()
    fake_context.job_cache = JobCache(db_context)
    build_poller(fake_context)._process_finished_jobs(
        [fake_context.job_cache.get_job(job.job_id)]
    )
    recorded = fake_context.job_cache.get_completed_job_by_uid(job.job_uid)
    assert recorded.total_time_secs == 120
    assert recorded.end_time == start.shift(seconds=120).datetime


@pytest.mark.parametrize('history_end', [True, False])
def test_completion_uses_current_attempt_history_before_old_hook(
    fake_context, pricing_spy, history_end
):
    from unittest.mock import Mock
    from ideadatamodel import SocaJobExecution, SocaJobExecutionRun

    start = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        provisioned=True,
        state=SocaJobState.FINISHED,
        start_time=start.datetime,
        end_time=start.shift(seconds=120).datetime if history_end else None,
    )
    fake_context.job_cache.get_job_execution_hosts = Mock(
        return_value=[
            SocaJobExecutionHost(
                execution=SocaJobExecution(
                    runs=[
                        SocaJobExecutionRun(
                            start=start.shift(hours=-2).datetime,
                            end=start.shift(hours=-1).datetime,
                        )
                    ]
                )
            )
        ]
    )
    fake_context.scheduler.jobs_by_id[job.job_id] = job
    build_poller(fake_context)._process_finished_jobs([job])
    assert job.start_time == start.datetime
    assert job.total_time_secs == (120 if history_end else None)


def test_failed_record_write_retains_hook_timestamps_for_retry(
    fake_context, pricing_spy
):
    from unittest.mock import Mock
    from ideadatamodel import SocaJobExecution, SocaJobExecutionRun

    start = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(provisioned=True, state=SocaJobState.FINISHED)
    cache = fake_context.job_cache
    cache.get_job_execution_hosts = Mock(
        return_value=[
            SocaJobExecutionHost(
                execution=SocaJobExecution(
                    runs=[
                        SocaJobExecutionRun(
                            start=start.datetime, end=start.shift(seconds=120).datetime
                        )
                    ]
                )
            )
        ]
    )
    cache.delete_job_execution_hosts = Mock()
    cache.add_finished_job = Mock(side_effect=RuntimeError('unavailable'))
    assert build_poller(fake_context)._process_finished_jobs([job]) == []
    cache.delete_job_execution_hosts.assert_not_called()


@pytest.mark.parametrize('same_host', [False, True])
@pytest.mark.parametrize('latest_hooks', ['both', 'begin', 'none'])
def test_recovery_uses_latest_real_pbs_attempt_after_restart(
    fake_context, pricing_spy, tmp_path, same_host, latest_hooks
):
    from test_job_cache import FakeSchedulerContext
    from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache
    from ideascheduler.app.scheduler.openpbs.openpbs_model import (
        OpenPBSEvent,
        OpenPBSJob,
    )

    db_context = FakeSchedulerContext(str(tmp_path))
    cache = JobCache(db_context)
    start = arrow.get('2026-08-01T00:00:00Z')
    job = make_job(
        job_uid='rerun',
        provisioned=True,
        state=SocaJobState.FINISHED,
        start_time=start.shift(seconds=7200).datetime,
    )
    cache.sync([job])
    for count, offset in [(2, 0), (10, 7200)]:
        host = 'compute-a' if same_host or count == 2 else 'compute-b'
        for event_type, seconds in [
            ('execjob_begin', offset),
            ('execjob_end', offset + 120),
        ]:
            if count == 10 and (
                latest_hooks == 'none'
                or (latest_hooks == 'begin' and event_type == 'execjob_end')
            ):
                continue
            event = OpenPBSEvent(
                type=event_type,
                timestamp=int(start.shift(seconds=seconds).float_timestamp * 1000),
                node_name=host,
            )
            pbs_job = OpenPBSJob(exec_host=f'{host}/0', run_count=count)
            cache.log_job_execution(
                job.job_id, pbs_job.get_soca_execution_hosts(event)[0]
            )
    cache.get_connection().close()
    fake_context.job_cache = JobCache(db_context)
    build_poller(fake_context)._process_finished_jobs(
        [fake_context.job_cache.get_job(job.job_id)]
    )
    recorded = fake_context.job_cache.get_completed_job_by_uid(job.job_uid)
    assert recorded.start_time == (
        None if latest_hooks == 'none' else start.shift(seconds=7200).datetime
    )
    assert recorded.total_time_secs == (120 if latest_hooks == 'both' else None)
    assert pricing_spy.calls == ([(job.job_id, 120)] if latest_hooks == 'both' else [])
    if latest_hooks != 'both':
        assert recorded.estimated_bom_cost is None
        assert recorded.end_time is None


# ------------------------------------------------------------ restore from an older copy


def test_a_restored_job_the_index_holds_is_not_processed_twice(
    fake_context, pricing_spy
):
    """
    the copy predates the job finishing: its row is still in the jobs table with no finished
    record. the index already has the job, so the owner gets no second completion email.
    """
    recorded = make_job(job_uid='uid-1', state=SocaJobState.FINISHED)
    fake_context.document_store.jobs_by_uid['uid-1'] = recorded

    ids = build_poller(fake_context)._process_finished_jobs(
        [make_job(provisioned=True, job_uid='uid-1')]
    )

    assert ids == ['14906']
    assert fake_context.job_notifications.completed == []
    assert fake_context.job_cache.finished_jobs == [recorded]
    assert fake_context.job_cache.pending_jobs == []


def test_a_restored_unprovisioned_job_the_index_holds_is_not_processed_twice(
    fake_context, pricing_spy
):
    recorded = make_job(job_uid='uid-2', state=SocaJobState.FINISHED)
    fake_context.document_store.jobs_by_uid['uid-2'] = recorded

    ids = build_poller(fake_context)._process_unprovisioned_jobs(
        [make_job(job_uid='uid-2')]
    )

    assert ids == ['14906']
    assert fake_context.job_cache.finished_jobs == [recorded]
    assert fake_context.job_cache.pending_jobs == []


def test_a_failed_index_lookup_still_records_the_job(fake_context, pricing_spy):
    fake_context.document_store.lookup_error = RuntimeError('index unreachable')

    ids = build_poller(fake_context)._process_finished_jobs(
        [make_job(provisioned=True, job_uid='uid-3')]
    )

    assert ids == ['14906']
    assert [job.job_uid for job in fake_context.job_cache.finished_jobs] == ['uid-3']
    assert fake_context.job_notifications.completed == ['14906']


# ------------------------------------------- what a job launched, not what its queue says now


def test_a_record_names_the_image_and_type_the_job_launched_not_its_queues_current_ones(
    context, fake_context, pricing_spy
):
    """
    433 and 456 launched an image that never joins, on a larger type, and never started.
    Their queue profile was put back to the baked image and t3.micro, they were deleted,
    and their records named the profile's image and type: the record is PBS history, and a
    job read from PBS takes what it did not name itself from its queue profile as it is at
    that read. The row written when the job was provisioned is what says what it launched.
    """
    from ideadatamodel import HpcQueueProfile
    from ideasdk.utils import Utils
    from ideascheduler.app.provisioning import JobCache
    from ideascheduler.app.scheduler.openpbs import OpenPBSJob

    def profile(image: str, instance_type: str) -> HpcQueueProfile:
        return HpcQueueProfile(
            name='test',
            default_job_params=SocaJobParams(
                instance_ami=image, instance_types=[instance_type], base_os='rocky9'
            ),
        )

    job_id = str(Utils.current_time_ms())
    stack = f'idea-mock-test-ondemand-{job_id}'

    def read_from_pbs(state: str, queue_profile: HpcQueueProfile) -> SocaJob:
        return OpenPBSJob(
            id=f'{job_id}.scheduler',
            Job_Owner='mockuser@scheduler',
            queue='test',
            job_state=state,
            Resource_List={
                'select': f'1:ncpus=1:compute_node={stack}',
                'nodect': 1,
                'ncpus': 1,
                'job_uid': f'uid{job_id}',
                'stack_id': f'arn:aws:cloudformation:us-east-2:123456789012:stack/{stack}/1',
            },
        ).as_soca_job(context=context, queue_profile=queue_profile)

    cache = JobCache(context=context)
    fake_context.job_cache = cache

    # provisioned while the profile named the image that never joins
    launched = read_from_pbs('Q', profile('ami-unbakedmock0001', 'c5.large'))
    assert launched.is_provisioned()
    assert launched.params.instance_ami == 'ami-unbakedmock0001'
    cache.sync(jobs=[launched])

    # the profile is put back. A later read of the live job does not rewrite the row
    restored = profile('ami-bakedmock000001', 't3.micro')
    cache.sync(jobs=[read_from_pbs('Q', restored)])
    cached = cache.get_job(job_id)
    assert cached.params.instance_ami == 'ami-unbakedmock0001'
    assert cached.params.instance_types == ['c5.large']

    # deleted: PBS history, read against the profile as it is now, says the new ones
    history = read_from_pbs('F', restored)
    assert history.params.instance_ami == 'ami-bakedmock000001'
    assert history.params.instance_types == ['t3.micro']
    fake_context.scheduler.jobs_by_id = {job_id: history}

    build_poller(fake_context)._process_finished_jobs([cached])

    record = cache.get_completed_job(job_id)
    assert record.params.instance_ami == 'ami-unbakedmock0001'
    assert record.params.instance_types == ['c5.large']

    # a job that is provisioned again, on a new stack, is on what that stack launched
    again = read_from_pbs('Q', restored)
    again.params.stack_id = again.params.stack_id[:-1] + '2'
    cache.sync(jobs=[again])
    assert cache.get_job(job_id).params.instance_types == ['t3.micro']
    cache.delete_jobs(job_ids=[job_id])
