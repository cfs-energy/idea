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
from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
    PasswordRefused,
)
from ideaclustermanager.app.projects import project_tasks
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideaclustermanager.app.tasks.base_task import BaseTask, TaskCannotSucceed
from ideaclustermanager.app.tasks.task_manager import TASK_MAX_ATTEMPTS, TaskManager
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
        'ApproximateReceiveCount': str(TASK_MAX_ATTEMPTS if final else 1)
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


def test_a_task_that_cannot_succeed_is_recorded_and_stops_blocking_its_group():
    manager, context, task, message = build('accounts.sync-password')
    task.entity_ref.return_value = None
    task.invoke.side_effect = TaskCannotSucceed('the directory refused the password')
    manager.execute_task(message)
    context.aws().sqs().delete_message.assert_called_once()
    context.logger().exception.assert_not_called()


def refused_password_task(tmp_path, side_effect):
    automation = tmp_path / 'automation'
    folder = automation / 'abc'
    folder.mkdir(parents=True)
    password = folder / 'password.txt'
    password.write_text('secret-value')
    context = Mock()
    context.config.return_value = FakeConfig(
        {'directoryservice.automation_dir': str(automation)}
    )
    context.ldap_client.change_password.side_effect = side_effect
    return account_tasks.SyncPasswordInDirectoryServiceTask(context), password


def test_a_password_the_directory_refuses_is_not_retried_and_leaves_no_file(tmp_path):
    task, password = refused_password_task(tmp_path, PasswordRefused())
    with pytest.raises(TaskCannotSucceed) as raised:
        task.invoke({'username': 'user', 'password_file': str(password)})
    assert 'secret-value' not in str(raised.value)
    assert not password.exists()


def test_any_other_directory_failure_keeps_the_file_for_the_retry(tmp_path):
    task, password = refused_password_task(tmp_path, RuntimeError('directory busy'))
    with pytest.raises(RuntimeError):
        task.invoke({'username': 'user', 'password_file': str(password)})
    assert password.exists()


def test_the_directory_client_says_when_a_password_is_refused():
    from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
        ActiveDirectoryClient,
    )

    class UserDoesNotExist(Exception):
        pass

    class InvalidPassword(Exception):
        pass

    client = ActiveDirectoryClient.__new__(ActiveDirectoryClient)
    client.context = Mock()
    client.logger = Mock()
    client.options = Mock(directory_id='d-1')
    client.context.config().get_string.return_value = (
        constants.DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY
    )
    ds = client.context.aws().ds()
    ds.exceptions.UserDoesNotExistException = UserDoesNotExist
    ds.exceptions.InvalidPasswordException = InvalidPassword
    ds.reset_user_password.side_effect = InvalidPassword()
    with pytest.raises(PasswordRefused):
        client.change_password('user', 'weak')


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

    for count in range(1, TASK_MAX_ATTEMPTS):
        message['Attributes'] = {'ApproximateReceiveCount': str(count)}
        manager.execute_task(message)
        assert 'last_task_failure' not in table.get_item(Key=entity_key)['Item']
    context.aws().sqs().delete_message.assert_not_called()
    assert context.aws().sqs().change_message_visibility.call_count == (
        TASK_MAX_ATTEMPTS - 1
    )

    message['Attributes']['ApproximateReceiveCount'] = str(TASK_MAX_ATTEMPTS)
    manager.execute_task(message)
    record = table.get_item(Key=entity_key)['Item']
    marker = record['last_task_failure']
    assert marker['task'] == task.get_name()
    assert marker['message'] == ('failure ' * 40)[:200]
    assert datetime.fromisoformat(marker['at']).utcoffset().total_seconds() == 0
    context.aws().sqs().delete_message.assert_called_once()
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
    assert context.aws().sqs().delete_message.call_count == 3


def test_failure_does_not_recreate_deleted_entity(entity_tables):
    manager, context, task, message = build()
    table = entity_tables['group']
    context.accounts.group_dao.table = table
    task.entity_ref.return_value = ('group', 'deleted')
    task.invoke.side_effect = RuntimeError('sync failed')
    message['Attributes'] = {'ApproximateReceiveCount': str(TASK_MAX_ATTEMPTS)}
    manager.execute_task(message)
    assert 'Item' not in table.get_item(Key={'group_name': 'deleted'})


