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


def test_replicas_share_checkpoint_and_resume_at_half_interval(collector):
    kind, clock, source, contexts, services = collector
    first, second = services
    db = contexts[0].config().db
    key = f'cluster-manager.metrics.{kind}.last_published'
    start = clock['now']
    first.run_once()
    assert db.values[key] == start + 10 == clock['now']
    assert db.writes == [(key, clock['now'])]
    completed = clock['now']
    clock['now'] = completed + first.get_interval_seconds() / 2 - 1
    second.run_once()
    assert source.calls == 1
    assert contexts[1].published() == []
    assert key not in contexts[1].config().values
    assert db.writes == [(key, completed)]
    clock['now'] += 1
    second.run_once()
    assert source.calls == 2
    assert len(db.writes) == 2
    assert db.values[key] == clock['now']
    assert contexts[0].published() and contexts[1].published()
    for context in contexts:
        assert all(
            context.dimensions(entry)['host'] == context.cluster_name()
            for entry in context.published()
        )
        assert context.distributed_lock().held == []
    assert all(read == ({'key': key}, True) for read in db.reads)


@pytest.mark.parametrize('failure', ['source', 'publish', 'read', 'write'])
def test_failed_run_keeps_checkpoint_and_can_retry(collector, failure, monkeypatch):
    kind, clock, source, contexts, services = collector
    context = contexts[0]
    db = context.config().db
    key = f'cluster-manager.metrics.{kind}.last_published'
    previous = clock['now'] - services[0].get_interval_seconds()
    db.values[key] = previous
    source.fail = failure == 'source'
    db.fail_read = failure == 'read'
    db.fail_write = failure == 'write'
    publish = context.metrics_service.publish
    if failure == 'publish':
        monkeypatch.setattr(
            context.metrics_service,
            'publish',
            Mock(side_effect=RuntimeError('publish failed')),
        )
    if kind == 'cost' or failure in ('read', 'write'):
        with pytest.raises(RuntimeError):
            services[0].run_once()
    else:
        services[0].run_once()
    assert db.values[key] == previous
    assert db.writes == []
    assert context.distributed_lock().held == []
    source.fail = db.fail_read = db.fail_write = False
    monkeypatch.setattr(context.metrics_service, 'publish', publish)
    context.config().secrets['secret'] = 'pw'
    services[1].run_once()
    assert db.values[key] == clock['now']
    assert len(db.writes) == 1


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
