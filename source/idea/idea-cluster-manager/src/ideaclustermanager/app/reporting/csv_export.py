"""CSV uses the same immutable values and ordering as the table."""

import csv
import io
from typing import get_args

from ideadatamodel import ExportReportingCsvResult, exceptions
from ideadatamodel.reporting.reporting_api import Column
from .snapshot_store import MAX_CSV_BYTES, column_value, too_large


COLUMN_TITLES = {
    'key': 'Key',
    'label': 'Name',
    'project_id': 'Project ID',
    'spend_total': 'Total spend',
    'jobs': 'Job spend',
    'desktops': 'Desktop spend',
    'desktop_disks': 'Desktop disk spend',
    'shared_storage': 'Shared storage spend',
    'ai': 'AI spend',
    'job_count': 'Jobs',
    'node_hours': 'Node-hours',
    'requested_walltime_hours': 'Requested hours',
    'elapsed_hours': 'Elapsed hours',
    'efficiency_pct': 'Walltime efficiency',
    'desktop_hours': 'Desktop hours',
    'idle_stops': 'Idle stops',
}


def completeness(coverage) -> str:
    """Plain words for the coverage of one row: nobody reads JSON in a spreadsheet."""
    missing = 0
    partial = False
    for value in (coverage or {}).values():
        if isinstance(value, dict):
            missing = max(missing, value.get('missing_days') or 0)
            partial = partial or value.get('status') in ('partial', 'unavailable')
    if missing:
        # facets and users can miss different days, and only their counts are kept: the
        # largest is a floor, the true number is their union
        return f'Partial: missing at least {missing} day{"" if missing == 1 else "s"}'
    return 'Partial' if partial else 'Complete'


def safe_text(value):
    text = str(value)
    if text.lstrip().startswith(
        ('=', '+', '-', '@', '\t', '\r', '\n')
    ) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def export_csv(summary, rows, table, columns):
    if (
        table not in ('user', 'project', 'facet')
        or not columns
        or len(set(columns)) != len(columns)
        or any(column not in get_args(Column) for column in columns)
    ):
        raise exceptions.invalid_params('Unsupported CSV columns or table')
    stream = io.StringIO(newline='')
    stream.write('\ufeff')
    writer = csv.writer(stream)
    writer.writerow(
        [
            'Period',
            'Start date',
            'End date',
            'Currency',
            'Costs as of',
            'Complete',
            *(COLUMN_TITLES.get(column, column) for column in columns),
        ]
    )
    period = summary['period']
    byte_count = len(stream.getvalue().encode('utf-8'))
    if byte_count > MAX_CSV_BYTES:
        too_large()
    for row in rows:
        line = io.StringIO(newline='')
        line_writer = csv.writer(line)
        values = []
        for column in columns:
            value = column_value(row, column)
            values.append(
                ''
                if value is None
                else safe_text(value)
                if column in ('key', 'label', 'project_id')
                else value
            )
        line_writer.writerow(
            [
                period['period'],
                period['start_date'],
                period['end_date'],
                summary['currency'],
                summary['as_of'],
                completeness(row.get('coverage')),
                *values,
            ]
        )
        encoded_row = line.getvalue()
        byte_count += len(encoded_row.encode('utf-8'))
        if byte_count > MAX_CSV_BYTES:
            too_large()
        stream.write(encoded_row)
    # Dates are normalized by the server; names contain no recorded labels.
    filename = f'reporting-{table}-{period["start_date"]}-{period["end_date"]}.csv'
    return ExportReportingCsvResult(
        filename=filename,
        content=stream.getvalue(),
        row_count=len(rows),
        as_of=summary['as_of'],
    )
