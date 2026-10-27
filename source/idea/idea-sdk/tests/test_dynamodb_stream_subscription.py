from unittest.mock import Mock

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber
from botocore.exceptions import ClientError

from ideasdk.dynamodb.dynamodb_stream_subscription import DynamoDBStreamSubscription


def error(code):
    return ClientError(
        {'Error': {'Code': code, 'Message': 'shard unavailable'}}, 'Streams'
    )


@pytest.fixture
def subscription(monkeypatch):
    monkeypatch.setattr('threading.Thread.start', lambda self: None)
    monkeypatch.setattr('ideasdk.utils.Utils.create_boto_session', Mock())
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    result = DynamoDBStreamSubscription(Mock(), 'settings', 'us-east-1', logger=Mock())
    result.stream_arn = 'stream'
    result.dynamodb_streams_client = Mock()
    return result


def once(subscription, method):
    subscription._exit = Mock()
    subscription._exit.is_set.return_value = False
    subscription._exit.wait.side_effect = lambda seconds: setattr(
        subscription._exit.is_set, 'return_value', True
    )
    method()


def record(value, sequence='1'):
    return {
        'eventName': 'MODIFY',
        'dynamodb': {
            'SequenceNumber': sequence,
            'OldImage': {'value': {'S': 'previous'}},
            'NewImage': {'value': {'S': value}},
        },
    }


def test_child_waits_until_parent_is_drained(subscription):
    streams = subscription.dynamodb_streams_client
    subscription._initialized = True
    subscription.shard_iterators_map = {'parent': 'iterator'}
    streams.describe_stream.return_value = {
        'StreamDescription': {
            'Shards': [
                {'ShardId': 'child', 'ParentShardId': 'parent'},
                {
                    'ShardId': 'parent',
                    'SequenceNumberRange': {'EndingSequenceNumber': '9'},
                },
            ]
        }
    }
    once(subscription, subscription.shard_iterator_initializer)
    streams.get_shard_iterator.assert_not_called()


def test_expired_iterator_resumes_after_last_delivered_record(subscription):
    streams = subscription.dynamodb_streams_client
    subscription._initialized = True
    subscription.shard_iterators_map = {'parent': 'iterator'}
    streams.get_records.side_effect = [
        {'Records': [record('first', '42')], 'NextShardIterator': 'expired'},
        error('ExpiredIteratorException'),
    ]
    once(subscription, subscription.shard_processor)
    streams.describe_stream.return_value = {
        'StreamDescription': {'Shards': [{'ShardId': 'parent'}]}
    }
    streams.get_shard_iterator.return_value = {'ShardIterator': 'renewed'}
    once(subscription, subscription.shard_iterator_initializer)
    streams.get_shard_iterator.assert_called_once_with(
        StreamArn='stream',
        ShardId='parent',
        ShardIteratorType='AFTER_SEQUENCE_NUMBER',
        SequenceNumber='42',
    )


