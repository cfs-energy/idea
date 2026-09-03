"""
Test Cases for the job provisioning retry cap
"""

import logging
from typing import Dict, List, Optional

import pytest

from ideadatamodel import (
    errorcodes,
    exceptions,
    SocaJob,
    SocaJobParams,
    SocaJobState,
)
from ideadatamodel.aws.cloudformation_stack import CloudFormationStack
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions
from ideasdk.aws import AWSUtil
from ideasdk.utils import Utils
from ideascheduler.app.api.job_waiting_signals import get_max_provisioning_attempts
from ideascheduler.app.provisioning import (
    JobCache,
    JobMonitor,
    JobProvisioner,
    JobProvisioningUtil,
)
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    ProvisionJobs,
    ProvisionJobsResult,
)
from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
    NodeHouseKeepingSession,
)

LOG_TAG = 'test_provisioning_retry_cap'


class MockScheduler:
    """records hold/attribute calls made by the retry cap"""

    def __init__(self):
        self.held_jobs = []
        self.reset_jobs: List[str] = []
        self.job_attributes: Dict[str, Dict] = {}
        self.comments: Dict[str, str] = {}
        self.live_jobs: Dict[str, SocaJob] = {}

    def hold_job(self, job_id: str) -> bool:
        self.held_jobs.append(job_id)
        return True

    def set_job_attributes(self, job_id: str, attributes: Dict) -> bool:
        self.job_attributes[job_id] = attributes
        return True

    def reset_job(self, job_id: str) -> bool:
        self.reset_jobs.append(job_id)
        return True

    def set_job_comment(self, job_id: str, comment: str) -> bool:
        self.comments[job_id] = comment
        return True

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return self.live_jobs.get(job_id)


@pytest.fixture()
def job_cache(context):
    cache = JobCache(context=context)
    context.job_cache = cache
    # the job cache is a sqlite file shared by every test run. the housekeeper scans the
    # whole jobs table, so jobs left behind by other tests would drive its decisions.
    cache.get_jobs_table().delete()
    return cache


@pytest.fixture()
def mock_scheduler(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler
    return scheduler


def mock_job(job_id: str) -> SocaJob:
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id,
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='normal',
        state=SocaJobState.QUEUED,
        params=SocaJobParams(nodes=1, cpus=1),
    )


def test_retry_count_increment_get_and_clear(context, job_cache):
    job_id = Utils.short_uuid()
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 0
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 1
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 2
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 3
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 3
    job_cache.clear_job_provisioning_retries(job_id=job_id)
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 0


def test_retry_count_survives_provisioning_error_lifecycle(context, job_cache):
    """
    the provisioning error is cleared on every successful stack creation, but the retry
    count must survive it - otherwise a job that cycles failed stacks resets its own cap.
    """
    job_id = Utils.short_uuid()
    job_cache.increment_job_provisioning_retry(job_id=job_id)
    job_cache.set_job_provisioning_error(
        job_id=job_id, error_code='MOCK_ERROR', message='mock error'
    )
    job_cache.clear_job_provisioning_error(job_id=job_id)
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 1


def test_track_provisioning_failure_below_cap(context, job_cache, mock_scheduler):
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is False
    )
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is False
    )
    assert len(mock_scheduler.held_jobs) == 0
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 2


@pytest.mark.parametrize('max_retries', [0, -1])
def test_a_cap_of_zero_or_less_is_unlimited_for_every_reader(
    context, job_cache, mock_scheduler, max_retries
):
    """
    max_provisioning_retries <= 0 disables the cap: the job is retried indefinitely and
    never held. every reader of the setting has to agree, or a job would be re-queued by
    one and treated as out of budget by another.
    """
    context.config().put(
        'scheduler.job_provisioning.max_provisioning_retries', max_retries
    )
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    for _ in range(5):
        assert (
            provisioning_util.track_provisioning_failure(
                job=job, message='mock failure'
            )
            is False
        )

    assert mock_scheduler.held_jobs == []
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 5

    # the job monitor keeps re-queueing the job
    job_monitor = JobMonitor.__new__(JobMonitor)
    job_monitor._context = context
    assert job_monitor._provisioning_retries_exhausted(job) is False

    # and the waiting-signal api reports no budget to count against
    assert get_max_provisioning_attempts(context) is None


