import {Box, CollectionPreferences, CollectionPreferencesProps, Pagination, SpaceBetween, Table} from '@cloudscape-design/components';
import {MetricCoverage, ReportingColumn, ReportingCoverage, ReportingNumber, ReportingRow, ReportingRows, ReportingTable as TableKind} from '../../client/reporting-model';

export const REPORTING_COLUMNS: {id: ReportingColumn; label: string; unit?: string}[] = [
    {id: 'label', label: 'Recorded label'},
    {id: 'spend_total', label: 'Total spend (recorded estimates/allocations)'},
    {id: 'jobs', label: 'Job-compute spend'},
    {id: 'desktops', label: 'Desktop spend'},
    {id: 'desktop_disks', label: 'Desktop disk spend'},
    {id: 'shared_storage', label: 'Shared storage spend'},
    {id: 'ai', label: 'AI spend'},
    {id: 'job_count', label: 'Jobs', unit: 'jobs'},
    {id: 'node_hours', label: 'Estimated node-hours', unit: 'node-hours'},
    {id: 'requested_walltime_hours', label: 'Requested walltime', unit: 'hours'},
    {id: 'elapsed_hours', label: 'Elapsed time', unit: 'hours'},
    {id: 'efficiency_pct', label: 'Walltime efficiency (elapsed/requested)', unit: '%'},
    {id: 'desktop_hours', label: 'Estimated desktop hours', unit: 'hours'},
    {id: 'idle_stops', label: 'Idle stops'}
];
export const DEFAULT_COLUMNS: CollectionPreferencesProps.ContentDisplayItem[] = REPORTING_COLUMNS.map(({id}) => ({id, visible: true}));
const FACETS = ['jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai'];
const JOB_METRICS = ['job_count', 'node_hours', 'requested_walltime_hours', 'elapsed_hours', 'efficiency_pct'];

export function CoverageDetails({coverage}: {coverage: ReportingCoverage}) {
    const reason = coverage.reason ?? '';
    const label = /not.deployed|disabled/i.test(reason) ? 'Not deployed' : /no.head|collecting|not.yet/i.test(reason) ? 'Collecting / not yet available' : coverage.status === 'not_applicable' ? 'Not applicable' : coverage.status === 'unavailable' ? 'Unavailable' : coverage.status === 'partial' ? 'Partial subtotal' : coverage.status === 'estimated' ? 'Estimated' : 'Ready';
    return <Box fontSize="body-s">
        {label}{reason && `: ${reason}`}
        {coverage.source_as_of && `; source as of ${coverage.source_as_of}`}
        {(coverage.available_start || coverage.available_end) && `; available bounds ${coverage.available_start ?? 'unknown'} to ${coverage.available_end ?? 'unknown'}`}
        {coverage.missing_days > 0 && `; missing history: ${coverage.missing_days} days`}
        {coverage.missing_records > 0 && `; ${coverage.missing_records} missing records`}
        {coverage.total_count > 0 && `; ${coverage.eligible_count}/${coverage.total_count} eligible records`}
        {coverage.freshness_spread_seconds != null && `; freshness spread ${coverage.freshness_spread_seconds} seconds`}
    </Box>;
}

export function ReportingMetric({value, coverage, unit = ''}: {value: ReportingNumber | null | undefined; coverage?: ReportingCoverage; unit?: string}) {
    if (coverage?.status === 'not_applicable' && /not.deployed|disabled/i.test(coverage.reason)) return <span>Not deployed: {coverage.reason}</span>;
    if (coverage?.status === 'not_applicable') return <span aria-label="Not applicable">—</span>;
    return <SpaceBetween size="xxs">
        <span>{value == null ? 'Unavailable' : `${value}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`}{value != null && coverage?.status === 'unavailable' ? ' (stale retained value)' : ''}</span>
        {coverage ? <CoverageDetails coverage={coverage}/> : value == null ? <Box fontSize="body-s">No recorded value or coverage supplied.</Box> : null}
    </SpaceBetween>;
}

export function ReportingCoverageList({coverage}: {coverage: MetricCoverage}) {
    return <SpaceBetween size="xs">{Object.entries(coverage).map(([metric, details]) => <div key={metric}><strong>{metric.replaceAll('_', ' ')}: </strong>{metric.startsWith('source_') && details.status === 'not_applicable' ? <Box>Not deployed</Box> : <CoverageDetails coverage={details}/>}</div>)}</SpaceBetween>;
}