class StreamSimulation:
    """In-memory AWS boundary; the subscription and deserializer run unchanged."""

    def __init__(self):
        self.shards = {}
        self.requests = []
        self.cursors = {}
        self.pages = []

    def add(self, shard_id, parent=None, closed=False, values=()):
        self.shards[shard_id] = {
            'parent': parent,
            'closed': closed,
            'records': [record(value, str(i + 1)) for i, value in enumerate(values)],
        }

    def describe_stream(self, **request):
        ids = list(self.shards)
        if self.pages:
            page = 0
            if 'ExclusiveStartShardId' in request:
                page = next(
                    i + 1
                    for i, ids in enumerate(self.pages)
                    if ids[-1] == request['ExclusiveStartShardId']
                )
            ids = self.pages[page]
        description = {'Shards': []}
        for shard_id in ids:
            shard = self.shards[shard_id]
            entry = {'ShardId': shard_id, 'SequenceNumberRange': {}}
            if shard['parent']:
                entry['ParentShardId'] = shard['parent']
            if shard['closed']:
                entry['SequenceNumberRange']['EndingSequenceNumber'] = '999'
            description['Shards'].append(entry)
        if self.pages and page < len(self.pages) - 1:
            description['LastEvaluatedShardId'] = ids[-1]
        return {'StreamDescription': description}

    def get_shard_iterator(self, **request):
        self.requests.append(request)
        shard_id = request['ShardId']
        records = self.shards[shard_id]['records']
        kind = request['ShardIteratorType']
        offset = len(records) if kind == 'LATEST' else 0
        if kind == 'AFTER_SEQUENCE_NUMBER':
            offset = int(request['SequenceNumber'])
        return {'ShardIterator': self.cursor(shard_id, offset)}

    def cursor(self, shard_id, offset):
        token = str(len(self.cursors))
        self.cursors[token] = (shard_id, offset)
        return token

    def get_records(self, ShardIterator, Limit):
        shard_id, offset = self.cursors[ShardIterator]
        shard = self.shards[shard_id]
        records = shard['records'][offset : offset + Limit]
        offset += len(records)
        return {
            'Records': records,
            'NextShardIterator': None
            if shard['closed'] and offset == len(shard['records'])
            else self.cursor(shard_id, offset),
        }


def delivered(subscription):
    return [
        call.args[1]['value']
        for call in subscription.stream_subscriber.on_update.call_args_list
    ]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('generations', [2, 3, 4])
def test_cold_start_skips_closed_history(subscription, reverse, generations):
    streams = StreamSimulation()
    ids = list(range(generations))
    for i in reversed(ids) if reverse else ids:
        streams.add(
            str(i), str(i - 1), closed=i < generations - 1, values=[f'stale-{i}']
        )
    # Exercise complete pagination, including child-before-parent responses.
    streams.pages = [[shard_id] for shard_id in streams.shards]
    subscription.dynamodb_streams_client = streams
    for _ in range(generations + 1):
        once(subscription, subscription.shard_iterator_initializer)
        once(subscription, subscription.shard_processor)
    assert delivered(subscription) == []
    assert [(r['ShardId'], r['ShardIteratorType']) for r in streams.requests] == [
        (str(generations - 1), 'LATEST')
    ]
    leaf = streams.shards[str(generations - 1)]
    leaf['records'].append(record('live', '2'))
    once(subscription, subscription.shard_processor)
    assert delivered(subscription) == ['live']


def test_live_split_and_late_descendants_are_parent_first(subscription):
    streams = StreamSimulation()
    streams.add('parent', values=['preboot'])
    subscription.dynamodb_streams_client = streams
    once(subscription, subscription.shard_iterator_initializer)
    streams.shards['parent']['records'].append(record('parent-live', '2'))
    streams.shards['parent']['closed'] = True
    # Deliberately return grandchildren before their still-unread parents.
    streams.add('grandchild', 'child-a', values=['grandchild-live'])
    streams.add('child-b', 'parent', values=['child-b-live'])
    streams.add('child-a', 'parent', closed=True, values=['child-a-live'])
    once(subscription, subscription.shard_iterator_initializer)
    assert len(streams.requests) == 1
    for _ in range(4):
        once(subscription, subscription.shard_processor)
        once(subscription, subscription.shard_iterator_initializer)
    values = delivered(subscription)
    assert sorted(values) == sorted(
        ['parent-live', 'child-a-live', 'child-b-live', 'grandchild-live']
    )
    assert (
        values.index('parent-live')
        < values.index('child-a-live')
        < values.index('grandchild-live')
    )
    assert values.index('parent-live') < values.index('child-b-live')
    assert all(r['ShardIteratorType'] == 'TRIM_HORIZON' for r in streams.requests[1:])


