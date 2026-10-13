"""
A job's select gets the CPUs of the whole node it is paying for.

On container clusters the calculate_ncpus hook shelled out to the AWS CLI from inside
pbs_server, got nothing, and fell back to ncpus=1: every job ran single-threaded on a paid
16-vCPU node. validate_job now writes the count from the app's instance type cache, and the
hook only fills a select with no ncpus, asking the app over the unix socket.
"""

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import SocaException, errorcodes
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookRequest
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent, OpenPBSJob
from ideatestutils.aws.mock_instance_types import MockInstanceTypes
from test_job_project_default import _projects, _use_queue, _validate_hook

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/calculate_ncpus_hook.py'


def run_hook(monkeypatch, tmp_path, resources, queue='normal', answer=None, refuse=False, event_type='queuejob'):
    logs = []
    event = SimpleNamespace(
        type=event_type,
        job=SimpleNamespace(id='9.scheduler', queue=queue, Resource_List=dict(resources)),
        accept=Mock(),
        reject=Mock(),
    )
    fake_pbs = SimpleNamespace(
        QUEUEJOB='queuejob',
        LOG_DEBUG=1,
        LOG_ERROR=2,
        logmsg=lambda level, message: logs.append(message),
        select=lambda value: value,
        event=lambda: event,
    )
    monkeypatch.setitem(sys.modules, 'pbs', fake_pbs)
    address = tmp_path / 'idea.sock'
    address.touch()
    monkeypatch.setenv('IDEA_SCHEDULER_UNIX_SOCKET', str(address))
    requests = []

    class FakeSocket:
        served = False

        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            assert path == str(address)
            if refuse:
                raise ConnectionRefusedError(111, 'Connection refused')

        def sendall(self, data):
            requests.append(bytes(data))

        def recv(self, _size):
            if self.served:
                return b''
            self.served = True
            body = json.dumps(answer).encode()
            return (
                b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                b'Content-Length: %d\r\n\r\n' % len(body)
            ) + body

        def close(self):
            pass

    monkeypatch.setattr(socket, 'socket', lambda *_args, **_kwargs: FakeSocket())
    try:
        exec(compile(HOOK.read_text(), str(HOOK), 'exec'), {'__name__': 'calculate_ncpus_hook'})
    except SystemExit:
        pass
    return event, requests, logs


def sent(requests):
    return [json.loads(r.split(b'\r\n\r\n', 1)[1]) for r in requests]


@pytest.mark.parametrize('ht_support,expected', [('False', 16), ('True', 32)])
def test_a_select_without_ncpus_gets_the_whole_node_from_the_scheduler(
    monkeypatch, tmp_path, ht_support, expected
):
    event, requests, _ = run_hook(
        monkeypatch,
        tmp_path,
        {'instance_type': 'r8g.4xlarge', 'select': '1:compute_node=x', 'ht_support': ht_support},
        answer={'success': True, 'payload': {'vcpus': 32, 'cores': 16, 'threads_per_core': 2}},
    )
    assert sent(requests) == [
        {'header': sent(requests)[0]['header'], 'payload': {'instance_type': 'r8g.4xlarge'}}
    ]
    assert sent(requests)[0]['header']['namespace'] == 'OpenPBSHook.InstanceTypeInfo'
    assert event.job.Resource_List['select'] == f'1:compute_node=x:ncpus={expected}'
    event.accept.assert_called_once()
    event.reject.assert_not_called()


def test_an_explicit_ncpus_is_kept_and_the_scheduler_is_not_asked(monkeypatch, tmp_path):
    event, requests, _ = run_hook(
        monkeypatch,
        tmp_path,
        {'instance_type': 'r8g.4xlarge', 'select': '1:ncpus=4:compute_node=x'},
        answer={'success': True, 'payload': {'vcpus': 16, 'cores': 16, 'threads_per_core': 1}},
    )
    assert requests == []
    assert event.job.Resource_List['select'] == '1:ncpus=4:compute_node=x'
    event.accept.assert_called_once()


