"""CSV preserves immutable values and neutralizes recorded text."""

import csv
import io
import time
from types import SimpleNamespace

import pytest

from ideadatamodel import (
    ExportReportingCsvRequest,
    ListReportingRowsRequest,
    exceptions,
)
from ideaclustermanager.app.reporting.csv_export import export_csv
from ideaclustermanager.app.reporting.csv_export import completeness
from ideaclustermanager.app.reporting.reporting_service import ReportingService, new_row
from ideaclustermanagertests.test_reporting_snapshot_store import store


def summary():
    return dict(
        period=dict(period='custom', start_date='2024-01-01', end_date='2024-01-31'),
        currency='USD',
        as_of='2024-02-01T00:00:00+00:00',
        coverage={},
        warnings=[],
    )


def test_csv_all_rows_same_order_as_pages_and_totals():
    cache = store()
    rows = [dict(new_row(str(i), f'row {i}'), spend_total=i) for i in range(251)]
    result = cache.publish(
        'reader',
        summary(),
        dict(user=rows, project=[], facet=[]),
        time.monotonic() + 30,
    )
    service = ReportingService(SimpleNamespace(), store=cache)
    request = dict(
        snapshot_id=result['snapshot_id'],
        table='user',
        sort_by='spend_total',
        descending=True,
    )
    exported = service.export(
        'reader',
        ExportReportingCsvRequest(**request, columns=['label', 'spend_total']),
        lambda: True,
    )
    content = list(csv.DictReader(io.StringIO(exported.content)))
    assert exported.row_count == 251
    assert sum(int(row['Total spend']) for row in content) == sum(range(251))
    assert list(content[0])[-2:] == ['Name', 'Total spend']
    collected, cursor = [], None
    while True:
        page = service.list_rows(
            'reader',
            ListReportingRowsRequest(
                **request, paginator=dict(page_size=50, cursor=cursor)
            ),
            lambda: True,
        )
        collected.extend(row.label for row in page.listing)
        cursor = page.paginator.cursor
        if cursor is None:
            break
    assert collected == [row['Name'] for row in content]
    assert content[0]['Currency'] == 'USD'
    assert content[0]['Complete'].startswith(('Complete', 'Partial'))


@pytest.mark.parametrize(
    'label', ['=SUM(1,2)', '+cmd', '-cmd', '@cmd', '\tformula', '  =cmd', '\nformula']
)
def test_untrusted_formula_prefixes_are_neutralized(label):
    result = export_csv(summary(), [new_row('key', label)], 'user', ['label'])
    assert list(csv.DictReader(io.StringIO(result.content)))[0]['Name'] == "'" + label


def test_null_zero_quotes_newlines_and_unicode():
    row = new_row('key', '雪, "quoted"\nnext')
    row['job_count'] = 0
    result = export_csv(summary(), [row], 'user', ['label', 'job_count', 'spend_total'])
    assert result.content.encode('utf-8').startswith(b'\xef\xbb\xbf')
    data = list(csv.DictReader(io.StringIO(result.content.lstrip('\ufeff'))))[0]
    assert data['Name'] == row['label']
    assert data['Jobs'] == '0'
    assert data['Total spend'] == ''
    assert result.filename == 'reporting-user-2024-01-01-2024-01-31.csv'
    assert result.content_type == 'text/csv;charset=utf-8'


def test_csv_size_columns_and_expiry_fail_explicitly(monkeypatch):
    import ideaclustermanager.app.reporting.csv_export as module

    monkeypatch.setattr(module, 'MAX_CSV_BYTES', 100)
    with pytest.raises(exceptions.SocaException) as error:
        export_csv(summary(), [new_row('key', 'x')], 'user', ['label'])
    assert error.value.error_code == 'REPORT_TOO_LARGE'
    for columns in (['sql'], ['label', 'label']):
        with pytest.raises(exceptions.SocaException):
            export_csv(summary(), [], 'user', columns)
    cache = store()
    result = cache.publish(
        'reader', summary(), dict(user=[], project=[], facet=[]), time.monotonic() + 30
    )
    metadata = cache.lookup(result['snapshot_id'], 'reader', lambda: True)
    monkeypatch.setattr(time, 'time', lambda: metadata['expires'])
    with pytest.raises(exceptions.SocaException) as error:
        ReportingService(SimpleNamespace(), store=cache).export(
            'reader',
            ExportReportingCsvRequest(
                snapshot_id=result['snapshot_id'], table='user', columns=['label']
            ),
            lambda: True,
        )
    assert error.value.error_code == 'REPORT_EXPIRED'


