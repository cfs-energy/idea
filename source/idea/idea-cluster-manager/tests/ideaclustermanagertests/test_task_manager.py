from datetime import datetime

import boto3
import json
import subprocess
import sys
import threading
from unittest.mock import Mock

import ldap
import pytest

from ideadatamodel import constants
from ideaclustermanager.app.accounts import account_tasks
from ideaclustermanager.app.accounts.db.user_dao import UserDAO
from ideaclustermanager.app.accounts.db.group_dao import GroupDAO
from ideaclustermanager.app.projects import project_tasks
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideaclustermanager.app.tasks.base_task import BaseTask
from ideaclustermanager.app.tasks.task_manager import TaskManager
from ideaclustermanagertests.metrics_fakes import FakeConfig


def build(task_name='test.task', values=None):
    context = Mock()
    context.config.return_value = FakeConfig(values or {})
    context.accounts.user_dao.get_user.return_value = {'username': 'user'}
    task = Mock()
    task.get_name.return_value = task_name
    task.entity_ref.return_value = None
    manager = TaskManager(context, [task])
    message = {
        'Body': json.dumps({'name': task_name, 'payload': {'username': 'user'}}),
        'ReceiptHandle': 'receipt',
    }
    return manager, context, task, message


def run_worker(hang, final=False, write_hangs=False):
    manager, context, task, message = build(
        values={'cluster-manager.task_manager.max_workers': 1}
    )
    manager._task_timeout = 0.05
    task.entity_ref.return_value = ('user', 'user')
    message['Attributes'] = {
        'ApproximateReceiveCount': str(
            constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS if final else 1
        )
    }

    def write_marker(**kwargs):
        if (
            'ExpressionAttributeValues' in kwargs
            and ':failure' in kwargs['ExpressionAttributeValues']
        ):
            print(
                json.dumps(kwargs['ExpressionAttributeValues'][':failure']), flush=True
            )
            if write_hangs:
                threading.Event().wait()

    context.accounts.user_dao.table.update_item.side_effect = write_marker
    if hang:
        task.invoke.side_effect = lambda _: threading.Event().wait()
    context.aws().sqs().delete_message.side_effect = lambda **_: print('acknowledged')
    assert manager._reserve_slot()
    manager.task_executors.submit(manager._execute_with_deadline, message).result()
    assert manager._slots.acquire(blocking=False)
    manager.task_executors.shutdown()


@pytest.mark.parametrize(
    'final,write_hangs', [(False, False), (True, False), (True, True)]
)
def test_hanging_task_terminates_worker_and_replacement_has_capacity(
    final, write_hangs
):
    # Exercise the actual hard exit outside pytest; a fake exit would leave the
    # stuck thread alive and would not establish that retrying is safe.
    for hang, expected in [(True, 1), (False, 0)]:
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                'from ideaclustermanagertests.test_task_manager import run_worker; '
                f'run_worker({hang}, {final}, {write_hangs})',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == expected, result.stderr
        assert ('acknowledged' in result.stdout) is not hang
        if hang and final:
            marker = json.loads(result.stdout.strip())
            assert marker['task'] == 'test.task'
            assert marker['message'] == 'Task deadline exceeded'
            assert datetime.fromisoformat(marker['at']).utcoffset().total_seconds() == 0
        else:
            assert 'Task deadline exceeded' not in result.stdout


@pytest.mark.parametrize(
    'name',
    [
        'accounts.sync-user',
        'accounts.sync-password',
        'accounts.create-home-directory',
        'accounts.group-membership-updated',
    ],
)
def test_deleted_user_is_acknowledged_without_invoking_task(name):
    manager, context, task, message = build(name)
    context.accounts.user_dao.get_user.return_value = None
    manager.execute_task(message)
    task.invoke.assert_not_called()
    context.aws().sqs().delete_message.assert_called_once()
    context.logger().warning.assert_called_once()


@pytest.mark.parametrize('deleted', [True, False])
def test_ldap_absence_only_discarded_when_account_is_deleted(deleted):
    manager, context, task, message = build('accounts.sync-password')
    context.accounts.user_dao.get_user.side_effect = [
        {'username': 'user'},
        None if deleted else {'username': 'user'},
    ]
    task.invoke.side_effect = ldap.NO_SUCH_OBJECT()
    manager.execute_task(message)
    assert context.aws().sqs().delete_message.call_count == int(deleted)


