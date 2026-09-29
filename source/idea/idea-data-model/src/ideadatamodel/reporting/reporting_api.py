"""Immutable reporting read contract. Money is serialized as decimal strings."""

from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictInt

from ideadatamodel import SocaPayload

__all__ = (
    'MetricCoverage',
    'ReportingRow',
    'ReportingPeriodRequest',
    'ReportingPeriod',
    'ReportingSummary',
    'ReportingPaginator',
    'GetReportingCapabilitiesRequest',
    'GetReportingCapabilitiesResult',
    'ListReportingRowsRequest',
    'ListReportingRowsResult',
    'ExportReportingCsvRequest',
    'ExportReportingCsvResult',
)

Facet = Literal['jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai']
Table = Literal['user', 'project', 'facet']
Period = Literal['this_month', 'last_month', 'last_30_days', 'custom']
Column = Literal[
    'key',
    'label',
    'project_id',
    'spend_total',
    'jobs',
    'desktops',
    'desktop_disks',
    'shared_storage',
    'ai',
    'job_count',
    'node_hours',
    'requested_walltime_hours',
    'elapsed_hours',
    'efficiency_pct',
    'desktop_hours',
    'idle_stops',
]


class MetricCoverage(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    status: Literal['ready', 'estimated', 'partial', 'unavailable', 'not_applicable']
    reason: str = ''
    available_start: str | None = None
    available_end: str | None = None
    source_as_of: str | None = None
    missing_days: int = 0
    missing_records: int = 0
    eligible_count: int = 0
    total_count: int = 0
    freshness_spread_seconds: float | None = None


class ReportingRow(SocaPayload):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    key: str
    label: str
    project_id: str | None = None
    spend_total: Decimal | None = None
    spend_by_facet: dict[Facet, Decimal | None] = Field(default_factory=dict)
    job_count: int | None = None
    node_hours: Decimal | None = None
    requested_walltime_hours: Decimal | None = None
    elapsed_hours: Decimal | None = None
    efficiency_pct: Decimal | None = None
    desktop_hours: Decimal | None = None
    idle_stops: int | None = None
    coverage: dict[str, MetricCoverage] = Field(default_factory=dict)


class ReportingPeriodRequest(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    period: Period
    start_date: str | None = None
    end_date: str | None = None


class ReportingPeriod(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    period: Period
    start_date: str
    end_date: str
    start: str
    end: str
    provisional: bool


class ReportingSummary(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str
    expires_at: str
    period: ReportingPeriod
    currency: str
    timezone: str
    as_of: str | None
    tiles: dict[str, ReportingRow]
    coverage: dict[str, MetricCoverage]
    warnings: list[str]


class GetReportingCapabilitiesRequest(SocaPayload):
    model_config = ConfigDict(extra='forbid')


class GetReportingCapabilitiesResult(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    can_read_reporting: StrictBool


class ReportingPaginator(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=4096)


class ListReportingRowsRequest(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(min_length=1, max_length=128)
    table: Table
    sort_by: Column = 'spend_total'
    descending: StrictBool = True
    paginator: ReportingPaginator = Field(default_factory=ReportingPaginator)


class ListReportingRowsResult(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    listing: list[ReportingRow]
    paginator: ReportingPaginator
    total_rows: int
    coverage: dict[str, MetricCoverage]
    warnings: list[str]


class ExportReportingCsvRequest(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(min_length=1, max_length=128)
    table: Table
    sort_by: Column = 'spend_total'
    descending: StrictBool = True
    columns: list[Column] = Field(min_length=1, max_length=17)


class ExportReportingCsvResult(SocaPayload):
    model_config = ConfigDict(extra='forbid')
    filename: str
    content_type: Literal['text/csv;charset=utf-8'] = 'text/csv;charset=utf-8'
    content: str
    row_count: int
    as_of: str | None
