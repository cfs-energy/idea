"""
Test Cases for how a batch's target capacity and admission are computed

BatchCapacityHelper runs for real against a real job cache; the instance cache, the
scheduler's nodes and the queue profile are doubles.
"""

import logging
from types import SimpleNamespace

import pytest

from ideadatamodel import SocaJob, SocaJobParams, SocaJobState
from ideadatamodel.scheduler import HpcQueueProfile, SocaQueueManagementParams
from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache
from ideascheduler.app.provisioning.job_provisioner.batch_capacity_helper import (
    BatchCapacityHelper,
)
from test_job_cache import FakeSchedulerContext


def job(job_id, provisioned=False, queue_type='batch', state=SocaJobState.QUEUED):
    return SocaJob(
        job_id=job_id,
        job_uid=f'uid-{job_id}',
        job_group='custom-g',
        cluster_name='idea-mock',
        owner='alice',
        queue='work',
        queue_type=queue_type,
        state=state,
        provisioned=provisioned,
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            job_group='custom-g',
            instance_types=['c5.large'],
        ),
    )


@pytest.fixture()
def world(tmp_path):
    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    limits = SocaQueueManagementParams()
    instances = {'count': 0}
    context = SimpleNamespace(
        job_cache=cache,
        logger=lambda name: logging.getLogger(name),
        cluster_name=lambda: 'idea-mock',
        instance_cache=SimpleNamespace(
            list_compute_instances=lambda **_: [],
            get_queue_profile_instance_count=lambda _profile: instances['count'],
        ),
        scheduler=SimpleNamespace(find_node=lambda hosts: None),
        queue_profiles=SimpleNamespace(
            get_queue_profile=lambda queue_profile_name: HpcQueueProfile(
                name=queue_profile_name, queue_management_params=limits
            )
        ),
    )
    yield SimpleNamespace(
        context=context, cache=cache, limits=limits, instances=instances
    )
    cache.stop()


def run(world, jobs, provisioned_capacity=0):
    return BatchCapacityHelper(
        context=world.context, jobs=jobs, provisioned_capacity=provisioned_capacity
    ).invoke()


def test_a_held_back_job_and_another_profiles_job_are_not_bought_nodes(world):
    # SCH-23: the group's desired capacity summed every cached job with that group name
    admitted, held = job('1'), job('2')
    elsewhere = job('3', queue_type='other-profile')
    world.cache.sync([admitted, held, elsewhere])
    result = run(world, [admitted])
    assert result.capacity_info.target_capacity == 1


def test_capacity_already_given_to_running_jobs_still_counts(world):
    running = job('4', provisioned=True, state=SocaJobState.RUNNING)
    new = job('5')
    world.cache.sync([running, new])
    assert run(world, [new]).capacity_info.target_capacity == 2


def test_a_batch_stops_at_max_running_jobs(world):
    # SCH-24: each job passed against the same pre-batch count
    world.limits.max_running_jobs = 1
    first, second = job('6'), job('7')
    world.cache.sync([first, second])
    result = run(world, [first, second])
    assert [j.job_id for j in result.provisioned_jobs] == ['6']
    assert [j.job_id for j in result.unprovisioned_jobs] == ['7']
    assert result.capacity_info.target_capacity == 1


def test_a_batch_stops_at_max_provisioned_instances(world):
    world.limits.max_provisioned_instances = 2
    world.instances['count'] = 1
    first, second = job('8'), job('9')
    world.cache.sync([first, second])
    result = run(world, [first, second])
    assert [j.job_id for j in result.provisioned_jobs] == ['8']
    assert [j.job_id for j in result.unprovisioned_jobs] == ['9']


def test_a_batch_past_the_limit_admits_nothing(world):
    world.limits.max_running_jobs = 1
    world.cache.sync(
        [job('10', provisioned=True, state=SocaJobState.RUNNING), job('11')]
    )
    result = run(world, [job('11')])
    assert result.provisioned_jobs == []
    assert [j.job_id for j in result.unprovisioned_jobs] == ['11']
