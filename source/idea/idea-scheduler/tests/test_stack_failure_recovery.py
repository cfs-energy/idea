"""
Test Cases for recovery from a compute stack that failed to create

a stack left in ROLLBACK_COMPLETE can only be deleted, never updated, and its name
blocks a new stack for the same job. these tests pin down who deletes it: the node
housekeeper for a job that still holds the stack, the provisioner for a job that does
not - and that neither of them resets a job on a stack status they cannot interpret.
"""

import logging
from datetime import datetime, timezone
from threading import Event
from typing import Dict, List, Optional
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    ProvisioningStatus,
    SocaJob,
    SocaJobLicenseAsk,
    SocaJobParams,
    SocaJobState,
    SocaJobSubjob,
    errorcodes,
)
from ideadatamodel.aws.cloudformation_stack import CloudFormationStack
from ideascheduler.app.app_protocols import LicenseAvailability
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
RAN_AT = datetime(2026, 11, 9, 22, 12, 2, tzinfo=timezone.utc)


class MockScheduler:
    """records the scheduler calls the stack failure paths make"""

    def __init__(self):
        self.reset_jobs: List[str] = []
        self.held_jobs: List[str] = []
        self.job_attributes: Dict[str, Dict] = {}
        self.comments: Dict[str, str] = {}
        self.live_jobs: Dict[str, SocaJob] = {}
        # an array's id -> (indices submitted, each subjob's state as PBS lists it, or its
        # state and the start time of one that ran)
        self.subjobs: Dict[str, tuple] = {}

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

    def get_array_subjobs(self, job_id: str):
        submitted, states = self.subjobs[job_id]
        return submitted, [
            SocaJobSubjob(
                job_id=f'{job_id[:-1]}{index}]',
                **dict(zip(('state', 'start_time'), state))
                if isinstance(state, tuple)
                else {'state': state},
            )
            for index, state in enumerate(states, start=1)
        ]


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


class MockLicenseService:
    def __init__(self, available_count: int):
        self.available_count = available_count

    def get_license_availability(self, license_resource_name: str):
        return LicenseAvailability(available_count=self.available_count, check_ok=True)