def test_track_provisioning_failure_exhausts_and_holds(
    context, job_cache, mock_scheduler
):
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    for _ in range(2):
        assert (
            provisioning_util.track_provisioning_failure(
                job=job, message='mock failure'
            )
            is False
        )
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is True
    )

    # job is held in the scheduler and the reason is visible on the job
    assert mock_scheduler.held_jobs == [job.job_id]
    assert 'error_message' in mock_scheduler.job_attributes[job.job_id]

    # final disposition is recorded in the job cache
    cached_job = job_cache.get_job(job_id=job.job_id)
    assert cached_job is not None
    assert cached_job.state == SocaJobState.HELD
    assert cached_job.error_message is not None
    assert 'provisioning failed 3 times' in cached_job.error_message


def test_track_provisioning_failure_hold_failure_is_not_fatal(
    context, job_cache, mock_scheduler
):
    def failing_hold(job_id: str) -> bool:
        raise exceptions.soca_exception(
            error_code=errorcodes.SCHEDULER_ERROR, message='qhold failed'
        )

    mock_scheduler.hold_job = failing_hold

    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])
    for _ in range(2):
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is True
    )


def make_provisioner(context) -> JobProvisioner:
    provisioner = JobProvisioner.__new__(JobProvisioner)
    provisioner._context = context
    provisioner._logger = context.logger('test-job-provisioner')
    return provisioner


def test_track_failed_jobs_ignores_wait_states(context, job_cache, mock_scheduler):
    provisioner = make_provisioner(context)
    job = mock_job(job_id=Utils.short_uuid())

    # stack still in progress - no error code, no exception
    result = ProvisionJobsResult(status=False)
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]

    # waiting on licenses
    result = ProvisionJobsResult(
        status=False, error_code=errorcodes.NOT_ENOUGH_LICENSES
    )
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]

    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0
    assert len(mock_scheduler.held_jobs) == 0


def test_track_failed_jobs_counts_failures_and_holds(
    context, job_cache, mock_scheduler
):
    provisioner = make_provisioner(context)
    job = mock_job(job_id=Utils.short_uuid())
    result = ProvisionJobsResult(
        status=False,
        error_code=errorcodes.GENERAL_ERROR,
        exception=exceptions.soca_exception(
            error_code=errorcodes.GENERAL_ERROR, message='mock provisioning failure'
        ),
    )

    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]
    # third failed cycle exhausts the cap - job is held and must not be re-queued
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == []
    assert mock_scheduler.held_jobs == [job.job_id]


class MockJobMonitor:
    def __init__(self):
        self.modified_jobs: List[str] = []

    def job_modified(self, job: SocaJob):
        self.modified_jobs.append(job.job_id)


class MockCloudFormation:
    """
    the stacks cloudformation reports. deleting one only accepts the request, as in aws:
    the stack keeps being reported until the delete completes, which is what lets a failed
    stack be seen twice, once by the housekeeper and again by the next provisioning cycle.
    """

    def __init__(self):
        self.stacks: Dict[str, CloudFormationStack] = {}
        self.delete_requests: List[str] = []

    def put(self, compute_stack: str, stack_id: str, stack_status: str):
        self.stacks[compute_stack] = CloudFormationStack(
            entry={
                'StackId': stack_id,
                'StackName': compute_stack,
                'StackStatus': stack_status,
            }
        )

    # bound methods, so they replace the AWSUtil methods without taking its self
    def describe(self, stack_name: str) -> Optional[CloudFormationStack]:
        return self.stacks.get(stack_name)

    def delete(self, stack_name: str):
        self.delete_requests.append(stack_name)


@pytest.fixture()
def mock_job_monitor(context):
    job_monitor = MockJobMonitor()
    context.job_monitor = job_monitor
    return job_monitor


@pytest.fixture()
def cloudformation(monkeypatch) -> MockCloudFormation:
    cfn = MockCloudFormation()
    monkeypatch.setattr(AWSUtil, 'cloudformation_describe_stack', cfn.describe)
    monkeypatch.setattr(AWSUtil, 'cloudformation_delete_stack', cfn.delete)
    return cfn


def compute_stack_name(job_id: str) -> str:
    return f'idea-mock-compute-ondemand-{job_id}'


def stack_arn(compute_stack: str, stack_uid: str) -> str:
    return f'arn:aws:cloudformation:us-east-1:123456789012:stack/{compute_stack}/{stack_uid}'