def test_without_an_answer_the_job_is_rejected_not_shrunk_to_one(monkeypatch, tmp_path):
    event, requests, logs = run_hook(
        monkeypatch,
        tmp_path,
        {'instance_type': 'r8g.4xlarge', 'select': '1:compute_node=x'},
        refuse=True,
    )
    assert event.job.Resource_List['select'] == '1:compute_node=x'
    event.accept.assert_not_called()
    event.reject.assert_called_once()
    message = event.reject.call_args.args[0]
    assert 'could not reach idea-scheduler' in message and '-l select=N:ncpus=M' in message
    assert any('ncpus not set' in line for line in logs)


@pytest.mark.parametrize('event_type,queue', [('runjob', 'normal'), ('queuejob', 'job-shared')])
def test_other_events_and_shared_queues_pass_through(monkeypatch, tmp_path, event_type, queue):
    event, requests, _ = run_hook(
        monkeypatch,
        tmp_path,
        {'instance_type': 'r8g.4xlarge', 'select': '1:compute_node=x'},
        queue=queue,
        event_type=event_type,
    )
    assert requests == []
    assert event.job.Resource_List['select'] == '1:compute_node=x'
    event.accept.assert_called_once()


def _api(context):
    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context
    api.logger = Mock()
    return api


def test_the_app_answers_instance_type_info_from_its_cache(context):
    api_context = Mock()
    api_context.namespace = 'OpenPBSHook.InstanceTypeInfo'
    api_context.is_administrator.return_value = True
    api_context.get_request.return_value = {'payload': {'instance_type': 'g5.xlarge'}}
    _api(context).invoke(api_context)
    api_context.success.assert_called_once_with(
        {'instance_type': 'g5.xlarge', 'vcpus': 4, 'cores': 2, 'threads_per_core': 2}
    )
    api_context.get_request.return_value = {'payload': {'instance_type': 'nope.large'}}
    with pytest.raises(SocaException) as exc:
        _api(context).invoke(api_context)
    assert exc.value.error_code == errorcodes.INVALID_EC2_INSTANCE_TYPE


@pytest.mark.parametrize('ht_support,expected', [('false', 1), ('true', 2)])
def test_validate_job_writes_the_whole_node_into_a_select_without_one(
    context, monkeypatch, ht_support, expected
):
    _use_queue(context, monkeypatch, _projects('project-a'))
    monkeypatch.setattr(context.projects_client, 'get_user_projects', lambda username: _projects('project-a'))
    monkeypatch.setattr(context, 'job_monitor', Mock())
    event = OpenPBSEvent(
        type='queuejob',
        requestor='researcher',
        job=OpenPBSJob(
            queue='normal',
            project='project-a',
            Job_Name='job',
            Resource_List={'instance_type': 't3.micro', 'nodes': '1', 'ht_support': ht_support},
        ),
    )
    api_context = Mock()
    api_context.context = context
    api_context.get_request_payload_as.return_value = OpenPBSHookRequest(event=event)
    hook = OpenPBSAPIInvocationContext(api_context)
    monkeypatch.setattr(hook, 'is_valid', lambda: True)
    monkeypatch.setattr(hook, 'check_incidentals', Mock())
    monkeypatch.setattr(hook, 'get_bom_cost', Mock())
    monkeypatch.setattr(hook, 'get_budget_usage', Mock())
    result = _validate_hook(context, monkeypatch, hook)
    assert result.accept is True
    # t3.micro: 2 vCPUs on 1 core
    assert MockInstanceTypes().get_instance_type('t3.micro').vcpu_info_default_vcpus == 2
    assert result.resources_updated['select'] == f'1:ncpus={expected}:compute_node=tbd'


def test_a_shared_queue_keeps_the_requested_cpus(context):
    job = SimpleNamespace(
        is_shared_capacity=lambda: True,
        params=SimpleNamespace(cpus=3, instance_types=['t3.micro'], enable_ht_support=True),
    )
    assert _api(context).whole_node_ncpus(job) == 3
    job.is_shared_capacity = lambda: False
    assert _api(context).whole_node_ncpus(job) == 2
    job.params.instance_types = ['nope.large']
    with pytest.raises(SocaException) as exc:
        _api(context).whole_node_ncpus(job)
    assert '-l select=N:ncpus=M' in exc.value.message