def test_entityless_failure_keeps_logging_only():
    manager, context, task, message = build()
    task.entity_ref.side_effect = BaseTask().entity_ref
    task.invoke.side_effect = RuntimeError('failed')
    message['Attributes'] = {'ApproximateReceiveCount': str(TASK_MAX_ATTEMPTS)}
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
    message['Attributes'] = {'ApproximateReceiveCount': str(TASK_MAX_ATTEMPTS)}
    manager.execute_task(message)
    assert 'last_task_failure' not in table.get_item(Key={'username': 'user'})['Item']


class FifoQueue:
    """the FIFO rules the consumer relies on: a group with a message in flight, or
    whose oldest message is not yet visible again, holds back its later messages."""

    def __init__(self):
        self.now = 0
        self.messages = []
        self.sequence = 0
        self.dedupe_ids = set()

    def send_message(
        self, MessageBody, MessageGroupId, MessageDeduplicationId=None, **_
    ):
        if MessageDeduplicationId and MessageDeduplicationId in self.dedupe_ids:
            return
        self.dedupe_ids.add(MessageDeduplicationId)
        self.sequence += 1
        self.messages.append(
            {
                'id': str(self.sequence),
                'group': MessageGroupId,
                'body': MessageBody,
                'visible_at': 0,
                'receives': 0,
                'sent_at': self.now * 1000 + self.sequence,
            }
        )

    def receive_message(self, VisibilityTimeout, **_):
        seen = set()
        for message in self.messages:
            if message['group'] in seen:
                continue
            seen.add(message['group'])
            if message['visible_at'] > self.now:
                continue
            message['receives'] += 1
            message['visible_at'] = self.now + VisibilityTimeout
            return {
                'Messages': [
                    {
                        'Body': message['body'],
                        'ReceiptHandle': message['id'],
                        'MessageId': message['id'],
                        'Attributes': {
                            'ApproximateReceiveCount': str(message['receives']),
                            'MessageGroupId': message['group'],
                            'SentTimestamp': str(message['sent_at']),
                        },
                    }
                ]
            }
        return {}

    def find(self, receipt_handle):
        return next(m for m in self.messages if m['id'] == receipt_handle)

    def delete_message(self, ReceiptHandle, **_):
        self.messages.remove(self.find(ReceiptHandle))

    def change_message_visibility(self, ReceiptHandle, VisibilityTimeout, **_):
        self.find(ReceiptHandle)['visible_at'] = self.now + VisibilityTimeout


def account_queue(tmp_path):
    from ideaclustermanager.app.tasks import task_manager as module

    context = Mock()
    automation = tmp_path / 'automation'
    context.config.return_value = FakeConfig(
        {'directoryservice.automation_dir': str(automation)}
    )
    context.accounts.user_dao.get_user.return_value = {'username': 'user'}
    queue = FifoQueue()
    context.aws.return_value.sqs.return_value = queue
    tasks = [
        account_tasks.SyncUserInDirectoryServiceTask(context),
        account_tasks.SyncPasswordInDirectoryServiceTask(context),
        account_tasks.GroupMembershipUpdatedTask(context),
        account_tasks.CreateUserHomeDirectoryTask(context),
    ]
    ran = []
    for task in tasks:
        task.invoke = Mock(
            side_effect=lambda payload, name=task.get_name(): ran.append(
                (name, payload.get('n'))
            )
        )
    manager = TaskManager(context, tasks)
    by_name = {task.get_name(): task for task in tasks}

    def password(n):
        folder = automation / f'p{n}'
        folder.mkdir(parents=True)
        path = folder / 'password.txt'
        path.write_text('secret')
        return path

    def drain():
        # every message receivable now, as the listener would take them
        while True:
            messages = queue.receive_message(
                VisibilityTimeout=manager._polling_visibility_timeout
            ).get('Messages')
            if not messages:
                return
            manager.execute_task(messages[0])

    return module, manager, queue, by_name, ran, password, drain