@pytest.mark.parametrize(
    'username,expected', [('user-b', ['user-b']), ('unknown-user', [])]
)
def test_filtered_export_contains_only_selected_user(username, expected):
    cache = store()
    result = cache.publish(
        'reader',
        summary(),
        dict(
            user=[new_row('user-a', 'user-a'), new_row('user-b', 'user-b')],
            project=[],
            facet=[],
        ),
        time.monotonic() + 30,
    )
    service = ReportingService(SimpleNamespace(), store=cache)
    request = ExportReportingCsvRequest(
        snapshot_id=result['snapshot_id'],
        table='user',
        columns=['label'],
        username=username,
    )
    exported = service.export('reader', request, lambda: True)
    content = list(csv.DictReader(io.StringIO(exported.content.lstrip('\ufeff'))))
    assert [row['Name'] for row in content] == expected
    assert exported.row_count == len(expected)
    assert exported.filename == f'reporting-user-2024-01-01-2024-01-31-{username}.csv'
    with pytest.raises(exceptions.SocaException):
        service.export('reader', request, lambda: False)
    request.table = 'project'
    with pytest.raises(exceptions.SocaException) as error:
        service.export('reader', request, lambda: True)
    assert error.value.error_code == 'INVALID_PARAMS'


def test_rows_filter_on_server_and_bind_cursor_to_username():
    cache = store()
    published = cache.publish(
        'reader',
        summary(),
        dict(
            user=[new_row(f'user-{i:03}', f'user-{i:03}') for i in range(251)],
            project=[],
            facet=[],
        ),
        time.monotonic() + 30,
    )
    service = ReportingService(SimpleNamespace(), store=cache)
    request = dict(snapshot_id=published['snapshot_id'], table='user')
    page = service.list_rows(
        'reader', ListReportingRowsRequest(**request, username='user-250'), lambda: True
    )
    assert [row.key for row in page.listing] == ['user-250']
    assert page.total_rows == 1
    assert page.paginator.cursor is None
    missing = service.list_rows(
        'reader', ListReportingRowsRequest(**request, username='unknown'), lambda: True
    )
    assert missing.total_rows == 0 and not missing.listing
    unfiltered = service.list_rows(
        'reader', ListReportingRowsRequest(**request), lambda: True
    )
    with pytest.raises(exceptions.SocaException):
        service.list_rows(
            'reader',
            ListReportingRowsRequest(
                **request,
                username='user-250',
                paginator=dict(cursor=unfiltered.paginator.cursor),
            ),
            lambda: True,
        )
    for table in ('project', 'facet'):
        with pytest.raises(exceptions.SocaException):
            service.list_rows(
                'reader',
                ListReportingRowsRequest(
                    **dict(request, table=table), username='user-250'
                ),
                lambda: True,
            )
    for actor, allowed in [('another-reader', True), ('reader', False)]:
        with pytest.raises(exceptions.SocaException):
            service.list_rows(
                actor,
                ListReportingRowsRequest(**request, username='user-250'),
                lambda: allowed,
            )


def test_csv_says_in_words_whether_a_row_is_complete():
    assert completeness({}) == 'Complete'
    assert completeness({'jobs': {'status': 'ready', 'missing_days': 0}}) == 'Complete'
    assert completeness({'jobs': {'status': 'partial'}}) == 'Partial'
    assert (
        completeness({'jobs': {'status': 'partial', 'missing_days': 12}})
        == 'Partial: missing at least 12 days'
    )
    # AI misses days 1-5 and jobs days 20-25: 11 days, but only the counts are kept
    assert (
        completeness(
            {
                'ai': {'status': 'partial', 'missing_days': 5},
                'jobs': {'status': 'partial', 'missing_days': 6},
            }
        )
        == 'Partial: missing at least 6 days'
    )
    assert (
        completeness({'jobs': {'status': 'partial', 'missing_days': 1}})
        == 'Partial: missing at least 1 day'
    )
