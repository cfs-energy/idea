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
    assert provider.format_entry(_entry('job.duration_seconds', 86514.0, [])).startswith(
        'idea.job.duration_seconds:86514|'
    )
    assert provider.format_entry(_entry('job.cost', 0.000123, [])).startswith(
        'idea.job.cost:0.000123|'
    )


def test_send_reaches_a_udp_listener(context, monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 0))
    listener.settimeout(2)
    monkeypatch.setenv('DD_DOGSTATSD_URL', f'udp://127.0.0.1:{listener.getsockname()[1]}')

    provider = DogStatsdMetrics(context=context, namespace='idea-mock/mock')
    provider.log([_entry('job.count', 1, [])])

    assert listener.recv(8192) == b'idea.job.count:1|c|#idea_cluster:idea-mock,idea_module:mock'
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