def fail_first(task, times):
    record = task.invoke.side_effect
    failures = [times]

    def invoke(payload):
        if failures[0] > 0:
            failures[0] -= 1
            raise RuntimeError('busy')
        record(payload)

    task.invoke.side_effect = invoke


def create_user_messages(manager, password_file):
    # the order create_user sends them, all under the account's group id
    manager.send('accounts.sync-user', {'username': 'user'}, 'user', 'u1')
    manager.send(
        'accounts.sync-password',
        {'username': 'user', 'password_file': str(password_file)},
        'user',
    )
    manager.send(
        'accounts.group-membership-updated',
        {'username': 'user', 'group_name': 'g', 'operation': 'add'},
        'user',
    )
    manager.send('accounts.create-home-directory', {'username': 'user'}, 'user')


def test_a_failing_password_sync_does_not_delay_home_creation(tmp_path):
    _, manager, _, tasks, ran, password, drain = account_queue(tmp_path)
    tasks['accounts.sync-password'].invoke.side_effect = RuntimeError('directory down')
    create_user_messages(manager, password(1))
    drain()
    assert [name for name, _ in ran] == [
        'accounts.sync-user',
        'accounts.group-membership-updated',
        'accounts.create-home-directory',
    ]


def test_a_retryable_failure_retries_with_backoff(tmp_path):
    module, manager, queue, tasks, ran, password, drain = account_queue(tmp_path)
    sync = tasks['accounts.sync-password']
    sync.invoke.side_effect = [RuntimeError('busy'), RuntimeError('busy'), None]
    file = password(1)
    manager.send(
        'accounts.sync-password',
        {'username': 'user', 'password_file': str(file)},
        'user',
    )
    drain()
    assert sync.invoke.call_count == 1
    for attempt in (1, 2):
        queue.now += module.retry_delay(attempt) - 1
        drain()
        assert sync.invoke.call_count == attempt
        queue.now += 1
        drain()
        assert sync.invoke.call_count == attempt + 1
    assert queue.messages == []
    assert module.retry_delay(1) < module.retry_delay(2) <= module.RETRY_MAX_SECONDS


def test_a_task_that_keeps_failing_stops_and_releases_its_lane(tmp_path):
    module, manager, queue, tasks, _, password, drain = account_queue(tmp_path)
    sync = tasks['accounts.sync-password']
    first, second = password(1), password(2)
    sync.invoke.side_effect = lambda payload: (
        (_ for _ in ()).throw(RuntimeError('directory down'))
        if payload['password_file'] == str(first)
        else None
    )
    for file in (first, second):
        manager.send(
            'accounts.sync-password',
            {'username': 'user', 'password_file': str(file)},
            'user',
        )
    for attempt in range(1, module.TASK_MAX_ATTEMPTS + 1):
        drain()
        queue.now += module.retry_delay(attempt)
    drain()
    assert sync.invoke.call_count == module.TASK_MAX_ATTEMPTS + 1
    assert sync.invoke.call_args.args[0]['password_file'] == str(second)
    assert queue.messages == []
    assert not first.exists()
    failure = manager.context.accounts.user_dao.table.update_item.call_args_list[0]
    marker = failure.kwargs['ExpressionAttributeValues'][':failure']
    assert marker['task'] == 'accounts.sync-password'


def test_ordering_dependent_tasks_stay_ordered(tmp_path):
    module, manager, queue, tasks, ran, password, drain = account_queue(tmp_path)
    fail_first(tasks['accounts.sync-user'], 1)
    fail_first(tasks['accounts.sync-password'], 1)
    manager.send('accounts.sync-user', {'username': 'user'}, 'user', 'u1')
    manager.send(
        'accounts.group-membership-updated',
        {'username': 'user', 'group_name': 'g', 'operation': 'add', 'n': 1},
        'user',
    )
    for n in (1, 2):
        manager.send(
            'accounts.sync-password',
            {'username': 'user', 'password_file': str(password(n)), 'n': n},
            'user',
        )
    drain()
    assert ran == []
    queue.now += module.retry_delay(1)
    drain()
    assert [entry for entry in ran if entry[0] != 'accounts.sync-password'] == [
        ('accounts.sync-user', None),
        ('accounts.group-membership-updated', 1),
    ]
    assert [n for name, n in ran if name == 'accounts.sync-password'] == [1, 2]


