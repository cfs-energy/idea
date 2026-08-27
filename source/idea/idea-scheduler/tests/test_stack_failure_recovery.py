"""
Test Cases for recovery from a compute stack that failed to create

a stack left in ROLLBACK_COMPLETE can only be deleted, never updated, and its name
blocks a new stack for the same job. these tests pin down who deletes it: the node
housekeeper for a job that still holds the stack, the provisioner for a job that does
not - and that neither of them resets a job on a stack status they cannot interpret.
"""

import logging
from threading import Event
from typing import Dict, List, Optional

import pytest

from ideadatamodel import (
    ProvisioningStatus,
    SocaJob,
    SocaJobParams,
    SocaJobState,
    errorcodes,
)
from ideadatamodel.aws.cloudformation_stack import CloudFormationStack
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions
from ideasdk.aws import AWSUtil
from ideasdk.utils import Utils
from ideascheduler.app.provisioning import (
    JobCache,
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

LOG_TAG = 'test_stack_failure_recovery'


class MockScheduler:
    """records the scheduler calls the stack failure paths make"""

    def __init__(self):
        self.reset_jobs: List[str] = []
        self.held_jobs: List[str] = []
        self.job_attributes: Dict[str, Dict] = {}
        self.comments: Dict[str, str] = {}
        self.live_jobs: Dict[str, SocaJob] = {}

    def reset_job(self, job_id: str) -> bool:
        self.reset_jobs.append(job_id)
        return True

    def hold_job(self, job_id: str) -> bool:
        self.held_jobs.append(job_id)
        return True

    def set_job_attributes(self, job_id: str, attributes: Dict) -> bool:
        self.job_attributes[job_id] = attributes
        return True

    def set_job_comment(self, job_id: str, comment: str) -> bool:
        self.comments[job_id] = comment
        return True

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return self.live_jobs.get(job_id)


class MockJobMonitor:
    def __init__(self):
        self.modified_jobs: List[str] = []

    def job_modified(self, job: SocaJob):
        self.modified_jobs.append(job.job_id)


class MockProvisioningQueue:
    queue_mode = None

    def __init__(self):
        self.put_job_ids: List[str] = []

    def put(self, job: SocaJob, modified: bool = False):
        self.put_job_ids.append(job.job_id)


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


@pytest.fixture()
def mock_job_monitor(context):
    job_monitor = MockJobMonitor()
    context.job_monitor = job_monitor
    return job_monitor


@pytest.fixture()
def deleted_stacks(monkeypatch) -> List[str]:
    stacks: List[str] = []

    def delete_stack(_self, stack_name: str):
        stacks.append(stack_name)

    monkeypatch.setattr(AWSUtil, 'cloudformation_delete_stack', delete_stack)
    return stacks


def mock_job(provisioned: bool) -> SocaJob:
    # a job id unique per test: the retry counts and jobs table outlive a single test
    job_id = Utils.short_uuid()
    compute_stack = f'idea-mock-compute-ondemand-{job_id}'
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id,
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='compute',
        state=SocaJobState.QUEUED,
        provisioned=provisioned,
        provisioning_options=SocaJobProvisioningOptions(),
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            compute_stack=compute_stack,
            stack_id=stack_id(compute_stack) if provisioned else 'tbd',
        ),
    )


def stack_id(compute_stack: str) -> str:
    return f'arn:aws:cloudformation:us-east-1:123456789012:stack/{compute_stack}'


def mock_stack(compute_stack: str, stack_status: str) -> CloudFormationStack:
    return CloudFormationStack(
        entry={
            'StackId': stack_id(compute_stack),
            'StackName': compute_stack,
            'StackStatus': stack_status,
        }
    )


def make_provision_jobs(context, job: SocaJob, stack_status: str) -> ProvisionJobs:
    provision_jobs = ProvisionJobs(
        context=context, jobs=[job], logger=logging.getLogger(LOG_TAG)
    )
    provision_jobs.provisioning_util._stack = mock_stack(
        compute_stack=job.get_compute_stack(), stack_status=stack_status
    )
    return provision_jobs


def housekeeping_session(context) -> NodeHouseKeepingSession:
    return NodeHouseKeepingSession(context=context, logger=logging.getLogger(LOG_TAG))


