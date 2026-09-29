#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.
import threading
from unittest.mock import Mock

from ideasdk.analytics.analytics_service import (
    AnalyticsEntry,
    AnalyticsService,
    EntryAction,
    EntryContent,
)


def test_buffer_processing_continues_after_delivery_exception(monkeypatch):
    delivery_succeeded = threading.Event()
    delivery_attempts = 0

    def put_records(**_kwargs):
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 1:
            raise RuntimeError('delivery failed')
        delivery_succeeded.set()
        return {'FailedRecordCount': 0}

    kinesis_client = Mock()
    kinesis_client.put_records.side_effect = put_records

    context = Mock()
    logger = context.logger.return_value
    context.config.return_value.get_string.return_value = 'analytics-stream'
    context.aws.return_value.kinesis.return_value = kinesis_client
    monkeypatch.setattr(
        'ideasdk.analytics.analytics_service.AwsOpenSearchClient', Mock()
    )
    service = AnalyticsService(context)

    try:
        service.post_entry(
            AnalyticsEntry(
                entry_id='entry-id',
                entry_action=EntryAction.CREATE_ENTRY,
                entry_content=EntryContent(
                    index_id='index-id', entry_record={'key': 'value'}
                ),
            )
        )
        service._enforce_buffer_processing()

        assert delivery_succeeded.wait(timeout=3)
        assert service._buffer_processing_thread.is_alive()
        logger.exception.assert_called_once_with(
            'Failed to post 1 buffered analytics entries'
        )
    finally:
        service.stop()


def make_entry(number):
    return AnalyticsEntry(
        entry_id=str(number),
        entry_action=EntryAction.CREATE_ENTRY,
        entry_content=EntryContent(index_id='index', entry_record={}),
    )


def make_service(monkeypatch):
    monkeypatch.setattr(AnalyticsService, '_initialize', lambda self: None)
    monkeypatch.setattr(
        'ideasdk.analytics.analytics_service.AwsOpenSearchClient', Mock()
    )
    context = Mock()
    context.config.return_value.get_string.return_value = 'analytics-stream'
    service = AnalyticsService(context)
    client = context.aws.return_value.kinesis.return_value
    client.put_records.return_value = {'FailedRecordCount': 0}
    return service, client


def test_delivery_chunks_large_buffer(monkeypatch):
    service, client = make_service(monkeypatch)
    service._buffer = [make_entry(i) for i in range(1001)]
    service._post_entries_to_kinesis()
    batches = [call.kwargs['Records'] for call in client.put_records.call_args_list]
    assert [len(batch) for batch in batches] == [500, 500, 1]
    assert [record['PartitionKey'] for batch in batches for record in batch] == [
        str(i) for i in range(1001)
    ]
    assert service._buffer == []


def test_delivery_requeues_only_partial_failures(monkeypatch):
    service, client = make_service(monkeypatch)
    entries = [make_entry(i) for i in range(501)]
    service._buffer = entries.copy()
    client.put_records.side_effect = [
        {
            'FailedRecordCount': 1,
            'Records': [
                {'ErrorCode': 'Throttled'} if i == 3 else {'SequenceNumber': str(i)}
                for i in range(500)
            ],
        },
        {'FailedRecordCount': 0},
    ]
    service._post_entries_to_kinesis()
    assert service._buffer == [entries[3]]
    client.put_records.side_effect = None
    service._post_entries_to_kinesis()
    assert service._buffer == []
    assert len(client.put_records.call_args.kwargs['Records']) == 1


def test_delivery_exception_preserves_unsent_and_concurrent_entries(monkeypatch):
    import pytest

    service, client = make_service(monkeypatch)
    entries = [make_entry(i) for i in range(1001)]
    service._buffer = entries.copy()
    appended = threading.Event()
    producer = threading.Thread(
        target=lambda: (service.post_entry(make_entry('new')), appended.set())
    )
    lock_released = []

    def deliver(**kwargs):
        assert len(kwargs['Records']) <= 500
        if client.put_records.call_count == 1:
            producer.start()
            lock_released.append(appended.wait(1))
            return {'FailedRecordCount': 0}
        raise RuntimeError('delivery failed')

    client.put_records.side_effect = deliver
    try:
        with pytest.raises(RuntimeError):
            service._post_entries_to_kinesis()
    finally:
        if producer.ident is not None:
            producer.join(2)
    assert lock_released == [True]
    assert service._buffer == entries[500:] + [make_entry('new')]


def test_retry_backoff_is_capped_and_resets(monkeypatch):
    service, _ = make_service(monkeypatch)
    delays = []
    attempts = 0

    def deliver():
        nonlocal attempts
        attempts += 1
        if attempts == 11:
            service._exit.set()
        if attempts == 9:
            return True
        if attempts % 2:
            raise RuntimeError('delivery failed')
        return False

    def wait(timeout):
        delays.append(timeout)
        return False

    monkeypatch.setattr(service._exit, 'wait', wait)
    monkeypatch.setattr(service._buffer_size_limit_reached_condition, 'wait', Mock())
    monkeypatch.setattr(service, '_post_entries_to_kinesis', deliver)
    service._process_buffer()
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 1]


def test_network_call_releases_buffer_lock(monkeypatch):
    service, client = make_service(monkeypatch)
    service._buffer = [make_entry('old')]
    appended = threading.Event()
    producer = threading.Thread(
        target=lambda: (service.post_entry(make_entry('new')), appended.set())
    )
    lock_released = []

    def deliver(**_kwargs):
        producer.start()
        lock_released.append(appended.wait(1))
        return {'FailedRecordCount': 0}

    client.put_records.side_effect = deliver
    try:
        service._post_entries_to_kinesis()
    finally:
        producer.join(2)
    assert lock_released == [True]
    assert service._buffer == [make_entry('new')]
