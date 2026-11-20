"""Job status events are accepted only from the scheduler or a compute node of the job."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest

from ideadatamodel import constants
from ideadatamodel.model_utils import ModelUtils
from ideasdk.utils import Utils
from ideascheduler.app.provisioning.job_monitor.job_monitor import JobMonitor
from test_job_duration import QUEUE_PROFILE


def node(
    context,
    job_id='12',
    job_group='g1',
    node_type=constants.NODE_TYPE_COMPUTE,
    instance_id='i-node',
    ephemeral=False,
    queue='normal',
):
    return SimpleNamespace(
        soca_job_queue=queue,
        instance_id=instance_id,
        soca_cluster_name=context.cluster_name(),
        soca_node_type=node_type,
        soca_job_id=job_id,
        soca_job_group=job_group,
        is_soca_ephemeral_capacity=ephemeral,
    )


def cached_job(job_group='g1', shared=True, queue='normal'):
    return SimpleNamespace(
        queue=queue,
        params=SimpleNamespace(job_group=job_group),
        is_shared_capacity=lambda: shared,
    )


def deliver(context, monkeypatch, sender, job_id='12', instance_id='i-node'):
    """one execjob_begin through the real queue consumer; returns the job status updates"""
    event = {
        'type': 'execjob_begin',
        'hook_name': 'job_status',
        'timestamp': arrow.utcnow().int_timestamp * 1000,
        'instance_id': instance_id,
        'node_name': 'compute',
        'requestor_host': 'compute',
        # the shape the execution hook serializes: PBS attribute names, plain values
        'job': {
            'id': f'{job_id}.scheduler',
            'Job_Owner': 'mockuser@ip-10-0-0-9',
            'queue': 'normal',
            'job_state': 'R',
            'exec_host': 'compute/0',
            'run_count': 1,
            'Resource_List': {
                'select': '1:ncpus=1',
                'nodect': 1,
                'ncpus': 1,
                'instance_type': 'c5.large',
            },
        },
    }
    body = Utils.base64_encode(json.dumps({'payload': {'event': event}}))
    monitor = JobMonitor(context)
    monitor._exit = Mock()
    monitor._exit.is_set.side_effect = [False, True]
    sqs = Mock()
    sqs.receive_message.return_value = {
        'Messages': [
            {
                'MessageId': 'm',
                'ReceiptHandle': 'r',
                'Body': body,
                'Attributes': {'SenderId': sender},
            }
        ]
    }
    monkeypatch.setattr(context, 'aws', lambda: SimpleNamespace(sqs=lambda: sqs))
    context.queue_profiles = Mock()
    context.queue_profiles.get_queue_profile.return_value = QUEUE_PROFILE
    updates = []
    monkeypatch.setattr(monitor, 'job_status_update', updates.append)
    monitor._monitor_job_execution()
    assert sqs.receive_message.call_args.kwargs['AttributeNames'] == ['SenderId']
    # a refused event is dropped, not retried
    sqs.delete_message_batch.assert_called_once()
    return [(job.job_id, job.execution_hosts[0].instance_id) for job in updates]


@pytest.fixture
def cluster(context, monkeypatch):
    context.config().put('scheduler.task_role_id', 'AROASCHEDULER')
    context.config().put('scheduler.job_status_sqs_queue_url', 'unused')
    context.instance_cache = Mock()
    context.instance_cache.get_instance.return_value = node(context)
    context.describe = Mock(return_value=[])
    monkeypatch.setattr(context.aws_util(), 'ec2_describe_instances', context.describe)
    context.job_cache = Mock()
    context.job_cache.get_job.return_value = cached_job()
    return context


def test_compute_node_of_the_job_is_accepted(cluster, monkeypatch):
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == [('12', 'i-node')]


def test_scheduler_task_is_accepted(cluster, monkeypatch):
    cluster.instance_cache.get_instance.return_value = None
    assert deliver(cluster, monkeypatch, 'AROASCHEDULER:0123abcd') == [('12', 'i-node')]


def test_shared_node_of_the_job_group_is_accepted(cluster, monkeypatch):
    cluster.instance_cache.get_instance.return_value = node(cluster, job_id='9')
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == [('12', 'i-node')]


@pytest.mark.parametrize(
    'node_ephemeral,job_shared',
    [
        # another job's ephemeral node with the same computed group
        (True, False),
        # a shared node naming an ephemeral job of its group
        (False, False),
        # an ephemeral node naming a shared job of its group
        (True, True),
    ],
)
def test_a_job_group_match_counts_only_between_shared_capacity(
    cluster, monkeypatch, node_ephemeral, job_shared
):
    cluster.instance_cache.get_instance.return_value = node(
        cluster, job_id='9', ephemeral=node_ephemeral
    )
    cluster.job_cache.get_job.return_value = cached_job(shared=job_shared)
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == []


def test_new_node_missing_from_the_cache_is_described(cluster, monkeypatch):
    cluster.instance_cache.get_instance.return_value = None
    cluster.describe.return_value = [node(cluster)]
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == [('12', 'i-node')]


@pytest.mark.parametrize(
    'sender,instance,event_instance',
    [
        # another job's node
        ('AROANODE:i-node', {'job_id': '9', 'job_group': 'g2'}, 'i-node'),
        # a desktop or other host of the cluster
        ('AROANODE:i-node', {'node_type': constants.NODE_TYPE_DCV_HOST}, 'i-node'),
        # an event naming an instance other than its sender
        ('AROANODE:i-node', {}, 'i-other'),
        # a sender that is not an instance
        ('AIDAUSER', {}, 'i-node'),
        ('AROAOTHER:session', {}, 'i-node'),
    ],
)
def test_other_senders_are_refused(
    cluster, monkeypatch, sender, instance, event_instance
):
    cluster.instance_cache.get_instance.return_value = node(cluster, **instance)
    assert deliver(cluster, monkeypatch, sender, instance_id=event_instance) == []


def test_array_subjob_from_a_node_tagged_for_its_array_is_accepted(
    cluster, monkeypatch
):
    # the idea:JobId tag of an array's nodes is 123-array; a subjob reports as 123[1]
    cluster.instance_cache.get_instance.return_value = node(
        cluster, job_id=ModelUtils.pbs_job_id(ModelUtils.aws_job_id('123[]'))
    )
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node', job_id='123[1]') == [
        ('123[1]', 'i-node')
    ]


def test_every_node_of_a_multi_node_job_is_accepted(cluster, monkeypatch):
    cluster.instance_cache.get_instance.side_effect = lambda instance_id: node(
        cluster, instance_id=instance_id
    )
    for instance in ('i-first', 'i-second'):
        assert deliver(
            cluster, monkeypatch, f'AROANODE:{instance}', instance_id=instance
        ) == [('12', instance)]


def test_later_job_on_a_kept_node_of_its_group_is_accepted(cluster, monkeypatch):
    # provisioned for job 9 on a keep-forever queue; job 12 of the same group runs there
    cluster.instance_cache.get_instance.return_value = node(cluster, job_id='9')
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == [('12', 'i-node')]
    cluster.job_cache.get_job.return_value = SimpleNamespace(
        params=SimpleNamespace(job_group='g2')
    )
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == []


def test_a_shared_node_of_another_queue_with_the_same_group_is_refused(
    cluster, monkeypatch
):
    # SCH-33: two shared profiles reusing one group name accepted each other's nodes
    cluster.instance_cache.get_instance.return_value = node(
        cluster, job_id='9', queue='other'
    )
    assert deliver(cluster, monkeypatch, 'AROANODE:i-node') == []
