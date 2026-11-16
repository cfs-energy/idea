"""What the fifth review found. Each runs against the real service over a real tree."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import errno
import os
import shutil

import boto3
import pytest
from moto import mock_aws

from ideasdk.client.notifications_async_client import NotificationsAsyncClient
from ideaclustermanager.app.notifications.notifications_service import (
    NotificationsService,
)
from ideascheduler.app.scratch_cleanup import service as module
from ideascheduler.app.scratch_cleanup import store as store_module
from ideascheduler.app.scratch_cleanup.rules import Reason
from ideascheduler.app.scratch_cleanup.store import ScratchCleanupStore
import test_scratch_cleanup_service as base
from test_scratch_cleanup_service import (
    LATER,
    REAL_NOW,
    digests_on,
    due_in,
    job_folder,
    logged_alert,
    put_event,
    run,
)

world = base.world
SENDER = 'cluster@example.com'


class Config:
    def __init__(self, values):
        self.values = values

    def get_string(self, key, default=None, required=False):
        return self.values.get(key, default)

    get_bool = get_int = get_string

    def get_module_id(self, name):
        return name


@pytest.fixture()
def on_aws(world, monkeypatch):
    """the cleanup table, the notifications queue and SES, with the cluster manager reading it"""
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.delenv('AWS_CONFIG_FILE', raising=False)
    # the table's own clock is the test's
    monkeypatch.setattr(
        store_module, 'time', SimpleNamespace(time=lambda: world.now.timestamp())
    )
    with mock_aws():
        dynamodb = boto3.resource('dynamodb')
        store = ScratchCleanupStore(world.context)
        dynamodb.create_table(
            TableName=store.table_name(),
            AttributeDefinitions=[
                {'AttributeName': 'pk', 'AttributeType': 'S'},
                {'AttributeName': 'sk', 'AttributeType': 'S'},
            ],
            KeySchema=[
                {'AttributeName': 'pk', 'KeyType': 'HASH'},
                {'AttributeName': 'sk', 'KeyType': 'RANGE'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        store.table = dynamodb.Table(store.table_name())
        world.store = world.service.store = store

        sqs = boto3.client('sqs')
        queue = sqs.create_queue(
            QueueName='notifications.fifo', Attributes={'FifoQueue': 'true'}
        )['QueueUrl']
        ses = boto3.client('ses')
        config = Config(
            {
                'cluster-manager.notifications_queue_url': queue,
                'cluster.ses.enabled': True,
                'cluster.ses.sender_email': SENDER,
                'cluster.ses.region': 'us-east-1',
                'cluster.ses.max_sending_rate': 1000,
                'cluster-manager.notifications.email.enabled': True,
            }
        )
        aws = SimpleNamespace(
            sqs=lambda: sqs,
            ses=lambda region_name: ses,
            dynamodb_table=lambda: dynamodb,
        )
        shared = dict(
            logger=lambda name: Mock(),
            config=lambda: config,
            aws=lambda: aws,
            cluster_name=lambda: 'idea-test',
        )
        world.context.notifications_client = NotificationsAsyncClient(
            SimpleNamespace(module_id=lambda: 'scheduler', **shared)
        )
        manager = NotificationsService(
            SimpleNamespace(service_registry=lambda: Mock(), **shared),
            accounts=SimpleNamespace(
                get_user=lambda username: SimpleNamespace(
                    enabled=True, email=f'{username}@example.com'
                )
            ),
            email_templates=SimpleNamespace(
                get_email_template=lambda request: SimpleNamespace(
                    template=SimpleNamespace(
                        subject='{{ first_count }} folders', body='{{ owner }}'
                    )
                )
            ),
        )

        def deliver():
            """what the cluster manager's queue listener does"""
            while True:
                messages = sqs.receive_message(
                    QueueUrl=queue, MaxNumberOfMessages=10
                ).get('Messages', [])
                if not messages:
                    return
                for message in messages:
                    manager.execute_notifications(message)

        world.ses = ses
        world.deliver = deliver
        yield world


def checks(world, days, every=timedelta(hours=6)):
    """the hourly check, coarser, with the cluster manager working the queue between"""
    removed = 0
    end = world.now + timedelta(days=days)
    while world.now < end:
        world.now += every
        removed += run(world)['removed_folders']
        world.deliver()
    return removed


def removals(world):
    return world.store.page_removals(100)[0]


# SCRATCH-3: a notice counts once SES takes it, not when it is queued


def test_a_digest_ses_refuses_tells_nobody_and_the_folder_stays(on_aws):
    world = on_aws
    digests_on(world)
    folder = due_in(world, days=1)
    run(world)
    world.deliver()
    assert checks(world, 35) == 0
    assert folder.exists(), 'SES refused every digest, so nobody was told'
    notices = next(iter(removals(world)[0]['targets'].values()))
    assert not notices.get('first_notice_at') and not notices.get('final_notice_at')
    assert any(
        'No digest reached SES for: alice' in str(call)
        for call in world.service._logger.error.call_args_list
    )


