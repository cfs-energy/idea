"""
Test Cases for retaining the record of a job that never ran.

covers the four terminal paths a job can take: never provisioned, provisioned then
failed, clean run, and deleted by its owner. a never-provisioned job must reach the
finished jobs table with its provisioning error rather than be deleted from the cache.
"""

from ideasdk.config.soca_config import SocaConfig
import logging
from typing import List, Optional

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

    @staticmethod
    def get_job_execution_hosts(job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        return []

    def delete_job_execution_hosts(self, job_id: str):
        pass

    def add_finished_job(self, job: SocaJob):
        self.finished_jobs.append(job)


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
    def __init__(self):
        self.indexed: List[SocaJob] = []

    def add_jobs(self, jobs: List[SocaJob], **_):
        self.indexed.extend(jobs)
        return True


class FakeScheduler:
    """returns the jobs PBS still knows about, keyed by job id."""

    def __init__(self, jobs_by_id=None):
        self.jobs_by_id = jobs_by_id if jobs_by_id is not None else {}

    def list_jobs(self, job_ids=None, **_):
        return [
            self.jobs_by_id[job_id] for job_id in job_ids if job_id in self.jobs_by_id
        ]


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
        'queue_type': 'compute',
        'params': SocaJobParams(walltime=WALLTIME, nodes=1, cpus=1),
    }
    attributes.update(kwargs)
    return SocaJob(**attributes)


# --------------------------------------------------------------- disposition taxonomy


def test_disposition_unprovisioned_without_error_is_deleted():
    """
    qdel of a normal queued job: nothing went wrong, so there is nothing to explain and
    no record is created.
    """
    job = make_job(provisioned=False)

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_DELETED
    assert FinishedJobProcessor.should_record_unprovisioned(job) is False


@pytest.mark.parametrize('error', [QUEUE_LIMIT_ERROR, PROVISIONING_ERROR])
def test_disposition_unprovisioned_with_a_wait_error_is_still_deleted(error):
    """
    error_message is written every time a queue limit or a capacity wait blocks the job
    and is cleared only on a successful provision. an owner who deletes a job the queue
    is still working on has not been failed by the platform.
    """
    job = make_job(provisioned=False, error_message=error)

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_DELETED
    assert FinishedJobProcessor.should_record_unprovisioned(job) is False


def test_disposition_held_at_retry_cap():
    """
    the retry cap is where provisioning stops trying, so it is the one unprovisioned
    outcome the platform owns and the one that becomes a record.
    """
    job = make_job(
        provisioned=False, state=SocaJobState.HELD, error_message=PROVISIONING_ERROR
    )

    assert ProvisioningLifecycleEvents.get_disposition(job) == DISPOSITION_HELD
    assert FinishedJobProcessor.should_record_unprovisioned(job) is True


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
        provisioned=False, state=SocaJobState.HELD, error_message=PROVISIONING_ERROR
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
def test_user_deleted_unprovisioned_job_creates_no_record(
    fake_context, pricing_spy, error
):
    """
    qdel of a job that was still waiting for capacity, whether or not a queue limit was
    recorded against it: no finished-jobs row, no index entry, no charge.
    """
    job = make_job(provisioned=False, error_message=error)

    assert FinishedJobProcessor.should_record_unprovisioned(job) is False

    # the poller emits the disposition and drops the job; nothing else runs
    fake_context.lifecycle_events.job_disposition(job=job)

    assert fake_context.job_cache.finished_jobs == []
    assert fake_context.document_store.indexed == []
    assert pricing_spy.calls == []
    assert fake_context.lifecycle_events.dispositions == [
        ('14906', DISPOSITION_DELETED)
    ]


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
