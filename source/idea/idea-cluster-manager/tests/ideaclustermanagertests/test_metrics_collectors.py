import arrow
import pytest
from unittest.mock import Mock

from ideaclustermanager.app.metrics.cost_metrics_service import CostMetricsService
from ideaclustermanager.app.metrics.storage_metrics_service import StorageMetricsService
from ideaclustermanagertests.metrics_fakes import FakeCollectorSource, FakeContext


@pytest.fixture(params=['cost', 'storage'])
def collector(request, monkeypatch):
    kind = request.param
    clock = {'now': 1800000000.0}
    monkeypatch.setattr(arrow, 'utcnow', lambda: arrow.get(clock['now']))
    source = FakeCollectorSource(clock)
    values = {
        'metrics.provider': 'dogstatsd',
        f'cluster-manager.metrics.{kind}.enabled': True,
        'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
        'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
        'shared-storage.data.fsx_netapp_ontap.svm.management_dns': 'fs-test.example',
        'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'secret',
    }
    contexts = [
        FakeContext(dict(values), cost_explorer=source, secrets={'secret': 'pw'})
        for _ in range(2)
    ]
    first, second = contexts
    second.config().db = first.config().db
    second._lock = first._lock
    second._aws._s3 = first._aws._s3
    first.config().db.lock = first._lock
    if kind == 'cost':
        monkeypatch.setattr(CostMetricsService, 'reader', lambda self: source)
        service_class = CostMetricsService
    else:
        monkeypatch.setattr(
            'ideaclustermanager.app.metrics.storage_metrics_service.OntapClient',
            lambda *args, **kwargs: source,
        )
        service_class = StorageMetricsService
    services = [service_class(context) for context in contexts]
    return kind, clock, source, contexts, services


def test_replicas_share_collection_checkpoint_but_always_retry_outbox(collector):
    kind, clock, source, contexts, services = collector
    first, second = services
    db = contexts[0].config().db
    key = f'cluster-manager.metrics.{kind}.last_collected'
    start = clock['now']
    first.run_once()
    assert db.values[key] == start + 10 == clock['now']
    assert db.writes[-1] == (key, clock['now'])
    completed = clock['now']
    clock['now'] = completed + first.get_interval_seconds() / 2 - 1
    second.run_once()
    assert source.calls == 1
    assert contexts[1].published() == []
    assert key not in contexts[1].config().values
    assert db.writes[-1] == (key, completed)
    clock['now'] += 1
    second.run_once()
    assert source.calls == 2
    assert len([write for write in db.writes if write[0] == key]) == 2
    assert db.values[key] == clock['now']
    assert contexts[0].published() and contexts[1].published()
    for context in contexts:
        assert all(
            'host' not in context.dimensions(entry) for entry in context.published()
        )
        assert context.distributed_lock().held == []
    assert all(read == ({'key': key}, True) for read in db.reads)


@pytest.mark.parametrize('failure', ['source', 'publish', 'read', 'write', 'outbox'])
def test_failed_run_keeps_checkpoint_and_can_retry(collector, failure, monkeypatch):
    kind, clock, source, contexts, services = collector
    context = contexts[0]
    db = context.config().db
    key = f'cluster-manager.metrics.{kind}.last_collected'
    previous = clock['now'] - services[0].get_interval_seconds()
    db.values[key] = previous
    source.fail = failure == 'source'
    db.fail_read = failure == 'read'
    db.fail_write = failure == 'write'
    context.aws().s3().fail_write = failure == 'outbox'
    publish = context.metrics_service.publish
    if failure == 'publish':
        monkeypatch.setattr(
            context.metrics_service,
            'publish',
            Mock(side_effect=RuntimeError('publish failed')),
        )
    if kind == 'cost' or failure in ('read', 'write', 'publish', 'outbox'):
        with pytest.raises(RuntimeError):
            services[0].run_once()
    else:
        services[0].run_once()
    if failure == 'publish':
        assert context.aws().s3().values
    else:
        assert db.values[key] == previous
        assert db.writes == []
    assert context.distributed_lock().held == []
    source.fail = db.fail_read = db.fail_write = False
    context.aws().s3().fail_write = False
    monkeypatch.setattr(context.metrics_service, 'publish', publish)
    context.config().secrets['secret'] = 'pw'
    services[1].run_once()
    assert db.values[key] == clock['now']
    assert context.aws().s3().values == {}


@pytest.mark.parametrize(
    'provider', ['dogstatsd', 'cloudwatch', 'prometheus', 'other', None, '']
)
def test_provider_support_and_single_warning(collector, provider):
    kind, clock, source, contexts, services = collector
    context = contexts[0]
    context.config().values['metrics.provider'] = provider
    supported = provider == 'dogstatsd' or (
        kind == 'storage' and provider == 'cloudwatch'
    )
    assert services[0].is_enabled() is supported
    assert services[0].is_enabled() is supported
    warnings = [line for line in context.logger().lines if line.startswith('warning ')]
    if supported:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert repr(provider) in warnings[0]
        assert (
            'timestamped replacement' if kind == 'cost' else 'gauge support'
        ) in warnings[0]


