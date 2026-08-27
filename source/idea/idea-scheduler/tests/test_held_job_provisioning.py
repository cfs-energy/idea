"""
Test Cases for the live job state check the provisioner makes before launching capacity

qhold leaves the job record in place, so an existence check reads a held job as active and
provisions a compute stack for a job the scheduler will never dispatch. these tests cover
the state read at the provisioning chokepoint every job passes through.
"""

from threading import Event
from typing import Dict, List, Optional

import pytest

from ideadatamodel import (
    SocaAnyPayload,
    SocaJob,
    SocaJobParams,
    SocaJobState,
    errorcodes,
    exceptions,
)
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions
from ideasdk.utils import Utils
from ideascheduler.app.provisioning import JobProvisioner, JobProvisioningQueueEmpty
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    ProvisionJobs,
    ProvisionJobsResult,
)
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect


class MockScheduler:
    """serves the live job state, and records how often it was asked for it"""

    def __init__(self):
        self.live_jobs: Dict[str, SocaJob] = {}
        self.get_job_calls: List[str] = []
        self.list_jobs_calls: List[List[str]] = []
        self.raise_error = False
        self.comments: Dict[str, str] = {}

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        self.get_job_calls.append(job_id)
        if self.raise_error:
            raise exceptions.soca_exception(
                errorcodes.SCHEDULER_ERROR, 'qstat unavailable'
            )
        return self.live_jobs.get(job_id)

    def list_jobs(self, job_ids: List[str] = None, **_) -> List[SocaJob]:
        # one qstat for the batch: unknown ids are skipped, as qstat skips them
        self.list_jobs_calls.append(list(job_ids))
        return [
            self.live_jobs[job_id] for job_id in job_ids if job_id in self.live_jobs
        ]

    def is_job_queued_or_running(self, job_id: str) -> bool:
        # what qstat's exit code says: zero while the job record exists, held or not
        return job_id in self.live_jobs

    def set_job_comment(self, job_id: str, comment: str) -> bool:
        self.comments[job_id] = comment
        return True


class MockProvisioningQueue:
    queue_mode = None

    def __init__(self):
        self.put_job_ids: List[str] = []

    def put(self, job: SocaJob, modified: bool = False):
        self.put_job_ids.append(job.job_id)


@pytest.fixture()
def mock_scheduler(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler
    return scheduler


@pytest.fixture()
def provisioned_batches(monkeypatch) -> List[List[str]]:
    """records the batches that reached CloudFormation stack creation"""
    batches: List[List[str]] = []

    def invoke(_self) -> ProvisionJobsResult:
        batches.append([job.job_id for job in _self.jobs])
        return ProvisionJobsResult(status=True)

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    return batches


def mock_job(state: SocaJobState = SocaJobState.QUEUED) -> SocaJob:
    job_id = Utils.short_uuid()
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id,
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='compute',
        state=state,
        provisioned=False,
        provisioning_options=SocaJobProvisioningOptions(),
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            compute_stack=f'idea-mock-compute-ondemand-{job_id}',
            stack_id='tbd',
        ),
    )


def live_job_in_state(job: SocaJob, state: SocaJobState) -> SocaJob:
    """the same job as the scheduler now reports it, leaving the cached copy untouched"""
    live_job = job.model_copy(deep=True)
    live_job.state = state
    return live_job


def make_provisioner(context) -> JobProvisioner:
    provisioner = JobProvisioner.__new__(JobProvisioner)
    provisioner._context = context
    provisioner._logger = context.logger('test-job-provisioner')
    provisioner._queue = MockProvisioningQueue()
    provisioner._exit = Event()
    return provisioner


def test_queued_job_is_provisioned(context, mock_scheduler, provisioned_batches):
    """a job the scheduler will dispatch must still reach stack creation"""
    job = mock_job()
    mock_scheduler.live_jobs[job.job_id] = job

    result = make_provisioner(context)._provision_with_retry_backoff(jobs=[job])

    assert result.status is True
    assert provisioned_batches == [[job.job_id]]
    # one live state read per provisioning attempt
    assert mock_scheduler.get_job_calls == [job.job_id]