def test_full_pool_warning_once_per_episode(monkeypatch):
    manager, context, _, _ = build(
        values={'cluster-manager.task_manager.max_workers': 1}
    )
    now = [0]
    monkeypatch.setattr(
        'ideaclustermanager.app.tasks.task_manager.time.monotonic', lambda: now[0]
    )
    assert manager._reserve_slot()
    assert not manager._reserve_slot()
    now[0] = manager._task_timeout
    assert not manager._reserve_slot()
    context.logger().warning.assert_not_called()
    now[0] += 1
    assert not manager._reserve_slot()
    assert not manager._reserve_slot()
    context.logger().warning.assert_called_once()
    manager._slots.release()
    assert manager._reserve_slot()
    assert not manager._reserve_slot()
    now[0] += manager._task_timeout + 1
    assert not manager._reserve_slot()
    assert context.logger().warning.call_count == 2


def test_full_pool_does_not_receive_messages():
    manager, context, _, _ = build(
        values={'cluster-manager.task_manager.max_workers': 1}
    )
    assert manager._reserve_slot()
    manager.exit = Mock()
    manager.exit.is_set.side_effect = [False, True]
    manager.task_queue_listener()
    context.aws().sqs().receive_message.assert_not_called()


def test_fifo_receives_one_with_visibility_longer_than_deadline():
    manager, context, _, message = build()
    manager.task_executors = Mock()
    manager.exit = Mock()
    manager.exit.is_set.side_effect = [False, True]
    context.aws().sqs().receive_message.return_value = {'Messages': [message]}
    manager.task_queue_listener()
    arguments = context.aws().sqs().receive_message.call_args.kwargs
    assert arguments['MaxNumberOfMessages'] == 1
    assert arguments['VisibilityTimeout'] > manager._task_timeout
    manager.task_executors.submit.assert_called_once_with(
        manager._execute_with_deadline, message
    )


def test_completed_task_cancels_deadline_and_releases_slot(monkeypatch):
    manager, context, task, message = build(
        values={'cluster-manager.task_manager.max_workers': 1}
    )
    timer = Mock()
    monkeypatch.setattr(
        'ideaclustermanager.app.tasks.task_manager.threading.Timer', timer
    )
    assert manager._reserve_slot()
    manager._execute_with_deadline(message)
    task.invoke.assert_called_once()
    context.aws().sqs().delete_message.assert_called_once()
    timer.return_value.cancel.assert_called_once()
    assert manager._reserve_slot()