def test_the_attempt_limit_ends_tasks_before_the_dead_letter_move():
    # the dead-letter move would skip the failure record and the password cleanup
    assert TASK_MAX_ATTEMPTS < constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS


def test_a_message_past_its_attempts_is_ended_without_running(tmp_path):
    # left in the queue by an earlier release, which retried until the dead-letter move
    module, manager, queue, tasks, _, password, drain = account_queue(tmp_path)
    file = password(1)
    queue.send_message(
        MessageBody=json.dumps(
            {
                'name': 'accounts.sync-password',
                'payload': {'username': 'user', 'password_file': str(file)},
            }
        ),
        MessageGroupId='user',
    )
    queue.messages[0]['receives'] = module.TASK_MAX_ATTEMPTS
    drain()
    tasks['accounts.sync-password'].invoke.assert_not_called()
    assert queue.messages == []
    assert not file.exists()


def test_a_discarded_password_for_a_deleted_user_leaves_no_file(tmp_path):
    _, manager, queue, _, _, password, drain = account_queue(tmp_path)
    manager.context.accounts.user_dao.get_user.return_value = None
    file = password(1)
    manager.send(
        'accounts.sync-password',
        {'username': 'user', 'password_file': str(file)},
        'user',
    )
    drain()
    assert queue.messages == []
    assert not file.exists()


def password_sync(tmp_path, entity_tables):
    # the real password task against a directory that records what it is given
    module, manager, queue, tasks, _, password, drain = account_queue(tmp_path)
    table = entity_tables['user']
    table.put_item(Item={'username': 'user'})
    user_dao = manager.context.accounts.user_dao
    user_dao.table = table
    user_dao.get_user.side_effect = lambda username: table.get_item(
        Key={'username': username}
    ).get('Item')
    task = account_tasks.SyncPasswordInDirectoryServiceTask(manager.context)
    manager.tasks[task.get_name()] = task
    directory = {'down': False, 'applied': []}

    def change_password(_, value):
        if directory['down']:
            raise RuntimeError('directory down')
        directory['applied'].append(value)

    manager.context.ldap_client.change_password.side_effect = change_password

    def queue_password(value, group=None):
        file = password(value)
        file.write_text(value)
        payload = {'username': 'user', 'password_file': str(file)}
        if group is None:
            manager.send('accounts.sync-password', payload, 'user')
        else:
            # as the release before lanes sent it
            queue.send_message(
                MessageBody=json.dumps(
                    {'name': 'accounts.sync-password', 'payload': payload}
                ),
                MessageGroupId=group,
            )
        return file

    return module, queue, directory, queue_password, drain, table


def test_a_password_queued_before_the_upgrade_cannot_replace_a_newer_one(
    tmp_path, entity_tables
):
    module, queue, directory, queue_password, drain, _ = password_sync(
        tmp_path, entity_tables
    )
    old = queue_password('old', group='user')
    # at the upgrade it waits out a failed attempt in the group it was sent to
    queue.messages[0]['receives'] = 3
    queue.messages[0]['visible_at'] = 150
    queue.now += 1
    queue_password('new')
    drain()
    assert directory['applied'] == ['new']
    for attempt in range(1, module.TASK_MAX_ATTEMPTS + 1):
        queue.now += module.retry_delay(attempt)
        drain()
    assert directory['applied'] == ['new']
    assert queue.messages == []
    assert not old.exists()


def test_a_password_from_the_old_group_still_applies_in_send_order(
    tmp_path, entity_tables
):
    # while the service rolls, a task of the earlier release can queue the newest one
    _, queue, directory, queue_password, drain, table = password_sync(
        tmp_path, entity_tables
    )
    directory['down'] = True
    queue_password('first')
    drain()
    queue.now += 1
    queue_password('second', group='user')
    drain()
    assert [message['group'] for message in queue.messages] == [
        'user.password',
        'user.password',
    ]
    directory['down'] = False
    queue.now += 3600
    drain()
    assert directory['applied'] == ['first', 'second']
    assert queue.messages == []
    record = table.get_item(Key={'username': 'user'})['Item']
    assert record['password_sync_sent_at'] == 1002


