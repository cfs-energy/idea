from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest
from ideadatamodel import exceptions

from ideadatamodel import SocaJob, SocaJobState
from ideasdk.metrics.history_backfill import CapturingPublisher
from ideascheduler.app.documents.document_store import DocumentStore
from ideascheduler.app.metrics.job_completion_metrics import JobCompletionMetrics
from ideascheduler.app.metrics.job_metrics_backfill import JobMetricsBackfill


def make_context():
    context = Mock()
    context.config().db = None
    context.distributed_lock().assert_held = Mock()
    context.module_id.return_value = 'scheduler'
    context.cluster_name.return_value = 'cluster'
    context.document_store.is_enabled.return_value = True
    context.document_store.is_initialized.return_value = True
    return context


def job(number):
    return SocaJob(
        job_id=str(number),
        job_uid=f'job-{number}',
        owner=f'user-{number}',
        state=SocaJobState.FINISHED,
        start_time=arrow.utcnow().shift(days=-3).datetime,
        end_time=arrow.utcnow().shift(days=-2).datetime,
    )


def payload(dry_run=True):
    return {
        'start_date': arrow.utcnow().shift(days=-4).format('YYYY-MM-DD'),
        'end_date': arrow.utcnow().format('YYYY-MM-DD'),
        'dry_run': dry_run,
    }


def wait(backfill):
    backfill.thread.join(timeout=5)
    assert not backfill.thread.is_alive()
    return backfill.status()


def test_pages_build_existing_entries_and_send_historical_points(monkeypatch):
    context = make_context()
    jobs = [job(1), job(2), job(3)]
    context.document_store.finished_job_pages.side_effect = lambda *args: (
        p for p in [jobs[:2], jobs[2:]]
    )
    transport = Mock()
    sent = []

    def send(entries, on_sent):
        sent.extend(entries)
        on_sent(len(entries))

    transport.log.side_effect = send
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', lambda c: transport
    )
    backfill = JobMetricsBackfill(context)
    result = backfill.start_request(payload(False))
    assert result['state'] == 'running'
    state = wait(backfill)
    expected = CapturingPublisher(context)
    for value in jobs:
        JobCompletionMetrics(expected, value).publish()
    assert sent == [e for e in expected.entries if e['MetricType'] == 'Counter']
    assert state['jobs_scanned'] == 3
    assert state['points_sent'] == 3
    assert state['points_skipped'] == 3
    assert state['state'] == 'completed'
    assert state['errors'] == 0
    assert state['started_at'] and state['finished_at']
    assert all(
        e['Timestamp'] == int(jobs[i].end_time.timestamp()) for i, e in enumerate(sent)
    )


def test_dry_run_counts_but_does_not_read_secret_or_send(monkeypatch):
    context = make_context()
    context.document_store.finished_job_pages.side_effect = lambda *args: (
        p for p in [[job(1)]]
    )
    factory = Mock()
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', factory
    )
    backfill = JobMetricsBackfill(context)
    backfill.start_request(payload())
    state = wait(backfill)
    assert state['jobs_scanned'] == state['points_built'] == 1
    assert state['points_sent'] == 0
    factory.assert_not_called()
    context.logger().info.assert_called_once()


def test_second_start_is_refused_and_status_is_live():
    context = make_context()
    entered, release = Event(), Event()

    def pages(*args):
        entered.set()
        assert release.wait(5)
        yield [job(1)]

    context.document_store.finished_job_pages.side_effect = pages
    backfill = JobMetricsBackfill(context)
    backfill.start_request(payload())
    assert entered.wait(5)
    try:
        assert backfill.status()['state'] == 'running'
        with pytest.raises(exceptions.SocaException, match='already running'):
            backfill.start_request(payload())
    finally:
        release.set()
    assert wait(backfill)['state'] == 'completed'
    context.distributed_lock().release.assert_called_once()


def test_errors_finish_the_run_and_leave_status_readable():
    context = make_context()
    context.document_store.finished_job_pages.side_effect = RuntimeError(
        'search unavailable'
    )
    backfill = JobMetricsBackfill(context)
    backfill.start_request(payload())
    state = wait(backfill)
    assert state['state'] == 'failed'
    assert state['errors'] == 1
    assert state['last_error'] == 'RuntimeError'
    assert state['finished_at']


