"""The table's lease and audit rows on a real DynamoDB model, and who can reach the API."""

from unittest.mock import Mock
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
from ideascheduler.app.scratch_cleanup.store import LeaseLost, ScratchCleanupStore

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
