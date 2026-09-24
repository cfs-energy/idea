"""
the dogstatsd provider: one line per metric with the namespace and dimensions as tags,
the split entries BaseMetrics adds dropped, delivered over UDP or a socket path.
"""

from ideasdk.metrics.dogstatsd.dogstatsd_metrics import DogStatsdMetrics

import pytest
import socket


def _entry(name, value, dimensions, metric_type='Counter'):
    return {
        'MetricType': metric_type,
        'MetricName': name,
        'Dimensions': [{'Name': k, 'Value': v} for k, v in dimensions],
        'Value': value,
        'Unit': 'Count',
    }


def test_only_the_complete_entry_is_sent(context):
    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    # what BaseMetrics publishes for two dimensions: one entry per dimension, then all
    batch = [
        _entry('jobs_finished', 1, [('queue_type', 'compute')]),
        _entry('jobs_finished', 1, [('project', 'p1')]),
        _entry('jobs_finished', 1, [('queue_type', 'compute'), ('project', 'p1')]),
    ]
    chosen = provider.complete_entries(batch)
    assert len(chosen) == 1
    assert provider.format_entry(chosen[0]) == (
        'idea.jobs_finished:1|c|#idea_cluster:idea-mock,idea_module:mock,queue_type:compute,project:p1'
    )


def test_summary_is_a_distribution_and_tags_are_sanitized(context):
    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock/api')
    line = provider.format_entry(
        _entry('api_invocations', 12.5, [('Api', 'Jobs.List, all|now')], 'Summary')
    )
    assert line == (
        'idea.api_invocations:12.5|d|#idea_cluster:idea-mock,idea_module:mock,component:api,api:Jobs.List_all_now'
    )


def test_values_are_plain_decimals(context):
    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    assert provider.format_entry(
        _entry('job.duration_seconds', 86514.0, [])
    ).startswith('idea.job.duration_seconds:86514|')
    assert provider.format_entry(_entry('job.cost', 0.000123, [])).startswith(
        'idea.job.cost:0.000123|'
    )


def test_send_reaches_a_udp_listener(context, monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 0))
    listener.settimeout(2)
    monkeypatch.setenv(
        'DD_DOGSTATSD_URL', f'udp://127.0.0.1:{listener.getsockname()[1]}'
    )

    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    provider.log([_entry('job.count', 1, [])])

    assert (
        listener.recv(8192)
        == b'idea.job.count:1|c|#idea_cluster:idea-mock,idea_module:mock'
    )
    listener.close()


def test_missing_agent_never_raises(context, monkeypatch):
    monkeypatch.setenv('DD_DOGSTATSD_URL', 'unix:///nonexistent/dsd.socket')
    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    provider.log([_entry('job.count', 1, [])])
    assert provider._send_failures == 1


def test_unsupported_url_is_rejected(context, monkeypatch):
    monkeypatch.setenv('DD_DOGSTATSD_URL', 'http://127.0.0.1:8125')
    with pytest.raises(ValueError):
        DogStatsdMetrics(context=context, namespace='idea-mock/mock')


def test_an_epoch_timestamp_marks_the_point_at_that_time(context):
    # Cost Explorer's day, not the scrape: gauges and counts carry it, a distribution cannot.
    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    day = 1757894400
    count = _entry('cost.amortized', 12.5, [('module', 'scheduler')])
    count['Timestamp'] = day
    assert provider.format_entry(count) == (
        'idea.cost.amortized:12.5|c|#idea_cluster:idea-mock,module:scheduler|T1757894400|card:none'
    )
    gauge = _entry('storage.used_bytes', 4096, [('user', 'alice')], metric_type='Gauge')
    gauge['Timestamp'] = day
    assert provider.format_entry(gauge).endswith(
        '|g|#idea_cluster:idea-mock,user:alice|T1757894400|card:none'
    )
    summary = _entry('job.cpu_efficiency', 0.5, [], metric_type='Summary')
    summary['Timestamp'] = day
    assert '|T' not in provider.format_entry(summary)
    # BaseMetrics' default is a formatted string for now: no field.
    live = _entry('jobs_finished', 1, [])
    live['Timestamp'] = '2026-09-15 00:00:00 +00:00'
    assert '|T' not in provider.format_entry(live)


@pytest.mark.parametrize(
    'transport', ['udp://127.0.0.1:8125', 'unix:///tmp/dsd.socket']
)
@pytest.mark.parametrize('family', ['cost.amortized', 'storage.used_bytes'])
def test_collectors_disable_origin_enrichment_on_shared_provider(
    context, monkeypatch, transport, family
):
    from unittest.mock import Mock

    monkeypatch.setenv('DD_DOGSTATSD_URL', transport)
    monkeypatch.setenv('DD_ENTITY_ID', 'pod-id')
    monkeypatch.setenv('DD_CONTAINER_ID', 'container-id')
    sender = Mock()
    monkeypatch.setattr(socket, 'socket', Mock(return_value=sender))
    provider = DogStatsdMetrics(context, 'idea-mock/mock/component')
    metric_type = 'Gauge' if family.startswith('storage.') else 'Counter'
    provider.log(
        [_entry(family, 2, [('module', 'cluster'), ('host', 'old-host')], metric_type)]
    )
    assert (
        sender.sendto.call_args.args[0]
        == (
            f'idea.{family}:2|{"g" if metric_type == "Gauge" else "c"}'
            '|#idea_cluster:idea-mock,module:cluster|card:none'
        ).encode()
    )
    provider.log([_entry('job.count', 1, [])])
    assert sender.sendto.call_args.args[0] == (
        b'idea.job.count:1|c|#idea_cluster:idea-mock,idea_module:mock,component:component'
    )


def test_synchronous_send_failure_raises_and_next_send_recovers(context, monkeypatch):
    from unittest.mock import Mock

    failed, recovered = Mock(), Mock()
    failed.sendto.side_effect = OSError('agent unavailable')
    monkeypatch.setattr(socket, 'socket', Mock(side_effect=[failed, recovered]))
    provider = DogStatsdMetrics(context, 'idea-mock/mock')
    with pytest.raises(OSError, match='agent unavailable'):
        provider.log([_entry('cost.amortized', 2, [])], raise_on_error=True)
    failed.close.assert_called_once()
    provider.log([_entry('cost.amortized', 2, [])], raise_on_error=True)
    recovered.sendto.assert_called_once()
    assert provider._send_failures == 0
