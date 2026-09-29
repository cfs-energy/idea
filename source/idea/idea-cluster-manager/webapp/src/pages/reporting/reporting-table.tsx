import {useState} from 'react';
import {Box, CollectionPreferences, CollectionPreferencesProps, Pagination, Table, TextFilter} from '@cloudscape-design/components';
import {ReportingColumn, ReportingNumber, ReportingRow, ReportingRows, ReportingTable as TableKind} from '../../client/reporting-model';
import {hours, metricInfo, money, percent} from './reporting-format';
import {InfoTitle, Missing} from './insights-components';

export const REPORTING_COLUMNS: {id: ReportingColumn; label: string; unit?: string; definition?: string}[] = [
    {id: 'label', label: 'Name'},
    {id: 'spend_total', label: 'Total spend'},
    {id: 'jobs', label: 'Job spend'},
    {id: 'desktops', label: 'Desktop spend'},
    {id: 'desktop_disks', label: 'Desktop disk spend'},
    {id: 'shared_storage', label: 'Shared storage spend'},
    {id: 'ai', label: 'AI spend'},
    {id: 'job_count', label: 'Jobs', unit: 'jobs', definition: 'Number of jobs that finished in this period.'},
    {id: 'node_hours', label: 'Node-hours', unit: 'hours', definition: 'Requested nodes multiplied by hours used.'},
    {id: 'requested_walltime_hours', label: 'Requested hours', unit: 'hours', definition: 'Total time requested by finished jobs.'},
    {id: 'elapsed_hours', label: 'Elapsed hours', unit: 'hours', definition: 'Total time used by finished jobs.'},
    {id: 'efficiency_pct', label: 'Walltime efficiency', unit: '%', definition: 'Total time used divided by total time requested; this can exceed 100%.'},
    {id: 'desktop_hours', label: 'Desktop hours', unit: 'hours', definition: 'Time between desktop creation and stopping within this period.'},
    {id: 'idle_stops', label: 'Idle stops', unit: 'count', definition: 'Number of desktops stopped because they were idle.'}
];
export const DEFAULT_COLUMNS: CollectionPreferencesProps.ContentDisplayItem[] = REPORTING_COLUMNS.map(({id}) => ({id, visible: true}));
const FACETS = ['jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai'];
export const rowValue = (row: ReportingRow, column: ReportingColumn): ReportingNumber | null | undefined => FACETS.includes(column) ? row.spend_by_facet[column as keyof typeof row.spend_by_facet] : row[column as keyof ReportingRow] as ReportingNumber | null;
export const availableColumns = (data?: ReportingRows) => REPORTING_COLUMNS.filter(column => column.id === 'label' || data?.listing.some(row => rowValue(row, column.id) != null));
export function ReportingMetric({value, unit = '', currency = 'USD'}: {value: ReportingNumber | null | undefined; unit?: string; currency?: string}) {
    if (value == null) return <Missing/>;
    return <span>{unit === '%' ? percent(value) : unit === 'hours' ? hours(value) : unit === 'jobs' || unit === 'count' ? Number(value).toLocaleString('en-US') : money(value, currency)}</span>;
}
export default function ReportingTable({table, data, currency, timezone = 'UTC', loading, disabled, sortBy, descending, page, pageSize, columns, onSort, onPage, onPreferences}: {
    table: TableKind; data?: ReportingRows; currency: string; timezone?: string; loading: boolean; disabled: boolean;
    sortBy: ReportingColumn; descending: boolean; page: number; pageSize: number; columns: CollectionPreferencesProps.ContentDisplayItem[];
    onSort: (column: ReportingColumn, descending: boolean) => void; onPage: (page: number) => void;
    onPreferences: (pageSize: number, columns: CollectionPreferencesProps.ContentDisplayItem[]) => void;
}) {
    const [filter, setFilter] = useState('');
    const available = availableColumns(data);
    const definitions = available.map(column => ({id: column.id, sortingField: column.id,
        header: column.id === 'label' ? 'Name' : <InfoTitle title={column.label}>{metricInfo(data?.coverage[column.id] ?? data?.coverage[`spend_by_facet.${column.id}`] ?? data?.listing.find(row => row.coverage[column.id])?.coverage[column.id], timezone, column.definition)}</InfoTitle>,
        cell: (row: ReportingRow) => column.id === 'label' ? (row.key === '!unallocated' ? 'No project' : row.label) : <ReportingMetric value={rowValue(row, column.id)} unit={column.unit} currency={currency}/>
    }));
    return <Table<ReportingRow> ariaLabels={{tableLabel: `Reporting by ${table}`}} items={(data?.listing ?? []).filter(row => available.some(column => String(rowValue(row, column.id) ?? '').toLowerCase().includes(filter.toLowerCase())))}
        columnDefinitions={definitions} columnDisplay={columns.filter(column => available.some(item => item.id === column.id))} trackBy="key"
        loading={loading} loadingText="Loading rows" sortingDisabled={disabled || loading}
        sortingColumn={definitions.find(column => column.id === sortBy)} sortingDescending={descending}
        onSortingChange={({detail}) => onSort(detail.sortingColumn.sortingField as ReportingColumn, detail.isDescending ?? false)}
        filter={<TextFilter filteringText={filter} onChange={({detail}) => setFilter(detail.filteringText)} filteringAriaLabel="Find on this page" filteringPlaceholder="Find on this page"/>}
        empty={<Box textAlign="center">{filter ? 'No matches on this page' : 'No costs or activity in this period'}</Box>}
        pagination={<Pagination currentPageIndex={page} pagesCount={page + (data?.paginator.cursor ? 1 : 0)} openEnd={!!data?.paginator.cursor} disabled={disabled || loading}
            onChange={({detail}) => onPage(detail.currentPageIndex)} ariaLabels={{nextPageLabel: 'Next page', previousPageLabel: 'Previous page', pageLabel: page => `Page ${page}`}}/>}
        preferences={<CollectionPreferences title="Reporting preferences" confirmLabel="Confirm" cancelLabel="Cancel" closeAriaLabel="Close preferences" disabled={disabled || loading}
            preferences={{pageSize, contentDisplay: columns.filter(column => available.some(item => item.id === column.id))}}
            pageSizePreference={{title: 'Page size', options: [25, 50, 100, 200].map(value => ({value, label: `${value} rows`}))}}
            contentDisplayPreference={{dragHandleAriaLabel: 'Reorder column', dragHandleAriaDescription: 'Press space to pick up, arrow keys to move, and space to drop. Escape cancels.', liveAnnouncementDndStarted: position => `Picked up column ${position}.`, liveAnnouncementDndItemReordered: (_, position) => `Column moved to position ${position}.`, liveAnnouncementDndItemCommitted: (_, position) => `Column placed at position ${position}.`, liveAnnouncementDndDiscarded: 'Column move cancelled.', title: 'Columns and order', description: 'The selected column order is also used for CSV downloads.', options: available.map(column => ({id: column.id, label: column.label}))}}
            onConfirm={({detail}) => onPreferences(Math.max(25, Math.min(200, detail.pageSize ?? 25)), [...(detail.contentDisplay ?? columns), ...columns.filter(column => !available.some(item => item.id === column.id))])}/>}/>;
}
