"""A process on ECS knows its task ARN; off ECS it says so, and a failed lookup is retried."""

import io
import json
import time
from unittest.mock import Mock

import pytest

from ideasdk.aws import ecs_task


def _serve(monkeypatch, body=None, error=None):
    calls = []

    class Opener:
        def open(self, url, timeout=None):
            calls.append(url)
            if error:
                raise error
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(ecs_task.urllib.request, 'build_opener', lambda *_: Opener())
    return calls


def test_the_task_arn_comes_from_the_metadata_endpoint_and_is_remembered(monkeypatch):
    monkeypatch.setattr(ecs_task, '_task_arn', None)
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', 'http://169.254.170.2/v4/abc')
    calls = _serve(monkeypatch, {'TaskARN': 'arn:aws:ecs:us-east-2:1:task/c/t1'})
    assert ecs_task.current_task_arn() == 'arn:aws:ecs:us-east-2:1:task/c/t1'
    assert ecs_task.current_task_arn() == 'arn:aws:ecs:us-east-2:1:task/c/t1'
    assert calls == ['http://169.254.170.2/v4/abc/task']


def test_off_ecs_has_no_arn_and_metadata_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(ecs_task, '_task_arn', None)
    monkeypatch.delenv('ECS_CONTAINER_METADATA_URI_V4', raising=False)
    assert ecs_task.current_task_arn() is None
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', 'http://169.254.170.2/v4/abc')
    monkeypatch.setattr(time, 'sleep', Mock())
    calls = _serve(monkeypatch, error=OSError('not yet'))
    with pytest.raises(RuntimeError, match='Unable to read ECS task ARN'):
        ecs_task.current_task_arn()
    _serve(monkeypatch, {'TaskARN': 'arn:aws:ecs:us-east-2:1:task/c/t2'})
    assert ecs_task.current_task_arn() == 'arn:aws:ecs:us-east-2:1:task/c/t2'
    assert calls == ['http://169.254.170.2/v4/abc/task'] * 3


def test_missing_task_arn_is_not_silently_sent_as_an_ec2_request(monkeypatch):
    monkeypatch.setattr(ecs_task, '_task_arn', None)
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', 'http://169.254.170.2/v4/abc')
    monkeypatch.setattr(time, 'sleep', Mock())
    _serve(monkeypatch, {})
    with pytest.raises(RuntimeError, match='did not include TaskARN'):
        ecs_task.current_task_arn()