export default function ReportingTable({table, data, currency, loading, disabled, sortBy, descending, page, pageSize, columns, onSort, onPage, onPreferences}: {
    table: TableKind;
    data?: ReportingRows;
    currency: string;
    loading: boolean;
    disabled: boolean;
    sortBy: ReportingColumn;
    descending: boolean;
    page: number;
    pageSize: number;
    columns: CollectionPreferencesProps.ContentDisplayItem[];
    onSort: (column: ReportingColumn, descending: boolean) => void;
    onPage: (page: number) => void;
    onPreferences: (pageSize: number, columns: CollectionPreferencesProps.ContentDisplayItem[]) => void;
}) {
    const definitions = REPORTING_COLUMNS.map(column => ({
        id: column.id,
        header: `${column.label}${FACETS.includes(column.id) || column.id === 'spend_total' ? ` (${currency})` : ''}`,
        sortingField: column.id,
        cell: (row: ReportingRow) => {
            if (column.id === 'label') return row.label;
            const coverage = row.coverage[column.id] ?? row.coverage[`spend_by_facet.${column.id}`];
            if (table === 'facet' && ((JOB_METRICS.includes(column.id) && row.key !== 'jobs') || (['desktop_hours', 'idle_stops'].includes(column.id) && row.key !== 'desktops') || (FACETS.includes(column.id) && row.key !== column.id))) return <span aria-label="Not applicable">—</span>;
            if (column.id === 'idle_stops') return <span>Unavailable: {coverage?.reason || 'Idle stops are not recorded.'}</span>;
            if (table === 'project' && row.key !== '!unallocated' && FACETS.includes(column.id) && column.id !== 'jobs') return <span>Unavailable: Project attribution is unavailable in v1.</span>;
            const value = FACETS.includes(column.id) ? row.spend_by_facet[column.id as keyof typeof row.spend_by_facet] : row[column.id as keyof ReportingRow] as ReportingNumber | null;
            return <SpaceBetween size="xxs"><ReportingMetric value={value} coverage={coverage} unit={column.unit ?? currency}/>
                {column.id === 'efficiency_pct' && coverage && <Box fontSize="body-s">{coverage.eligible_count} eligible jobs / {coverage.total_count} total jobs</Box>}
            </SpaceBetween>;
        }
    }));
    return <Table<ReportingRow>
        ariaLabels={{tableLabel: `Reporting by ${table}`, allItemsSelectionLabel: () => 'All rows', itemSelectionLabel: () => 'Row'}}
        items={data?.listing ?? []} columnDefinitions={definitions} columnDisplay={columns} trackBy="key"
        loading={loading} loadingText="Loading reporting rows" sortingDisabled={disabled || loading}
        sortingColumn={definitions.find(column => column.id === sortBy)} sortingDescending={descending}
        onSortingChange={({detail}) => onSort(detail.sortingColumn.sortingField as ReportingColumn, detail.isDescending ?? false)}
        empty={<Box textAlign="center">{data ? data.coverage.spend_total?.status === 'unavailable' ? /collecting|no.head|not.yet/i.test(data.coverage.spend_total.reason) ? 'Collecting / not yet available. See source coverage.' : 'Unavailable. See source coverage.' : 'No recorded rows for this period. See source coverage.' : 'Rows are not yet available.'}</Box>}
        pagination={<Pagination currentPageIndex={page} pagesCount={page + (data?.paginator.cursor ? 1 : 0)} openEnd={!!data?.paginator.cursor} disabled={disabled || loading}
            onChange={({detail}) => onPage(detail.currentPageIndex)} ariaLabels={{nextPageLabel: 'Next page', previousPageLabel: 'Previous page', pageLabel: page => `Page ${page}`}}/>}
        preferences={<CollectionPreferences title="Reporting preferences" confirmLabel="Confirm" cancelLabel="Cancel" closeAriaLabel="Close preferences" disabled={disabled || loading}
            preferences={{pageSize, contentDisplay: columns}}
            pageSizePreference={{title: 'Page size', options: [1, 25, 50, 100, 200].map(value => ({value, label: `${value} rows`}))}}
            contentDisplayPreference={{dragHandleAriaLabel: 'Reorder column', dragHandleAriaDescription: 'Press space to pick up, arrow keys to move, and space to drop. Escape cancels.', liveAnnouncementDndStarted: position => `Picked up column ${position}.`, liveAnnouncementDndItemReordered: (_, position) => `Column moved to position ${position}.`, liveAnnouncementDndItemCommitted: (_, position) => `Column placed at position ${position}.`, liveAnnouncementDndDiscarded: 'Column move cancelled.', title: 'Columns and order', description: 'The selected column order is also used for CSV downloads.', options: REPORTING_COLUMNS.map(column => ({id: column.id, label: column.label}))}}
            onConfirm={({detail}) => onPreferences(Math.max(1, Math.min(200, detail.pageSize ?? 50)), [...(detail.contentDisplay ?? columns)])}/>}
    />;
}
