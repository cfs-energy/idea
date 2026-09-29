import {useEffect, useRef, useState, useSyncExternalStore} from 'react';
import {withRouter} from '../../navigation/navigation-utils';
import {useLocation, useNavigate, useSearchParams} from 'react-router-dom';
import {Alert, Box, Button, CollectionPreferencesProps, ColumnLayout, Container, ContentLayout, Header, SpaceBetween, StatusIndicator, Tabs} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {ReportingColumn, ReportingRows, ReportingSummary, ReportingSummaryRequest, ReportingTable as TableKind} from '../../client/reporting-model';
import ReportingPeriodPicker, {REPORTING_PERIODS, validateReportingPeriod} from './reporting-period-picker';
import ReportingTable, {DEFAULT_COLUMNS, REPORTING_COLUMNS, ReportingCoverageList, ReportingMetric} from './reporting-table';

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
    const routeTable = location.pathname.endsWith('/projects') ? 'project' : location.pathname.endsWith('/facets') ? 'facet' : 'user';
    const table: TableKind = ['user', 'project', 'facet'].includes(query.get('table') ?? '') ? query.get('table') as TableKind : routeTable;
    const sortBy = (REPORTING_COLUMNS.find(column => column.id === query.get('sort_by'))?.id ?? 'spend_total') as ReportingColumn;
    const descending = query.get('descending') !== 'false';
    const [snapshot, setSnapshot] = useState<{key: string; data: ReportingSummary}>();
    const [summaryBusy, setSummaryBusy] = useState(false);
    const [summaryError, setSummaryError] = useState('');
    const [rowError, setRowError] = useState('');
    const [exportStatus, setExportStatus] = useState('');
    const [exportError, setExportError] = useState('');
    const [exportBusy, setExportBusy] = useState(false);
    const [expiredId, setExpiredId] = useState('');
    const [reload, setReload] = useState(0);
    const [retryRows, setRetryRows] = useState(0);
    const [pageSize, setPageSize] = useState(50);
    const [columns, setColumns] = useState<CollectionPreferencesProps.ContentDisplayItem[]>(DEFAULT_COLUMNS);
    const summary = snapshot?.key === periodKey ? snapshot.data : undefined;
    const [timezone, setTimezone] = useState<string>();
    const validation = validateReportingPeriod(period, timezone);
    const expired = !!summary && (expiredId === summary.snapshot_id || Date.parse(summary.expires_at) <= Date.now());
    const rowKey = JSON.stringify([summary?.snapshot_id, table, sortBy, descending, pageSize]);
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
        return [result.errorCode, result.message ?? 'Reporting request failed. Retry or select a narrower period.', result.payload?.guidance].filter(Boolean).join(': ');
    }

    useEffect(() => {
        if (!allowed || validation) {setSummaryBusy(false); return;}
        let current = true;
        setSummaryBusy(true);
        setSummaryError('');
        setSnapshot(undefined);
        setExportStatus('');
        setExportError('');
        client.getSummary(JSON.parse(periodKey)).then(result => {
            if (!current) return;
            setTimezone(result.timezone);
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
        if (!allowed || !summary || expired || validation) {setRowsBusy(false); return;}
        let current = true;
        setRowsBusy(true);
        setRowError('');
        client.listRows({snapshot_id: summary.snapshot_id, table, sort_by: sortBy, descending, paginator: {page_size: pageSize, ...(cursor ? {cursor} : {})}})
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
    }, [client, allowed, summary, expired, validation, rowKey, page, cursor, retryRows]);

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
            const result = await client.exportCsv({snapshot_id: summary.snapshot_id, table, sort_by: sortBy, descending, columns: columns.filter(column => column.visible).map(column => column.id as ReportingColumn)});
            if (!mounted.current || activeSelection.current !== selected || Date.parse(summary.expires_at) <= Date.now()) return;
            const url = URL.createObjectURL(new Blob([result.content], {type: result.content_type}));
            const link = document.createElement('a');
            try {
                link.href = url; link.download = result.filename;
                document.body.appendChild(link); link.click();
            } finally {
                link.remove(); URL.revokeObjectURL(url);
            }
            setExportStatus(`Downloaded ${result.row_count} rows across all pages; source as of ${result.as_of ?? 'unavailable'}.`);
        } catch (error) {
            if (mounted.current && activeSelection.current === selected) setExportError(errorText(error));
        } finally {
            if (mounted.current) setExportBusy(false);
        }
    };

    if (!auth.isReportingResolved()) return <StatusIndicator type="loading">Checking Reporting access</StatusIndicator>;
    if (!allowed) return <Alert type="error">Access denied</Alert>;
    return <ContentLayout header={<Header variant="h2" actions={<SpaceBetween direction="horizontal" size="s">
        <Button onClick={() => setReload(value => value + 1)} disabled={summaryBusy}>Reload snapshot</Button>
        <Button onClick={exportCsv} loading={exportBusy} disabled={!summary || expired || summaryBusy || rowsBusy || !columns.some(column => column.visible)}>Export CSV</Button>
    </SpaceBetween>}>Cost and activity overview</Header>}>
        <SpaceBetween size="l">
            <ReportingPeriodPicker value={period} timezone={timezone} onChange={changePeriod}/>
            {validation && <Alert type="error">{validation}</Alert>}
            {summaryBusy && <StatusIndicator type="loading">Building reporting snapshot</StatusIndicator>}
            {summaryError && <Alert type="error">{summaryError}</Alert>}
            {expired && <Alert type="warning">Snapshot expired. Reload explicitly to view current data or export CSV.</Alert>}
            {summary && <SpaceBetween size="m">
                <Box>Returned bounds: {summary.period.start_date} through {summary.period.end_date} (inclusive), {summary.timezone}. Oldest source as of: {summary.as_of ?? 'Unavailable'}.</Box>
                {summary.period.provisional && <Alert type="info">The current day is provisional. Recorded estimates and allocations may change.</Alert>}
                <ColumnLayout columns={4}>
                    <Container header={<Header variant="h3">Total spend (recorded estimates/allocations) ({summary.currency})</Header>}><ReportingMetric value={summary.tiles.total?.spend_total} coverage={summary.tiles.total?.coverage.spend_total ?? summary.coverage.spend_total} unit={summary.currency}/></Container>
                    <Container header={<Header variant="h3">Jobs</Header>}><ReportingMetric value={summary.tiles.total?.job_count} coverage={summary.tiles.total?.coverage.job_count ?? summary.coverage.job_count} unit="jobs"/></Container>
                    <Container header={<Header variant="h3">Estimated desktop hours</Header>}><ReportingMetric value={summary.tiles.total?.desktop_hours} coverage={summary.tiles.total?.coverage.desktop_hours ?? summary.coverage.desktop_hours} unit="hours"/></Container>
                    <Container header={<Header variant="h3" description="by recorded job-compute spend; other facets unallocated">Top project</Header>}>
                        {summary.tiles.top_project?.spend_total != null ? <SpaceBetween size="xs"><Box>{summary.tiles.top_project.label}</Box><ReportingMetric value={summary.tiles.top_project.spend_total} coverage={summary.tiles.top_project.coverage.spend_total} unit={summary.currency}/></SpaceBetween> : <Box>Unavailable: No priced projects.</Box>}
                    </Container>
                </ColumnLayout>
                <Alert type="warning">Totals cover recorded values only; partial subtotals and unavailable facets make totals incomplete. Project desktop, disk, storage and AI spend is unavailable in v1. "Unallocated to project" is separate from missing recorded project "Unassigned"; current membership is never used to allocate spend.</Alert>
                {summary.warnings.map((warning, index) => <Alert key={index} type="warning">{warning}</Alert>)}
                {summary.tiles.job_spend_difference && <Container header={<Header variant="h3">Signed job projection/index timing difference</Header>}>
                    <ReportingMetric value={summary.tiles.job_spend_difference.spend_total} coverage={summary.tiles.job_spend_difference.coverage.spend_total} unit={summary.currency}/>
                </Container>}
                <ReportingCoverageList coverage={summary.coverage}/>
                <Box>Walltime efficiency (elapsed/requested) is duration-weighted and may exceed 100%. It is not CPU/GPU use. Requested and elapsed hours and eligible-job counts describe its coverage. Estimated node-hours = requested nodes × elapsed. Desktop hours estimate creation-to-stop overlap, not exact running time. Idle stops are unavailable and are never inferred. Signed job projection/index timing differences are shown separately when reported.</Box>
                <Tabs activeTabId={table} onChange={({detail}) => {
                    const next = new URLSearchParams(query); next.set('table', detail.activeTabId);
                    navigate({pathname: detail.activeTabId === 'project' ? '/reporting/projects' : detail.activeTabId === 'facet' ? '/reporting/facets' : '/reporting', search: next.toString()});
                }} tabs={[{id: 'user', label: 'Overview / By user'}, {id: 'project', label: 'By project'}, {id: 'facet', label: 'By facet'}]}/>
                {rowError && <Alert type="error" action={!expired && <Button onClick={() => setRetryRows(value => value + 1)}>Retry rows</Button>}>{rowError}{data && ' Previously loaded rows are retained.'}</Alert>}
                {data?.warnings.map((warning, index) => <Alert key={index} type="warning">{warning}</Alert>)}
                {data && <Box>{data.total_rows} recorded rows across all pages.{rows?.page !== page && ' Showing the previous page until the requested page loads.'}</Box>}
                {data && <ReportingCoverageList coverage={data.coverage}/>}
                <ReportingTable table={table} data={data} currency={summary.currency} loading={rowsBusy} disabled={expired || summaryBusy || exportBusy}
                    sortBy={sortBy} descending={descending} page={page} pageSize={pageSize} columns={columns}
                    onPage={page => setPaging(previous => ({...previous, page}))}
                    onSort={(column, descending) => {const next = new URLSearchParams(query); next.set('sort_by', column); next.set('descending', String(descending)); setQuery(next);}}
                    onPreferences={(pageSize, columns) => {setPageSize(pageSize); setColumns(columns);}}/>
            </SpaceBetween>}
            {exportError && <Alert type="error">{exportError} Rows have been retained.</Alert>}
            <div role="status" aria-label="CSV download status" aria-live="polite">{exportStatus}</div>
        </SpaceBetween>
    </ContentLayout>;
}

function Reporting(props: IdeaAppLayoutProps) {
    return <IdeaAppLayout {...props} content={<ReportingContent/>}/>;
}

export default withRouter(Reporting);
