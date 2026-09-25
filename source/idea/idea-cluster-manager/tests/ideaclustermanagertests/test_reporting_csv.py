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
    assert sum(int(row['spend_total']) for row in content) == sum(range(251))
    assert list(content[0])[-2:] == ['label', 'spend_total']
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
    assert collected == [row['label'] for row in content]
    assert content[0]['currency'] == 'USD'
    assert 'spend_total' in content[0]['coverage']


@pytest.mark.parametrize(
    'label', ['=SUM(1,2)', '+cmd', '-cmd', '@cmd', '\tformula', '  =cmd', '\nformula']
)
def test_untrusted_formula_prefixes_are_neutralized(label):
    result = export_csv(summary(), [new_row('key', label)], 'user', ['label'])
    assert list(csv.DictReader(io.StringIO(result.content)))[0]['label'] == "'" + label


def test_null_zero_quotes_newlines_and_unicode():
    row = new_row('key', '雪, "quoted"\nnext')
    row['job_count'] = 0
    result = export_csv(summary(), [row], 'user', ['label', 'job_count', 'spend_total'])
    data = list(csv.DictReader(io.StringIO(result.content)))[0]
    assert data['label'] == row['label']
    assert data['job_count'] == '0'
    assert data['spend_total'] == ''
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
