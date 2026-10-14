"""A desktop controller task names itself in the AD delete request it sends."""

import json
import io
from unittest.mock import Mock

import pytest
from ideasdk.aws import ecs_task
from types import SimpleNamespace

from ideadatamodel import VirtualDesktopServer
from ideavirtualdesktopcontroller.app.servers import virtual_desktop_server_utils
from test_terminate_partial import FakeEc2, server_utils

INSTANCE = 'i-0000000000000000a'


def _terminate(monkeypatch, task_arn, real_metadata=False):
    utils = server_utils(FakeEc2([INSTANCE]))
    utils._controller_utils.is_active_directory.return_value = True
    utils._server_db.get.return_value = SimpleNamespace(
        instance_id=INSTANCE, hostname='C-ABCDEF123456'
    )
    utils.context.config().get_string.return_value = 'https://sqs/queue.fifo'
    if not real_metadata:
        monkeypatch.setattr(
            virtual_desktop_server_utils, 'current_task_arn', lambda: task_arn
        )
    utils.terminate_dcv_hosts([VirtualDesktopServer(instance_id=INSTANCE)], force=True)
    request = utils.context.aws().sqs().send_message.call_args.kwargs
    return json.loads(request['MessageBody'])['payload']


def test_a_task_sends_its_arn_and_a_host_sends_none(monkeypatch):
    payload = _terminate(monkeypatch, 'arn:aws:ecs:us-east-2:1:task/idea-com-ecs/t1')
    assert payload['computer_name'] == 'C-ABCDEF123456'
    assert payload['instance_id'] == INSTANCE
    assert payload['task_arn'] == 'arn:aws:ecs:us-east-2:1:task/idea-com-ecs/t1'
    assert 'task_arn' not in _terminate(monkeypatch, None)


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
    payload = _terminate(monkeypatch, None, real_metadata=True)
    assert opener.open.call_count == 3
    assert sleep.call_count == 2
    assert all(0 < call.args[0] <= 1 for call in sleep.call_args_list)
    assert payload['computer_name']
    assert payload['instance_id']
    if recovers:
        assert payload['task_arn'] == 'arn:task/recovered'
    else:
        assert 'task_arn' not in payload
