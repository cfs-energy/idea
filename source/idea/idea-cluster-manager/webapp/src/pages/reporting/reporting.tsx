import {useEffect, useRef, useState, useSyncExternalStore} from 'react';
import {withRouter} from '../../navigation/navigation-utils';
import {useLocation, useNavigate, useSearchParams} from 'react-router-dom';
import {Alert, Box, Button, CollectionPreferencesProps, ContentLayout, Header, SpaceBetween, StatusIndicator, Tabs} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {ReportingColumn, ReportingInsights, ReportingRows, ReportingSummary, ReportingSummaryRequest, ReportingTable as TableKind} from '../../client/reporting-model';
import ReportingPeriodPicker, {REPORTING_PERIODS, validateReportingPeriod} from './reporting-period-picker';
import ReportingTable, {DEFAULT_COLUMNS, REPORTING_COLUMNS, availableColumns} from './reporting-table';
import {InfoTitle, InsightTab} from './insights-components';
import {date, updated} from './reporting-format';

export function ReportingContent() {
    const context = AppContext.get();
    const auth = context.auth();
    const access = useSyncExternalStore(listener => auth.subscribeReporting(listener), () => `${auth.isReportingResolved()}:${auth.canReadReporting()}`);
    const allowed = access === 'true:true';
    const client = context.reporting();
    const [query, setQuery] = useSearchParams();
    const location = useLocation();
    const navigate = useNavigate();
    const period: ReportingSummaryRequest = {
        period: REPORTING_PERIODS.find(option => option.value === query.get('period'))?.value as ReportingSummaryRequest['period'] ?? 'this_month',
        ...(query.get('period') === 'custom' ? {start_date: query.get('start_date') ?? '', end_date: query.get('end_date') ?? ''} : {})
    };
    const periodKey = JSON.stringify(period);
    const tabPaths: Record<string, string> = {overview: '/reporting', jobs: '/reporting/jobs', desktops: '/reporting/desktops', storage: '/reporting/storage', user: '/reporting/users', project: '/reporting/projects'};
    const routeTab = Object.entries(tabPaths).find(([, path]) => path === location.pathname)?.[0] ?? 'overview';
    const tab = ['overview', 'jobs', 'desktops', 'storage', 'user', 'project'].includes(query.get('table') ?? '') ? query.get('table')! : routeTab;
    const table: TableKind = tab === 'project' ? 'project' : 'user';
    const tableVisible = tab === 'user' || tab === 'project';
    const sortBy = (!tableVisible ? 'spend_total' : REPORTING_COLUMNS.find(column => column.id === query.get('sort_by'))?.id ?? 'spend_total') as ReportingColumn;
    const descending = !tableVisible || query.get('descending') !== 'false';
    const [snapshot, setSnapshot] = useState<{key: string; data: ReportingSummary}>();
    const [insights, setInsights] = useState<ReportingInsights>();
    const [summaryBusy, setSummaryBusy] = useState(false);
    const [summaryError, setSummaryError] = useState('');
    const [rowError, setRowError] = useState('');
    const [exportStatus, setExportStatus] = useState('');
    const [exportError, setExportError] = useState('');
    const [exportBusy, setExportBusy] = useState(false);
    const [expiredId, setExpiredId] = useState('');
    const [reload, setReload] = useState(0);
    const [retryRows, setRetryRows] = useState(0);
    const [pageSize, setPageSize] = useState(25);
    const [columns, setColumns] = useState<CollectionPreferencesProps.ContentDisplayItem[]>(DEFAULT_COLUMNS);
    const summary = snapshot?.key === periodKey ? snapshot.data : undefined;
    const [timezone, setTimezone] = useState<string>();
    const validation = validateReportingPeriod(period, timezone);
    const expired = !!summary && (expiredId === summary.snapshot_id || Date.parse(summary.expires_at) <= Date.now());
    const effectivePageSize = tableVisible ? pageSize : 25;
    const rowKey = JSON.stringify([summary?.snapshot_id, tab, sortBy, descending, effectivePageSize]);
    const [paging, setPaging] = useState<{key: string; page: number; cursors: (string | undefined)[]}>({key: '', page: 1, cursors: [undefined]});
    const page = paging.key === rowKey ? paging.page : 1;
    const cursor = paging.key === rowKey ? paging.cursors[page - 1] : undefined;
    const [rows, setRows] = useState<{key: string; page: number; data: ReportingRows}>();
    const [rowsBusy, setRowsBusy] = useState(false);
    const data = rows?.key === rowKey ? rows.data : undefined;
    const selectionKey = JSON.stringify([periodKey, rowKey, columns, allowed]);
    const activeSelection = useRef(selectionKey);
    activeSelection.current = selectionKey;
    const mounted = useRef(true);
    useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);

    function errorText(error: unknown): string {
        const result = error as {errorCode?: string; message?: string; payload?: {guidance?: string}};
        if (result.errorCode === 'REPORT_SNAPSHOT_EXPIRED' || result.errorCode === 'REPORT_EXPIRED') setExpiredId(summary?.snapshot_id ?? '');
        return 'Could not load the report. Reload or choose a shorter period.';
    }

    useEffect(() => {
        if (!allowed || validation) {setSummaryBusy(false); return;}
        let current = true;
        setSummaryBusy(true);
        setSummaryError('');
        setSnapshot(undefined);
        setInsights(undefined);
        setRowError('');
        setExportStatus('');
        setExportError('');
        Promise.all([client.getSummary(JSON.parse(periodKey)), client.getInsights(JSON.parse(periodKey))]).then(([result, insights]) => {
            if (!current) return;
            setTimezone(result.timezone);
            setInsights(insights);
            setSnapshot({key: periodKey, data: result});
        }).catch(error => {if (current) setSummaryError(errorText(error));})
            .finally(() => {if (current) setSummaryBusy(false);});
        return () => {current = false;};
    }, [client, allowed, periodKey, reload, validation]);

    useEffect(() => {
        if (!summary) return;
        const timeout = setTimeout(() => setExpiredId(summary.snapshot_id), Math.max(0, Date.parse(summary.expires_at) - Date.now()));
        return () => clearTimeout(timeout);
    }, [summary]);

    useEffect(() => {
        if (!allowed || !summary || !tableVisible || expired || validation) {setRowsBusy(false); return;}
        let current = true;
        setRowsBusy(true);
        setRowError('');
        client.listRows({snapshot_id: summary.snapshot_id, table, sort_by: sortBy, descending, paginator: {page_size: effectivePageSize, ...(cursor ? {cursor} : {})}})
            .then(result => {
                if (!current) return;
                setRows({key: rowKey, page, data: result});
                setPaging(previous => {
                    const cursors = previous.key === rowKey ? [...previous.cursors] : [undefined];
                    cursors[page] = result.paginator.cursor ?? undefined;
                    return {key: rowKey, page, cursors};
                });
            }).catch(error => {if (current) setRowError(errorText(error));})
            .finally(() => {if (current) setRowsBusy(false);});
        return () => {current = false;};
    }, [client, allowed, summary, tableVisible, tab, expired, validation, rowKey, page, cursor, retryRows]);

    const changePeriod = (value: ReportingSummaryRequest) => {
        const next = new URLSearchParams(query);
        next.set('period', value.period);
        next.delete('start_date'); next.delete('end_date');
        if (value.start_date) next.set('start_date', value.start_date);
        if (value.end_date) next.set('end_date', value.end_date);
        setQuery(next);
    };
    const exportCsv = async () => {
        if (!summary || expired || exportBusy || summaryBusy || rowsBusy || !allowed) return;
        const selected = selectionKey;
        setExportBusy(true); setExportError(''); setExportStatus('');
        try {
            const result = await client.exportCsv({snapshot_id: summary.snapshot_id, table, sort_by: sortBy, descending, columns: columns.filter(column => column.visible && availableColumns(data).some(item => item.id === column.id)).map(column => column.id as ReportingColumn)});
            if (!mounted.current || activeSelection.current !== selected || Date.parse(summary.expires_at) <= Date.now()) return;
            const url = URL.createObjectURL(new Blob([result.content], {type: result.content_type}));
            const link = document.createElement('a');
            try {
                link.href = url; link.download = result.filename;
                document.body.appendChild(link); link.click();
            } finally {
                link.remove(); URL.revokeObjectURL(url);
            }
            setExportStatus(`Downloaded ${result.row_count} rows across all pages.`);
        } catch (error) {
            if (mounted.current && activeSelection.current === selected) setExportError(errorText(error));
        } finally {
            if (mounted.current) setExportBusy(false);
        }
    };

    if (!auth.isReportingResolved()) return <StatusIndicator type="loading">Checking access</StatusIndicator>;
    if (!allowed) return <Alert type="error">Access denied. Ask your administrator for reporting access.</Alert>;
    const error = validation || summaryError || exportError || rowError;
    return <ContentLayout header={<Header variant="h2" description="Estimated costs and resource use" actions={<SpaceBetween direction="horizontal" size="s">
        <Button onClick={() => setReload(value => value + 1)} disabled={summaryBusy}>Reload</Button>
        {tableVisible && <Button onClick={exportCsv} loading={exportBusy} disabled={!summary || expired || summaryBusy || rowsBusy || !columns.some(column => column.visible && availableColumns(data).some(item => item.id === column.id))}>Export CSV</Button>}
    </SpaceBetween>}>Cost and activity overview</Header>}>
        <SpaceBetween size="l">
            <ReportingPeriodPicker value={period} timezone={timezone} onChange={changePeriod}/>
            {summaryBusy && <StatusIndicator type="loading">Loading report</StatusIndicator>}
            {error ? <Alert type="error" action={rowError && !expired ? <Button onClick={() => setRetryRows(value => value + 1)}>Retry rows</Button> : undefined}>{error}</Alert>
                : expired && <Alert type="warning">This report has expired. Reload to see current data or export CSV.</Alert>}
            {summary && insights && <SpaceBetween size="m">
                <Box>{date(insights.period.start, summary.timezone)} – {date(insights.period.end, summary.timezone)} · {updated(insights.updated_at, summary.timezone)} {insights.notes.length > 0 && <InfoTitle title="Report details">{insights.notes.slice(0, 3).join(' ')}</InfoTitle>}</Box>
                <Tabs activeTabId={tab} onChange={({detail}) => {
                    const next = new URLSearchParams(query); next.set('table', detail.activeTabId);
                    navigate({pathname: tabPaths[detail.activeTabId], search: next.toString()});
                }} tabs={[{id: 'overview', label: 'Overview'}, {id: 'jobs', label: 'Jobs'}, {id: 'desktops', label: 'Desktops'}, {id: 'storage', label: 'Storage'}, {id: 'user', label: 'By user'}, {id: 'project', label: 'By project'}]}/>
                {tableVisible ? <ReportingTable key={table} table={table} data={data} currency={summary.currency} timezone={summary.timezone} loading={rowsBusy} disabled={expired || summaryBusy || exportBusy}
                    sortBy={sortBy} descending={descending} page={page} pageSize={pageSize} columns={columns}
                    onPage={page => setPaging(previous => ({...previous, page}))}
                    onSort={(column, descending) => {const next = new URLSearchParams(query); next.set('sort_by', column); next.set('descending', String(descending)); setQuery(next);}}
                    onPreferences={(pageSize, columns) => {setPageSize(pageSize); setColumns(columns);}}/>
                    : <InsightTab tab={tab} insights={insights} summary={summary} timezone={summary.timezone}/>}
            </SpaceBetween>}
            <div role="status" aria-label="CSV download status" aria-live="polite">{exportStatus}</div>
        </SpaceBetween>
    </ContentLayout>;
}

function Reporting(props: IdeaAppLayoutProps) {
    return <IdeaAppLayout {...props} content={<ReportingContent/>}/>;
}

export default withRouter(Reporting);