def test_connection_failure_keeps_payload_for_another_replica(collector, monkeypatch):
    kind, clock, source, contexts, services = collector
    monkeypatch.setattr(
        contexts[0].metrics_service,
        'publish',
        Mock(side_effect=ConnectionError('unavailable')),
    )
    with pytest.raises(ConnectionError, match='unavailable'):
        services[0].run_once()
    assert contexts[0].aws().s3().values

    # A retry must survive the source day leaving the moving lookback window.
    # The saved points remain deliverable even when a later source read fails.
    clock['now'] += 90 * 86400
    source.fail = True
    if kind == 'cost':
        with pytest.raises(RuntimeError, match='source unavailable'):
            services[1].run_once()
    else:
        services[1].run_once()
    published = list(contexts[1].published())
    assert published
    assert contexts[0].aws().s3().values == {}
    if kind == 'cost':
        with pytest.raises(RuntimeError, match='source unavailable'):
            services[1].run_once()
    else:
        services[1].run_once()
    assert contexts[1].published() == published


def test_outbox_paginates_and_keeps_latest_correction(collector, monkeypatch):
    import json
    from ideaclustermanager.app.metrics.collector_outbox import CollectorOutbox

    kind, clock, source, contexts, services = collector
    context = contexts[0]
    db = context.aws().s3()
    outbox = CollectorOutbox(context, 'saved', historical=True)
    metric = {'MetricName': 'cost', 'Timestamp': 123, 'Dimensions': [], 'Value': 1}
    context.distributed_lock().acquire('test')
    try:
        outbox.publish([metric])
        outbox.save()
        metric['Value'] = 2
        outbox.save()
        assert len(db.values) == 1
        saved = next(iter(db.values.values()))
        assert json.loads(saved)['Value'] == 2
        requests = []

        def listing(**request):
            requests.append(request)
            if len(requests) == 1:
                return {'Contents': [], 'NextContinuationToken': 'next'}
            assert request['ContinuationToken'] == 'next'
            return {'Contents': [{'Key': next(iter(db.values))}]}

        monkeypatch.setattr(db, 'list_objects_v2', listing)
        outbox.replay()
        assert len(requests) == 2
        assert context.published() == [metric]
        assert db.values == {}
    finally:
        context.distributed_lock().release('test')


def test_outbox_retires_only_successful_points(collector):
    from ideaclustermanager.app.metrics.collector_outbox import CollectorOutbox

    _, _, _, contexts, _ = collector
    context = contexts[0]
    outbox = CollectorOutbox(context, 'partial', historical=True)
    entries = [
        {
            'MetricName': 'cost.amortized',
            'Timestamp': day,
            'Dimensions': [],
            'Value': day,
        }
        for day in (1, 2, 3)
    ]
    outbox.publish(entries)
    outbox.save()
    assert len(context.aws().s3().values) == 3
    context.metrics_service.fail_after = 1
    with pytest.raises(RuntimeError, match='publish failed'):
        outbox.replay()
    assert context.published() == entries[:1]
    assert len(context.aws().s3().values) == 2
    context.metrics_service.fail_after = None
    outbox.replay()
    outbox.replay()
    assert context.published() == entries
    assert context.aws().s3().values == {}


@pytest.mark.parametrize('outcome', [202, 200, 204, 503, 'connection'])
def test_outbox_requires_http_202(collector, monkeypatch, outcome):
    import requests
    from ideasdk.metrics.datadog_api import DatadogAPI
    from ideaclustermanager.app.metrics.collector_outbox import CollectorOutbox

    _, _, _, contexts, _ = collector
    context = contexts[0]
    transport = DatadogAPI('key', 'datadoghq.com', 'test-cluster/cluster-manager')
    monkeypatch.setattr(DatadogAPI, 'from_context', lambda _: transport)
    response = requests.Response()
    response.status_code = outcome if isinstance(outcome, int) else 503
    post = Mock(return_value=response)
    if outcome == 'connection':
        post.side_effect = requests.ConnectionError('offline')
    monkeypatch.setattr('ideasdk.metrics.datadog_api.requests.post', post)
    monkeypatch.setattr('ideasdk.metrics.datadog_api.time.sleep', lambda _: None)
    outbox = CollectorOutbox(context, 'acknowledged', historical=True)
    outbox.publish(
        [
            dict(
                MetricName='cost.amortized',
                MetricType='Counter',
                Value=1,
                Timestamp=1700000000,
                Dimensions=[],
            )
        ]
    )
    outbox.save()
    if outcome == 202:
        outbox.replay()
        assert not context.aws().s3().values
    else:
        with pytest.raises(requests.RequestException):
            outbox.replay()
        assert len(context.aws().s3().values) == 1
    assert context.published() == []
    assert post.called
