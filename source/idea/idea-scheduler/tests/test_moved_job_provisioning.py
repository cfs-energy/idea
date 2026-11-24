# ruff: noqa: F811 (the submit fixture is imported)
"""
A moved job is provisioned once, by its new queue's profile (review: opepbs_api.py:286).

on a qmove the job cache kept the job's source queue and queue type, so the reconcile that
followed put it back into the SOURCE profile's provisioning queue: the source's limits and
scaling mode applied, and once it was read under its new queue the destination's could take
it too. the request is the move of job 545 captured on the dev cluster (movejob_unbound).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    HpcQueueProfile,
    SocaJob,
    SocaJobParams,
    SocaJobState,
    SocaQueueManagementParams,
    SocaQueueMode,
    SocaScalingMode,
)
from ideascheduler.app.provisioning.job_monitor import job_monitor as job_monitor_module
from ideascheduler.app.provisioning.job_monitor.job_monitor import JobMonitor
from ideascheduler.app.provisioning.job_provisioning_queue.job_provisioning_queue import (
    JobProvisioningQueue,
    JobProvisioningQueueEmpty,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from ideasdk.config.soca_config import SocaConfig
from test_job_monitor import FakeQSelect, FakeScheduler
from test_validate_job_ncpus import submit  # noqa: F401 (fixture)

FIXTURE = (
    Path(__file__).parent / 'fixtures' / 'pbs_hook_requests' / 'movejob_unbound.json'
)


def moved_event():
    return json.loads(FIXTURE.read_text())['payload']['event']


def profile(name, queues, **limits):
    return HpcQueueProfile(
        queue_profile_id=f'qp-{name}',
        name=name,
        queues=queues,
        enabled=True,
        queue_mode=SocaQueueMode.FIFO,
        scaling_mode=SocaScalingMode.SINGLE_JOB,
        queue_management_params=SocaQueueManagementParams(**limits),
    )


class Cache:
    """the job cache, as both the monitor and the provisioning queues read it"""

    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def sync(self, jobs):
        for job in jobs:
            self.jobs[job.job_id] = job

    def get_job_provisioning_retry_count(self, job_id):
        return 0

    def get_active_jobs(self, queue_type):
        return 0

    def set_job_provisioning_error(self, job_id, error_code, message):
        pass


class Context:
    def __init__(self, profiles):
        self.job_cache = Cache()
        self.instance_cache = Mock(
            get_queue_profile_instance_count=lambda queue_type: 1
        )
        self.metrics = Mock()
        self.lifecycle_events = Mock()
        self.accounts_client = Mock()
        self.accounts_client.get_user.return_value.user.enabled = True
        self.scheduler = FakeScheduler()
        self._config = SocaConfig({'scheduler': {'job_status_sqs_queue_url': 'unused'}})
        self.queues = {
            p.name: JobProvisioningQueue(context=self, queue_profile=p)
            for p in profiles
        }
        self.queue_profiles = Mock(
            get_provisioning_queue=lambda queue_profile_name: self.queues.get(
                queue_profile_name
            ),
            list_queue_profiles=lambda: profiles,
        )

    def logger(self, name=None):
        return logging.getLogger(name or 'test')

    def config(self):
        return self._config

    def service_registry(self):
        return Mock()

    @staticmethod
    def is_ready():
        return True


def cached_in_source(job_id='545', state=SocaJobState.QUEUED):
    return SocaJob(
        cluster_name='idea-test',
        job_id=job_id,
        job_uid='uidmoved',
        job_group='gab40a040',
        queue='test',
        queue_type='test',
        owner='researcher2',
        state=state,
        provisioned=False,
        queue_time=datetime(2026, 11, 20, tzinfo=timezone.utc),
        scaling_mode=SocaScalingMode.SINGLE_JOB,
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            instance_types=['c5.large'],
            instance_ami='ami-0123456789abcdef0',
            base_os='rocky9',
            enable_ht_support=False,
        ),
    )


def test_an_accepted_move_caches_the_job_under_its_new_queue_and_leaves_the_source(
    submit, context
):
    job_id = moved_event()['job']['id'].split('.')[0]
    source, destination = Mock(), Mock()
    cache = Cache()
    cache.sync([cached_in_source(job_id)])
    context.job_cache = cache
    queues = {'test': source, 'normal': destination}

    def provisioning_queue(queue_profile_name):
        return queues.get(queue_profile_name)

    event = moved_event()
    event['job']['queue'] = 'normal'  # the submit fixture's destination queue profile
    result, _ = submit(
        {},
        event_type='movejob',
        src_queue=event['src_queue'],
        job=OpenPBSJob(**event['job']),
        defaults=SocaJobParams(instance_types=['c5.large']),
        before_response=lambda hook: setattr(
            hook.app_context.queue_profiles,
            'get_provisioning_queue',
            provisioning_queue,
        ),
    )
    assert result.accept is True, result.formatted_user_message
    cached = cache.get_job(job_id)
    assert (cached.queue, cached.queue_type) == ('normal', 'normal')
    source.delete.assert_called_once_with(job_id=job_id)
    context.job_monitor.job_queued.assert_called_once()


@pytest.fixture
def monitored(monkeypatch):
    monkeypatch.setattr(job_monitor_module, 'OpenPBSQSelect', FakeQSelect)
    FakeQSelect.reset()
    # one instance runs in each profile: the destination allows one, the source has no limit
    context = Context(
        [
            profile('test', ['test']),
            profile('job-shared', ['job-shared'], max_provisioned_instances=1),
        ]
    )
    monitor = JobMonitor(context=context)
    monitor._exit = Event()
    return context, monitor


def move(context, job, destination='job-shared'):
    """the API's own step on an accepted move, with the job the hook built for it"""
    from types import SimpleNamespace

    from ideascheduler.app.api.opepbs_api import OpenPBSAPI

    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context
    pbs_context = SimpleNamespace(
        event=SimpleNamespace(
            type='movejob',
            job_o=None,
            job=SimpleNamespace(id=f'{job.job_id}.scheduler'),
        )
    )
    api._cache_moved_job(
        pbs_context,
        job.model_copy(
            update={'job_id': 'tbd', 'queue': destination, 'queue_type': destination}
        ),
    )


