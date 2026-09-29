export type ReportingPeriod = 'this_month' | 'last_month' | 'last_30_days' | 'custom';
export type ReportingTable = 'user' | 'project' | 'facet';
export type ReportingFacet = 'jobs' | 'desktops' | 'desktop_disks' | 'shared_storage' | 'ai';
export type CoverageStatus = 'ready' | 'estimated' | 'partial' | 'unavailable' | 'not_applicable';

export interface ReportingCoverage {
    status: CoverageStatus;
    reason: string;
    available_start: string | null;
    available_end: string | null;
    source_as_of: string | null;
    missing_days: number;
    missing_records: number;
    eligible_count: number;
    total_count: number;
    freshness_spread_seconds: number | null;
}
export type ReportingNumber = number | string;
export type ReportingColumn = 'key' | 'label' | 'project_id' | 'spend_total' | ReportingFacet | 'job_count' | 'node_hours' | 'requested_walltime_hours' | 'elapsed_hours' | 'efficiency_pct' | 'desktop_hours' | 'idle_stops';
export type MetricCoverage = Record<string, ReportingCoverage>;
export interface ReportingRow {
    key: string;
    label: string;
    project_id?: string | null;
    spend_total: ReportingNumber | null;
    spend_by_facet: Partial<Record<ReportingFacet, ReportingNumber | null>>;
    job_count: number | null;
    node_hours: ReportingNumber | null;
    requested_walltime_hours: ReportingNumber | null;
    elapsed_hours: ReportingNumber | null;
    efficiency_pct: ReportingNumber | null;
    desktop_hours: ReportingNumber | null;
    idle_stops: number | null;
    coverage: MetricCoverage;
}
export interface ReportingSummaryRequest {
    period: ReportingPeriod;
    start_date?: string;
    end_date?: string;
}
export interface ReportingSummary {
    snapshot_id: string;
    expires_at: string;
    period: {period: ReportingPeriod; start_date: string; end_date: string; start: string; end: string; provisional: boolean};
    currency: string;
    timezone: string;
    as_of: string | null;
    tiles: Record<string, ReportingRow>;
    coverage: MetricCoverage;
    warnings: string[];
}
export interface ReportingRowsRequest {
    snapshot_id: string;
    table: ReportingTable;
    sort_by: ReportingColumn;
    descending: boolean;
    paginator: {page_size: number; cursor?: string};
}
export interface ReportingRows {
    listing: ReportingRow[];
    paginator: {page_size: number; cursor?: string | null};
    total_rows: number;
    coverage: MetricCoverage;
    warnings: string[];
}
export type ReportingExportRequest = Omit<ReportingRowsRequest, 'paginator'> & {columns: ReportingColumn[]};
export interface ReportingCsv {
    filename: string;
    content_type: 'text/csv;charset=utf-8';
    content: string;
    row_count: number;
    as_of: string | null;
}