@pytest.fixture(scope='module')
def entity_tables(ddb_local):
    database = boto3.resource(
        'dynamodb',
        endpoint_url=f'http://localhost:{ddb_local.port}/',
        region_name='us-east-1',
        aws_access_key_id='test',
        aws_secret_access_key='test',
    )
    tables = {}
    for kind, key in [
        ('user', 'username'),
        ('group', 'group_name'),
        ('project', 'project_id'),
    ]:
        tables[kind] = database.create_table(
            TableName=f'task-{kind}',
            KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
    yield tables
    for table in tables.values():
        table.delete()


@pytest.mark.parametrize(
    'task_class, kind, key',
    [
        (account_tasks.SyncUserInDirectoryServiceTask, 'user', 'username'),
        (account_tasks.SyncGroupInDirectoryServiceTask, 'group', 'group_name'),
        (account_tasks.CreateUserHomeDirectoryTask, 'user', 'username'),
        (account_tasks.SyncPasswordInDirectoryServiceTask, 'user', 'username'),
        (account_tasks.GroupMembershipUpdatedTask, 'user', 'username'),
        (project_tasks.ProjectEnabledTask, 'project', 'project_id'),
        (project_tasks.ProjectDisabledTask, 'project', 'project_id'),
        (project_tasks.ProjectGroupsUpdatedTask, 'project', 'project_id'),
        (project_tasks.ProjectBedrockReconcileTask, 'project', 'project_id'),
    ],
)
def test_terminal_failure_is_visible_until_same_task_succeeds(
    entity_tables, task_class, kind, key
):
    manager, context, _, message = build()
    task = task_class(context)
    task.invoke = Mock(side_effect=RuntimeError('failure ' * 40))
    manager.tasks = {task.get_name(): task}
    payload = {'username': 'user', 'group_name': 'group', 'project_id': 'project'}
    message['Body'] = json.dumps({'name': task.get_name(), 'payload': payload})
    table = entity_tables[kind]
    context.accounts.user_dao.table = entity_tables['user']
    context.accounts.group_dao.table = entity_tables['group']
    context.projects.projects_dao.table = entity_tables['project']
    entity_key = {key: payload[key]}
    table.put_item(Item={**entity_key, 'created_on': 1, 'updated_on': 1})

    for count in range(1, constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS):
        message['Attributes'] = {'ApproximateReceiveCount': str(count)}
        manager.execute_task(message)
        assert 'last_task_failure' not in table.get_item(Key=entity_key)['Item']

    message['Attributes']['ApproximateReceiveCount'] = str(
        constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS
    )
    manager.execute_task(message)
    record = table.get_item(Key=entity_key)['Item']
    marker = record['last_task_failure']
    assert marker['task'] == task.get_name()
    assert marker['message'] == ('failure ' * 40)[:200]
    assert datetime.fromisoformat(marker['at']).utcoffset().total_seconds() == 0
    context.aws().sqs().delete_message.assert_not_called()
    if kind == 'user':
        entity = UserDAO(context, Mock()).convert_from_db(record)
    elif kind == 'group':
        entity = GroupDAO.convert_from_db(record)
    else:
        entity = ProjectsDAO.convert_from_db(record)
    assert entity.model_dump()['last_task_failure'] == marker

    other_task = Mock()
    other_task.get_name.return_value = 'test.other-task'
    other_task.entity_ref.return_value = (kind, payload[key])
    manager.tasks[other_task.get_name()] = other_task
    message['Body'] = json.dumps({'name': other_task.get_name(), 'payload': payload})
    manager.execute_task(message)
    assert table.get_item(Key=entity_key)['Item']['last_task_failure'] == marker

    message['Body'] = json.dumps({'name': task.get_name(), 'payload': payload})
    task.invoke.side_effect = None
    manager.execute_task(message)
    assert 'last_task_failure' not in table.get_item(Key=entity_key)['Item']
    assert context.aws().sqs().delete_message.call_count == 2


def test_failure_does_not_recreate_deleted_entity(entity_tables):
    manager, context, task, message = build()
    table = entity_tables['group']
    context.accounts.group_dao.table = table
    task.entity_ref.return_value = ('group', 'deleted')
    task.invoke.side_effect = RuntimeError('sync failed')
    message['Attributes'] = {
        'ApproximateReceiveCount': str(constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS)
    }
    manager.execute_task(message)
    assert 'Item' not in table.get_item(Key={'group_name': 'deleted'})


def test_entityless_failure_keeps_logging_only():
    manager, context, task, message = build()
    task.entity_ref.side_effect = BaseTask().entity_ref
    task.invoke.side_effect = RuntimeError('failed')
    message['Attributes'] = {
        'ApproximateReceiveCount': str(constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS)
    }
    manager.execute_task(message)
    context.logger().exception.assert_called_once()
    context.accounts.user_dao.table.update_item.assert_not_called()
    context.accounts.group_dao.table.update_item.assert_not_called()
    context.projects.projects_dao.table.update_item.assert_not_called()


def test_nonretryable_ldap_failure_is_recorded_before_acknowledgement(entity_tables):
    manager, context, task, message = build()
    table = entity_tables['user']
    context.accounts.user_dao.table = table
    task.entity_ref.return_value = ('user', 'user')
    table.put_item(Item={'username': 'user'})
    task.invoke.side_effect = ldap.ALREADY_EXISTS('Entry already exists')
    manager.execute_task(message)
    marker = table.get_item(Key={'username': 'user'})['Item']['last_task_failure']
    assert marker['task'] == task.get_name()
    assert marker['message'] == 'Entry already exists'
    context.aws().sqs().delete_message.assert_called_once()


def test_acknowledgement_failure_does_not_mark_successful_task(entity_tables):
    manager, context, task, message = build()
    table = entity_tables['user']
    context.accounts.user_dao.table = table
    task.entity_ref.return_value = ('user', 'user')
    table.put_item(Item={'username': 'user'})
    context.aws().sqs().delete_message.side_effect = RuntimeError('queue unavailable')
    message['Attributes'] = {
        'ApproximateReceiveCount': str(constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS)
    }
    manager.execute_task(message)
    assert 'last_task_failure' not in table.get_item(Key={'username': 'user'})['Item']