def test_housekeeper_releases_the_licenses_of_a_job_it_resets(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    a job's license count is recorded when it is provisioned. a job reset after its stack
    failed holds none, and the retry's license check adds the job's ask to what is
    recorded: with its own count still there, a job asking for 2 of 3 was refused as
    "needs 2, 1 are free" for as long as it stayed queued, and so was any other job.
    """
    monkeypatch.setattr(
        JobProvisioningUtil, 'check_status', lambda _self: ProvisioningStatus.FAILED
    )
    license_name = f'license_{Utils.short_uuid()}'
    job = mock_job(provisioned=True)
    job.params.licenses = [SocaJobLicenseAsk(name=license_name, count=2)]
    other = mock_job(provisioned=True)
    other.params.licenses = [SocaJobLicenseAsk(name=license_name, count=1)]
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job
    job_cache.add_active_licenses([job, other])
    assert job_cache.get_active_license_count(license_name=license_name) == 3

    housekeeping_session(context).retry_provisioning_cleanup()

    assert mock_scheduler.reset_jobs == [job.job_id]
    # the other job, still provisioned, keeps its count
    assert job_cache.get_active_license_count(license_name=license_name) == 1
    monkeypatch.setattr(
        context, 'license_service', MockLicenseService(3), raising=False
    )
    JobProvisioningUtil(context=context, jobs=[job]).check_licenses()


def test_housekeeper_keeps_the_licenses_of_a_job_the_scheduler_says_is_running(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    the cache can read queued for a job that runs. its stack is left in place, the job is
    not reset, and it still holds its licenses: giving them back would let another job in.
    """
    monkeypatch.setattr(
        JobProvisioningUtil, 'check_status', lambda _self: ProvisioningStatus.FAILED
    )
    license_name = f'license_{Utils.short_uuid()}'
    job = mock_job(provisioned=True)
    job.params.licenses = [SocaJobLicenseAsk(name=license_name, count=2)]
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job.model_copy(
        update={'state': SocaJobState.RUNNING}
    )
    job_cache.add_active_licenses([job])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert mock_scheduler.reset_jobs == []
    assert deleted_stacks == []
    assert job_cache.get_active_license_count(license_name=license_name) == 2


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
    mock_scheduler.live_jobs[job.job_id] = job

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == [job.get_compute_stack()]
    assert mock_scheduler.reset_jobs == [job.job_id]
    assert mock_job_monitor.modified_jobs == [job.job_id]
    # the failed cycle is counted, so a stack that keeps failing cannot retry forever
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1
    # a failed create is retried up to the cap; only nodes that never ran the job hold it
    assert mock_scheduler.held_jobs == []


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


def test_housekeeper_holds_and_explains_a_job_whose_nodes_never_started_it(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    a stack that came up but never ran the job (nodes that never joined the scheduler) is
    deleted, and the job is held with a comment instead of retried silently: a retry
    launches the same broken nodes again while the job looks like it is simply waiting.
    """
    # check_status itself is asked, as on a cluster: it reports a created stack past the
    # timeout as TIMEOUT, which the sweep took for a failed create and retried. Seen as
    # "attempt 1 of 3 ... will be re-tried", a new stack, and no hold.
    created_stack(monkeypatch, minutes_ago=31)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == [job.get_compute_stack()]
    assert mock_scheduler.reset_jobs == [job.job_id]
    assert mock_scheduler.held_jobs == [job.job_id]
    comment = mock_scheduler.comments[job.job_id]
    assert comment.startswith('IDEA: held. Nodes launched but the job did not start in')
    assert comment.endswith(f'qrls {job.job_id} to retry')
    assert len(comment) <= 128
    # the reason the owner reads, recorded as the platform's hold
    held = job_cache.get_job(job.job_id)
    assert held.reason_class == 'stack_timeout'
    assert 'nodes launched but the job did not start in 30 min' in held.error_message


def created_stack(monkeypatch, minutes_ago: int, stack_id: Optional[str] = None):
    """a compute stack that reached CREATE_COMPLETE, as cloudformation describes it"""
    import arrow
    from types import SimpleNamespace

    monkeypatch.setattr(
        JobProvisioningUtil,
        'stack',
        property(
            lambda _self: SimpleNamespace(
                stack_status='CREATE_COMPLETE',
                stack_id=stack_id,
                creation_time=arrow.utcnow().shift(minutes=-minutes_ago),
                soca_keep_forever=False,
                soca_terminate_when_idle=0,
            )
        ),
    )


class MockInstanceCache:
    def __init__(self, instances):
        self.instances = instances

    def list_compute_instances(self, job_id=None, states=None, **_):
        return [
            instance
            for instance in self.instances
            if instance.soca_job_id == job_id and instance.state in states
        ]


def job_instance(job: SocaJob, bootstrap_status=None, stack=None, state='running'):
    from ideadatamodel import EC2Instance, constants

    tags = {
        constants.IDEA_TAG_JOB_ID: job.job_id.replace('[]', '-array'),
        'aws:cloudformation:stack-id': stack or job.params.stack_id,
    }
    if bootstrap_status:
        tags['idea:BootstrapStatus'] = bootstrap_status
    return EC2Instance(
        {
            'InstanceId': 'i-0123456789abcdef0',
            'InstanceType': 't3.micro',
            'State': {'Name': state},
            'Tags': [{'Key': key, 'Value': value} for key, value in tags.items()],
        }
    )


def test_a_node_too_small_to_bootstrap_holds_its_job_at_once_and_says_so(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    a compute node that cannot set itself up for lack of memory tags itself and stops.
    Nothing read the tag: the job showed "Provisioning" for the whole timeout, three
    times over. The sweep already has the instance, so the first one after the tag holds
    the job and names the instance type.
    """
    created_stack(monkeypatch, minutes_ago=4)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job
    context.instance_cache = MockInstanceCache(
        [job_instance(job, bootstrap_status='insufficient-memory')]
    )

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == [job.get_compute_stack()]
    assert mock_scheduler.reset_jobs == [job.job_id]
    assert mock_scheduler.held_jobs == [job.job_id]
    comment = mock_scheduler.comments[job.job_id]
    assert comment.startswith('IDEA: held. Instance type t3.micro is too small')
    assert len(comment) <= 128
    held = job_cache.get_job(job.job_id)
    assert held.reason_class == 'stack_timeout'
    assert 'instance type t3.micro is too small' in held.error_message
    assert ',' not in held.error_message, 'qalter splits on commas'


@pytest.mark.parametrize(
    'instance',
    [
        # still setting itself up, or failed some other way: the timeout decides
        dict(),
        dict(bootstrap_status='reboot-failed'),
        # a node of an earlier stack of this job, on its way out
        dict(bootstrap_status='insufficient-memory', stack='arn:earlier-stack'),
        dict(bootstrap_status='insufficient-memory', state='shutting-down'),
    ],
)
def test_a_job_is_not_held_as_too_small_without_its_own_nodes_word(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    instance,
):
    created_stack(monkeypatch, minutes_ago=4)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job
    context.instance_cache = MockInstanceCache([job_instance(job, **instance)])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.held_jobs == []
    assert mock_scheduler.reset_jobs == []


def test_a_begun_array_is_left_alone_though_a_node_says_it_is_too_small(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    created_stack(monkeypatch, minutes_ago=4)
    job = mock_job(provisioned=True)
    job.job_id = '357[]'
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job.model_copy(
        update={'state': SocaJobState.SUBJOB_BEGUN}
    )
    context.instance_cache = MockInstanceCache(
        [job_instance(job, bootstrap_status='insufficient-memory')]
    )

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.held_jobs == []
    assert job_cache.get_job(job.job_id).state == SocaJobState.SUBJOB_BEGUN


def test_a_begun_array_past_the_timeout_is_not_held(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """406[]: its row read queued at 30 minutes with its first subjob still running"""
    created_stack(monkeypatch, minutes_ago=31)
    job = mock_job(provisioned=True)
    job.job_id = '357[]'
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job.model_copy(
        update={'state': SocaJobState.SUBJOB_BEGUN}
    )

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.held_jobs == []
    assert mock_scheduler.reset_jobs == []


@pytest.mark.parametrize(
    'status', [ProvisioningStatus.COMPLETED, ProvisioningStatus.TIMEOUT]
)
@pytest.mark.parametrize(
    'job_id, live_state',
    [
        # an array whose first subjob began: PBS reports the run under 357[1], so the
        # array's own row can still read queued
        ('357[]', SocaJobState.SUBJOB_BEGUN),
        # any job whose run event was lost
        ('358', SocaJobState.RUNNING),
        ('359', SocaJobState.EXIT),
    ],
)
def test_the_timeout_sweep_leaves_the_stack_of_a_job_that_runs(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    status,
    job_id,
    live_state,
):
    """
    the sweep reads queued rows from the job cache. A row can read queued for a job that
    runs, so PBS is asked before a stack is deleted: seen on a cluster as the stack of a
    running array deleted 31 minutes after it was created.
    """
    import arrow
    from types import SimpleNamespace

    monkeypatch.setattr(JobProvisioningUtil, 'check_status', lambda _self: status)
    monkeypatch.setattr(
        JobProvisioningUtil,
        'stack',
        property(
            lambda _self: SimpleNamespace(
                creation_time=arrow.utcnow().shift(minutes=-31)
            )
        ),
    )
    job = mock_job(provisioned=True)
    job.job_id = job_id
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job_id] = job.model_copy(update={'state': live_state})

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.reset_jobs == []
    assert mock_scheduler.held_jobs == []
    # what PBS said is kept, so the next sweep does not look at this job at all
    assert job_cache.get_job(job_id).state == live_state
    housekeeping_session(context).retry_provisioning_cleanup()
    assert deleted_stacks == []


@pytest.mark.parametrize('answer', ['gone', 'error'])
def test_the_timeout_sweep_leaves_a_stack_when_pbs_gives_no_answer(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    answer,
):
    monkeypatch.setattr(
        JobProvisioningUtil, 'check_status', lambda _self: ProvisioningStatus.TIMEOUT
    )
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    if answer == 'error':
        monkeypatch.setattr(
            mock_scheduler, 'get_job', Mock(side_effect=RuntimeError('qstat failed'))
        )

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.reset_jobs == []


def timed_out_array(job_cache, mock_scheduler, monkeypatch, live_state) -> SocaJob:
    """a provisioned array whose row reads queued, on a stack past the timeout"""
    monkeypatch.setattr(
        JobProvisioningUtil, 'check_status', lambda _self: ProvisioningStatus.TIMEOUT
    )
    job = mock_job(provisioned=True)
    job.job_id = '357[]'
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job.model_copy(update={'state': live_state})
    return job


@pytest.mark.parametrize('live_state', [SocaJobState.QUEUED, SocaJobState.HELD])
@pytest.mark.parametrize(
    'begun',
    [
        SocaJobState.RUNNING,
        SocaJobState.EXIT,
        (SocaJobState.SUBJOB_EXPIRED, RAN_AT),
        (SocaJobState.FINISHED, RAN_AT),
    ],
)
def test_the_timeout_sweep_does_not_take_an_arrays_own_state_as_not_started(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    live_state,
    begun,
):
    """
    What PBS reports for a begun array after qhold and qrls has not been seen. If it reads
    queued or held while a subjob runs, or with one done and the rest to come, the stack
    stays: the subjobs are asked, not only the array.
    """
    job = timed_out_array(job_cache, mock_scheduler, monkeypatch, live_state)
    mock_scheduler.subjobs[job.job_id] = (3, [begun, SocaJobState.QUEUED, live_state])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.reset_jobs == []
    assert mock_scheduler.held_jobs == []
    # kept as begun, which protects its nodes and takes it out of this sweep
    assert job_cache.get_job(job.job_id).state == SocaJobState.SUBJOB_BEGUN


@pytest.mark.parametrize(
    'answer',
    [
        # not every index listed, the count not known, and no answer at all
        (3, [SocaJobState.QUEUED]),
        (None, [SocaJobState.QUEUED, SocaJobState.QUEUED]),
        (None, []),
        None,
    ],
)
def test_the_timeout_sweep_keeps_an_arrays_stack_unless_every_subjob_is_read(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    answer,
):
    job = timed_out_array(job_cache, mock_scheduler, monkeypatch, SocaJobState.QUEUED)
    if answer is not None:
        mock_scheduler.subjobs[job.job_id] = answer

    housekeeping_session(context).retry_provisioning_cleanup()

    assert deleted_stacks == []
    assert mock_scheduler.reset_jobs == []
    assert job_cache.get_job(job.job_id).state == SocaJobState.QUEUED


def test_the_timeout_sweep_still_retries_an_array_that_never_started(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    job = timed_out_array(job_cache, mock_scheduler, monkeypatch, SocaJobState.QUEUED)
    mock_scheduler.subjobs[job.job_id] = (2, [SocaJobState.QUEUED, SocaJobState.HELD])

    housekeeping_session(context).retry_provisioning_cleanup()

    assert len(deleted_stacks) == 1
    assert mock_scheduler.reset_jobs == [job.job_id]


@pytest.mark.parametrize(
    'stack_status', [ProvisioningStatus.TIMEOUT, ProvisioningStatus.FAILED]
)
@pytest.mark.parametrize(
    'deleted', [SocaJobState.SUBJOB_EXPIRED, SocaJobState.FINISHED]
)
def test_a_subjob_deleted_while_queued_does_not_cost_an_unstarted_array_its_retry(
    context,
    job_cache,
    mock_scheduler,
    mock_job_monitor,
    deleted_stacks,
    monkeypatch,
    stack_status,
    deleted,
):
    """
    qsub -J 1-3, the nodes never join, and the owner deletes 357[2] while it is queued. PBS
    lists that subjob as done, with no start time because it never ran. Nothing has started,
    so the stack is deleted and the array retried as before, and the row is not written as
    begun, which would take it out of this sweep with its nodes left running.
    """
    job = timed_out_array(job_cache, mock_scheduler, monkeypatch, SocaJobState.QUEUED)
    monkeypatch.setattr(JobProvisioningUtil, 'check_status', lambda _self: stack_status)
    mock_scheduler.subjobs[job.job_id] = (
        3,
        [SocaJobState.QUEUED, deleted, SocaJobState.QUEUED],
    )

    housekeeping_session(context).retry_provisioning_cleanup()

    assert len(deleted_stacks) == 1
    assert mock_scheduler.reset_jobs == [job.job_id]
    assert job_cache.get_job(job.job_id).state != SocaJobState.SUBJOB_BEGUN


# one whole housekeeping sweep, as the scheduler runs it once a minute: the node-down rule,
# its cleanup and the job's own stack timeout, in the order begin() runs them. Only AWS and
# PBS are stood in for, each with the state the sweep's own calls leave behind.


class SweepAws:
    """cloudformation, auto scaling and ec2 for the one compute stack of a job"""

    def __init__(self, job: SocaJob, stack_age_secs: int, node_age_secs: int):
        import arrow

        self.job = job
        self.stack_name = job.get_compute_stack()
        self.stack = {
            'StackId': job.params.stack_id,
            'StackName': self.stack_name,
            'StackStatus': 'CREATE_COMPLETE',
            'CreationTime': arrow.utcnow().shift(seconds=-stack_age_secs).datetime,
        }
        self.launch_time = arrow.utcnow().shift(seconds=-node_age_secs)
        self.instance_state = 'running'
        self.deleted_stacks: List[str] = []
        self.terminated: List[str] = []

    # cloudformation
    def describe_stacks(self, StackName):  # noqa: N803
        from botocore.exceptions import ClientError

        if self.stack is None or StackName != self.stack_name:
            raise ClientError(
                {'Error': {'Code': 'ValidationError', 'Message': 'does not exist'}},
                'DescribeStacks',
            )
        return {'Stacks': [dict(self.stack)]}

    def delete_stack(self, StackName):  # noqa: N803
        self.deleted_stacks.append(StackName)
        self.stack['StackStatus'] = 'DELETE_IN_PROGRESS'
        self.instance_state = 'shutting-down'

    def stack_is_gone(self):
        self.stack = None
        self.instance_state = 'terminated'

    # auto scaling
    def describe_auto_scaling_groups(self, AutoScalingGroupNames):  # noqa: N803
        return {
            'AutoScalingGroups': [
                {
                    'AutoScalingGroupName': AutoScalingGroupNames[0],
                    'DesiredCapacity': 1,
                }
            ]
        }

    def detach_instances(self, **_):
        pass

    def update_auto_scaling_group(self, **_):
        pass

    # ec2
    def terminate_instances(self, InstanceIds, **_):  # noqa: N803
        self.terminated.extend(InstanceIds)
        self.instance_state = 'shutting-down'

    def instance(self, bootstrap_status=None):
        from ideadatamodel import EC2Instance, constants

        tags = {
            constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_COMPUTE,
            constants.IDEA_TAG_JOB_ID: self.job.job_id.replace('[]', '-array'),
            constants.IDEA_TAG_JOB_GROUP: 'job-group-1',
            constants.IDEA_TAG_QUEUE_TYPE: 'compute',
            constants.IDEA_TAG_TERMINATE_WHEN_IDLE: '0',
            constants.IDEA_TAG_KEEP_FOREVER: 'false',
            constants.IDEA_TAG_CAPACITY_TYPE: 'on-demand',
            constants.IDEA_TAG_COMPUTE_STACK: self.stack_name,
            constants.AWS_TAG_AUTOSCALING_GROUP_NAME: f'{self.stack_name}-asg',
            constants.AWS_TAG_CFN_STACK_ID: self.job.params.stack_id,
        }
        if bootstrap_status:
            tags['idea:BootstrapStatus'] = bootstrap_status
        return EC2Instance(
            {
                'InstanceId': 'i-0123456789abcdef0',
                'InstanceType': 't3.large',
                'PrivateIpAddress': '10.0.0.1',
                'LaunchTime': self.launch_time.isoformat(),
                'State': {'Name': self.instance_state},
                'Tags': [{'Key': key, 'Value': value} for key, value in tags.items()],
            }
        )


class SweepInstanceCache:
    def __init__(self, aws: SweepAws, bootstrap_status=None):
        self.aws = aws
        self.bootstrap_status = bootstrap_status

    def get_instance(self, instance_id: str):
        return self.aws.instance(self.bootstrap_status)

    def list_compute_instances(self, job_id=None, states=None, **_):
        instance = self.aws.instance(self.bootstrap_status)
        if instance.soca_job_id == job_id and instance.state in states:
            return [instance]
        return []


class SweepScheduler(MockScheduler):
    """PBS with the job and the node its instance registered: down, never heard from"""

    def __init__(self, context, aws: SweepAws):
        from ideadatamodel import SocaComputeNode, SocaComputeNodeState

        super().__init__()
        self.deleted_nodes: List[str] = []
        self.node = SocaComputeNode(
            host='10.0.0.1',
            cluster_name=context.cluster_name(),
            states=[SocaComputeNodeState.DOWN, SocaComputeNodeState.UNKNOWN],
            queue_type='compute',
            instance_id='i-0123456789abcdef0',
            job_id=aws.job.job_id,
            compute_stack=aws.stack_name,
        )

    def list_nodes(self, **_):
        return [] if self.node is None else [self.node]

    def find_node(self, hosts, **_):
        return self.node

    def set_node_state(self, host, state):
        return True

    def delete_node(self, host):
        self.deleted_nodes.append(host)
        self.node = None
        return True

    def reset_job(self, job_id: str) -> bool:
        # qalter back to no stack, as PBS then lists the job
        live = self.live_jobs[job_id].model_copy(update={'provisioned': False})
        live.params = live.params.model_copy(
            update={'stack_id': 'tbd', 'compute_stack': 'tbd'}
        )
        self.live_jobs[job_id] = live
        return super().reset_job(job_id)

    def hold_job(self, job_id: str) -> bool:
        self.live_jobs[job_id] = self.live_jobs[job_id].model_copy(
            update={'state': SocaJobState.HELD}
        )
        return super().hold_job(job_id)


class SweepJobMonitor(MockJobMonitor):
    """a modified job is read from PBS again and its row replaced, as the monitor does"""

    def __init__(self, context):
        super().__init__()
        self.context = context

    def job_modified(self, job: SocaJob):
        super().job_modified(job)
        self.context.job_cache.sync(jobs=[self.context.scheduler.live_jobs[job.job_id]])


def sweep_world(
    context,
    monkeypatch,
    job_cache,
    stack_age_secs: int,
    node_age_secs: int,
    job: Optional[SocaJob] = None,
    bootstrap_status: Optional[str] = None,
):
    from ideasdk.aws import AwsClientProvider

    job = job or mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    aws = SweepAws(job, stack_age_secs=stack_age_secs, node_age_secs=node_age_secs)
    for client in ('cloudformation', 'autoscaling', 'ec2'):
        monkeypatch.setattr(AwsClientProvider, client, lambda *_, **__: aws)
    monkeypatch.setattr('time.sleep', lambda *_: None)
    context.instance_cache = SweepInstanceCache(aws, bootstrap_status)
    context.metrics = Mock()
    context.document_store = Mock()
    context.job_monitor = SweepJobMonitor(context)
    scheduler = SweepScheduler(context, aws)
    scheduler.live_jobs[job.job_id] = job
    context.scheduler = scheduler
    for timeout in ('stack_provisioning_timeout', 'node_unavailable_timeout'):
        context.config().put(f'scheduler.job_provisioning.{timeout}_seconds', 1800)
    return job, aws, scheduler


def sweep(context):
    housekeeping_session(context).begin()


@pytest.mark.parametrize(
    'stack_age_secs, node_age_secs, node_timeout_secs',
    [
        # the gap the hold used to need: the stack past its timeout, its node 12 s younger
        # and not yet past the node-down one
        (1805, 1793, 1800),
        # the usual sweep: both are past. Job 433's first two stacks, 1814 s and 1845 s
        (1826, 1814, 1800),
        (1857, 1845, 1800),
        # the node-down rule alone is past: a shorter node timeout than stack timeout
        (1700, 1688, 1500),
    ],
)
def test_one_sweep_holds_a_job_whose_nodes_never_joined_whichever_rule_sees_it_first(
    context, job_cache, monkeypatch, stack_age_secs, node_age_secs, node_timeout_secs
):
    """
    The node-down rule and the stack timeout both run for 30 minutes, from the instance's
    launch and from the stack's creation 12 to 14 s before it. Unless a sweep landed in
    that gap, the node-down rule deleted the stack first and the job's own sweep then found
    it gone: reset, provisioned again, no hold, no count, a new stack every 32 minutes with
    nothing shown to the owner. Whichever rule is due first, the job's sweep deletes the
    stack, holds the job and counts the attempt, once.
    """
    job, aws, scheduler = sweep_world(
        context, monkeypatch, job_cache, stack_age_secs, node_age_secs
    )
    context.config().put(
        'scheduler.job_provisioning.node_unavailable_timeout_seconds', node_timeout_secs
    )

    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert scheduler.held_jobs == [job.job_id]
    assert scheduler.reset_jobs == [job.job_id]
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1
    held = job_cache.get_job(job.job_id)
    assert held.state == SocaJobState.HELD
    assert held.reason_class == 'stack_timeout'
    assert 'nodes launched but the job did not start in' in held.error_message
    # the stack's own delete takes the instance with it; the node-down rule did not act
    assert aws.terminated == []

    # the sweeps that follow, while the stack goes and after it has gone: the held job is
    # not provisioned again and nothing is counted twice
    sweep(context)
    aws.stack_is_gone()
    sweep(context)
    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert scheduler.held_jobs == [job.job_id]
    assert scheduler.reset_jobs == [job.job_id]
    assert context.job_monitor.modified_jobs == [job.job_id]
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1
    # the node the dead instance registered is cleared away
    assert scheduler.deleted_nodes == ['10.0.0.1']


@pytest.mark.parametrize(
    'stack_age_secs, node_age_secs', [(1805, 1793), (1826, 1814), (1857, 1845)]
)
def test_the_hold_does_not_depend_on_which_half_of_the_sweep_runs_first(
    context, job_cache, monkeypatch, stack_age_secs, node_age_secs
):
    """the same sweep with the job's half before the nodes' half: the other order"""
    job, aws, scheduler = sweep_world(
        context, monkeypatch, job_cache, stack_age_secs, node_age_secs
    )

    session = housekeeping_session(context)
    session.retry_provisioning_cleanup()
    session.pass1_identify_potential_candidates_for_deletion()
    session.pass2_compute_present_capacities()
    session.pass4_set_offline()
    session.pass5_compute_desired_capacities()
    session.pass6_finalize_resources_to_delete()
    session.cleanup()
    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert aws.terminated == []
    assert scheduler.held_jobs == [job.job_id]
    assert scheduler.reset_jobs == [job.job_id]
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1
    assert job_cache.get_job(job.job_id).reason_class == 'stack_timeout'


def test_one_sweep_leaves_a_job_whose_nodes_are_still_coming_up(
    context, job_cache, monkeypatch
):
    job, aws, scheduler = sweep_world(context, monkeypatch, job_cache, 612, 600)

    sweep(context)

    assert aws.deleted_stacks == []
    assert aws.terminated == []
    assert scheduler.held_jobs == []
    assert scheduler.reset_jobs == []
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0


def test_one_sweep_holds_a_job_as_too_small_before_either_timeout(
    context, job_cache, monkeypatch
):
    job, aws, scheduler = sweep_world(
        context,
        monkeypatch,
        job_cache,
        132,
        120,
        bootstrap_status='insufficient-memory',
    )

    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert scheduler.held_jobs == [job.job_id]
    assert 'instance type t3.large is too small' in (
        job_cache.get_job(job.job_id).error_message
    )


def test_one_sweep_past_both_timeouts_leaves_a_begun_array_alone(
    context, job_cache, monkeypatch
):
    """its row reads queued, as after a lost run event; PBS says a subjob has begun"""
    array = mock_job(provisioned=True)
    array.job_id = '357[]'
    job, aws, scheduler = sweep_world(
        context, monkeypatch, job_cache, 1826, 1814, job=array
    )
    scheduler.live_jobs[job.job_id] = job.model_copy(
        update={'state': SocaJobState.SUBJOB_BEGUN}
    )

    sweep(context)

    assert aws.deleted_stacks == []
    assert aws.terminated == []
    assert scheduler.held_jobs == []
    assert scheduler.reset_jobs == []
    assert job_cache.get_job(job.job_id).state == SocaJobState.SUBJOB_BEGUN


@pytest.mark.parametrize('job_is', ['deleted', 'finished'])
def test_one_sweep_takes_the_stack_of_a_job_that_is_gone(
    context, job_cache, monkeypatch, job_is
):
    """
    433 and 456 were deleted while queued, and their stacks and nodes stayed up for 26 and
    29 minutes more: the node-down rule looked at how long the node had been down before
    it looked at whether its job still existed.
    """
    job, aws, scheduler = sweep_world(context, monkeypatch, job_cache, 312, 300)
    if job_is == 'deleted':
        job_cache.delete_jobs(job_ids=[job.job_id])
    else:
        job_cache.sync(jobs=[job.model_copy(update={'state': SocaJobState.FINISHED})])

    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert aws.terminated == ['i-0123456789abcdef0']
    assert scheduler.held_jobs == []
    assert scheduler.reset_jobs == []


def test_one_sweep_reclaims_a_down_node_under_a_held_job_and_counts_it_on_release(
    context, job_cache, monkeypatch
):
    """
    An owner's qhold after the job was provisioned: the node-down rule still reclaims the
    dead node, as before. The job comes back from qrls on a stack that is gone, and that
    is counted as the attempt it was.
    """
    job, aws, scheduler = sweep_world(context, monkeypatch, job_cache, 1826, 1814)
    held = job.model_copy(update={'state': SocaJobState.HELD})
    job_cache.sync(jobs=[held])
    scheduler.live_jobs[job.job_id] = held

    sweep(context)

    assert aws.deleted_stacks == [job.get_compute_stack()]
    assert aws.terminated == ['i-0123456789abcdef0']
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0

    # qrls: the first sweep after it reads the released job, the next acts on it
    aws.stack_is_gone()
    scheduler.live_jobs[job.job_id] = job
    sweep(context)
    sweep(context)

    assert scheduler.reset_jobs == [job.job_id]
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 1
    assert scheduler.held_jobs == []


def test_a_held_job_deleted_from_pbs_is_not_a_failed_sweep(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, caplog
):
    """
    434 was deleted while held, and the next sweep logged "[ERROR] provisioning retry
    clean-up failed ... [SCHEDULER_JOB_FINISHED]" for it. PBS answers a held job that is
    gone with that error (qstat exit 35) or with nothing.
    """
    from ideadatamodel import exceptions

    finished = mock_job(provisioned=False)
    finished.state = SocaJobState.HELD
    unknown = mock_job(provisioned=False)
    unknown.state = SocaJobState.HELD
    job_cache.sync(jobs=[finished, unknown])

    def get_job(job_id: str):
        if job_id == finished.job_id:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_JOB_FINISHED,
                message='qstat: Job has finished, use -x or -H',
            )
        return None

    mock_scheduler.get_job = get_job

    with caplog.at_level(logging.DEBUG, logger=LOG_TAG):
        housekeeping_session(context).retry_provisioning_cleanup()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert mock_job_monitor.modified_jobs == []
    assert caplog.text.count('held job is gone from the scheduler') == 2


def test_a_released_job_no_longer_carries_the_reason_it_was_held_for(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    456 after qrls kept reason class stack_timeout and the hold's text, and when its owner
    then deleted it the record read deleted / stack_timeout with no "Cancelled by the
    owner.": only the retry count was cleared on release.
    """
    created_stack(monkeypatch, minutes_ago=31)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job
    housekeeping_session(context).retry_provisioning_cleanup()
    job_cache.sync(jobs=[job.model_copy(update={'state': SocaJobState.HELD})])
    assert job_cache.get_job(job.job_id).reason_class == 'stack_timeout'
    assert mock_scheduler.job_attributes[job.job_id]['error_message'].startswith(
        'JOB_HELD_STACK_TIMEOUT'
    )

    # qrls: PBS lists it queued again
    housekeeping_session(context).retry_provisioning_cleanup()

    released = job_cache.get_job(job.job_id)
    assert released.reason_class is None
    assert released.error_message is None
    assert mock_scheduler.job_attributes[job.job_id] == {'error_message': None}
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0
    assert mock_job_monitor.modified_jobs[-1] == job.job_id


def read_from_pbs(context, job: SocaJob, state: str, **resources) -> SocaJob:
    """the job as a read of PBS returns it: through the same conversion qstat's output takes"""
    from ideadatamodel import HpcQueueProfile
    from ideascheduler.app.scheduler.openpbs import OpenPBSJob

    comment = resources.pop('comment', None)
    stack = resources.pop('compute_node', 'tbd')
    return OpenPBSJob(
        id=f'{job.job_id}.scheduler',
        Job_Owner=f'{job.owner}@scheduler',
        queue=job.queue,
        job_state=state,
        qtime='1785542400',
        comment=comment,
        Resource_List={
            'select': f'1:ncpus=1:compute_node={stack}',
            'nodect': 1,
            'ncpus': 1,
            'instance_type': 't3.micro',
            'job_uid': job.job_uid,
            'stack_id': 'tbd',
            **resources,
        },
    ).as_soca_job(context=context, queue_profile=HpcQueueProfile(name='compute'))


def test_a_held_row_counts_the_attempt_it_made_and_says_why_in_plain_words(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    434 and 456, held after their first attempt, read "attempt 2 of 3" (the portal's "held
    after 2 of 3 attempts"), and their comment was the text PBS holds for qstat:
    "JOB_HELD_STACK_TIMEOUT:_Job_held:_nodes_launched_but_...", while the reason beside it
    was the sentence.
    """
    from ideascheduler.app.api.job_waiting_signals import apply_waiting_signals

    created_stack(monkeypatch, minutes_ago=31)
    job = mock_job(provisioned=True)
    job_cache.sync(jobs=[job])
    mock_scheduler.live_jobs[job.job_id] = job
    housekeeping_session(context).retry_provisioning_cleanup()
    assert mock_scheduler.held_jobs == [job.job_id]

    # the reason as PBS stores it: what the scheduler's own qalter sends for the value
    # the hold published
    from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

    qalter = Mock(return_value=Mock(returncode=0))
    pbs = OpenPBSScheduler(context=context)
    pbs._shell = Mock(invoke=qalter)
    pbs.set_job_attributes(job.job_id, mock_scheduler.job_attributes[job.job_id])
    stored = qalter.call_args.args[0][2].removeprefix('error_message=')
    assert stored == (
        f'JOB_HELD_STACK_TIMEOUT:_Job_held:_nodes_launched_but_the_job_did_not_start_'
        f'in_30_min._Check_instance_ami._qrls_{job.job_id}_to_retry.'
    )

    # the job monitor's read of the held job
    job_cache.sync(
        jobs=[
            read_from_pbs(
                context,
                job,
                'H',
                error_message=stored,
                comment=mock_scheduler.comments[job.job_id],
            )
        ]
    )

    rows = job_cache.list_jobs(job_id=job.job_id)
    apply_waiting_signals(context, rows)
    (row,) = rows

    assert row.state == SocaJobState.HELD
    sentence = (
        f'Job held: nodes launched but the job did not start in 30 min. '
        f'Check instance_ami. qrls {job.job_id} to retry.'
    )
    assert row.status_reason == sentence
    assert row.comment == sentence
    assert (row.provisioning_attempt, row.max_provisioning_attempts) == (1, 3)


def test_a_job_held_by_its_owner_before_any_attempt_reports_none(
    context, job_cache, mock_scheduler
):
    from ideascheduler.app.api.job_waiting_signals import apply_waiting_signals

    job = mock_job(provisioned=False)
    job.state = SocaJobState.HELD
    queued = mock_job(provisioned=False)
    job_cache.sync(jobs=[job, queued])

    rows = [job_cache.get_job(job.job_id), job_cache.get_job(queued.job_id)]
    apply_waiting_signals(context, rows)

    assert rows[0].provisioning_attempt is None
    # a job that waits is on its first
    assert rows[1].provisioning_attempt == 1


def test_provisioning_a_job_again_does_not_write_its_old_reason_back(
    context, job_cache, mock_scheduler, mock_job_monitor, deleted_stacks, monkeypatch
):
    """
    456 after qrls and a new stack: its row still read reason class stack_timeout and the
    hold's text as its comment. The provisioner takes the job from the cache with its last
    error on it, and wrote all of it back with the row as it cleared the error.
    """
    job = mock_job(provisioned=False)
    job_cache.sync(
        jobs=[
            read_from_pbs(
                context,
                job,
                'Q',
                error_message='JOB_HELD_STACK_TIMEOUT:_Job_held:_nodes_launched',
            )
        ]
    )
    job_cache.set_job_provisioning_error(
        job_id=job.job_id,
        error_code=errorcodes.JOB_HELD_STACK_TIMEOUT,
        message='Job held: nodes launched',
    )
    # as the provisioning queue hands it over
    queued = job_cache.get_job(job.job_id)
    assert queued.reason_class == 'stack_timeout'
    mock_scheduler.provision_job = lambda job, stack_id: Utils.current_time_ms()
    context.metrics = Mock()

    make_provision_jobs(context, queued, 'CREATE_COMPLETE').provision_job_in_scheduler(
        job=queued, stack_id=stack_id(queued.get_compute_stack())
    )

    row = job_cache.get_job(job.job_id)
    assert row.is_provisioned()
    assert row.reason_class is None
    assert row.error_message is None
    assert row.comment is None
