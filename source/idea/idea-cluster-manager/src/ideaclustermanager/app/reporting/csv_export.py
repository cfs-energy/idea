"""CSV uses the same immutable values and ordering as the table."""

import csv
import io
import json
from typing import get_args

from ideadatamodel import ExportReportingCsvResult, exceptions
from ideadatamodel.reporting.reporting_api import Column
from .snapshot_store import MAX_CSV_BYTES, column_value, too_large


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
    writer = csv.writer(stream)
    writer.writerow(
        [
            'period',
            'start_date',
            'end_date',
            'currency',
            'source_as_of',
            'coverage',
            *columns,
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
                json.dumps(row['coverage'], ensure_ascii=False, separators=(',', ':')),
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