def test_a_moved_message_is_not_queued_twice_when_its_delete_fails(
    tmp_path, entity_tables
):
    _, queue, directory, queue_password, drain, _ = password_sync(
        tmp_path, entity_tables
    )
    queue_password('only', group='user')
    delete, failed = queue.delete_message, []

    def fail_once(**kwargs):
        if not failed:
            failed.append(True)
            raise RuntimeError('queue unavailable')
        delete(**kwargs)

    queue.delete_message = fail_once
    drain()
    queue.now += 3600
    drain()
    assert directory['applied'] == ['only']
    assert queue.messages == []


def test_a_password_given_up_on_is_not_replaced_by_an_older_one_behind_it(
    tmp_path, entity_tables
):
    module, queue, directory, queue_password, drain, table = password_sync(
        tmp_path, entity_tables
    )
    old = queue_password('old', group='user')
    # at the upgrade it waits out a failed attempt in the group it was sent to
    queue.messages[0]['receives'] = 3
    queue.messages[0]['visible_at'] = 10000
    queue.now += 1
    directory['down'] = True
    queue_password('new')
    for attempt in range(1, module.TASK_MAX_ATTEMPTS + 1):
        drain()
        queue.now += module.retry_delay(attempt)
    assert [message['group'] for message in queue.messages] == ['user']
    failure = table.get_item(Key={'username': 'user'})['Item']['last_task_failure']
    assert failure['task'] == 'accounts.sync-password'

    directory['down'] = False
    queue.now = 10000
    drain()
    assert directory['applied'] == []
    assert queue.messages == []
    assert not old.exists()
    # the user is still told the newest password did not sync
    record = table.get_item(Key={'username': 'user'})['Item']
    assert record['last_task_failure'] == failure


def test_a_redelivered_password_does_not_clear_a_recorded_failure(
    tmp_path, entity_tables
):
    # refused, recorded, then acknowledged late: the same message comes back
    _, queue, directory, queue_password, drain, table = password_sync(
        tmp_path, entity_tables
    )
    table.put_item(
        Item={
            'username': 'user',
            'last_task_failure': {'task': 'accounts.sync-password', 'message': 'no'},
        }
    )
    file = queue_password('refused')
    file.unlink()
    drain()
    assert directory['applied'] == []
    assert queue.messages == []
    assert 'last_task_failure' in table.get_item(Key={'username': 'user'})['Item']


@pytest.mark.parametrize(
    'username, group, moved',
    [
        # sent by the release before lanes, whatever the name ends in
        ('user', 'user', True),
        ('user.password', 'user.password', True),
        ('user', 'user.password', False),
        ('user.password', 'user.password.password', False),
    ],
)
def test_only_a_message_in_the_bare_group_of_its_user_is_moved_into_the_lane(
    tmp_path, username, group, moved
):
    _, manager, queue, tasks, ran, password, drain = account_queue(tmp_path)
    queue.send_message(
        MessageBody=json.dumps(
            {
                'name': 'accounts.sync-password',
                'payload': {'username': username, 'password_file': str(password(1))},
            }
        ),
        MessageGroupId=group,
    )
    message = queue.receive_message(VisibilityTimeout=30)['Messages'][0]
    task = tasks['accounts.sync-password']
    payload = json.loads(message['Body'])['payload']
    assert manager._move_into_lane(task, payload, message) is moved
    assert [m['group'] for m in queue.messages] == [
        f'{username}.password' if moved else group
    ]


def test_a_redelivered_password_that_already_synced_is_done(tmp_path):
    task, password = refused_password_task(tmp_path, None)
    payload = {'username': 'user', 'password_file': str(password)}
    task.invoke(payload)
    assert not password.exists()
    # acknowledged late: the same message comes back
    task.invoke(payload)
    task.context.ldap_client.change_password.assert_called_once()


def test_a_password_file_on_an_absent_file_system_is_retried(tmp_path):
    task, password = refused_password_task(tmp_path, None)
    away = tmp_path / 'unmounted' / 'abc' / 'password.txt'
    with pytest.raises(FileNotFoundError):
        task.invoke({'username': 'user', 'password_file': str(away)})
    task.context.ldap_client.change_password.assert_not_called()