@pytest.mark.parametrize(
    'stack_status, expected',
    [
        ('CREATE_IN_PROGRESS', ProvisioningStatus.IN_PROGRESS),
        ('ROLLBACK_IN_PROGRESS', ProvisioningStatus.DELETE_IN_PROGRESS),
        ('ROLLBACK_COMPLETE', ProvisioningStatus.FAILED),
        ('CREATE_FAILED', ProvisioningStatus.FAILED),
        ('DELETE_FAILED', ProvisioningStatus.FAILED),
    ],
)
def test_check_status_maps_every_failed_create_state(context, stack_status, expected):
    """
    every state a failed create passes through must map to a status the callers act on.
    an unmapped state returns None, which reads as "no stack failure" and is what left
    rolled back stacks in place.
    """
    job = mock_job(provisioned=True)
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])
    provisioning_util._stack = mock_stack(
        compute_stack=job.get_compute_stack(), stack_status=stack_status
    )

    assert provisioning_util.check_status() == expected


def test_housekeeper_deletes_failed_stack_and_retries(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    a job holding a stack that failed to create must have the stack deleted and the job
    reset, so the next provisioning cycle can build a new stack under the same name.
    """
    monkeypatch.setattr(
        JobProvisioningUtil, 'check_status', lambda _self: ProvisioningStatus.FAILED
    )
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == [job.get_compute_stack()]
    assert mock_scheduler.reset_jobs == [job.job_id]
    assert mock_job_monitor.modified_jobs == [job.job_id]
    # the failed cycle is counted, so a stack that keeps failing cannot retry forever
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1


def test_housekeeper_leaves_job_alone_on_unmapped_stack_status(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    an unmapped stack status (eg. a rollback still in flight) must not reset the job:
    a reset detaches the job from a stack the housekeeper then never inspects again, and
    the job can never provision under that stack name again.
    """
    monkeypatch.setattr(JobProvisioningUtil, 'check_status', lambda _self: None)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.reset_jobs == []
    assert mock_job_monitor.modified_jobs == []
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0


def test_provisioner_deletes_failed_stack_of_unprovisioned_job(
    context, job_cache, deleted_stacks
):
    """
    a job that holds no stack is never inspected by the housekeeper, so a failed stack
    left under its name is the provisioner's to delete - without it the job reports
    ProvisioningStatus.FAILED on every cycle and never provisions again.
    """
    job = mock_job(provisioned=False)

    result = make_provision_jobs(context, job, 'ROLLBACK_COMPLETE').invoke()

    assert result.status is False
    assert result.error_code == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
    assert Utils.is_true(result.stack_deleted) is True
    assert deleted_stacks == [job.get_compute_stack()]


def test_provisioner_leaves_failed_stack_of_provisioned_job(
    context, job_cache, deleted_stacks
):
    """
    a job that still holds the stack belongs to the housekeeper: it deletes the stack and
    resets the job together. the provisioner must not delete it from under the job.
    """
    job = mock_job(provisioned=True)

    result = make_provision_jobs(context, job, 'ROLLBACK_COMPLETE').invoke()

    assert result.error_code == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
    assert Utils.is_true(result.stack_deleted) is False
    assert deleted_stacks == []


def make_provisioner(context) -> JobProvisioner:
    provisioner = JobProvisioner.__new__(JobProvisioner)
    provisioner._context = context
    provisioner._logger = context.logger('test-job-provisioner')
    provisioner._queue = MockProvisioningQueue()
    provisioner._exit = Event()
    return provisioner


def provision_with_result(
    context, monkeypatch, job: SocaJob, result: ProvisionJobsResult
) -> ProvisionJobsResult:
    monkeypatch.setattr(ProvisionJobs, 'invoke', lambda _self: result)
    # the provisioner reads the job's live state before provisioning it
    context.scheduler.live_jobs[job.job_id] = job
    return make_provisioner(context)._provision_with_retry_backoff(jobs=[job])


def test_deleted_failed_stack_counts_a_provisioning_failure(
    context, job_cache, mock_scheduler, monkeypatch
):
    """
    the cycle that deletes a failed stack is a real provisioning failure. left uncounted,
    the job cycles failed stacks forever without ever reaching the retry cap.
    """
    job = mock_job(provisioned=False)
    result = provision_with_result(
        context,
        monkeypatch,
        job,
        ProvisionJobsResult(
            status=False,
            error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
            message='compute node provisioning Failed',
            stack_deleted=True,
        ),
    )

    assert result.status is True
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1


def test_stack_left_for_housekeeper_is_not_counted_twice(
    context, job_cache, mock_scheduler, monkeypatch
):
    """
    when the stack is left for the housekeeper, the housekeeper counts the failure as it
    deletes it. counting here as well would consume the budget at twice the rate.
    """
    job = mock_job(provisioned=True)
    result = provision_with_result(
        context,
        monkeypatch,
        job,
        ProvisionJobsResult(
            status=False,
            error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
            message='compute node provisioning Failed',
        ),
    )

    assert result.status is True
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0
