"""
The record of finished jobs: what goes on it, and its reads on a real DynamoDB model. The table's
lease and audit rows, and who can reach the API.
"""

from types import SimpleNamespace
from unittest.mock import Mock
import errno
import os
import shutil

import boto3
import pytest
from moto import mock_aws

from ideadatamodel import errorcodes, exceptions
from ideadatamodel.scheduler import (
    ListScratchCleanupPreviewRequest,
    PreviewScratchCleanupRequest,
    BackfillScratchCleanupRequest,
)
from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI
from ideascheduler.app.scratch_cleanup import service as module
from ideascheduler.app.scratch_cleanup.store import LeaseLost, ScratchCleanupStore
import test_scratch_cleanup_service as base
from test_scratch_cleanup_service import (
    finished_job,
    job_folder,
    logged_alert,
    put_event,
    record,
    run,
)

world = base.world

NAMESPACES = [
    'SchedulerAdmin.GetScratchCleanupStatus',
    'SchedulerAdmin.PreviewScratchCleanup',
    'SchedulerAdmin.ListScratchCleanupPreview',
    'SchedulerAdmin.BackfillScratchCleanup',
    'SchedulerAdmin.ListScratchCleanupUpcoming',
    'SchedulerAdmin.ListScratchCleanupAudit',
]


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        resource.create_table(
            TableName='c.scheduler.scratch-cleanup',
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
        instance = ScratchCleanupStore(Mock())
        instance.table = resource.Table('c.scheduler.scratch-cleanup')
        yield instance


def test_one_holder_at_a_time(store):
    assert store.acquire_lease('a')
    assert not store.acquire_lease('b')
    assert store.acquire_lease('a'), 'the holder can take it again'
    store.release_lease('b')
    assert not store.acquire_lease('b'), 'another holder cannot release it'
    store.release_lease('a')
    assert store.acquire_lease('b')


def test_an_expired_lease_can_be_taken_and_its_old_holder_loses_it(store):
    assert store.acquire_lease('a')
    store.table.update_item(
        Key={'pk': 'lease', 'sk': 'run'},
        UpdateExpression='SET lease_until = :past',
        ExpressionAttributeValues={':past': 1},
    )
    assert store.acquire_lease('b')
    with pytest.raises(LeaseLost):
        store.renew_lease('a')
    store.renew_lease('b')


def test_audit_rows_update_in_place_and_list_newest_first(store):
    row = {
        'path': '/l/a/1',
        'started_at': '2026-11-06T00:00:00+00:00',
        'status': 'started',
        'device': 1,
        'inode': 2**62,
        'size_bytes': 5,
    }
    store.put_audit(row)
    assert (1, 2**62) in store.unfinished_removals()
    store.put_audit({**row, 'status': 'removed', 'finished_at': 'x', 'gone': None})
    assert store.unfinished_removals() == {}
    store.put_audit(
        {**row, 'path': '/l/a/2', 'started_at': '2026-11-07T00:00:00+00:00'}
    )
    rows, _ = store.list_audit()
    assert [r['path'] for r in rows] == ['/l/a/2', '/l/a/1']


def test_runs_are_kept_with_floats_and_newest_first(store):
    store.put_run(
        {'run_id': '1', 'started_at': '2026-01-01', 'used_percent_before': 91.5}
    )
    store.put_run({'run_id': '2', 'started_at': '2026-01-02'})
    runs = store.list_runs()
    assert [r['run_id'] for r in runs] == ['2', '1']
    assert runs[1]['used_percent_before'] == 91.5
    assert 'pk' not in runs[0]


def test_what_went_waits_for_the_next_digest(store):
    store.put_removed(
        'alice#/l/alice/1', {'owner': 'alice', 'size_bytes': 5, 'partly': True}
    )
    assert store.list_removed()[0]['partly'] is True
    store.delete_removed('alice#/l/alice/1')
    assert store.list_removed() == []


def test_what_stopped_waits_for_the_next_summary(store):
    store.put_failure('scratch:' + 'x' * 400, {'message': 'm'})
    store.put_failure('scratch:' + 'x' * 400, {'message': 'm'})
    rows = store.list_failures()
    assert len(rows) == 1, 'the same failure twice is one line'
    store.delete_failure(rows[0]['sk'])
    assert store.list_failures() == []


def test_an_alert_is_sent_once_for_its_window(store):
    assert store.should_alert('reason')
    assert not store.should_alert('reason')
    assert store.should_alert('another')
    assert store.should_alert('reason', every_seconds=-1)


def test_no_request_can_name_a_path():
    for request in (
        PreviewScratchCleanupRequest,
        BackfillScratchCleanupRequest,
        ListScratchCleanupPreviewRequest,
    ):
        assert not {'path', 'paths', 'folder', 'mount_dir'} & set(request.model_fields)
    # an extra field sent anyway is dropped, not used
    assert not hasattr(BackfillScratchCleanupRequest(path='/etc'), 'path') or (
        BackfillScratchCleanupRequest(path='/etc').model_dump().get('path') is None
    )


class Invocation:
    def __init__(self, namespace, elevated):
        self.namespace = namespace
        self.elevated = elevated
        self.result = None
        self.seen = None

    def is_authorized(self, elevated_access=False, scopes=None):
        self.seen = (elevated_access, scopes)
        return self.elevated if not scopes else False

    def get_request_payload_as(self, kind):
        return kind()

    def get_username(self):
        return 'admin'

    def is_administrator(self):
        return True

    def success(self, result):
        self.result = result


def api(service=None):
    context = Mock()
    context.module_id.return_value = 'scheduler'
    context.scratch_cleanup = service
    return SchedulerAdminAPI(context=context)


@pytest.mark.parametrize('namespace', NAMESPACES)
def test_only_an_elevated_user_reaches_cleanup(namespace):
    service = Mock()
    invocation = Invocation(namespace, elevated=False)
    with pytest.raises(exceptions.SocaException) as raised:
        api(service).invoke(invocation)
    assert raised.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert invocation.seen == (True, None), 'no scope lets an application token through'
    assert not service.mock_calls


def test_status_reaches_an_elevated_user_with_dates_and_rows():
    service = Mock()
    service.status.return_value = {
        'available': True,
        'file_systems': [{'name': 'scratch', 'used_percent': 97.1}],
        'runs': [
            {
                'run_id': 'r',
                'started_at': '2026-11-06T00:00:00+00:00',
                'status': 'stopped',
            }
        ],
        'previews': [],
        'settings': {'enabled': False, 'file_systems': []},
        'progress': None,
    }
    invocation = Invocation(NAMESPACES[0], elevated=True)
    api(service).invoke(invocation)
    assert invocation.result.file_systems[0].used_percent == 97.1
    assert invocation.result.runs[0].status == 'stopped'


def test_status_with_no_service_says_unavailable():
    invocation = Invocation(NAMESPACES[0], elevated=True)
    api(None).invoke(invocation)
    assert invocation.result.available is False


def test_a_backfill_passes_only_the_preview_id_and_phrase():
    service = Mock()
    service.start_backfill.return_value = 'run-1'
    invocation = Invocation('SchedulerAdmin.BackfillScratchCleanup', elevated=True)
    invocation.get_request_payload_as = lambda kind: kind(
        preview_id='p', confirm_phrase='schedule 1'
    )
    api(service).invoke(invocation)
    service.start_backfill.assert_called_once_with('p', 'schedule 1', 'admin')
    assert invocation.result.run_id == 'run-1'


def test_listing_uses_only_preview_id_action_and_page():
    service = Mock()
    service.list_preview.return_value = ([{'path': '/l/a/1', 'action': 'remove'}], 1)
    invocation = Invocation('SchedulerAdmin.ListScratchCleanupPreview', elevated=True)
    invocation.get_request_payload_as = lambda kind: kind(
        preview_id='p', action='remove'
    )
    api(service).invoke(invocation)
    assert invocation.result.listing[0].path == '/l/a/1'
    assert invocation.result.paginator.total == 1


@pytest.mark.parametrize(
    'namespace',
    [
        'SchedulerAdmin.PreviewScratchCleanup',
        'SchedulerAdmin.ListScratchCleanupPreview',
        'SchedulerAdmin.BackfillScratchCleanup',
    ],
)
def test_a_manager_cannot_walk_or_arm_every_job_folder(namespace):
    service = Mock()
    invocation = Invocation(namespace, elevated=True)
    invocation.is_administrator = lambda: False
    with pytest.raises(exceptions.SocaException):
        api(service).invoke(invocation)
    assert not service.start_preview.called
    assert not service.list_preview.called
    assert not service.start_backfill.called


def test_a_preview_request_cannot_ask_past_the_active_owner_rule():
    service = Mock()
    invocation = Invocation('SchedulerAdmin.PreviewScratchCleanup', elevated=True)
    invocation.get_request_payload_as = lambda kind: kind(file_system='scratch')
    api(service).invoke(invocation)
    service.start_preview.assert_called_once_with('scratch', requested_by='admin')
    assert 'include_active_owners' not in PreviewScratchCleanupRequest.model_fields


def removal_rows(world):
    return list(world.store.removals.values())


@pytest.mark.parametrize(
    'setting', [{'enabled': False}, {'kill_switch': True}], ids=['off', 'kill switch']
)
def test_nothing_is_written_unless_armed(world, setting):
    world.config.update(
        {f'scheduler.scratch_cleanup.{k}': v for k, v in setting.items()}
    )
    world.service.record_job_finished(finished_job())
    assert not world.store.removals


def test_off_does_not_even_read_the_table(world):
    world.config['scheduler.scratch_cleanup.enabled'] = False
    table = world.context.aws().dynamodb_table().Table()
    table.get_item.reset_mock()
    world.store.get_removal = Mock(side_effect=AssertionError('read the record'))
    world.service.record_job_finished(finished_job())
    assert not table.get_item.called


def test_the_page_does_not_read_the_record_whole(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(
        world.store, 'list_due', Mock(side_effect=AssertionError('read it all'))
    )
    assert world.service.status()['scheduled_folders'] == 1


@pytest.fixture()
def real_store(monkeypatch):
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.delenv('AWS_CONFIG_FILE', raising=False)
    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        resource.create_table(
            TableName='t',
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
        instance = ScratchCleanupStore(Mock())
        instance.table = resource.Table('t')
        yield instance


def removal(day, **more):
    return {
        'job_id': '1',
        'owner': 'a',
        'remove_at': f'2026-12-{day:02d}T00:00:00',
        **more,
    }


def test_due_reads_stop_at_the_date_oldest_first(real_store):
    for day in range(1, 11):
        real_store.put_removal(f'k{day}', removal(day))
    now = '2026-12-01T00:00:00'
    due = real_store.list_due('2026-12-05T12:00:00', now=now)
    assert [row['key'] for row in due] == ['k1', 'k2', 'k3', 'k4', 'k5']


def test_due_reads_pass_over_a_kept_record_only_when_asked(real_store):
    real_store.put_removal('k1', removal(1, next_check_at='2026-12-02T00:00:00'))
    real_store.put_removal('k2', removal(2))
    now = '2026-12-01T00:00:00'
    assert [row['key'] for row in real_store.list_due('2026-12-31', now=now)] == ['k2']
    assert [row['key'] for row in real_store.list_due('2026-12-31')] == ['k1', 'k2']


def test_a_new_date_replaces_the_old_row(real_store):
    real_store.put_removal('k', removal(1))
    real_store.put_removal('k', removal(9, outcome='failed'))
    assert real_store.count_removals() == 1
    assert real_store.get_removal('k')['outcome'] == 'failed'
    assert real_store.has_removal('k')
    real_store.delete_removal('k')
    assert not real_store.has_removal('k')
    assert real_store.get_removal('k') is None
    assert real_store.count_removals() == 0


def test_counts_do_not_keep_rows(real_store):
    real_store.put_removal('a', removal(1, outcome='failed'))
    real_store.put_removal('b', removal(2, outcome='succeeded'))
    assert real_store.count_removals() == 2
    assert real_store.count_removals(outcome='failed') == 1


def test_upcoming_pages_with_a_cursor(real_store):
    for day in range(1, 6):
        real_store.put_removal(f'k{day}', removal(day))
    rows, cursor = real_store.page_removals(2)
    assert [row['key'] for row in rows] == ['k1', 'k2'] and cursor
    rows, _ = real_store.page_removals(2, cursor)
    assert [row['key'] for row in rows] == ['k3', 'k4']


@pytest.mark.parametrize(
    'namespace',
    ['SchedulerAdmin.PreviewScratchCleanup', 'SchedulerAdmin.BackfillScratchCleanup'],
)
def test_a_module_manager_cannot_preview_or_backfill(namespace):
    service = Mock()
    invocation = Invocation(namespace, elevated=True)
    invocation.is_administrator = lambda: False
    with pytest.raises(exceptions.SocaException):
        api(service).invoke(invocation)
    assert not service.mock_calls


def test_a_second_file_system_not_mounted_does_not_stop_the_check(world):
    world.config['shared-storage']['team'] = {
        'provider': 'fsx_lustre',
        'mount_dir': str(world.mount) + '-absent',
    }
    world.config['scheduler.scratch_cleanup.file_systems'] = ['scratch', 'team']
    put_event(world, record('555'))
    outcome = run(world)
    assert outcome['status'] != 'failed', outcome.get('message')


def test_subjobs_of_one_array_do_not_replace_each_others_record(world):
    world.service.record_job_finished(finished_job('77[1]', uid='shared'))
    world.service.record_job_finished(finished_job('77[2]', uid='shared'))
    world.service.record_job_finished(finished_job('77[]', uid='shared'))
    assert {row['job_id'] for row in removal_rows(world)} == {'77[1]', '77[2]', '77[]'}


def test_a_cursor_with_a_bad_key_is_refused(world):
    import base64
    import json

    cursor = base64.urlsafe_b64encode(
        json.dumps({'pk': 'audit', 'sk': 5}).encode()
    ).decode()
    with pytest.raises(exceptions.SocaException):
        world.service.list_audit(10, cursor)


def test_a_job_relearned_after_a_task_loss_goes_on_the_record():
    from ideascheduler.app.provisioning.job_monitor import (
        finished_job_processor as path,
    )

    context = Mock()
    processor = path.FinishedJobProcessor.__new__(path.FinishedJobProcessor)
    processor._context = context
    processor._logger = Mock()
    indexed = SimpleNamespace(job_uid='u')
    context.job_cache.get_completed_job_by_uid.return_value = None
    context.document_store.get_job.return_value = indexed
    assert processor._recorded_job('u') is indexed
    context.scratch_cleanup.record_job_finished.assert_called_once_with(indexed)


def test_the_finished_job_path_never_opens_the_table(world):
    world.service._store_ready = False
    world.store.initialize = Mock(side_effect=AssertionError('blocked finishing a job'))
    world.service.record_job_finished(finished_job())
    assert not world.store.removals


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
    assert any(
        f'Could not read {world.mount}/alice' in str(call)
        for call in world.service._logger.error.call_args_list
    ), 'and an administrator hears of it'
    assert run(world)['removed_folders'] == 1, 'and the next check finds it'
    assert not folder.exists()


def test_a_folder_that_is_gone_still_ends_its_record(world):
    shutil.rmtree(job_folder(world))
    run(world)
    assert not world.store.removals


def test_a_job_finishing_before_the_table_opens_is_recorded_after(world):
    world.service._store_ready = False
    world.service.record_job_finished(base.finished_job())
    assert not world.store.removals
    world.service._record_waiting()
    assert len(world.store.removals) == 1


def test_a_waiting_job_that_cannot_be_recorded_holds_up_no_other(world, monkeypatch):
    world.service._store_ready = False
    broken, fine = base.finished_job('7'), base.finished_job('8')
    world.service.record_job_finished(broken)
    world.service.record_job_finished(fine)
    real = world.store.get_removal

    def get_removal(key):
        if key == module.job_key(broken):
            raise RuntimeError('ValidationException')
        return real(key)

    monkeypatch.setattr(world.store, 'get_removal', get_removal)
    world.service._record_waiting()
    assert [row['job_id'] for row in world.store.removals.values()] == ['8']
    assert not world.service._waiting
    assert any(
        module.ALERT_MARKER in str(call)
        for call in world.service._logger.exception.call_args_list
    )


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
