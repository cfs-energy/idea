"""Scheduler cleanup sends the node name independently of its own ECS identity."""

import json
import io
from ideasdk.aws import ecs_task
from unittest.mock import Mock

import pytest

from ideascheduler.app.provisioning.node_monitor import node_house_keeper
from test_node_identity import build_instance


@pytest.mark.parametrize(
    'task_arn', [None, 'arn:aws:ecs:us-east-2:1:task/cluster/task']
)
def test_scheduler_delete_payload(monkeypatch, task_arn):
    context = Mock()
    values = {
        'directoryservice.provider': 'activedirectory',
        'directoryservice.ad_automation.sqs_queue_url': 'queue',
    }
    context.config().get_string.side_effect = lambda key, **_: values[key]
    session = node_house_keeper.NodeHouseKeepingSession(context, Mock())
    instance = build_instance()
    session.instances_to_terminate = {instance}
    monkeypatch.setattr(node_house_keeper, 'current_task_arn', lambda: task_arn)
    session.cleanup()
    request = context.aws().sqs().send_message.call_args.kwargs
    payload = json.loads(request['MessageBody'])['payload']
    expected = {'computer_name': 'ip-10-0-79-226', 'instance_id': instance.instance_id}
    if task_arn:
        expected['task_arn'] = task_arn
    assert payload == expected


@pytest.mark.parametrize('recovers', [False, True])
@pytest.mark.parametrize('failure', ['unavailable', 'missing-arn'])
def test_metadata_retries_then_publishes_even_on_failure(
    monkeypatch, recovers, failure
):
    monkeypatch.setattr(ecs_task, '_task_arn', None)
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', 'http://metadata')
    # sleep is patched by name so this same regression runs against the old module.
    import time

    sleep = Mock()
    monkeypatch.setattr(time, 'sleep', sleep)
    error = (
        OSError('metadata unavailable')
        if failure == 'unavailable'
        else io.BytesIO(b'{}')
    )
    last = (
        io.BytesIO(b'{"TaskARN":"arn:task/recovered"}')
        if recovers
        else (
            OSError('metadata unavailable')
            if failure == 'unavailable'
            else io.BytesIO(b'{}')
        )
    )
    second = (
        OSError('metadata unavailable')
        if failure == 'unavailable'
        else io.BytesIO(b'{}')
    )
    opener = Mock()
    opener.open.side_effect = [error, second, last]
    monkeypatch.setattr(ecs_task.urllib.request, 'build_opener', lambda *_: opener)
    context = Mock()
    values = {
        'directoryservice.provider': 'activedirectory',
        'directoryservice.ad_automation.sqs_queue_url': 'queue',
    }
    context.config().get_string.side_effect = lambda key, **_: values[key]
    session = node_house_keeper.NodeHouseKeepingSession(context, Mock())
    session.instances_to_terminate = {build_instance()}
    session.cleanup()
    request = context.aws().sqs().send_message.call_args.kwargs
    payload = json.loads(request['MessageBody'])['payload']
    assert opener.open.call_count == 3
    assert sleep.call_count == 2
    assert all(0 < call.args[0] <= 1 for call in sleep.call_args_list)
    assert payload['computer_name']
    assert payload['instance_id']
    if recovers:
        assert payload['task_arn'] == 'arn:task/recovered'
    else:
        assert 'task_arn' not in payload