def drain(provisioning_queue):
    got = []
    while True:
        try:
            got.append(provisioning_queue.get(timeout=0).job_id)
        except JobProvisioningQueueEmpty:
            return got


def test_a_moved_job_is_queued_once_by_the_destinations_profile_only(monitored):
    context, monitor = monitored
    job = cached_in_source()
    context.job_cache.sync([job])
    context.queues['test'].put(job=job)
    move(context, job)
    FakeQSelect.reset([['545']])
    monitor._reconcile_queue(queue='job-shared', log_tag='queued-jobs')
    FakeQSelect.reset([['545']])
    monitor._reconcile_queue(queue='job-shared', log_tag='queued-jobs')

    assert drain(context.queues['test']) == [], 'the source profile does not take it'
    assert context.queues['job-shared'].get_queue_size() == 1


def test_the_source_profile_skips_a_job_moved_out_whoever_queued_it(monitored):
    # the backstop: an entry left in the source queue is not provisioned there once the
    # cache shows the job's new queue
    context, monitor = monitored
    job = cached_in_source()
    context.job_cache.sync([job])
    context.queues['test'].put(job=job)
    context.job_cache.sync(
        [job.model_copy(update={'queue': 'job-shared', 'queue_type': 'job-shared'})]
    )
    assert drain(context.queues['test']) == []


def test_a_held_job_moved_then_released_is_provisioned_by_the_destination_once(
    monitored,
):
    context, monitor = monitored
    held = cached_in_source(state=SocaJobState.HELD)
    context.job_cache.sync([held])
    move(context, held)
    # released (qrls as root, no hook): the next read shows it queued in job-shared
    released = context.job_cache.get_job('545').model_copy(
        update={'state': SocaJobState.QUEUED}
    )
    context.job_cache.sync([released])
    for _ in range(2):
        FakeQSelect.reset([['545']])
        monitor._reconcile_queue(queue='job-shared', log_tag='queued-jobs')
    assert drain(context.queues['test']) == []
    destination = context.queues['job-shared']
    assert destination.get_queue_size() == 1
    # the destination's own limit applies: its one instance is taken, so it waits
    assert drain(destination) == []
    assert destination.get_limit_info() is not None