def test_merge_free_close_waits_for_late_child(subscription):
    streams = StreamSimulation()
    streams.add('parent')
    subscription.dynamodb_streams_client = streams
    once(subscription, subscription.shard_iterator_initializer)
    streams.shards['parent']['closed'] = True
    once(subscription, subscription.shard_processor)
    once(subscription, subscription.shard_iterator_initializer)
    assert len(streams.requests) == 1
    streams.add('child', 'parent', values=['after-close'])
    once(subscription, subscription.shard_iterator_initializer)
    once(subscription, subscription.shard_processor)
    assert delivered(subscription) == ['after-close']


def test_restart_mid_chain_uses_fresh_baseline(subscription):
    streams = StreamSimulation()
    streams.add('parent')
    subscription.dynamodb_streams_client = streams
    once(subscription, subscription.shard_iterator_initializer)
    streams.shards['parent']['closed'] = True
    streams.add('child', 'parent', closed=True, values=['before-restart'])
    streams.add('leaf', 'child', values=['snapshot-value'])
    once(subscription, subscription.shard_processor)
    once(subscription, subscription.shard_iterator_initializer)
    # A fresh process loses all checkpoints and has a new table snapshot.
    restarted = DynamoDBStreamSubscription(
        Mock(), 'settings', 'us-east-1', logger=Mock()
    )
    restarted.stream_arn = 'stream'
    restarted.dynamodb_streams_client = streams
    streams.requests.clear()
    for _ in range(4):
        once(restarted, restarted.shard_iterator_initializer)
        once(restarted, restarted.shard_processor)
    assert delivered(restarted) == []
    assert [(r['ShardId'], r['ShardIteratorType']) for r in streams.requests] == [
        ('leaf', 'LATEST')
    ]


def add_scan(stub, items, *, consistent_read=True, start=None, last=None):
    serialize = TypeSerializer().serialize
    response = {
        'Items': [
            {key: serialize(value) for key, value in item.items()} for item in items
        ]
    }
    expected = {
        'TableName': 'test.cluster-settings',
        'ReturnConsumedCapacity': 'TOTAL',
        'ConsistentRead': consistent_read,
    }
    if start is not None:
        expected['ExclusiveStartKey'] = {'key': start}
    if last is not None:
        response['LastEvaluatedKey'] = {'key': serialize(last)}
    stub.add_response('scan', response, expected)


@pytest.fixture
def config_table(monkeypatch):
    resource = boto3.resource(
        'dynamodb',
        region_name='us-east-1',
        aws_access_key_id='testing',
        aws_secret_access_key='testing',
    )
    provider = Mock()
    provider.dynamodb.return_value.describe_table.return_value = {
        'Table': {'TableStatus': 'ACTIVE'}
    }
    provider.dynamodb_table.return_value = resource
    monkeypatch.setattr(
        'ideasdk.config.cluster_config_db.AwsClientProvider',
        Mock(return_value=provider),
    )
    with Stubber(resource.meta.client) as stub:
        yield resource.Table('test.cluster-settings'), stub
        stub.assert_no_pending_responses()


@pytest.fixture
def live_config(subscription, config_table):
    from ideasdk.config.cluster_config import ClusterConfig

    table, stub = config_table
    add_scan(
        stub,
        [
            {'key': 'maintenance', 'value': False},
            {'key': 'deleted', 'value': 'old'},
        ],
        consistent_read=False,
    )
    config = ClusterConfig('test', 'us-east-1', create_subscription=True, logger=Mock())
    sub = config.db.stream_subscription
    sub.set_logger(config.logger)
    sub.stream_arn = 'stream'
    streams = StreamSimulation()
    streams.add('parent')
    sub.dynamodb_streams_client = streams
    add_scan(
        stub,
        [
            {'key': 'maintenance', 'value': False},
            {'key': 'deleted', 'value': 'old'},
        ],
    )
    once(sub, sub.shard_iterator_initializer)
    return config, sub, streams, stub


