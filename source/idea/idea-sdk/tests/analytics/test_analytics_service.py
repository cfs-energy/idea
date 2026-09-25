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
