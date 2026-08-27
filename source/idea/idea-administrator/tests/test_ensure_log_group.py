"""
The custom resource that creates the bedrock invocation log group, or adopts it if
it is already there.

The group deliberately outlives the stack, because idea does not own the account
level invocation logging configuration and another caller may still be delivering
to it. A plain CloudFormation log group with a fixed name cannot express that: once
retained, the next deploy fails because the group already exists. These tests pin
the three behaviours that matter - create when absent, adopt when present, and
never delete.
"""

import importlib.util
import os
import sys
import types

import pytest

LOG_GROUP = '/idea-test/cluster-manager/bedrock-invocations'


def _load_handler_module():
    if 'idea_lambda_commons' not in sys.modules:
        commons = types.ModuleType('idea_lambda_commons')

        class CfnResponseStatus:
            SUCCESS = 'SUCCESS'
            FAILED = 'FAILED'

        class CfnResponse:
            def __init__(self, context, event, status, data, physical_resource_id):
                self.status = status
                self.data = data
                self.physical_resource_id = physical_resource_id

        class HttpClient:
            sent = []

            def send_cfn_response(self, response):
                HttpClient.sent.append(response)

        commons.CfnResponseStatus = CfnResponseStatus
        commons.CfnResponse = CfnResponse
        commons.HttpClient = HttpClient
        sys.modules['idea_lambda_commons'] = commons

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources',
        'lambda_functions',
        'idea_custom_resource_ensure_log_group',
        'handler.py',
    )
    spec = importlib.util.spec_from_file_location('ensure_log_group_handler', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler_module = _load_handler_module()


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    # the handler builds its boto client before the work function is reached,
    # so a region has to be present even when that work is stubbed out
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')


class FakeAlreadyExists(Exception):
    pass


class FakeLogsClient:
    class exceptions:
        ResourceAlreadyExistsException = FakeAlreadyExists

    def __init__(self, existing=()):
        self._existing = set(existing)
        self.created = []
        self.retention = []

    def create_log_group(self, logGroupName):  # noqa: N803 - boto3 kwarg name
        if logGroupName in self._existing:
            raise FakeAlreadyExists(logGroupName)
        self.created.append(logGroupName)

    def put_retention_policy(self, logGroupName, retentionInDays):  # noqa: N803
        self.retention.append((logGroupName, retentionInDays))


def test_creates_the_group_when_it_is_absent():
    client = FakeLogsClient()
    outcome = handler_module.ensure_log_group(client, LOG_GROUP, 30)
    assert outcome == 'created'
    assert client.created == [LOG_GROUP]
    assert client.retention == [(LOG_GROUP, 30)]


def test_adopts_the_group_when_it_already_exists():
    client = FakeLogsClient(existing=[LOG_GROUP])
    outcome = handler_module.ensure_log_group(client, LOG_GROUP, 30)
    assert outcome == 'adopted'
    assert client.created == []
    # retention is still applied to a group we adopted
    assert client.retention == [(LOG_GROUP, 30)]


def test_retention_is_left_alone_when_not_supplied():
    client = FakeLogsClient()
    handler_module.ensure_log_group(client, LOG_GROUP, None)
    assert client.retention == []


def test_retention_arrives_as_a_string_from_cloudformation():
    # cloudformation stringifies every resource property
    client = FakeLogsClient()
    handler_module.ensure_log_group(client, LOG_GROUP, '90')
    assert client.retention == [(LOG_GROUP, 90)]


@pytest.mark.parametrize('request_type', ['Create', 'Update'])
def test_create_and_update_ensure_the_group(request_type, monkeypatch):
    seen = []
    monkeypatch.setattr(
        handler_module,
        'ensure_log_group',
        lambda client, name, retention: seen.append((name, retention)) or 'created',
    )
    sent = _run_handler(request_type)
    assert seen == [(LOG_GROUP, '30')]
    assert sent.status == 'SUCCESS'
    assert sent.data['Outcome'] == 'created'


def test_delete_never_removes_the_group(monkeypatch):
    called = []
    monkeypatch.setattr(
        handler_module, 'ensure_log_group', lambda *a, **k: called.append(a)
    )
    sent = _run_handler('Delete')
    assert called == []
    assert sent.status == 'SUCCESS'
    assert sent.physical_resource_id == LOG_GROUP


def test_a_real_failure_is_reported(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError('logs is having a day')

    monkeypatch.setattr(handler_module, 'ensure_log_group', explode)
    sent = _run_handler('Create')
    assert sent.status == 'FAILED'
    assert 'logs is having a day' in sent.data['error']


def _run_handler(request_type):
    http_client_cls = sys.modules['idea_lambda_commons'].HttpClient
    http_client_cls.sent = []
    handler_module.handler(
        {
            'RequestType': request_type,
            'ResourceProperties': {
                'LogGroupName': LOG_GROUP,
                'RetentionInDays': '30',
            },
        },
        object(),
    )
    assert len(http_client_cls.sent) == 1
    return http_client_cls.sent[0]