@pytest.mark.parametrize('consistent_read', [False, True])
def test_snapshot_pagination_uses_scan_input_shape(config_table, consistent_read):
    from ideasdk.config.cluster_config_db import ClusterConfigDB

    table, stub = config_table
    db = ClusterConfigDB('test', 'us-east-1', None)
    add_scan(
        stub,
        [{'key': 'first', 'value': 'one'}],
        consistent_read=consistent_read,
        last='first',
    )
    add_scan(
        stub,
        [{'key': 'second', 'value': 'two'}],
        consistent_read=consistent_read,
        start='first',
    )
    assert db.build_config_from_db(consistent_read=consistent_read).as_dict() == {
        'first': 'one',
        'second': 'two',
    }


def test_delete_config_pagination_uses_scan_input_shape(config_table):
    from ideasdk.config.cluster_config_db import ClusterConfigDB

    table, stub = config_table
    db = ClusterConfigDB('test', 'us-east-1', None)
    stub.add_response(
        'scan',
        {
            'Items': [{'key': {'S': 'keep'}}],
            'LastEvaluatedKey': {'key': {'S': 'keep'}},
        },
        {'TableName': table.name},
    )
    stub.add_response(
        'scan',
        {
            'Items': [{'key': {'S': 'remove.this'}}],
        },
        {'TableName': table.name, 'ExclusiveStartKey': {'key': 'keep'}},
    )
    stub.add_response(
        'delete_item',
        {},
        {
            'TableName': table.name,
            'Key': {'key': 'remove.this'},
        },
    )
    db.delete_config_entries('remove.')


def test_expired_parent_missing_from_complete_topology_refreshes_snapshot(live_config):
    config, sub, streams, stub = live_config
    streams.shards['parent']['records'].append(record('delivered', '1'))
    # Checkpoint a real delivered record, then expire its iterator.
    streams.shards['parent']['records'][0]['dynamodb']['OldImage']['key'] = {
        'S': 'other'
    }
    streams.shards['parent']['records'][0]['dynamodb']['NewImage']['key'] = {
        'S': 'other'
    }
    once(sub, sub.shard_processor)
    poll = streams.get_records
    streams.get_records = Mock(side_effect=error('ExpiredIteratorException'))
    once(sub, sub.shard_processor)
    streams.get_records = poll
    del streams.shards['parent']
    streams.add('child', 'parent', closed=True, values=['stale'])
    streams.add('leaf', 'child', values=['snapshot'])
    streams.pages = [['leaf'], ['child']]
    add_scan(stub, [{'key': 'maintenance', 'value': True}], last='maintenance')
    add_scan(stub, [{'key': 'added', 'value': 'current'}], start='maintenance')
    once(sub, sub.shard_iterator_initializer)
    assert config.get_bool('maintenance') is True
    assert config.get('deleted') is None
    assert config.get('added') == 'current'
    assert [(r['ShardId'], r['ShardIteratorType']) for r in streams.requests] == [
        ('parent', 'LATEST'),
        ('leaf', 'LATEST'),
    ]
    assert 'parent' not in sub._shard_sequence_numbers
    event = record('next', '2')
    event['dynamodb']['OldImage']['key'] = {'S': 'added'}
    event['dynamodb']['NewImage']['key'] = {'S': 'added'}
    streams.shards['leaf']['records'].append(event)
    once(sub, sub.shard_processor)
    assert config.get('added') == 'next'