def test_document_store_scrolls_and_cleans_up():
    context = make_context()
    store = DocumentStore(context)
    client = Mock()
    store.opensearch_client = SimpleNamespace(os_client=client)
    client.search.return_value = {
        '_scroll_id': 'first',
        'hits': {'hits': [{'_source': {'job_id': '1'}}]},
    }
    client.scroll.side_effect = [
        {'_scroll_id': 'second', 'hits': {'hits': [{'_source': {'job_id': '2'}}]}},
        {'_scroll_id': 'third', 'hits': {'hits': []}},
    ]
    start, end = arrow.get('2026-01-01'), arrow.get('2026-02-01')
    assert [[j.job_id for j in p] for p in store.finished_job_pages(start, end, 1)] == [
        ['1'],
        ['2'],
    ]
    request = client.search.call_args.kwargs
    assert request['size'] == 1
    assert request['body']['query']['bool']['filter'][1]['range']['end_time'] == {
        'gte': start.isoformat(),
        'lt': end.isoformat(),
    }
    assert request['body']['query']['bool']['filter'][0] == {
        'term': {'state': 'finished'}
    }
    assert client.scroll.call_count == 2
    client.clear_scroll.assert_called_once_with(scroll_id='third')


def test_scroll_is_released_when_consumer_stops():
    store = DocumentStore(make_context())
    client = Mock()
    store.opensearch_client = SimpleNamespace(os_client=client)
    client.search.return_value = {
        '_scroll_id': 'cursor',
        'hits': {'hits': [{'_source': {'job_id': '1'}}]},
    }
    pages = store.finished_job_pages(arrow.utcnow(), arrow.utcnow())
    next(pages)
    pages.close()
    client.clear_scroll.assert_called_once_with(scroll_id='cursor')


@pytest.mark.parametrize(
    'changes',
    [
        {'start_date': 'bad'},
        {'start_date': '2020-01-01'},
        {'end_date': '2099-01-01'},
        {'dry_run': 'false'},
    ],
)
def test_bad_ranges_and_boolean_are_rejected(changes):
    backfill = JobMetricsBackfill(make_context())
    with pytest.raises(exceptions.SocaException):
        backfill.start_request({**payload(), **changes})
    assert backfill.thread is None


def test_jobs_finishing_in_the_same_second_are_summed_across_pages(monkeypatch):
    context = make_context()
    first, second = job(1), job(1)
    second.end_time = first.end_time
    context.document_store.finished_job_pages.side_effect = lambda *args: (
        p for p in [[first], [second]]
    )
    sent = []
    transport = Mock()

    def send(entries, on_sent):
        sent.extend(entries)
        on_sent(len(entries))

    transport.log.side_effect = send
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', lambda c: transport
    )
    backfill = JobMetricsBackfill(context)
    backfill.start_request(payload(False))
    status = wait(backfill)
    assert status['jobs_scanned'] == 2
    assert len(sent) == status['points_sent'] == 1
    assert sent[0]['Value'] == 2


@pytest.mark.parametrize('namespace', ['BackfillJobMetrics', 'GetJobMetricsBackfill'])
def test_job_backfill_api_requires_elevated_access(namespace):
    from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI

    api = SchedulerAdminAPI(make_context())
    invocation = Mock(namespace=f'SchedulerAdmin.{namespace}')
    invocation.is_authorized.return_value = False
    with pytest.raises(exceptions.SocaException):
        api.invoke(invocation)
    invocation.is_authorized.assert_called_once_with(elevated_access=True, scopes=None)
    invocation.success.assert_not_called()


@pytest.mark.parametrize(
    'namespace, method',
    [
        ('BackfillJobMetrics', 'start_request'),
        ('GetJobMetricsBackfill', 'status'),
    ],
)
def test_job_backfill_api_returns_worker_status(namespace, method):
    from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI

    api = SchedulerAdminAPI(make_context())
    api.job_metrics_backfill = Mock()
    expected = {'state': 'running', 'jobs_scanned': 12, 'points_sent': 23, 'errors': 0}
    getattr(api.job_metrics_backfill, method).return_value = expected
    invocation = Mock(
        namespace=f'SchedulerAdmin.{namespace}', request_payload=payload()
    )
    invocation.is_authorized.return_value = True
    api.invoke(invocation)
    invocation.success.assert_called_once_with(expected)
    if method == 'start_request':
        api.job_metrics_backfill.start_request.assert_called_once_with(
            invocation.request_payload
        )


def test_lost_lease_stops_delivery(monkeypatch):
    context = make_context()
    context.document_store.finished_job_pages.side_effect = lambda *args: (
        p for p in [[job(1)]]
    )
    context.distributed_lock().assert_held.side_effect = RuntimeError('lease lost')
    transport = Mock()
    monkeypatch.setattr(
        'ideasdk.metrics.history_backfill.DatadogAPI.from_context', lambda c: transport
    )
    backfill = JobMetricsBackfill(context)
    backfill.start_request(payload(False))
    state = wait(backfill)
    assert state['state'] == 'failed'
    assert state['errors'] == 1
    transport.log.assert_not_called()
