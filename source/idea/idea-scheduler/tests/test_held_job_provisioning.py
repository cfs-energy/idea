"""
Test Cases for the live job state check the provisioner makes before launching capacity

qhold leaves the job record in place, so an existence check reads a held job as active and
provisions a compute stack for a job the scheduler will never dispatch. these tests cover
the state read at the provisioning chokepoint every job passes through.
"""

from threading import Event
from types import SimpleNamespace
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
            instance_types=['c5.large'],
            instance_ami='ami-mock',
            base_os='amazonlinux2023',
            enable_ht_support=False,
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
    def __init__(self, jobs: List[SocaJob], queues=('normal',)):
        super().__init__()
        self._jobs = list(jobs)
        self.queue_profile = SocaAnyPayload()
        self.queue_profile.queues = list(queues)

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
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda _self, raise_on_error=False: ['job'] * 1,
    )
    synced: List[SocaJob] = []
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: synced.extend(jobs)

    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([held_job, queued_job])
    provisioner._exit = _StopAtFirstWait()

    batches = provisioner._drain_batch_queue()

    assert [[job.job_id for job in batch] for batch in batches.values()] == [
        [queued_job.job_id]
    ]
    # the cache learns the live state, so the job monitor stops re-queueing the held job;
    # the queued one is cached as PBS holds it too, so its placement uses any edit (SJ-36)
    assert [(job.job_id, job.state) for job in synced] == [
        (held_job.job_id, SocaJobState.HELD),
        (queued_job.job_id, SocaJobState.QUEUED),
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


def test_jobs_sharing_a_group_but_not_a_signature_are_separate_batches(
    context, mock_scheduler, monkeypatch
):
    # SCH-02: a custom group put another project's job on a stack built with this one's role
    first, second = mock_job(), mock_job()
    for job, profile in ((first, 'project-a-profile'), (second, 'project-b-profile')):
        job.params.job_group = 'custom-shared'
        job.params.instance_profile = profile
        mock_scheduler.live_jobs[job.job_id] = job
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda _self, raise_on_error=False: ['job'] * 2,
    )
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: None

    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([first, second])
    provisioner._exit = _StopAtFirstWait()

    batches = list(provisioner._drain_batch_queue().values())
    assert sorted([job.job_id for job in batch] for batch in batches) == sorted(
        [[first.job_id], [second.job_id]]
    )


def test_a_job_whose_signature_differs_from_the_stack_is_held_back(context):
    from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
        ProvisionJobs,
    )

    first, second = mock_job(), mock_job()
    first.params.instance_profile = 'project-a-profile'
    second.params.instance_profile = 'project-b-profile'
    provision = ProvisionJobs(
        context=context, jobs=[first, second], logger=context.logger('test')
    )
    stack = SocaAnyPayload()
    stack.soca_capacity_signature = first.get_capacity_signature()
    provision.provisioning_util._stack = stack
    provision.raise_if_capacity_signature_differs()
    assert [job.job_id for job in provision.jobs] == [first.job_id]
    assert [job.job_id for job in provision._held_back] == [second.job_id]


def test_the_expected_batch_counts_only_this_profiles_queues_and_the_wait_is_bounded(
    context, mock_scheduler, monkeypatch
):
    # SCH-25: the same group queued in another profile's queue was waited for forever
    job = mock_job()
    job.params.job_group = 'custom-shared'
    mock_scheduler.live_jobs[job.job_id] = job
    asked = []

    def get_count(qselect):
        asked.append(qselect.queue)
        # the profile's own queue holds this job; a queue elsewhere would add a second
        return 1 if qselect.queue == 'normal' else 1

    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda qselect, raise_on_error=False: ['job'] * get_count(qselect),
    )
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: None
    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([job], queues=('normal',))
    provisioner._exit = Event()
    batches = provisioner._drain_batch_queue()
    assert set(asked) == {'normal'}
    assert [[j.job_id for j in b] for b in batches.values()] == [[job.job_id]]


