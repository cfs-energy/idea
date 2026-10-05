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
export type ReportingPeriodRequest = ReportingSummaryRequest;
export type ReportingInsightsRequest = ReportingPeriodRequest & {username?: string};
export interface Ranked {
    name: string;
    cost: string;
    count: number | null;
    share_pct: number;
}
export interface JobRow {
    job_id: string;
    name: string | null;
    owner: string;
    project: string | null;
    queue: string | null;
    instance_type: string | null;
    nodes: number | null;
    requested_cores: number | null;
    used_cores: number | null;
    requested_memory_gib: number | null;
    peak_memory_gib: number | null;
    instance_memory_gib?: number | null;
    finished_at: string;
    elapsed_hours: number | null;
    cost: string | null;
    cpu_efficiency_pct: number | null;
    memory_efficiency_pct: number | null;
    walltime_efficiency_pct: number | null;
    wasted_core_hours: number | null;
}
export interface DesktopRow {
    idea_session_id: string;
    name: string | null;
    owner: string;
    project: string | null;
    instance_type: string | null;
    checked_hours: number;
    idle_hours: number;
    idle_pct: number;
    idle_cost: string | null;
}
export interface ReportingBudget {
    project: string;
    budget_name: string;
    limit: string;
    spent: string;
    forecast: string | null;
    pct_at_forecast: number | null;
    headroom: string | null;
    status: 'ok' | 'watch' | 'over';
}
export interface ReportingInsights {
    period: {start: string; end: string; label: string};
    currency: string;
    updated_at: string;
    jobs: {
        count: number;
        cost: string | null;
        savings: string | null;
        cpu_efficiency_pct: number | null;
        cpu_efficiency_weighted_pct: number | null;
        memory_efficiency_pct: number | null;
        walltime_efficiency_pct: number | null;
        wasted_core_hours: number | null;
        wasted_cost: string | null;
        jobs_with_efficiency: number;
        by_user?: Ranked[];
        by_project: Ranked[];
        by_queue: Ranked[];
        by_instance_family: Ranked[];
        daily_by_project: {date: string; project: string; cost: string}[];
        costliest: JobRow[];
        least_efficient: JobRow[];
    };
    desktops: {
        cost: string | null;
        hours: number | null;
        count: number;
        by_user?: Ranked[];
        by_project: Ranked[];
        daily_top_users: {date: string; user: string; cost: string}[];
        desktops_with_activity: number;
        checked_hours: number | null;
        idle_hours: number | null;
        idle_cost: string | null;
        idle_by_user?: Ranked[];
        idle_by_project: Ranked[];
        least_efficient: DesktopRow[];
    };
    storage: {
        cost: string | null;
        used_bytes: number | null;
        by_user?: {name: string; bytes: number; cost: string | null}[];
        tier_daily: {date: string; tier: 'ssd' | 'capacity_pool'; bytes: number}[];
    };
    budgets: ReportingBudget[];
    notes: string[];
}
export interface ReportingSummary {
    users: string[];
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
    username?: string;
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
export type ReportingExportRequest = Omit<ReportingRowsRequest, 'paginator'> & {columns: ReportingColumn[]; username?: string};
export interface ReportingCsv {
    filename: string;
    content_type: 'text/csv;charset=utf-8';
    content: string;
    row_count: number;
    as_of: string | null;
}