def test_checkpoint_parent_on_later_page_is_reacquired_before_child(subscription):
    streams = StreamSimulation()
    streams.add('parent')
    subscription.dynamodb_streams_client = streams
    once(subscription, subscription.shard_iterator_initializer)
    streams.shards['parent']['records'].append(record('first', '1'))
    once(subscription, subscription.shard_processor)
    poll = streams.get_records
    streams.get_records = Mock(side_effect=error('ExpiredIteratorException'))
    once(subscription, subscription.shard_processor)
    streams.get_records = poll
    streams.shards['parent']['closed'] = True
    streams.shards['parent']['records'].append(record('second', '2'))
    streams.add('child', 'parent', values=['third'])
    streams.pages = [['child'], ['parent']]
    once(subscription, subscription.shard_iterator_initializer)
    assert streams.requests[-1]['ShardIteratorType'] == 'AFTER_SEQUENCE_NUMBER'
    assert streams.requests[-1]['SequenceNumber'] == '1'
    once(subscription, subscription.shard_processor)
    once(subscription, subscription.shard_iterator_initializer)
    once(subscription, subscription.shard_processor)
    assert delivered(subscription) == ['first', 'second', 'third']
    subscription.stream_subscriber.on_reset.assert_called_once_with()


@pytest.mark.parametrize(
    'operation,code',
    [
        ('get_records', 'ResourceNotFoundException'),
        ('get_records', 'TrimmedDataAccessException'),
        ('get_records', 'ExpiredIteratorException'),
        ('get_shard_iterator', 'ResourceNotFoundException'),
        ('get_shard_iterator', 'TrimmedDataAccessException'),
    ],
)
def test_history_loss_reloads_before_polling(live_config, code, operation):
    config, sub, streams, stub = live_config
    if operation == 'get_shard_iterator':
        sub.shard_iterators_map.clear()
        sub._shard_sequence_numbers['parent'] = '42'
    original = getattr(streams, operation)
    setattr(streams, operation, Mock(side_effect=error(code)))
    method = (
        sub.shard_processor
        if operation == 'get_records'
        else sub.shard_iterator_initializer
    )
    once(sub, method)
    assert sub._resync_required
    setattr(streams, operation, original)
    if code == 'ResourceNotFoundException':
        sub.dynamodb_client = boto3.client(
            'dynamodb',
            region_name='us-east-1',
            aws_access_key_id='testing',
            aws_secret_access_key='testing',
        )
        table_stub = Stubber(sub.dynamodb_client)
        table_stub.add_response(
            'describe_table',
            {
                'Table': {
                    'LatestStreamArn': 'arn:aws:dynamodb:us-east-1:123456789012:table/test.cluster-settings/stream/replacement'
                }
            },
            {'TableName': sub.table_name},
        )
        table_stub.activate()
    # The first refresh fails on page two: preserve the old complete snapshot,
    # keep polling paused, and retry from fresh cursors on the next pass.
    add_scan(stub, [{'key': 'maintenance', 'value': True}], last='maintenance')
    stub.add_client_error(
        'scan',
        'InternalServerError',
        expected_params={
            'TableName': 'test.cluster-settings',
            'ReturnConsumedCapacity': 'TOTAL',
            'ConsistentRead': True,
            'ExclusiveStartKey': {'key': 'maintenance'},
        },
    )
    once(sub, sub.shard_iterator_initializer)
    sub.logger.exception.assert_any_call(
        'test.cluster-settings snapshot resync failed; stream processing remains paused'
    )
    assert config.get_bool('maintenance') is False
    assert config.get('deleted') == 'old'
    assert sub._resync_required
    streams.get_records = Mock(wraps=streams.get_records)
    once(sub, sub.shard_processor)
    streams.get_records.assert_not_called()
    add_scan(stub, [{'key': 'maintenance', 'value': True}])
    once(sub, sub.shard_iterator_initializer)
    assert config.get_bool('maintenance') is True
    assert config.get('deleted') is None
    assert not sub._resync_required
    sub.logger.info.assert_any_call(
        'test.cluster-settings snapshot resync started; stream processing paused'
    )
    sub.logger.info.assert_any_call(
        'test.cluster-settings snapshot resync finished; stream processing resumed'
    )
    once(sub, sub.shard_processor)
    streams.get_records.assert_called_once()
    assert streams.requests[-1]['ShardIteratorType'] == 'LATEST'