def test_held_job_is_not_provisioned(context, mock_scheduler, provisioned_batches):
    """
    the job was queued when it entered the provisioning queue and is held by the time it is
    picked up: the cached state still reads QUEUED, so only the live state can stop the stack.
    """
    job = mock_job()
    mock_scheduler.live_jobs[job.job_id] = live_job_in_state(job, SocaJobState.HELD)

    result = make_provisioner(context)._provision_with_retry_backoff(jobs=[job])

    assert result.status is True
    assert provisioned_batches == []


def test_held_first_job_skips_the_whole_batch(
    context, mock_scheduler, provisioned_batches
):
    """batch provisioning creates one stack for the batch, keyed off the first job"""
    held_job = mock_job()
    other_job = mock_job()
    mock_scheduler.live_jobs[held_job.job_id] = live_job_in_state(
        held_job, SocaJobState.HELD
    )
    mock_scheduler.live_jobs[other_job.job_id] = other_job

    result = make_provisioner(context)._provision_with_retry_backoff(
        jobs=[held_job, other_job]
    )

    assert result.status is True
    assert provisioned_batches == []


def test_deleted_job_is_not_provisioned(context, mock_scheduler, provisioned_batches):
    """a job the scheduler no longer knows about must not launch capacity"""
    job = mock_job()

    result = make_provisioner(context)._provision_with_retry_backoff(jobs=[job])

    assert result.status is True
    assert provisioned_batches == []


class _BatchQueue(MockProvisioningQueue):
    def __init__(self, jobs: List[SocaJob]):
        super().__init__()
        self._jobs = list(jobs)

    def get(self, timeout: float = 1) -> SocaJob:
        if len(self._jobs) > 0:
            return self._jobs.pop(0)
        raise JobProvisioningQueueEmpty()


class _StopAtFirstWait(Event):
    """the drain waits between passes; a wait means the batch did not fill on the first"""

    def wait(self, timeout=None):
        self.set()
        return True


def test_held_job_does_not_block_the_batch(context, mock_scheduler, monkeypatch):
    """
    the queued-jobs count a batch waits for excludes a held job while the cache still
    reads it as QUEUED. the batch drops the held job and provisions the rest.
    """
    held_job = mock_job()
    queued_job = mock_job()
    for job in (held_job, queued_job):
        job.params.job_group = 'g-batch'
    mock_scheduler.live_jobs[held_job.job_id] = live_job_in_state(
        held_job, SocaJobState.HELD
    )
    mock_scheduler.live_jobs[queued_job.job_id] = queued_job
    # what qselect counts for the group: the one queued job
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda _self: 1)
    synced: List[SocaJob] = []
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: synced.extend(jobs)

    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([held_job, queued_job])
    provisioner._exit = _StopAtFirstWait()

    batches = provisioner._drain_batch_queue()

    assert [job.job_id for job in batches['g-batch']] == [queued_job.job_id]
    # the cache learns the live state, so the job monitor stops re-queueing the held job
    assert [(job.job_id, job.state) for job in synced] == [
        (held_job.job_id, SocaJobState.HELD)
    ]
    assert mock_scheduler.list_jobs_calls == [[held_job.job_id, queued_job.job_id]]


def test_unreadable_job_state_does_not_provision(
    context, mock_scheduler, provisioned_batches
):
    """
    a scheduler that cannot be queried is not permission to launch capacity: the job stays in
    the scheduler and the job monitor re-submits it on the next cycle.
    """
    job = mock_job()
    mock_scheduler.live_jobs[job.job_id] = job
    mock_scheduler.raise_error = True

    result = make_provisioner(context)._provision_with_retry_backoff(jobs=[job])

    assert result.status is True
    assert provisioned_batches == []