def test_a_digest_ses_takes_counts_from_then_and_the_folder_goes(on_aws):
    world = on_aws
    world.ses.verify_email_identity(EmailAddress=SENDER)
    digests_on(world)
    folder = due_in(world, days=1)
    run(world)
    world.deliver()
    assert checks(world, 21) == 1
    assert not folder.exists()
    assert not logged_alert(world)


# SCRATCH-1: kept folders keep their date, and must not fill every check


def test_kept_folders_do_not_hold_back_the_rest(on_aws, monkeypatch):
    world = on_aws
    monkeypatch.setattr(module, 'MAX_RECORDS_PER_CHECK', 2)
    # changed after their jobs ended, with earlier dates than the next one
    for name in ('100', '101'):
        job_folder(
            world,
            name,
            start_time=REAL_NOW - timedelta(days=5),
            end_time=REAL_NOW - timedelta(days=4),
        )
        put_event(
            world,
            world.state.records[name][0],
            remove_at=(LATER - timedelta(days=10)).isoformat(),
        )
    fresh = job_folder(world, '200')
    first = run(world)
    assert first['removed_folders'] == 0, 'the first check reads only the first two'
    world.now += timedelta(hours=1)
    assert run(world)['removed_folders'] == 1
    assert not fresh.exists()
    assert logged_alert(world), 'and the first said later ones wait'
    kept = {row['job_id']: row for row in removals(world)}
    assert set(kept) == {'100', '101'}
    assert {row['last_reason'] for row in kept.values()} == {
        Reason.CHANGED_AFTER_JOB.value
    }


# SCRATCH-2: an owner folder that cannot be read is not a folder that is gone


def _failing(real, name, error):
    def call(path, *args, **kwargs):
        if path == name and kwargs.get('dir_fd') is not None:
            raise OSError(error, os.strerror(error))
        return real(path, *args, **kwargs)

    return call


@pytest.mark.parametrize(
    'where, name', [('open', 'alice'), ('stat', '123')], ids=['owner', 'folder']
)
@pytest.mark.parametrize('error', [errno.EIO, errno.EACCES], ids=['eio', 'eacces'])
def test_a_read_error_keeps_the_record(world, monkeypatch, where, name, error):
    folder = job_folder(world)
    with monkeypatch.context() as patch:
        patch.setattr(os, where, _failing(getattr(os, where), name, error))
        outcome = run(world)
    assert outcome['removed_folders'] == 0 and folder.exists()
    assert world.store.removals, 'the record stays'
    assert run(world)['removed_folders'] == 1, 'and the next check finds it'
    assert not folder.exists()


def test_a_folder_that_is_gone_still_ends_its_record(world):
    shutil.rmtree(job_folder(world))
    run(world)
    assert not world.store.removals


# SCRATCH-4: jobs that finish before the table is open are recorded once it is


def test_a_job_finishing_before_the_table_opens_is_recorded_after(world):
    world.service._store_ready = False
    world.service.record_job_finished(base.finished_job())
    assert not world.store.removals
    world.service._record_waiting()
    assert len(world.store.removals) == 1


def test_the_service_exists_before_finished_jobs_are_processed(monkeypatch):
    from ideascheduler.app import scheduler_app

    seen = {}

    def job_monitor(context):
        seen['scratch_cleanup'] = getattr(context, 'scratch_cleanup', None)
        return Mock()

    for name in dir(scheduler_app):
        value = getattr(scheduler_app, name)
        if isinstance(value, type) and name not in (
            'SchedulerApp',
            'SocaClientOptions',
        ):
            monkeypatch.setattr(scheduler_app, name, Mock())
    monkeypatch.setattr(scheduler_app, 'JobMonitor', job_monitor)
    monkeypatch.setattr(scheduler_app.time, 'sleep', lambda _: None)
    context = Mock()
    del context.scratch_cleanup
    app = scheduler_app.SchedulerApp.__new__(scheduler_app.SchedulerApp)
    app.context, app.logger = context, Mock()
    app.app_initialize()
    assert isinstance(seen['scratch_cleanup'], module.ScratchCleanupService)


# SCRATCH-6: every way the hourly check stops reaches the alert


def test_settings_that_stop_cleanup_alert(world):
    world.config['scheduler.scratch_cleanup.success_retention_days'] = 7
    world.service._run_if_due()
    assert logged_alert(world)


def test_a_table_that_cannot_be_opened_alerts(world, monkeypatch):
    world.service._store_ready = False
    monkeypatch.setattr(
        world.store, 'initialize', Mock(side_effect=RuntimeError('AccessDenied'))
    )
    assert not world.service._ensure_store()
    assert logged_alert(world)


def test_a_loop_that_fails_alerts(world):
    world.switch_error = RuntimeError('throttled')
    world.service._stop.set()
    world.service._loop()
    assert any(
        module.ALERT_MARKER in str(call)
        for call in world.service._logger.exception.call_args_list
    )
