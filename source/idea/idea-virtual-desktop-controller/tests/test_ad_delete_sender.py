"""A desktop controller task names itself in the AD delete request it sends."""

import json
from unittest.mock import Mock

from ideadatamodel import VirtualDesktopServer
from ideavirtualdesktopcontroller.app.servers import virtual_desktop_server_utils
from test_terminate_partial import FakeEc2, server_utils

INSTANCE = 'i-0000000000000000a'


def _terminate(monkeypatch, task_arn):
    utils = server_utils(FakeEc2([INSTANCE]))
    utils._controller_utils.is_active_directory.return_value = True
    utils._server_db.get.return_value = VirtualDesktopServer(instance_id=INSTANCE, hostname='C-ABCDEF123456')
    utils.context.config().get_string.return_value = 'https://sqs/queue.fifo'
    monkeypatch.setattr(virtual_desktop_server_utils, 'current_task_arn', lambda: task_arn)
    utils.terminate_dcv_hosts([VirtualDesktopServer(instance_id=INSTANCE)], force=True)
    request = utils.context.aws().sqs().send_message.call_args.kwargs
    return json.loads(request['MessageBody'])['payload']


def test_a_task_sends_its_arn_and_a_host_sends_none(monkeypatch):
    payload = _terminate(monkeypatch, 'arn:aws:ecs:us-east-2:1:task/idea-com-ecs/t1')
    assert payload['instance_id'] == INSTANCE
    assert payload['task_arn'] == 'arn:aws:ecs:us-east-2:1:task/idea-com-ecs/t1'
    assert 'task_arn' not in _terminate(monkeypatch, None)