def test_failed_discovery_does_not_establish_partial_baseline(subscription):
    streams = subscription.dynamodb_streams_client
    streams.describe_stream.side_effect = [
        {
            'StreamDescription': {
                'Shards': [{'ShardId': 'child', 'ParentShardId': 'parent'}],
                'LastEvaluatedShardId': 'child',
            }
        },
        error('InternalServerError'),
    ]
    once(subscription, subscription.shard_iterator_initializer)
    assert not subscription._initialized
    streams.get_shard_iterator.assert_not_called()
    streams.describe_stream.side_effect = None
    streams.describe_stream.return_value = {
        'StreamDescription': {
            'Shards': [
                {'ShardId': 'child', 'ParentShardId': 'parent'},
                {
                    'ShardId': 'parent',
                    'SequenceNumberRange': {'EndingSequenceNumber': '42'},
                },
            ]
        }
    }
    streams.get_shard_iterator.return_value = {'ShardIterator': 'live'}
    once(subscription, subscription.shard_iterator_initializer)
    streams.get_shard_iterator.assert_called_once_with(
        StreamArn='stream', ShardId='child', ShardIteratorType='LATEST'
    )


def setting_record(value, sequence='1', key='maintenance'):
    event = record(value, sequence)
    event['dynamodb']['OldImage']['key'] = {'S': key}
    event['dynamodb']['NewImage'] = {
        'key': {'S': key},
        'value': TypeSerializer().serialize(value),
    }
    return event


def test_cold_start_rescans_after_latest_and_keeps_concurrent_write(
    subscription, config_table
):
    from ideasdk.config.cluster_config import ClusterConfig

    table, stub = config_table
    add_scan(stub, [{'key': 'maintenance', 'value': False}], consistent_read=False)
    config = ClusterConfig('test', 'us-east-1', create_subscription=True)
    sub = config.db.stream_subscription
    sub.stream_arn = 'stream'
    streams = StreamSimulation()
    streams.add('parent')
    # This write lands after the constructor's scan and before LATEST acquisition.
    streams.shards['parent']['records'].append(setting_record(True))
    sub.dynamodb_streams_client = streams
    add_scan(stub, [{'key': 'maintenance', 'value': True}])
    once(sub, sub.shard_iterator_initializer)
    assert config.get_bool('maintenance') is True
    assert streams.requests[0]['ShardIteratorType'] == 'LATEST'
    # A write after cursor acquisition must still arrive through the stream.
    streams.shards['parent']['records'].append(setting_record(False, '2'))
    once(sub, sub.shard_processor)
    assert config.get_bool('maintenance') is False
    assert not sub._resync_required


def test_fast_initializer_cannot_reset_an_unconstructed_config(
    subscription, config_table, monkeypatch
):
    from ideasdk.config.cluster_config import ClusterConfig

    table, stub = config_table
    streams = StreamSimulation()
    streams.add('parent', values=['preboot'])
    session = Mock()
    dynamodb = Mock()
    dynamodb.describe_table.return_value = {'Table': {'LatestStreamArn': 'stream'}}
    session.client.side_effect = lambda service_name, **kwargs: (
        streams if service_name == 'dynamodbstreams' else dynamodb
    )
    monkeypatch.setattr(
        'ideasdk.utils.Utils.create_boto_session', lambda *args: session
    )
    add_scan(stub, [{'key': 'maintenance', 'value': False}], consistent_read=False)
    add_scan(stub, [{'key': 'maintenance', 'value': True}])

    def start(thread):
        if thread.name.endswith('shard-iterator-initializer'):
            sub = thread._target.__self__
            once(sub, thread._target)

    # Run the real initializer at the earliest legal thread interleaving.
    monkeypatch.setattr('threading.Thread.start', start)
    config = ClusterConfig('test', 'us-east-1', create_subscription=True)
    assert config.get_bool('maintenance') is True
    assert not config.db.stream_subscription._resync_required


