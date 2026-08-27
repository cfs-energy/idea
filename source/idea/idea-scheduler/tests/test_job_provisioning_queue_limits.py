"""
Test Cases for JobProvisioningQueue limit handling

a job that trips a queue limit must stay in the provisioning queue. dropping it
strands the job in the scheduler with stack_id=tbd until something re-submits it.
"""

from ideasdk.config.soca_config import SocaConfig
import logging
from datetime import datetime, timezone

import pytest

from ideadatamodel import (
    SocaJob,
    SocaJobParams,
    SocaJobState,
    SocaQueueMode,
    SocaScalingMode,
    SocaQueueManagementParams,
    HpcQueueProfile,
)

from ideascheduler.app.provisioning.job_provisioning_queue.job_provisioning_queue import (
    JobProvisioningQueue,
    JobProvisioningQueueEmpty,
    LIMIT_TYPE_MAX_PROVISIONED_INSTANCES,
    LIMIT_TYPE_MAX_RUNNING_JOBS,
)


class FakeJobCache:
    def __init__(self):
        self.jobs = {}
        self.active_jobs = 0
        self.provisioning_errors = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_active_jobs(self, queue_type):
        return self.active_jobs

    def set_job_provisioning_error(self, job_id, error_code, message):
        self.provisioning_errors.append((job_id, error_code, message))


class FakeInstanceCache:
    def __init__(self):
        self.count = 0

    def get_queue_profile_instance_count(self, queue_type):
        return self.count


class FakeMetrics:
    def __init__(self):
        self.pending = []

    def jobs_pending(self, queue_type, pending):
        self.pending.append((queue_type, pending))


class FakeLifecycleEvents:
    def __init__(self):
        self.held_at_cap_events = []

    def held_at_cap(self, **kwargs):
        self.held_at_cap_events.append(kwargs)

    def attempts_consumed(self, job) -> None:
        return None

    def job_disposition(self, job, disposition=None, attempt_number=None):
        return None


class FakeContext:
    def __init__(self):
        self.job_cache = FakeJobCache()
        self.instance_cache = FakeInstanceCache()
        self.metrics = FakeMetrics()
        self.lifecycle_events = FakeLifecycleEvents()
        self._config = SocaConfig({})

    def logger(self, name=None):
        return logging.getLogger(name if name is not None else 'test')

    def config(self):
        return self._config

    @staticmethod
    def is_ready():
        return True


def make_queue_profile(**management_params) -> HpcQueueProfile:
    return HpcQueueProfile(
        queue_profile_id='qp-1',
        name='compute',
        queues=['normal'],
        enabled=True,
        queue_mode=SocaQueueMode.FIFO,
        scaling_mode=SocaScalingMode.SINGLE_JOB,
        queue_management_params=SocaQueueManagementParams(**management_params),
    )


def make_job(job_id='1', nodes=1) -> SocaJob:
    return SocaJob(
        job_id=job_id,
        job_uid=f'mock-job-{job_id}',
        owner='mockuser',
        cluster_name='idea-mock',
        queue_type='compute',
        queue='normal',
        state=SocaJobState.QUEUED,
        queue_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        scaling_mode=SocaScalingMode.SINGLE_JOB,
        params=SocaJobParams(
            nodes=nodes,
            cpus=2,
            instance_types=['c5.large'],
            instance_ami='ami-0123456789abcdef0',
            base_os='amazonlinux2',
            enable_ht_support=False,
        ),
    )


def build(**management_params):
    context = FakeContext()
    queue_profile = make_queue_profile(**management_params)
    provisioning_queue = JobProvisioningQueue(
        context=context, queue_profile=queue_profile
    )
    return context, provisioning_queue


def test_job_tripping_instances_limit_stays_queued():
    """
    a job that exceeds max_provisioned_instances must remain in the provisioning
    queue. if the re-queue inside the limit check is swallowed as 'already queued',
    the popped job is dropped for good.
    """
    context, provisioning_queue = build(max_provisioned_instances=1)
    job = make_job(nodes=4)
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    assert provisioning_queue.get_queue_size() == 1

    with pytest.raises(JobProvisioningQueueEmpty):
        provisioning_queue.get(timeout=0)

    assert provisioning_queue.is_queue_blocked_by_limits() is True
    assert provisioning_queue.get_limit_info().limit_type == (
        LIMIT_TYPE_MAX_PROVISIONED_INSTANCES
    )
    # the job must still be pending, not silently discarded
    assert provisioning_queue.get_queue_size() == 1


def test_job_is_provisioned_after_limit_clears():
    """
    once the limit clears the retained job must be handed to the provisioner
    without any new submission.
    """
    context, provisioning_queue = build(max_provisioned_instances=1)
    job = make_job(nodes=4)
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    with pytest.raises(JobProvisioningQueueEmpty):
        provisioning_queue.get(timeout=0)

    # operator raises the cap
    provisioning_queue.queue_profile.queue_management_params.max_provisioned_instances = 10

    assert provisioning_queue.get(timeout=0).job_id == job.job_id


def test_running_jobs_limit_is_reported_as_a_queue_limit():
    """
    max_running_jobs is a queue level limit. reported with the
    max_provisioned_instances threshold, is_queue_limit() reads None and the
    breach is routed to the group limit branch: the queue never shows as
    blocked and no limit info reaches the admin api.
    """
    context, provisioning_queue = build(max_running_jobs=1)
    context.job_cache.active_jobs = 1
    job = make_job()
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    with pytest.raises(JobProvisioningQueueEmpty):
        provisioning_queue.get(timeout=0)

    limit_info = provisioning_queue.get_limit_info()
    assert provisioning_queue.is_queue_blocked_by_limits() is True
    assert limit_info.limit_type == LIMIT_TYPE_MAX_RUNNING_JOBS
    assert limit_info.queue_threshold == 1
    assert limit_info.is_queue_limit() is True
    assert provisioning_queue.get_queue_size() == 1


def test_running_jobs_limit_emits_held_at_cap_event():
    """
    the held_at_cap lifecycle event is the signal that distinguishes a capped
    queue from a lost job. it must fire for max_running_jobs too.
    """
    context, provisioning_queue = build(max_running_jobs=1)
    context.job_cache.active_jobs = 1
    job = make_job()
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    with pytest.raises(JobProvisioningQueueEmpty):
        provisioning_queue.get(timeout=0)

    assert len(context.lifecycle_events.held_at_cap_events) == 1
    event = context.lifecycle_events.held_at_cap_events[0]
    assert event['limit_type'] == LIMIT_TYPE_MAX_RUNNING_JOBS
    assert event['pending_jobs'] == 1


def test_successful_get_releases_dedupe_entry():
    """
    a job handed to the provisioner must be re-queueable: the provisioner puts it
    back when provisioning fails.
    """
    context, provisioning_queue = build()
    job = make_job()
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    assert provisioning_queue.get(timeout=0).job_id == job.job_id
    assert provisioning_queue.get_queue_size() == 0

    provisioning_queue.put(job=job)
    assert provisioning_queue.get_queue_size() == 1


def test_get_skips_job_that_left_queued_state():
    """
    a job that is no longer QUEUED is dropped from the provisioning queue, and
    must not be resurrected by the retained-on-limit path.
    """
    context, provisioning_queue = build()
    job = make_job()
    context.job_cache.jobs[job.job_id] = job

    provisioning_queue.put(job=job)
    job.state = SocaJobState.RUNNING

    with pytest.raises(JobProvisioningQueueEmpty):
        provisioning_queue.get(timeout=0)
    assert provisioning_queue.get_queue_size() == 0