def membership_task(group):
    context = Mock()
    context.accounts.group_dao.get_group.return_value = group
    context.ldap_client.is_readonly.return_value = False
    return account_tasks.GroupMembershipUpdatedTask(context), context


def test_removing_a_member_of_a_deleted_group_finishes_without_a_retry():
    task, context = membership_task(None)
    task.invoke({'group_name': 'gone', 'username': 'user', 'operation': 'remove'})
    context.projects.user_projects_dao.group_member_removed.assert_called_once_with(
        group_name='gone', username='user'
    )
    context.ldap_client.remove_user_from_group.assert_not_called()


def test_adding_a_member_to_a_deleted_group_is_not_retried():
    task, context = membership_task(None)
    with pytest.raises(TaskCannotSucceed):
        task.invoke({'group_name': 'gone', 'username': 'user', 'operation': 'add'})
    context.projects.user_projects_dao.group_member_added.assert_not_called()


def test_a_retry_line_names_its_task_and_message_and_no_user():
    # several users' tasks retry at once, and each line has to be told from the others
    manager, context, task, message = build()
    task.invoke.side_effect = RuntimeError('directory down')
    message['MessageId'] = 'm-1'
    manager.execute_task(message)
    lines = [call.args[0] for call in context.logger().info.call_args_list]
    (retry,) = [line for line in lines if ' retry ' in line]
    assert retry == 'task test.task (message m-1) retry 2 of 10 in 10s'
    # the line that carries the payload carries the same id
    assert any('message m-1' in line and 'executing' in line for line in lines)
    failed = context.logger().exception.call_args.args[0]
    assert 'test.task (message m-1)' in failed


class Directory:
    """what the account tasks ask of the directory, with two habits of Active
    Directory: a read can be answered by a domain controller that a write has not
    reached yet, and an account it does not have cannot be given a password."""

    def __init__(self):
        # one entry per add: a second add of one name is the copy the directory keeps
        self.accounts = []
        self.groups = []
        self.passwords = {}
        self.refused = 0
        self.now = 0
        # until then every read misses what was written
        self.behind_until = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def is_readonly(self):
        return False

    def is_existing_user(self, username):
        return self.now >= self.behind_until and username in self.accounts

    def is_existing_group(self, group_name):
        return self.now >= self.behind_until and group_name in self.groups

    def is_sudo_user(self, _):
        return False

    def sync_group(self, group_name, gid):
        if not self.is_existing_group(group_name):
            self.groups.append(group_name)

    def sync_user(self, *, username, **_):
        if not self.is_existing_user(username):
            self.accounts.append(username)

    def delete_user(self, username):
        self.accounts.remove(username)

    def delete_group(self, group_name):
        self.groups.remove(group_name)

    def add_user_to_group(self, usernames, group_name):
        pass

    def change_password(self, username, password):
        if username not in self.accounts:
            self.refused += 1
            raise RuntimeError('not yet synced')
        self.passwords[username] = password


@pytest.fixture
def live_accounts(context, monkeypatch, tmp_path):
    # the real accounts service and the real tasks, joined by the queue's FIFO rules
    queue, directory = FifoQueue(), Directory()
    tasks = [
        account_tasks.SyncUserInDirectoryServiceTask(context),
        account_tasks.SyncGroupInDirectoryServiceTask(context),
        account_tasks.SyncPasswordInDirectoryServiceTask(context),
        account_tasks.GroupMembershipUpdatedTask(context),
        account_tasks.CreateUserHomeDirectoryTask(context),
    ]
    home = tasks[-1]
    home.invoke = Mock()
    manager = TaskManager(context, tasks)
    monkeypatch.setattr(context.aws(), 'sqs', lambda: queue, raising=False)
    monkeypatch.setattr(manager, 'get_task_queue_url', lambda: 'queue')
    monkeypatch.setattr(context, 'ldap_client', directory)
    monkeypatch.setattr(context.accounts, 'ldap_client', directory)
    monkeypatch.setattr(context.accounts, 'task_manager', manager)
    monkeypatch.setattr(context.accounts, 'ds_automation_dir', str(tmp_path))
    # the wait for the directory to catch up runs on the directory's clock
    monkeypatch.setattr(account_tasks, 'time', directory, raising=False)

    def create(username):
        from ideadatamodel import User

        context.accounts.create_user(
            user=User(
                username=username,
                email=f'{username}@example.com',
                password='MockPassword_123',
            ),
            email_verified=True,
        )

    def drain():
        while True:
            messages = queue.receive_message(
                VisibilityTimeout=manager._polling_visibility_timeout
            ).get('Messages')
            if not messages:
                return
            manager.execute_task(messages[0])

    return context, manager, queue, directory, home, create, drain