@pytest.mark.parametrize('after_history_loss', [False, True])
@pytest.mark.parametrize('closed_child', [False, True])
@pytest.mark.parametrize('failed_snapshot', [False, True])
def test_late_baseline_child_refreshes_without_replaying_stale_history(
    subscription,
    config_table,
    after_history_loss,
    closed_child,
    failed_snapshot,
):
    from ideasdk.config.cluster_config import ClusterConfig

    table, stub = config_table
    add_scan(stub, [{'key': 'maintenance', 'value': False}], consistent_read=False)
    config = ClusterConfig('test', 'us-east-1', create_subscription=True, logger=Mock())
    sub = config.db.stream_subscription
    sub.stream_arn = 'stream'
    streams = StreamSimulation()
    sub.dynamodb_streams_client = streams
    if after_history_loss:
        streams.add('expired')
        add_scan(stub, [{'key': 'maintenance', 'value': False}])
        once(sub, sub.shard_iterator_initializer)
        poll = streams.get_records
        streams.get_records = Mock(side_effect=error('TrimmedDataAccessException'))
        once(sub, sub.shard_processor)
        streams.get_records = poll
        streams.shards.clear()
        assert sub._resync_required

    # Closed-leaf metadata: the child exists but is not yet in DescribeStream.
    streams.add('parent', closed=True)
    add_scan(stub, [{'key': 'maintenance', 'value': False}])
    once(sub, sub.shard_iterator_initializer)
    assert not sub._resync_required
    streams.add('child', 'parent', closed=closed_child)
    streams.shards['child']['records'] = [
        setting_record(False),
        setting_record(True, '2'),
    ]
    leaf = 'child'
    if closed_child:
        streams.add('leaf', 'child')
        leaf = 'leaf'
        # Descendants precede parents, across DescribeStream pages.
        streams.pages = [['leaf'], ['child'], ['parent']]

    config.on_update = Mock(wraps=config.on_update)
    if failed_snapshot:
        stub.add_client_error(
            'scan',
            'InternalServerError',
            expected_params={
                'TableName': table.name,
                'ReturnConsumedCapacity': 'TOTAL',
                'ConsistentRead': True,
            },
        )
        once(sub, sub.shard_iterator_initializer)
        assert sub._resync_required
        once(sub, sub.shard_processor)
        config.on_update.assert_not_called()
        assert config.get_bool('maintenance') is False

    add_scan(stub, [{'key': 'maintenance', 'value': True}])

    def write_during_scan(**kwargs):
        assert streams.requests[-1]['ShardId'] == leaf
        assert streams.requests[-1]['ShardIteratorType'] == 'LATEST'
        records = streams.shards[leaf]['records']
        records.append(setting_record(False, str(len(records) + 1)))

    table.meta.client.meta.events.register(
        'before-parameter-build.dynamodb.Scan', write_during_scan
    )
    once(sub, sub.shard_iterator_initializer)
    table.meta.client.meta.events.unregister(
        'before-parameter-build.dynamodb.Scan', write_during_scan
    )
    assert config.get_bool('maintenance') is True
    assert not sub._resync_required
    once(sub, sub.shard_processor)
    # Only the concurrent write is delivered: old child history cannot roll back the snapshot.
    assert config.get_bool('maintenance') is False
    assert [call.args[1]['value'] for call in config.on_update.call_args_list] == [
        False
    ]
    for _ in range(3):
        once(sub, sub.shard_iterator_initializer)
        once(sub, sub.shard_processor)
    assert config.on_update.call_count == 1


