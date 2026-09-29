"""Snapshot publication, isolation and cursor integrity."""

import copy
import time
from types import SimpleNamespace

import pytest

from ideadatamodel import exceptions
from ideaclustermanager.app.reporting.snapshot_store import SnapshotStore, sort_rows


class MemoryTable:
    def __init__(self):
        self.items = {}
        self.writes = []

    def put_item(self, Item, **kwargs):
        self.writes.append(copy.deepcopy(Item))
        self.items[(Item['snapshot_id'], Item['record'])] = copy.deepcopy(Item)

    def get_item(self, Key, **kwargs):
        return {
            'Item': copy.deepcopy(self.items.get((Key['snapshot_id'], Key['record'])))
        }


def store(table=None, cluster=''):
    result = SnapshotStore(SimpleNamespace(cluster_name=lambda: cluster))
    result.table = table or MemoryTable()
    return result


def publish(cache, rows=None):
    return cache.publish(
        'reader',
        {},
        {'user': rows or [], 'project': [], 'facet': []},
        time.monotonic() + 30,
    )


def test_metadata_is_last_and_all_chunks_are_bounded():
    cache = store()
    result = publish(cache, [{'key': str(i), 'label': 'x' * 1000} for i in range(1000)])
    assert cache.table.writes[-1]['record'] == 'metadata'
    assert len(cache.table.writes) > 3
    assert all(len(item['payload'].encode()) <= 240000 for item in cache.table.writes)
    assert all(item['expires_at'] for item in cache.table.writes)
    metadata = cache.lookup(result['snapshot_id'], 'reader', lambda: True)
    assert len(cache.rows(result['snapshot_id'], metadata, 'user')) == 1000


def test_failed_chunk_does_not_publish_metadata():
    cache = store()
    original = cache.table.put_item

    def put(Item, **kwargs):
        if cache.table.writes:
            raise RuntimeError('write failed')
        original(Item, **kwargs)

    cache.table.put_item = put
    with pytest.raises(RuntimeError):
        publish(cache, [{'key': str(i), 'label': 'x' * 1000} for i in range(500)])
    assert not any(item['record'] == 'metadata' for item in cache.table.writes)


def test_expiry_is_enforced_before_ttl_deletion(monkeypatch):
    cache = store()
    result = publish(cache)
    metadata = cache.lookup(result['snapshot_id'], 'reader', lambda: True)
    monkeypatch.setattr(time, 'time', lambda: metadata['expires'])
    with pytest.raises(exceptions.SocaException, match='expired'):
        cache.lookup(result['snapshot_id'], 'reader', lambda: True)
    assert cache.table.items


@pytest.mark.parametrize(
    'actor,cluster,allowed',
    [('other', '', True), ('reader', 'other', True), ('reader', '', False)],
)
def test_actor_cluster_and_revoked_grant_are_isolated(actor, cluster, allowed):
    cache = store()
    result = publish(cache)
    replica = store(cache.table, cluster)
    with pytest.raises(exceptions.SocaException):
        replica.lookup(result['snapshot_id'], actor, lambda: allowed)


def test_replicas_share_signing_secret_and_reject_cursor_changes():
    cache = store()
    result = publish(cache)
    binding = dict(
        actor='reader',
        snapshot_id=result['snapshot_id'],
        table='user',
        sort_by='label',
        descending=False,
    )
    first = cache.lookup(result['snapshot_id'], 'reader', lambda: True)
    replica = store(cache.table)
    second = replica.lookup(result['snapshot_id'], 'reader', lambda: True)
    cursor = cache.cursor(first, binding, 50)
    assert replica.offset(second, binding, cursor) == 50
    for changed in (
        dict(binding, actor='other'),
        dict(binding, table='project'),
        dict(binding, descending=True),
        dict(binding, snapshot_id='other'),
        dict(binding, sort_by='key'),
    ):
        with pytest.raises(exceptions.SocaException):
            replica.offset(second, changed, cursor)
    with pytest.raises(exceptions.SocaException):
        replica.offset(second, binding, 'AAAA' + cursor[4:])


@pytest.mark.parametrize(
    'descending,expected', [(False, ['c', 'a', 'b', 'd']), (True, ['a', 'b', 'c', 'd'])]
)
def test_sort_is_numeric_stable_and_null_last(descending, expected):
    rows = [
        dict(key='b', spend_total='10'),
        dict(key='d', spend_total=None),
        dict(key='c', spend_total='2'),
        dict(key='a', spend_total='10'),
    ]
    assert [r['key'] for r in sort_rows(rows, 'spend_total', descending)] == expected


def test_row_and_deadline_bounds_publish_nothing():
    cache = store()
    with pytest.raises(exceptions.SocaException) as error:
        publish(cache, [{'key': str(i)} for i in range(10001)])
    assert error.value.error_code == 'REPORT_TOO_LARGE'
    with pytest.raises(exceptions.SocaException) as error:
        cache.publish(
            'reader', {}, dict(user=[], project=[], facet=[]), time.monotonic() - 1
        )
    assert error.value.error_code == 'REPORT_TIMEOUT'
    assert not cache.table.writes


def test_startup_handles_simultaneous_replica_table_creation():
    from unittest.mock import Mock
    from botocore.exceptions import ClientError

    utility = Mock()
    utility.dynamodb_check_table_exists.side_effect = [False, True]
    utility.dynamodb_create_table.side_effect = ClientError(
        {'Error': {'Code': 'ResourceInUseException'}}, 'CreateTable'
    )
    aws = Mock()
    cache = SnapshotStore(
        SimpleNamespace(
            cluster_name=lambda: '', aws_util=lambda: utility, aws=lambda: aws
        )
    )
    cache.initialize()
    assert utility.dynamodb_check_table_exists.call_count == 2
    request = utility.dynamodb_create_table.call_args.kwargs
    assert request['ttl'] and request['ttl_attribute_name'] == 'expires_at'
    assert request['create_table_request']['KeySchema'] == [
        {'AttributeName': 'snapshot_id', 'KeyType': 'HASH'},
        {'AttributeName': 'record', 'KeyType': 'RANGE'},
    ]
    assert cache.table is aws.dynamodb_table().Table.return_value


def test_revocation_is_checked_before_any_snapshot_read():
    from unittest.mock import Mock

    cache = store()
    cache.table.get_item = Mock(side_effect=AssertionError('Read before authorization'))
    with pytest.raises(exceptions.SocaException):
        cache.lookup('opaque', 'reader', lambda: False)
    cache.table.get_item.assert_not_called()
