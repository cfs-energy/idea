"""
AD automation messages from the cluster's containers are verified through ECS, as a host's
are through EC2: the desktop controller and the scheduler retire computer objects from tasks
now, and their SenderId is a task role session, not an instance.
"""

from unittest.mock import Mock

import pytest

from ideaclustermanager.app.accounts.helpers import delete_computer_helper
from ideaclustermanager.app.accounts.helpers.delete_computer_helper import (
    DeleteComputerHelper,
)
from ideaclustermanager.app.accounts.helpers.task_directory_identity import (
    verify_bastion_task,
    verify_cluster_task,
)
from ideaclustermanagertests.metrics_fakes import FakeContext
from ideadatamodel import errorcodes, exceptions

CLUSTER = 'idea-com-ecs'
ROLES = {
    'bastion-host': 'AROABASTION',
    'virtual-desktop-controller': 'AROACONTROLLER',
    'scheduler': 'AROASCHEDULER',
}
SERVICES = {
    'bastion-host': 'idea-com-bastion-host',
    'virtual-desktop-controller': 'idea-com-vdc-controller',
    'scheduler': 'idea-com-scheduler',
}


def _context(task=None, failures=None):
    values = {'ecs.cluster_name': CLUSTER}
    for module in ROLES:
        values[f'{module}.task_role_id'] = ROLES[module]
        values[f'{module}.service_name'] = SERVICES[module]
    context = FakeContext(values)
    context._aws = Mock()
    context.aws().get_client('ecs').describe_tasks.return_value = {
        'tasks': [task] if task else [],
        'failures': failures or [],
    }
    return context


def _task(module, task_id='a08865093a3b49bda527d44d31bce224', status='RUNNING', desired='RUNNING'):
    return {
        'taskArn': f'arn:aws:ecs:us-east-2:871375171278:task/{CLUSTER}/{task_id}',
        'lastStatus': status,
        'desiredStatus': desired,
        'group': f'service:{SERVICES[module]}',
    }


@pytest.mark.parametrize('module', sorted(ROLES))
def test_a_running_task_of_a_cluster_service_is_its_module(module):
    task = _task(module)
    context = _context(task)
    sender = f'{ROLES[module]}:a08865093a3b49bda527d44d31bce224'
    assert verify_cluster_task(context, sender, task['taskArn']) == module
    context.aws().get_client('ecs').describe_tasks.assert_called_with(
        cluster=CLUSTER, tasks=[task['taskArn']]
    )


def test_the_bastion_join_still_verifies_as_the_bastion():
    task = _task('bastion-host')
    sender = f'{ROLES["bastion-host"]}:a08865093a3b49bda527d44d31bce224'
    assert verify_bastion_task(_context(task), sender, task['taskArn']) == 'a08865093a3b49bda527d44d31bce224'
    scheduler = _task('scheduler')
    with pytest.raises(ValueError):
        verify_bastion_task(_context(scheduler), f'{ROLES["scheduler"]}:a08865093a3b49bda527d44d31bce224', scheduler['taskArn'])


@pytest.mark.parametrize(
    'sender,task,failures,reason',
    [
        ('AIDAIMTEBMVXFWRSXIKCK', _task('scheduler'), None, 'not a role session'),
        ('AROASOMEONEELSE:a08865093a3b49bda527d44d31bce224', _task('scheduler'), None, 'not a cluster task role'),
        (f'{ROLES["scheduler"]}:ffffffffffffffffffffffffffffffff', _task('scheduler'), None, 'session does not match'),
        (f'{ROLES["scheduler"]}:a08865093a3b49bda527d44d31bce224', None, [{'reason': 'MISSING'}], 'not found'),
        (f'{ROLES["scheduler"]}:a08865093a3b49bda527d44d31bce224', _task('scheduler', status='STOPPED'), None, 'not a running member'),
        (f'{ROLES["scheduler"]}:a08865093a3b49bda527d44d31bce224', _task('virtual-desktop-controller'), None, 'not a running member'),
    ],
)
def test_anything_else_is_refused(sender, task, failures, reason):
    arn = (task or _task('scheduler'))['taskArn']
    with pytest.raises(ValueError, match=reason):
        verify_cluster_task(_context(task, failures), sender, arn)


def _delete_helper(monkeypatch, preset_error):
    context = FakeContext(
        {
            'cluster.aws.region': 'us-east-2',
            'cluster.aws.account_id': '871375171278',
            'cluster.cluster_name': 'idea-com',
            'directoryservice.ad_automation.hostname_prefix': 'C-',
        }
    )
    helper = DeleteComputerHelper.__new__(DeleteComputerHelper)
    helper.context = context
    helper.logger = Mock()
    helper.ldap_client = Mock()
    helper.ad_automation_dao = Mock()
    helper.sender_id = 'AIDAIMTEBMVXFWRSXIKCK'
    helper.request = {'payload': {'instance_id': 'i-00de1e62d106952d8'}}

    def preset(**_kwargs):
        raise preset_error

    monkeypatch.setattr(delete_computer_helper, 'PresetComputeHelper', preset)
    return helper


def test_a_request_from_an_unverifiable_sender_is_dropped_not_retried(monkeypatch):
    # the agent deletes a message that failed with AD_AUTOMATION_PRESET_COMPUTER_FAILED and
    # retries any other error: wrapping it had the same five requests redelivered all day
    helper = _delete_helper(
        monkeypatch,
        exceptions.soca_exception(
            error_code=errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED,
            message='Unable to verify cluster node identity: Invalid SenderId',
        ),
    )
    with pytest.raises(exceptions.SocaException) as exc:
        helper.invoke()
    assert exc.value.error_code == errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED

    helper = _delete_helper(monkeypatch, RuntimeError('ldap is down'))
    with pytest.raises(exceptions.SocaException) as exc:
        helper.invoke()
    assert exc.value.error_code == errorcodes.AD_AUTOMATION_DELETE_COMPUTER_FAILED