def stack_job(job_id: str, stack_id: Optional[str]) -> SocaJob:
    """
    the same job as each path sees it: holding a stack (the housekeeper's view) or
    holding none after the housekeeper reset it (the provisioner's view).
    """
    job = mock_job(job_id=job_id)
    job.queue_type = 'compute'
    job.provisioned = stack_id is not None
    job.provisioning_options = SocaJobProvisioningOptions()
    job.params.compute_stack = compute_stack_name(job_id)
    job.params.stack_id = stack_id if stack_id is not None else 'tbd'
    return job


def housekeeper_pass(context):
    NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    ).retry_provisioning_cleanup()


def provisioner_pass(context, job: SocaJob) -> List[SocaJob]:
    """one provisioning cycle; returns the jobs that may be re-queued"""
    result = ProvisionJobs(
        context=context, jobs=[job], logger=logging.getLogger(LOG_TAG)
    ).invoke()
    return make_provisioner(context)._track_failed_jobs(jobs=[job], result=result)


def test_one_failed_stack_counts_once_across_housekeeper_and_provisioner(
    context, job_cache, mock_scheduler, mock_job_monitor, cloudformation
):
    """
    the housekeeper deletes a failed stack and hands the job straight back, but
    cloudformation still reports the stack, so the next provisioning cycle finds the same
    failure and deletes it again. one failed stack has to cost one retry, not two.
    """
    job_id = Utils.short_uuid()
    compute_stack = compute_stack_name(job_id)
    cloudformation.put(
        compute_stack=compute_stack,
        stack_id=stack_arn(compute_stack, 'first'),
        stack_status='ROLLBACK_COMPLETE',
    )

    held_job = stack_job(job_id=job_id, stack_id=stack_arn(compute_stack, 'first'))
    job_cache.sync(jobs=[held_job])

    housekeeper_pass(context)
    assert mock_scheduler.reset_jobs == [job_id]
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 1

    # the housekeeper reset the job, so it holds no stack, but the failed stack is still
    # there because cloudformation has not finished deleting it.
    requeued_job = stack_job(job_id=job_id, stack_id=None)
    mock_scheduler.live_jobs[job_id] = requeued_job
    assert provisioner_pass(context, requeued_job) == [requeued_job]

    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 1


def test_a_stack_stuck_in_delete_failed_counts_every_cycle(
    context, job_cache, mock_scheduler, cloudformation
):
    """
    a stack that cannot be deleted keeps its id for good and DeleteStack still returns
    success, so counting it once would pin the retry count and the job would never be
    held. every sighting is charged instead, and the cap eventually stops it.
    """
    job_id = Utils.short_uuid()
    compute_stack = compute_stack_name(job_id)
    cloudformation.put(
        compute_stack=compute_stack,
        stack_id=stack_arn(compute_stack, 'stuck'),
        stack_status='DELETE_FAILED',
    )
    job = stack_job(job_id=job_id, stack_id=None)
    mock_scheduler.live_jobs[job_id] = job

    for attempt in (1, 2):
        assert provisioner_pass(context, job) == [job]
        assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == attempt

    # the third sighting exhausts the cap and the job is not returned for re-queue
    assert provisioner_pass(context, job) == []
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 3
    assert mock_scheduler.held_jobs == [job_id]

    cached_job = job_cache.get_job(job_id=job_id)
    assert cached_job.error_message is not None
    assert 'provisioning failed 3 times' in cached_job.error_message


def test_each_new_failed_stack_costs_one_retry_then_the_job_is_held(
    context, job_cache, mock_scheduler, cloudformation
):
    """
    with max_provisioning_retries=3 a job whose stack fails gets exactly 3 attempts: the
    third exhausts the budget, the job is held and is not returned for re-queue.
    """
    job_id = Utils.short_uuid()
    compute_stack = compute_stack_name(job_id)
    job = stack_job(job_id=job_id, stack_id=None)
    mock_scheduler.live_jobs[job_id] = job

    for attempt in range(1, 3):
        cloudformation.put(
            compute_stack=compute_stack,
            stack_id=stack_arn(compute_stack, f'attempt-{attempt}'),
            stack_status='ROLLBACK_COMPLETE',
        )
        assert provisioner_pass(context, job) == [job]
        assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == attempt

    cloudformation.put(
        compute_stack=compute_stack,
        stack_id=stack_arn(compute_stack, 'attempt-3'),
        stack_status='ROLLBACK_COMPLETE',
    )
    assert provisioner_pass(context, job) == []

    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 3
    assert mock_scheduler.held_jobs == [job_id]
