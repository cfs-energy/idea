from unittest.mock import Mock

import pytest
import requests

from ideasdk.metrics.datadog_api import DatadogAPI
from ideasdk.metrics.dogstatsd.dogstatsd_metrics import DogStatsdMetrics


def entry(name='job.count', kind='Counter', timestamp=1700000000):
    return {
        'MetricName': name,
        'MetricType': kind,
        'Value': 2.5,
        'Timestamp': timestamp,
        'Dimensions': [
            {'Name': 'Project', 'Value': 'team, one'},
            {'Name': 'Host', 'Value': 'collector'},
        ],
    }


def response(code):
    result = requests.Response()
    result.status_code = code
    return result


def test_request_body_and_shared_names_and_tags(context, monkeypatch):
    post = Mock(return_value=response(202))
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    transport = DatadogAPI('key', 'datadoghq.eu', 'cluster/scheduler/component')
    provider = DogStatsdMetrics(context, 'cluster/scheduler/component')
    entries = [entry(), entry('cost.amortized'), entry('storage.bytes', 'Gauge')]
    assert transport.log(entries) == 3
    call = post.call_args
    assert call.args == ('https://api.datadoghq.eu/api/v2/series',)
    assert call.kwargs['headers']['DD-API-KEY'] == 'key'
    series = call.kwargs['json']['series']
    assert series[0] == {
        'metric': 'idea.job.count',
        'type': 1,
        'interval': 1,
        'points': [{'timestamp': 1700000000, 'value': 2.5}],
        'tags': [
            'idea_cluster:cluster',
            'idea_module:scheduler',
            'component:component',
            'project:team_one',
            'host:collector',
        ],
    }
    assert series[2]['type'] == 3
    assert 'interval' not in series[2]
    for original, actual in zip(entries, series):
        line = provider.format_entry(original)
        assert line.startswith(actual['metric'] + ':')
        assert line.split('|#')[1].split('|')[0].split(',') == actual['tags']
    assert series[1]['tags'] == ['idea_cluster:cluster', 'project:team_one']


@pytest.mark.parametrize(
    'size, batches', [(500, [500]), (501, [500, 1]), (1001, [500, 500, 1])]
)
def test_batch_boundary_preserves_repeated_names(monkeypatch, size, batches):
    post = Mock(return_value=response(202))
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    entries = [entry(timestamp=1700000000 + index) for index in range(size)]
    sent = []
    assert DatadogAPI('key', 'datadoghq.com', 'c/m').log(entries, sent.append) == size
    assert sent == batches
    assert [
        len(call.kwargs['json']['series']) for call in post.call_args_list
    ] == batches


def test_retries_rate_limits_and_server_errors(monkeypatch):
    post = Mock(side_effect=[response(429), response(503), response(202)])
    sleep = Mock()
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    monkeypatch.setattr('ideasdk.metrics.datadog_api.time.sleep', sleep)
    assert DatadogAPI('key', 'datadoghq.com', 'c/m').log([entry()]) == 1
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert post.call_args_list[0] == post.call_args_list[2]


@pytest.mark.parametrize(
    'code, attempts', [(200, 1), (204, 1), (400, 1), (403, 1), (429, 5), (500, 5)]
)
def test_failure_is_raised_and_retries_are_bounded(monkeypatch, code, attempts):
    post = Mock(return_value=response(code))
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    monkeypatch.setattr('ideasdk.metrics.datadog_api.time.sleep', Mock())
    with pytest.raises(requests.HTTPError):
        DatadogAPI('key', 'datadoghq.com', 'c/m').log([entry()])
    assert post.call_count == attempts


def test_distributions_are_not_retyped(monkeypatch):
    post = Mock()
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    with pytest.raises(ValueError, match='Counter or Gauge'):
        DatadogAPI('key', 'datadoghq.com', 'c/m').log([entry(kind='Summary')])
    post.assert_not_called()


def test_context_reads_the_agent_secret_and_defaults_site():
    context = Mock()
    context.config().get_secret.return_value = 'secret-value'
    context.config().get_string.side_effect = lambda key, default: default
    context.cluster_name.return_value = 'cluster'
    context.module_id.return_value = 'scheduler'
    transport = DatadogAPI.from_context(context)
    assert transport.url == 'https://api.datadoghq.com/api/v2/series'
    assert transport.api_key == 'secret-value'
    context.config().get_secret.assert_called_once_with(
        'ecs.datadog.api_key_secret_arn', required=True
    )


def test_partial_failure_only_counts_accepted_batches(monkeypatch):
    post = Mock(side_effect=[response(202), response(400)])
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    sent = []
    with pytest.raises(requests.HTTPError):
        DatadogAPI('key', 'datadoghq.com', 'c/m').log(
            [entry() for _ in range(501)], sent.append
        )
    assert sent == [500]


@pytest.mark.parametrize(
    'timestamp', ['2023-11-14T22:13:20+00:00', '2023-11-14 22:13:20 +00:00']
)
def test_provider_string_timestamps_keep_the_historical_instant(timestamp):
    point = DatadogAPI('key', 'datadoghq.com', 'c/m').series(entry(timestamp=timestamp))
    assert point['points'][0]['timestamp'] == 1700000000


def test_cost_series_has_only_documented_dimensions_and_cluster():
    point = entry('cost.amortized', 'Gauge')
    point['Dimensions'] = [
        {'Name': name, 'Value': value}
        for name, value in [
            ('module', 'scheduler'),
            ('project', 'research'),
            ('owner', 'alice'),
            ('host', 'task-host'),
        ]
    ]
    series = DatadogAPI(
        'key', 'datadoghq.com', 'cluster/cluster-manager/replica'
    ).series(point)
    assert series['tags'] == [
        'idea_cluster:cluster',
        'module:scheduler',
        'project:research',
        'owner:alice',
    ]
    assert 'resources' not in series
