from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ideasdk.metrics.cloudwatch.cloudwatch_metrics import CloudWatchMetrics


@pytest.mark.parametrize('timestamp', [1757894400, 1757894400.125])
def test_epoch_timestamp_reaches_client_as_aware_datetime(
    context, monkeypatch, timestamp
):
    client = Mock()
    monkeypatch.setattr(context, 'aws', lambda: Mock(cloudwatch=lambda: client))
    provider = CloudWatchMetrics(context=context, namespace='idea-mock/mock')
    provider.log(
        [
            {
                'MetricName': 'api_invocations',
                'MetricType': 'Counter',
                'Dimensions': [],
                'Value': 1,
                'Unit': 'Count',
                'Timestamp': timestamp,
            }
        ]
    )
    provider.flush()
    client.put_metric_data.assert_called_once()
    sent = client.put_metric_data.call_args.kwargs['MetricData'][0]['Timestamp']
    assert isinstance(sent, datetime)
    assert sent.utcoffset() is not None
    assert sent == datetime.fromtimestamp(timestamp, tz=timezone.utc)
    assert client.put_metric_data.call_args.kwargs['Namespace'] == 'idea-mock/mock'


def test_formatted_timestamp_reaches_client_unchanged(context, monkeypatch):
    client = Mock()
    monkeypatch.setattr(context, 'aws', lambda: Mock(cloudwatch=lambda: client))
    provider = CloudWatchMetrics(context=context, namespace='idea-mock/mock')
    timestamp = '2026-09-15 00:00:00 +00:00'
    provider.log(
        [
            {
                'MetricName': 'api_invocations',
                'MetricType': 'Counter',
                'Dimensions': [],
                'Value': 1,
                'Unit': 'Count',
                'Timestamp': timestamp,
            }
        ]
    )
    provider.flush()
    client.put_metric_data.assert_called_once()
    assert (
        client.put_metric_data.call_args.kwargs['MetricData'][0]['Timestamp']
        == timestamp
    )
