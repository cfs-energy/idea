"""Digests: each folder's two notices, counted once SES takes them. Against the real service over a real tree."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
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
from ideascheduler.app.scratch_cleanup.rules import ActiveJobs, Reason
from ideascheduler.app.scratch_cleanup.store import ScratchCleanupStore
import test_scratch_cleanup_service as base
from test_scratch_cleanup_service import (
    LATER,
    REAL_NOW,
    digests,
    digests_on,
    due_in,
    folder_on,
    job_folder,
    logged_alert,
    put_event,
    run,
    run_on,
    second_file_system,
    told_about,
    week,
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


def ses_sent(world):
    return world.ses.get_send_quota()['SentLast24Hours']


def test_there_is_no_setting_that_skips_notices(world):
    world.config['scheduler.scratch_cleanup.notify_owners'] = False
    folder = job_folder(world, with_notices=False)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


@pytest.mark.parametrize(
    'broken',
    [
        lambda world: world.config.update({'cluster.ses.enabled': False}),
        lambda world: world.config.update(
            {'cluster-manager.notifications.email.enabled': False}
        ),
    ],
    ids=['ses off', 'email off'],
)
def test_delivery_off_pauses_removal_even_past_every_date(world, broken):
    digests_on(world)
    broken(world)
    folder = job_folder(world, with_notices=False)
    assert run(world)['removed_folders'] == 0
    week(world, 20)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_digest_day_hiccup_retries_on_the_next_check(world):
    # the check an hour on is still the digest day, whatever hour the suite runs at
    world.now = world.now.replace(hour=12)
    digests_on(world)
    due_in(world, days=10)
    world.state.records_error = module.StateUnavailable('index busy')
    assert run(world)['noticed_folders'] == 0
    world.state.records_error = None
    world.now += timedelta(hours=1)
    # still the digest day, whatever hour this runs: an hour on can be tomorrow
    world.config['scheduler.scratch_cleanup.digest_weekday'] = world.now.weekday()
    assert run(world)['noticed_folders'] == 1
    assert len(digests(world)) == 1


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


def test_a_digest_still_on_its_way_is_not_sent_again(on_aws):
    world = on_aws
    world.ses.verify_email_identity(EmailAddress=SENDER)
    digests_on(world)
    due_in(world, days=10)
    assert run(world)['noticed_folders'] == 1
    # the same day's digest tried again, before the cluster manager has worked the queue
    world.store.release_alert('digest:scratch')
    world.now += timedelta(hours=1)
    assert run(world)['noticed_folders'] == 0
    world.deliver()
    assert ses_sent(world) == 1


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


def test_folders_that_wait_for_their_owner_hold_back_no_one(on_aws, monkeypatch):
    world = on_aws
    world.ses.verify_email_identity(EmailAddress=SENDER)
    monkeypatch.setattr(module, 'MAX_RECORDS_PER_CHECK', 2)
    digests_on(world)
    # never told, with the earliest dates: bob cannot be told, carol always has a job running
    world.disabled.add('bob')
    for name in ('100', '101'):
        due_in(world, name=name, owner='bob', days=-60)
    for name in ('102', '103'):
        due_in(world, name=name, owner='carol', days=-59)
    world.state.active = ActiveJobs(owners=frozenset({'carol'}))
    fresh = due_in(world, name='200', days=10)
    assert checks(world, 21) == 1
    assert not fresh.exists(), 'alice was told twice and her folder went'
    assert {row['owner'] for row in removals(world)} == {'bob', 'carol'}


def test_a_folder_kept_on_one_file_system_is_still_told_of_on_another(on_aws, tmp_path):
    world = on_aws
    world.ses.verify_email_identity(EmailAddress=SENDER)
    digests_on(world)
    other = second_file_system(world, tmp_path)
    job_folder(world)
    folder_on(other)
    # told twice on scratch, kept there an hour ago: not asked about again until tomorrow
    put_event(
        world,
        world.state.records['123'][0],
        remove_at=(world.now + timedelta(days=3)).isoformat(),
        targets=told_about(world, '123'),
        next_check_at=(world.now + timedelta(hours=23)).isoformat(),
        last_reason=Reason.CHANGED_AFTER_JOB.value,
    )
    assert run_on(world, 'other')['noticed_folders'] == 1


def test_a_folder_whose_owner_cannot_be_told_uses_none_of_the_digest_walk(
    world, monkeypatch
):
    # one folder's worth: bob's, the oldest, would use all of it every week
    monkeypatch.setattr(module, 'SCAN_BUDGET_ENTRIES', 3)
    digests_on(world)
    world.disabled.add('bob')
    waiting = due_in(world, name='100', owner='bob', days=-60)
    shutil.rmtree(due_in(world, name='101', owner='bob', days=-60))
    due_in(world, name='200', days=10)
    outcome = run(world)
    assert outcome['noticed_folders'] == 1, 'alice is told'
    assert 'their owner has no enabled account' in outcome['message']
    assert 'bob' in outcome['message'] and logged_alert(world)
    assert waiting.exists()
    left = {row['job_id'] for row in world.store.removals.values()}
    assert left == {'100', '200'}, 'a record of bob whose folder is gone still ends'


def test_a_digest_that_runs_out_of_walk_says_so(world, monkeypatch):
    monkeypatch.setattr(module, 'SCAN_BUDGET_ENTRIES', 3)
    digests_on(world)
    due_in(world, name='100', days=9)
    due_in(world, name='200', days=10)
    outcome = run(world)
    assert outcome['noticed_folders'] == 1
    assert '1 folders were not checked' in (outcome.get('message') or '')
    assert logged_alert(world)


def test_a_record_gone_here_and_waiting_on_another_file_system_holds_back_no_one(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(module, 'MAX_RECORDS_PER_CHECK', 2)
    world.config['scheduler.scratch_cleanup.digest_weekday'] = (base.DAY + 1) % 7
    other = second_file_system(world, tmp_path)
    # told twice here and gone from here, still on other and not yet told there
    for name in ('100', '101'):
        here = job_folder(world, name)
        made = world.state.records[name][0]
        put_event(world, made, remove_at=(LATER - timedelta(days=40)).isoformat())
        folder_on(other, name=name)
        shutil.rmtree(here)
    fresh = job_folder(world, '200')
    removed = 0
    for _ in range(3):
        world.now += timedelta(hours=1)
        removed += run_on(world, 'scratch')['removed_folders']
    assert removed == 1 and not fresh.exists()
    left = {row['job_id']: row for row in world.store.removals.values()}
    assert set(left) == {'100', '101'}, 'their folders on other still wait'
    assert not any(
        key.startswith('scratch|') for row in left.values() for key in row['targets']
    ), 'what they were told about here is gone'
