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


def cloudwatch_with(context, monkeypatch, fail_on_call=None):
    from ideasdk.metrics.cloudwatch.cloudwatch_metrics import CloudWatchMetrics

    sent = []

    class Client:
        def put_metric_data(self, Namespace, MetricData):
            if fail_on_call is not None and len(sent) == fail_on_call:
                raise RuntimeError('throttled')
            sent.append(list(MetricData))

    client = Client()
    monkeypatch.setattr(context, 'aws', lambda: Mock(cloudwatch=lambda: client))
    return CloudWatchMetrics(context=context, namespace='cluster/module'), sent


@pytest.mark.parametrize('count', [19, 500])
def test_a_synchronous_cloudwatch_publish_sends_every_point_before_returning(
    context, monkeypatch, count
):
    provider, sent = cloudwatch_with(context, monkeypatch)
    service = service_with(provider)
    service.publish(
        [{'MetricName': 'storage.used_bytes', 'Value': i} for i in range(count)],
        synchronous=True,
    )
    assert sum(len(page) for page in sent) == count
    assert all(len(page) <= 20 for page in sent)


def test_a_synchronous_cloudwatch_publish_says_when_a_page_failed(context, monkeypatch):
    provider, sent = cloudwatch_with(context, monkeypatch, fail_on_call=2)
    service = service_with(provider)
    with pytest.raises(RuntimeError, match='throttled'):
        service.publish(
            [{'MetricName': 'cost.amortized', 'Value': i} for i in range(100)],
            synchronous=True,
        )


def test_the_last_partial_page_is_sent_whole(context, monkeypatch):
    provider, sent = cloudwatch_with(context, monkeypatch)
    # a full page and a remainder waiting together, as flush is handed them
    provider.buffers['cluster/module'] = [
        {'MetricName': 'api_invocations', 'Value': i} for i in range(21)
    ]
    provider.flush()
    assert sorted(len(page) for page in sent) == [1, 20]


def test_a_full_backlog_drops_and_counts_instead_of_waiting():
    import threading

    service = service_with(Mock())
    service._metrics_backlog_queue = queue.Queue(maxsize=1)
    service._logger = Mock()
    service.dropped_publications = 0
    service.publish([{'MetricName': 'a'}])
    done = threading.Event()
    threading.Thread(
        target=lambda: (service.publish([{'MetricName': 'b'}]), done.set()), daemon=True
    ).start()
    assert done.wait(2), 'publish waited for a consumer that is not reading'
    assert service.dropped_publications == 1


def test_small_helpers_do_what_they_say():
    from decimal import Decimal
    import yaml
    from ideadatamodel.model_utils import ModelUtils
    from ideasdk.utils import Utils
    from ideasdk.utils.datetime_utils import DateTimeUtils

    assert DateTimeUtils.to_minutes(hours=2) == 120
    assert Utils.from_yaml('a: 1', yaml.SafeLoader) == {'a': 1}
    assert ModelUtils.get_as_float_list(['1.5', 2]) == [1.5, 2.0]
    assert Utils.check_and_convert_decimal_value([Decimal('1'), Decimal('2.5')]) == [
        1,
        2.5,
    ]