def test_a_batch_that_never_fills_goes_after_the_bounded_wait(
    context, mock_scheduler, monkeypatch
):
    from ideascheduler.app.provisioning.job_provisioner import job_provisioner

    job = mock_job()
    job.params.job_group = 'custom-shared'
    mock_scheduler.live_jobs[job.job_id] = job
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda _self, raise_on_error=False: ['job'] * 2,
    )
    monkeypatch.setattr(job_provisioner, 'BATCH_DRAIN_MAX_ROUNDS', 3)
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: None
    waits = []

    class CountingExit(Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([job])
    provisioner._exit = CountingExit()
    batches = provisioner._drain_batch_queue()
    assert len(waits) == 2
    assert [[j.job_id for j in b] for b in batches.values()] == [[job.job_id]]


def test_a_retry_places_only_the_jobs_left_unprovisioned(
    context, mock_scheduler, monkeypatch
):
    # SP-11: the retry re-ran the whole batch, placing placed jobs again
    from ideadatamodel import errorcodes

    placed, held = mock_job(), mock_job()
    for job in (placed, held):
        mock_scheduler.live_jobs[job.job_id] = job
    attempts = []

    def invoke(self):
        attempts.append([job.job_id for job in self.jobs])
        if len(attempts) == 1:
            return ProvisionJobsResult(
                status=False,
                unprovisioned_jobs=[held],
                error_code=errorcodes.RETRY_JOB_PROVISIONING,
            )
        return ProvisionJobsResult(status=True)

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    provisioner = make_provisioner(context)
    provisioner._exit = SimpleNamespace(
        is_set=lambda: False, wait=lambda timeout=None: None
    )
    result = provisioner._provision_with_retry_backoff(jobs=[placed, held])
    assert result.status is True
    assert attempts == [[placed.job_id, held.job_id], [held.job_id]]


def test_a_later_failed_round_names_only_the_jobs_still_unplaced(
    context, mock_scheduler, monkeypatch
):
    # B-S1: round 2 failed without naming jobs, and the caller charged the whole batch,
    # including the job round 1 had placed
    from ideadatamodel import errorcodes

    placed, held = mock_job(), mock_job()
    for job in (placed, held):
        mock_scheduler.live_jobs[job.job_id] = job
    rounds = []

    def invoke(self):
        rounds.append([job.job_id for job in self.jobs])
        if len(rounds) == 1:
            return ProvisionJobsResult(
                status=False,
                unprovisioned_jobs=[held],
                error_code=errorcodes.RETRY_JOB_PROVISIONING,
            )
        return ProvisionJobsResult(
            status=False, error_code=errorcodes.GENERAL_ERROR, message='quota'
        )

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    provisioner = make_provisioner(context)
    provisioner._exit = SimpleNamespace(
        is_set=lambda: False, wait=lambda timeout=None: None
    )
    result = provisioner._provision_with_retry_backoff(jobs=[placed, held])
    assert result.status is False
    assert [job.job_id for job in result.unprovisioned_jobs] == [held.job_id]


def test_an_edit_of_a_job_with_no_capacity_yet_is_what_placement_uses(
    context, mock_scheduler, monkeypatch
):
    # SJ-36: an accepted qalter of an unbound job only signals its queue; the cache kept
    # the request from before the edit and placement and license seats used it
    from ideadatamodel import SocaJobLicenseAsk

    placed: List[SocaJob] = []

    def invoke(_self) -> ProvisionJobsResult:
        placed.extend(_self.jobs)
        return ProvisionJobsResult(status=True)

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    cached = mock_job()
    cached.params.licenses = [SocaJobLicenseAsk(name='lic', count=1)]
    edited = cached.model_copy(deep=True)
    edited.params.licenses = [SocaJobLicenseAsk(name='lic', count=3)]
    edited.params.nodes = 2
    mock_scheduler.live_jobs[cached.job_id] = edited
    store = {}
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: store.update({j.job_id: j for j in jobs})
    context.job_cache.get_job = lambda job_id: store.get(job_id)

    make_provisioner(context)._provision_with_retry_backoff(jobs=[cached])

    [job] = placed
    assert job.params.licenses[0].count == 3
    assert job.params.nodes == 2
    assert store[cached.job_id].params.licenses[0].count == 3


def test_a_batch_is_placed_as_pbs_holds_its_jobs_now(
    context, mock_scheduler, monkeypatch
):
    first, second = mock_job(), mock_job()
    for job in (first, second):
        job.params.job_group = 'g-batch'
        mock_scheduler.live_jobs[job.job_id] = job
    edited = second.model_copy(deep=True)
    edited.params.nodes = 4
    mock_scheduler.live_jobs[second.job_id] = edited
    monkeypatch.setattr(
        OpenPBSQSelect, 'list_jobs_ids', lambda _self, raise_on_error=False: ['j'] * 2
    )
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: None
    context.job_cache.get_job = lambda job_id: None
    provisioner = make_provisioner(context)
    provisioner._queue = _BatchQueue([first, second])
    provisioner._exit = _StopAtFirstWait()

    [batch] = provisioner._drain_batch_queue().values()
    assert {job.job_id: job.params.nodes for job in batch}[second.job_id] == 4


def _edited_instance_type(job: SocaJob) -> SocaJob:
    edited = job.model_copy(deep=True)
    edited.params.instance_types = ['m5.large']
    assert edited.get_capacity_signature() != job.get_capacity_signature()
    return edited


def test_a_job_edited_after_batching_is_requeued_not_placed_with_the_others(
    context, mock_scheduler, monkeypatch
):
    # signoff2 B1: the SJ-36 refresh put a job whose signature changed into a batch
    # grouped under its old one, and the new stack was built from one job for all
    jobs = [mock_job(), mock_job(), mock_job()]
    for job in jobs:
        job.params.job_group = 'custom-batch'
        mock_scheduler.live_jobs[job.job_id] = job
    edited = _edited_instance_type(jobs[1])
    mock_scheduler.live_jobs[jobs[1].job_id] = edited
    monkeypatch.setattr(
        OpenPBSQSelect, 'list_jobs_ids', lambda _self, raise_on_error=False: ['j'] * 3
    )
    store = {}
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: store.update({j.job_id: j for j in jobs})
    context.job_cache.get_job = lambda job_id: store.get(job_id)
    provisioner = make_provisioner(context)
    queue = _BatchQueue(jobs)
    provisioner._queue = queue
    provisioner._exit = _StopAtFirstWait()

    batches = provisioner._drain_batch_queue()

    placed = [job for batch in batches.values() for job in batch]
    assert sorted(job.job_id for job in placed) == sorted(
        [jobs[0].job_id, jobs[2].job_id]
    )
    assert all(job.params.instance_types == ['c5.large'] for job in placed)
    assert queue.put_job_ids == [jobs[1].job_id]

    # next cycle: the edited job is batched as it is now, on its own
    queue._jobs = [store[jobs[1].job_id]]
    provisioner._exit = _StopAtFirstWait()
    [[next_job]] = provisioner._drain_batch_queue().values()
    assert next_job.job_id == jobs[1].job_id
    assert next_job.params.instance_types == ['m5.large']


def test_a_first_job_edited_before_its_attempt_is_requeued_and_the_rest_placed(
    context, mock_scheduler, monkeypatch
):
    placed: List[List[str]] = []

    def invoke(_self) -> ProvisionJobsResult:
        placed.append([job.job_id for job in _self.jobs])
        return ProvisionJobsResult(status=True)

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    jobs = [mock_job(), mock_job(), mock_job()]
    for job in jobs:
        job.params.job_group = 'custom-batch'
        mock_scheduler.live_jobs[job.job_id] = job
    mock_scheduler.live_jobs[jobs[0].job_id] = _edited_instance_type(jobs[0])
    context.job_cache = SocaAnyPayload()
    context.job_cache.sync = lambda jobs: None
    context.job_cache.get_job = lambda job_id: None
    provisioner = make_provisioner(context)

    provisioner._provision_with_retry_backoff(jobs=list(jobs))

    assert placed == [[jobs[1].job_id, jobs[2].job_id]]
    assert provisioner._queue.put_job_ids == [jobs[0].job_id]


def test_a_new_batch_stack_is_built_only_for_jobs_of_the_first_jobs_signature(
    context, monkeypatch
):
    from ideadatamodel import SocaScalingMode
    from ideascheduler.app.provisioning.job_provisioner import job_provisioner
    from ideascheduler.app.provisioning import JobProvisioningUtil

    first, second = mock_job(), mock_job()
    other = _edited_instance_type(mock_job())
    batch = [first, other, second]
    for job in batch:
        job.scaling_mode = SocaScalingMode.BATCH
    for check in (
        'check_bedrock',
        'ec2_dry_run',
        'check_service_quota',
        'check_reserved_instance_usage',
    ):
        monkeypatch.setattr(JobProvisioningUtil, check, lambda self: None)
    monkeypatch.setattr(JobProvisioningUtil, 'check_budgets', lambda self: [])
    monkeypatch.setattr(JobProvisioningUtil, 'check_licenses', lambda self: [])
    monkeypatch.setattr(JobProvisioningUtil, 'provisioned_capacity', 0)
    helper_jobs = []
    monkeypatch.setattr(
        job_provisioner,
        'BatchCapacityHelper',
        lambda context, jobs, provisioned_capacity: (
            helper_jobs.extend(jobs)
            or SimpleNamespace(
                invoke=lambda: SimpleNamespace(
                    provisioned_jobs=jobs,
                    unprovisioned_jobs=[],
                    capacity_info=SimpleNamespace(
                        target_capacity=len(jobs), comment=''
                    ),
                )
            )
        ),
    )
    built_for = []
    monkeypatch.setattr(
        job_provisioner,
        'CloudFormationStackBuilder',
        lambda context, job, **_: (
            built_for.append(job.job_id) or SimpleNamespace(build=lambda: 'stack-id')
        ),
    )
    bound = []
    monkeypatch.setattr(
        ProvisionJobs,
        'provision_job_in_scheduler',
        lambda self, job, stack_id: bound.append(job.job_id),
    )
    from unittest.mock import Mock

    monkeypatch.setattr(context, 'metrics', Mock(), raising=False)
    monkeypatch.setattr(context, 'lifecycle_events', None, raising=False)
    provision = ProvisionJobs(context=context, jobs=batch, logger=context.logger('t'))
    with pytest.raises(exceptions.SocaException) as exc_info:
        provision.create_new_stack()
    assert exc_info.value.error_code == errorcodes.RETRY_JOB_PROVISIONING
    assert [job.job_id for job in exc_info.value.ref] == [other.job_id]
    assert built_for == [first.job_id]
    assert bound == [first.job_id, second.job_id]