def test_expired_baseline_child_reopens_checkpoint_without_reset(subscription):
    streams = StreamSimulation()
    streams.add('parent', closed=True)
    streams.add('child', 'parent')
    subscription.dynamodb_streams_client = streams
    once(subscription, subscription.shard_iterator_initializer)
    streams.shards['child']['records'].append(record('first', '1'))
    once(subscription, subscription.shard_processor)
    subscription.stream_subscriber.on_reset.reset_mock()
    poll = streams.get_records

    def expired(ShardIterator, Limit):
        raise error('ExpiredIteratorException')

    streams.get_records = expired
    once(subscription, subscription.shard_processor)
    streams.get_records = poll
    streams.shards['child']['records'].append(record('second', '2'))
    once(subscription, subscription.shard_iterator_initializer)
    assert streams.requests[-1]['ShardIteratorType'] == 'AFTER_SEQUENCE_NUMBER'
    assert streams.requests[-1]['SequenceNumber'] == '1'
    subscription.stream_subscriber.on_reset.assert_not_called()
    once(subscription, subscription.shard_processor)
    assert delivered(subscription) == ['first', 'second']


@pytest.mark.parametrize(
    'failure',
    ['describe_stream', 'get_shard_iterator', 'get_records', 'DISABLED', 'DISABLING'],
)
def test_replaced_stream_reloads_latest_arn_and_snapshot(live_config, failure):
    config, sub, simulation, snapshot = live_config
    old_arn = 'arn:aws:dynamodb:us-east-1:123456789012:table/test.cluster-settings/stream/2026-10-01T00:00:00.000'
    new_arn = old_arn.replace('10-01', '10-27')
    sub.stream_arn = old_arn
    kwargs = dict(
        region_name='us-east-1',
        aws_access_key_id='testing',
        aws_secret_access_key='testing',
    )
    sub.dynamodb_client = boto3.client('dynamodb', **kwargs)
    sub.dynamodb_streams_client = boto3.client('dynamodbstreams', **kwargs)
    with (
        Stubber(sub.dynamodb_client) as tables,
        Stubber(sub.dynamodb_streams_client) as streams,
    ):
        if failure == 'get_records':
            streams.add_client_error('get_records', 'ResourceNotFoundException')
            once(sub, sub.shard_processor)
        elif failure == 'get_shard_iterator':
            sub.shard_iterators_map.clear()
            streams.add_response(
                'describe_stream',
                {
                    'StreamDescription': {
                        'StreamStatus': 'ENABLED',
                        'Shards': [{'ShardId': 'shardId-000000000000000000000001'}],
                    }
                },
                {'StreamArn': old_arn},
            )
            streams.add_client_error('get_shard_iterator', 'ResourceNotFoundException')
            once(sub, sub.shard_iterator_initializer)
        else:
            if failure == 'describe_stream':
                streams.add_client_error('describe_stream', 'ResourceNotFoundException')
            else:
                streams.add_response(
                    'describe_stream',
                    {'StreamDescription': {'StreamStatus': failure, 'Shards': []}},
                    {'StreamArn': old_arn},
                )
            once(sub, sub.shard_iterator_initializer)
        assert sub.stream_arn is None
        assert sub._resync_required
        tables.add_response(
            'describe_table',
            {'Table': {'LatestStreamArn': new_arn}},
            {'TableName': sub.table_name},
        )
        streams.add_response(
            'describe_stream',
            {
                'StreamDescription': {
                    'StreamStatus': 'ENABLED',
                    'Shards': [{'ShardId': 'shardId-000000000000000000000002'}],
                }
            },
            {'StreamArn': new_arn},
        )
        streams.add_response(
            'get_shard_iterator',
            {'ShardIterator': 'fresh-iterator'},
            {
                'StreamArn': new_arn,
                'ShardId': 'shardId-000000000000000000000002',
                'ShardIteratorType': 'LATEST',
            },
        )
        add_scan(snapshot, [{'key': 'maintenance', 'value': True}])
        once(sub, sub.shard_iterator_initializer)
        assert sub.stream_arn == new_arn
        assert config.get_bool('maintenance') is True
        assert config.get('deleted') is None
        assert not sub._resync_required
        tables.assert_no_pending_responses()
        streams.assert_no_pending_responses()
