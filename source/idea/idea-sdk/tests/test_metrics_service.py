import queue
from unittest.mock import Mock

import pytest

from ideasdk.metrics.metrics_service import MetricsService
from ideasdk.metrics.dogstatsd.dogstatsd_metrics import DogStatsdMetrics


def service_with(provider):
    service = MetricsService.__new__(MetricsService)
    service.default_namespace = 'cluster/module'
    service._metrics_backlog_queue = queue.Queue()
    service._factory = Mock()
    service._factory.get_provider.return_value = provider
    return service


def test_default_publish_queues_without_sending():
    provider = Mock()
    service = service_with(provider)
    entries = [{'MetricName': 'job.count'}]
    service.publish(entries)
    assert service._metrics_backlog_queue.get_nowait() == entries
    provider.log.assert_not_called()


def test_synchronous_publish_bypasses_queue_and_propagates_socket_error():
    provider = Mock(spec=DogStatsdMetrics)
    provider.log.side_effect = OSError('agent unavailable')
    service = service_with(provider)
    entries = [{'MetricName': 'cost.amortized'}]
    with pytest.raises(OSError, match='agent unavailable'):
        service.publish(entries, synchronous=True)
    provider.log.assert_called_once_with(metric_data=entries, raise_on_error=True)
    service._factory.get_provider.assert_called_once_with('cluster/module')
    assert service._metrics_backlog_queue.empty()


def test_synchronous_publish_preserves_other_provider_interface_and_namespaces():
    provider = Mock()
    service = service_with(provider)
    entries = [
        {'MetricName': 'storage.used_bytes'},
        {
            'MetricName': 'storage.used_bytes',
            'Namespace': 'other/module',
        },
    ]
    service.publish(entries, synchronous=True)
    assert service._metrics_backlog_queue.empty()
    assert [call.args[0] for call in service._factory.get_provider.call_args_list] == [
        'cluster/module',
        'other/module',
    ]
    assert [call.kwargs for call in provider.log.call_args_list] == [
        {'metric_data': [entries[0]]},
        {'metric_data': [entries[1]]},
    ]


def test_backlog_delivery_keeps_default_send_behavior():
    provider = Mock(spec=DogStatsdMetrics)
    service = service_with(provider)
    entries = [{'MetricName': 'job.count'}]
    service.publish(entries)
    service._publish(service._metrics_backlog_queue.get_nowait())
    provider.log.assert_called_once_with(metric_data=entries)
