"""
Test Cases for the job waiting signals

The signals answer "why is my job waiting": which provisioning attempt the job is on, and
which queue limit is holding it. They must never carry a queue position, a start-time
estimate, or a cluster-wide threshold.
"""

import datetime
from typing import Optional

import pytest

from ideadatamodel import (
    LimitCheckResult,
    SocaJob,
    SocaJobParams,
    SocaJobState,
)
from ideasdk.utils import Utils
from ideascheduler.app.api.job_waiting_signals import (
    apply_waiting_signals,
    get_blocking_limit_type,
    get_max_provisioning_attempts,
    get_provisioning_attempt,
    is_awaiting_provisioning,
)
from ideascheduler.app.provisioning import JobCache


class MockProvisioningQueue:
    def __init__(
        self, blocked: bool = False, limit_info: Optional[LimitCheckResult] = None
    ):
        self.blocked = blocked
        self.limit_info = limit_info

    def is_queue_blocked_by_limits(self) -> bool:
        return self.blocked

    def get_limit_info(self) -> Optional[LimitCheckResult]:
        return self.limit_info


class MockQueueProfiles:
    def __init__(self, provisioning_queue=None, raises: bool = False):
        self.provisioning_queue = provisioning_queue
        self.raises = raises

    def get_provisioning_queue(self, queue_profile_name: str):
        if self.raises:
            raise Exception('queue profile not found')
        return self.provisioning_queue


@pytest.fixture()
def job_cache(context):
    cache = JobCache(context=context)
    context.job_cache = cache
    return cache


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def mock_job(
    job_id: Optional[str] = None,
    queued_minutes_ago: int = 10,
    stack_id: Optional[str] = 'tbd',
    start_time: Optional[datetime.datetime] = None,
    state: SocaJobState = SocaJobState.QUEUED,
) -> SocaJob:
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id if job_id is not None else Utils.short_uuid(),
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='normal',
        state=state,
        queue_time=utcnow() - datetime.timedelta(minutes=queued_minutes_ago),
        start_time=start_time,
        params=SocaJobParams(nodes=1, cpus=1, stack_id=stack_id),
    )


# ------------------------------------------------------------ attempt counting


def test_attempt_is_one_before_any_failure():
    assert get_provisioning_attempt(failed_attempts=0, max_attempts=3) == 1


def test_attempt_follows_the_failure_count():
    """the persistent counter records failures, so the job is on the next attempt"""
    assert get_provisioning_attempt(failed_attempts=2, max_attempts=3) == 3


def test_attempt_is_clamped_at_the_cap():
    """at the cap the job is held; there is no fourth attempt to report"""
    assert get_provisioning_attempt(failed_attempts=3, max_attempts=3) == 3
    assert get_provisioning_attempt(failed_attempts=9, max_attempts=3) == 3


def test_attempt_is_unclamped_when_the_cap_is_disabled():
    assert get_provisioning_attempt(failed_attempts=7, max_attempts=None) == 8


def test_max_attempts_reads_the_configured_cap(context):
    assert get_max_provisioning_attempts(context=context) == 3


def test_max_attempts_is_absent_when_the_cap_is_disabled(context, monkeypatch):
    monkeypatch.setattr(
        context.config(), 'get_int', lambda *_args, **_kwargs: 0, raising=False
    )
    assert get_max_provisioning_attempts(context=context) is None


# --------------------------------------------------------------- awaiting gate


def test_awaiting_provisioning_is_false_once_started():
    job = mock_job(start_time=utcnow())
    assert is_awaiting_provisioning(job=job) is False


def test_awaiting_provisioning_is_false_once_provisioned():
    job = mock_job(stack_id='arn:aws:cloudformation:us-east-1:mock:stack/mock')
    assert is_awaiting_provisioning(job=job) is False


def test_awaiting_provisioning_is_true_for_a_queued_job():
    assert is_awaiting_provisioning(job=mock_job()) is True


# -------------------------------------------------------------- blocking limit


def test_blocking_limit_type_is_reported_when_the_queue_is_blocked(context):
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(
            blocked=True,
            limit_info=LimitCheckResult(
                limit_type='max_provisioned_instances',
                queue_threshold=10,
                queue_current=11,
            ),
        )
    )
    assert (
        get_blocking_limit_type(context=context, job=mock_job())
        == 'max_provisioned_instances'
    )


def test_blocking_limit_type_is_absent_when_the_queue_is_not_blocked(context):
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(blocked=False)
    )
    assert get_blocking_limit_type(context=context, job=mock_job()) is None


def test_blocking_limit_type_is_absent_without_a_provisioning_queue(context):
    context.queue_profiles = MockQueueProfiles(provisioning_queue=None)
    assert get_blocking_limit_type(context=context, job=mock_job()) is None


def test_blocking_limit_lookup_failure_does_not_fail_the_read(context):
    context.queue_profiles = MockQueueProfiles(raises=True)
    assert get_blocking_limit_type(context=context, job=mock_job()) is None


# ----------------------------------------------------------------- end to end


def test_apply_waiting_signals_on_a_queued_job(context, job_cache):
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(
            blocked=True,
            limit_info=LimitCheckResult(
                limit_type='max_running_jobs', queue_threshold=5, queue_current=6
            ),
        )
    )
    job = mock_job(queued_minutes_ago=30)
    job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    apply_waiting_signals(context=context, jobs=[job])

    assert job.provisioning_attempt == 2
    assert job.max_provisioning_attempts == 3
    assert job.blocking_limit_type == 'max_running_jobs'