def names(queue):
    return [json.loads(message['body'])['name'] for message in queue.messages]


def test_a_new_user_is_put_in_the_directory_by_one_task(live_accounts):
    # a second copy can ask a domain controller the account has not reached yet
    _, _, queue, _, _, create, _ = live_accounts
    create('tasks_new1')
    assert names(queue).count('accounts.sync-user') == 1


def test_a_new_users_password_waits_for_the_account_and_is_never_refused(
    live_accounts,
):
    _, manager, queue, directory, home, create, drain = live_accounts
    create('tasks_new2')
    # the task that creates the account is still running
    creating = queue.receive_message(VisibilityTimeout=600)['Messages'][0]
    assert json.loads(creating['Body'])['name'] == 'accounts.sync-user'
    drain()
    # the home folder does not wait for it, and the password does
    home.invoke.assert_called_once()
    assert directory.refused == 0
    assert directory.passwords == {}
    manager.execute_task(creating)
    drain()
    assert directory.passwords == {'tasks_new2': 'MockPassword_123'}
    assert directory.refused == 0
    assert queue.messages == []


def test_a_password_for_an_account_the_directory_has_goes_straight_to_its_lane(
    live_accounts,
):
    context, manager, queue, directory, _, create, drain = live_accounts
    create('tasks_new3')
    drain()
    # the account's other tasks are held up; a new password is not
    manager.send('accounts.sync-user', {'username': 'tasks_new3'}, 'tasks_new3', 'u2')
    queue.receive_message(VisibilityTimeout=600)
    context.accounts.change_ldap_password('tasks_new3', 'second')
    assert names(queue) == ['accounts.sync-user', 'accounts.sync-password']
    drain()
    assert directory.passwords == {'tasks_new3': 'second'}


def test_an_account_is_not_added_again_while_the_directory_catches_up(live_accounts):
    context, manager, _, directory, _, create, drain = live_accounts
    create('tasks_new4')
    drain()
    assert directory.accounts == ['tasks_new4']
    # the next read is answered by a domain controller the account reaches in 15 s
    directory.behind_until = directory.now + 15
    manager.send('accounts.sync-user', {'username': 'tasks_new4'}, 'tasks_new4', 'u2')
    drain()
    assert directory.accounts == ['tasks_new4']
    assert directory.groups == ['tasks_new4-user-group']
    assert directory.now >= 15


def test_disabling_a_user_removes_an_account_the_first_read_does_not_show(
    live_accounts,
):
    context, _, _, directory, _, create, drain = live_accounts
    create('tasks_new5')
    drain()
    directory.behind_until = directory.now + 15
    context.accounts.disable_user('tasks_new5')
    drain()
    assert directory.accounts == []
    assert directory.groups == []
    # and it is added again when the user is enabled, without waiting for it
    waited = directory.now
    context.accounts.enable_user('tasks_new5')
    drain()
    assert directory.accounts == ['tasks_new5']
    assert directory.now == waited


def test_an_account_removed_from_the_directory_by_hand_is_added_again(live_accounts):
    _, manager, _, directory, _, create, drain = live_accounts
    create('tasks_new6')
    drain()
    directory.accounts.clear()
    manager.send('accounts.sync-user', {'username': 'tasks_new6'}, 'tasks_new6', 'u2')
    drain()
    assert directory.accounts == ['tasks_new6']
    assert (
        directory.now >= account_tasks.SyncUserInDirectoryServiceTask.CATCH_UP_SECONDS
    )
