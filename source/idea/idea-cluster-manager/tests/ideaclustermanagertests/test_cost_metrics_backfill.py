from threading import Event
from unittest.mock import Mock

import arrow
import pytest
from ideadatamodel import exceptions

from ideaclustermanager.app.metrics.cost_metrics_backfill import CostMetricsBackfill
from ideaclustermanager.app.metrics.cost_metrics_service import (
    CostMetrics,
    CostMetricsService,
    CostRow,
)
from ideasdk.metrics.history_backfill import CapturingPublisher
from ideaclustermanagertests.metrics_fakes import FakeContext


def make_context():
    return FakeContext(
        {
            'metrics.provider': 'dogstatsd',
            'cluster-manager.metrics.cost.by_account': True,
        }
    )


def wait(backfill):
    backfill.thread.join(timeout=5)
    assert not backfill.thread.is_alive()
    return backfill.status()


def test_cost_collection_reuses_rows_dimensions_and_full_day_windows_without_outbox(
    monkeypatch,
):
    context = make_context()
    reader = Mock()
    row = CostRow(
        'cost',
        arrow.utcnow().shift(days=-2).format('YYYY-MM-DD'),
        {'module': 'scheduler', 'project': 'research', 'owner': 'alice'},
        1.25,
        2.5,
    )
    reader.fetch_all.side_effect = [[row, row], []]
    monkeypatch.setattr(CostMetricsService, 'reader', lambda self: reader)
    entries = []
    transport = Mock()

    def send(batch, on_sent):
        entries.extend(batch)
        on_sent(len(batch))

    transport.log.side_effect = send
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', lambda c: transport
    )
    backfill = CostMetricsBackfill(context)
    backfill.start_request({'days': 31, 'dry_run': False})
    status = wait(backfill)
    assert status['state'] == 'completed'
    assert status['jobs_scanned'] == 1
    assert status['points_sent'] == status['points_built'] == 2
    capture = CapturingPublisher(context)
    CostMetrics(capture).publish(
        row.family, int(arrow.get(row.day).timestamp()), row.dimensions, 2.5, 5
    )
    assert entries == capture.entries
    calls = reader.fetch_all.call_args_list
    today = arrow.utcnow().floor('day')
    assert calls[0].args == (today.shift(days=-31), today.shift(days=-1), True)
    assert calls[1].args == (today.shift(days=-1), today, True)
    assert context.metrics_service.published == []
    assert all(
        'outbox' not in key and 'last_collected' not in key
        for key in context.config().db.values
    )
    assert CostMetricsBackfill(context).status() == status
    assert context.config().db.reads[-1][1] is True


def test_cost_dry_run_counts_and_never_constructs_transport_or_outbox(monkeypatch):
    context = make_context()
    reader = Mock()
    reader.fetch_all.return_value = [
        CostRow('cost.by_service', '2026-09-01', {'service': 'ec2'}, 1, 2)
    ]
    monkeypatch.setattr(CostMetricsService, 'reader', lambda self: reader)
    factory = Mock()
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', factory
    )
    outbox = Mock(side_effect=AssertionError('outbox must not be used'))
    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.cost_metrics_service.CollectorOutbox', outbox
    )
    backfill = CostMetricsBackfill(context)
    backfill.start_request({'days': 400, 'dry_run': True})
    status = wait(backfill)
    assert status['jobs_scanned'] == 14
    assert status['points_built'] == 28
    assert status['points_sent'] == 0
    factory.assert_not_called()
    outbox.assert_not_called()
    assert context._logger.lines


def test_cost_second_start_is_refused_and_failure_is_reported(monkeypatch):
    entered, release = Event(), Event()

    def fetch(*args):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('cost read failed')

    reader = Mock()
    reader.fetch_all.side_effect = fetch
    monkeypatch.setattr(CostMetricsService, 'reader', lambda self: reader)
    backfill = CostMetricsBackfill(make_context())
    backfill.start_request({'days': 1, 'dry_run': True})
    assert entered.wait(5)
    try:
        assert backfill.status()['state'] == 'running'
        with pytest.raises(exceptions.SocaException, match='already running'):
            backfill.start_request({'days': 1})
    finally:
        release.set()
    status = wait(backfill)
    assert status['state'] == 'failed'
    assert status['errors'] == 1
    assert status['started_at'] and status['finished_at']
    assert backfill.context._lock.held == []


@pytest.mark.parametrize('days', [0, -1, 500, 1.5, True, '400'])
def test_invalid_days_are_rejected(days):
    backfill = CostMetricsBackfill(make_context())
    with pytest.raises(exceptions.SocaException, match='days must be an integer'):
        backfill.start_request({'days': days})
    assert backfill.thread is None


@pytest.mark.parametrize('namespace', ['BackfillCostMetrics', 'GetCostMetricsBackfill'])
def test_cost_backfill_api_requires_elevated_access(namespace):
    from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI

    api = ClusterSettingsAPI(make_context())
    invocation = Mock(namespace=f'ClusterSettings.{namespace}')
    invocation.is_authenticated.return_value = True
    invocation.is_authorized.return_value = False
    with pytest.raises(exceptions.SocaException):
        api.invoke(invocation)
    invocation.is_authorized.assert_called_once_with(elevated_access=True, scopes=None)
    invocation.success.assert_not_called()


@pytest.mark.parametrize(
    'namespace, method',
    [
        ('BackfillCostMetrics', 'start_request'),
        ('GetCostMetricsBackfill', 'status'),
    ],
)
def test_cost_backfill_api_returns_worker_status(namespace, method):
    from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI

    api = ClusterSettingsAPI(make_context())
    api.cost_metrics_backfill = Mock()
    expected = {'state': 'running', 'jobs_scanned': 12, 'points_sent': 23, 'errors': 0}
    getattr(api.cost_metrics_backfill, method).return_value = expected
    invocation = Mock(
        namespace=f'ClusterSettings.{namespace}',
        request_payload={'days': 400, 'dry_run': True},
    )
    invocation.is_authenticated.return_value = True
    invocation.is_authorized.return_value = True
    api.invoke(invocation)
    invocation.success.assert_called_once_with(expected)
    if method == 'start_request':
        api.cost_metrics_backfill.start_request.assert_called_once_with(
            invocation.request_payload
        )


def test_a_run_on_another_replica_is_refused(monkeypatch):
    context = make_context()
    context.distributed_lock().acquire = Mock(
        side_effect=RuntimeError('held by another replica')
    )
    backfill = CostMetricsBackfill(context)
    with pytest.raises(exceptions.SocaException, match='already running'):
        backfill.start_request({'days': 1})
    assert backfill.thread is None
    assert backfill.status()['state'] == 'idle'