def test_apply_waiting_signals_carries_no_thresholds(context, job_cache):
    """
    the threshold and the current usage are cluster-wide. only the limit type may reach
    the job owner.
    """
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(
            blocked=True,
            limit_info=LimitCheckResult(
                limit_type='max_provisioned_instances',
                queue_threshold=137,
                queue_current=142,
            ),
        )
    )
    job = mock_job()

    apply_waiting_signals(context=context, jobs=[job])

    assert job.blocking_limit_type == 'max_provisioned_instances'
    serialized = Utils.to_json(job)
    assert 'queue_threshold' not in serialized
    assert 'queue_current' not in serialized


def test_apply_waiting_signals_omits_attempts_for_a_provisioned_job(context, job_cache):
    """
    the retry counter is cleared on release, not on a successful provision. reporting it
    after the stack exists would show a stale attempt on a job that is no longer waiting
    for one.
    """
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(blocked=True)
    )
    job = mock_job(stack_id='arn:aws:cloudformation:us-east-1:mock:stack/mock')
    job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    apply_waiting_signals(context=context, jobs=[job])

    assert job.provisioning_attempt is None
    assert job.max_provisioning_attempts is None
    assert job.blocking_limit_type is None


def test_apply_waiting_signals_omits_everything_for_a_running_job(context, job_cache):
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(blocked=True)
    )
    job = mock_job(start_time=utcnow() - datetime.timedelta(minutes=1))
    job.state = SocaJobState.RUNNING

    apply_waiting_signals(context=context, jobs=[job])

    assert job.provisioning_attempt is None
    assert job.max_provisioning_attempts is None
    assert job.blocking_limit_type is None


def test_apply_waiting_signals_reports_the_cap_reached(context, job_cache):
    context.queue_profiles = MockQueueProfiles(provisioning_queue=None)
    job = mock_job()
    for _ in range(3):
        job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    apply_waiting_signals(context=context, jobs=[job])

    assert job.provisioning_attempt == 3
    assert job.max_provisioning_attempts == 3


def test_apply_waiting_signals_uses_the_persistent_counter(context, job_cache):
    """
    the persistent per-job counter is the one number surfaced. it is read through the job
    cache, so a scheduler restart does not reset what the owner sees.
    """
    context.queue_profiles = MockQueueProfiles(provisioning_queue=None)
    job = mock_job()
    job_cache.increment_job_provisioning_retry(job_id=job.job_id)
    job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    apply_waiting_signals(context=context, jobs=[job])
    assert job.provisioning_attempt == 3

    job_cache.clear_job_provisioning_retries(job_id=job.job_id)
    apply_waiting_signals(context=context, jobs=[job])
    assert job.provisioning_attempt == 1


def test_apply_waiting_signals_tolerates_an_empty_listing(context, job_cache):
    apply_waiting_signals(context=context, jobs=[])
    apply_waiting_signals(context=context, jobs=None)


def test_no_queue_position_or_estimate_is_exposed():
    """
    guard rail: the underlying data for a position or an ETA does not exist. no field on
    the job may claim otherwise.
    """
    fields = set(SocaJob.model_fields.keys())
    for forbidden in (
        'queue_position',
        'queue_rank',
        'estimated_start_time',
        'jobs_ahead',
    ):
        assert forbidden not in fields


# ----------------------------------------------------- wired into the read path


def test_list_active_jobs_applies_the_signals(context, job_cache, monkeypatch):
    """
    the enrichment is only useful if the owner-scoped listing actually calls it. this is
    the regression guard for the wiring, not for the arithmetic.
    """
    from ideadatamodel import constants
    from ideadatamodel.scheduler import ListJobsResult
    from ideasdk.api import ApiInvocationContext
    from ideasdk.utils import GroupNameHelper
    from ideascheduler.app.api.scheduler_api import SchedulerAPI

    owner = 'mockuser'
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: owner, raising=False
    )
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(
            blocked=True,
            limit_info=LimitCheckResult(
                limit_type='max_provisioned_instances',
                queue_threshold=10,
                queue_current=11,
            ),
        )
    )

    job = mock_job(job_id=Utils.short_uuid())
    job_cache.sync(jobs=[job])
    job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    api_context = ApiInvocationContext(
        context=context,
        request={
            'header': {
                'namespace': 'Scheduler.ListActiveJobs',
                'request_id': Utils.uuid(),
            },
            'payload': {},
        },
        invocation_source=constants.API_INVOCATION_SOURCE_HTTP,
        group_name_helper=GroupNameHelper(context=context),
        logger=context.logger(),
    )

    SchedulerAPI(context=context).list_active_jobs(api_context)

    result = api_context.get_response_payload_as(ListJobsResult)
    listed = {entry.job_id: entry for entry in result.listing}
    assert job.job_id in listed
    assert listed[job.job_id].provisioning_attempt == 2
    assert listed[job.job_id].max_provisioning_attempts == 3
    assert listed[job.job_id].blocking_limit_type == 'max_provisioned_instances'


def test_apply_waiting_signals_names_no_limit_for_a_held_job(context, job_cache):
    """
    a job held at the retry cap is not queued behind a limit; provisioning stopped
    retrying it. naming the queue limit sends its owner after the wrong thing, and the
    limit flag is set for the whole queue profile rather than for this job.
    """
    context.queue_profiles = MockQueueProfiles(
        provisioning_queue=MockProvisioningQueue(
            blocked=True,
            limit_info=LimitCheckResult(
                limit_type='max_provisioned_instances',
                queue_threshold=5,
                queue_current=6,
            ),
        )
    )
    job = mock_job(state=SocaJobState.HELD)
    for _ in range(3):
        job_cache.increment_job_provisioning_retry(job_id=job.job_id)

    apply_waiting_signals(context=context, jobs=[job])

    assert job.blocking_limit_type is None
    # the attempt count is still reported: it is what the cap was reached on
    assert job.provisioning_attempt == 3
    assert job.max_provisioning_attempts == 3
