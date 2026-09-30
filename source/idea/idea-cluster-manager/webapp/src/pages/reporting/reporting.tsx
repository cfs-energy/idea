import {useEffect, useRef, useState, useSyncExternalStore} from 'react';
import {withRouter} from '../../navigation/navigation-utils';
import {useLocation, useNavigate, useSearchParams} from 'react-router-dom';
import {Alert, Box, Button, CollectionPreferencesProps, ContentLayout, Header, SpaceBetween, StatusIndicator, Tabs, SegmentedControl, FormField, Select} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {ReportingColumn, ReportingInsights, ReportingRows, ReportingSummary, ReportingSummaryRequest, ReportingTable as TableKind} from '../../client/reporting-model';
import ReportingPeriodPicker, {REPORTING_PERIODS, validateReportingPeriod} from './reporting-period-picker';
import ReportingTable, {DEFAULT_COLUMNS, REPORTING_COLUMNS} from './reporting-table';
import {InfoTitle, InsightTab} from './insights-components';
import {readPreferences, savePreferences} from './insights-table';
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
    const username = query.get('user') || '';
    const tabPaths: Record<string, string> = {overview: '/reporting', jobs: '/reporting/jobs', desktops: '/reporting/desktops', storage: '/reporting/storage', user: '/reporting/users', project: '/reporting/projects', breakdown: '/reporting/users'};
    const routeTab = Object.entries(tabPaths).find(([, path]) => path === location.pathname)?.[0] ?? 'overview';
    const selectedTab = ['overview', 'jobs', 'desktops', 'storage', 'user', 'project', 'breakdown'].includes(query.get('table') ?? '') ? query.get('table')! : routeTab;
    const tab = ['user', 'project'].includes(selectedTab) ? 'breakdown' : selectedTab;
    const table: TableKind = !username && (query.get('group') === 'project' || (!query.has('group') && selectedTab === 'project')) ? 'project' : 'user';
    const tableVisible = tab === 'breakdown';
    const sortBy = (!tableVisible ? 'spend_total' : REPORTING_COLUMNS.find(column => column.id === query.get('sort_by'))?.id ?? 'spend_total') as ReportingColumn;
    const descending = !tableVisible || query.get('descending') !== 'false';
    const [snapshot, setSnapshot] = useState<{key: string; data: ReportingSummary; insights: ReportingInsights; users: string[]}>();
    const [summaryBusy, setSummaryBusy] = useState(false);
    const [summaryError, setSummaryError] = useState('');
    const [rowError, setRowError] = useState('');
    const [exportStatus, setExportStatus] = useState('');
    const [exportError, setExportError] = useState('');
    const [exportBusy, setExportBusy] = useState(false);
    const [expiredId, setExpiredId] = useState('');
    const [reload, setReload] = useState(0);
    const reloadedSnapshots = useRef(new Set<string>());
    const requestKey = JSON.stringify([periodKey, username, reload]);
    const [retryRows, setRetryRows] = useState(0);
    const [pageSize, setPageSize] = useState(25);
    const [columns, setColumns] = useState<CollectionPreferencesProps.ContentDisplayItem[]>(DEFAULT_COLUMNS);
    useEffect(() => {
        const saved = readPreferences(`reporting.breakdown.${table}`, {pageSize: 25, columns: DEFAULT_COLUMNS});
        setPageSize(saved.pageSize); setColumns(saved.columns);
    }, [table]);
    const summary = snapshot?.key === requestKey && !summaryError ? snapshot.data : undefined;
    const insights = summary ? snapshot?.insights : undefined;
    const [timezone, setTimezone] = useState<string>();
    const validation = validateReportingPeriod(period, timezone);
    const expired = !!summary && expiredId === summary.snapshot_id;
    const effectivePageSize = tableVisible ? pageSize : 25;
    const rowKey = JSON.stringify([requestKey, summary?.snapshot_id, table, sortBy, descending, effectivePageSize]);
    const [paging, setPaging] = useState<{key: string; page: number; cursors: (string | undefined)[]}>({key: '', page: 1, cursors: [undefined]});
    const page = paging.key === rowKey ? paging.page : 1;
    const cursor = paging.key === rowKey ? paging.cursors[page - 1] : undefined;
    const [rows, setRows] = useState<{key: string; table: TableKind; page: number; data: ReportingRows}>();
    const [rowsBusy, setRowsBusy] = useState(false);
    const data = rows?.key === rowKey && rows.page === page && !rowsBusy && !rowError ? rows.data : undefined;
    const selectionKey = JSON.stringify([periodKey, rowKey, columns, allowed]);
    const activeSelection = useRef(selectionKey);
    activeSelection.current = selectionKey;
    const mounted = useRef(true);
    useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);

    function errorText(error: unknown, snapshotId?: string): string {
        const result = error as {errorCode?: string; message?: string; payload?: {guidance?: string}};
        if (result.errorCode === 'REPORT_SNAPSHOT_EXPIRED' || result.errorCode === 'REPORT_EXPIRED' || result.errorCode === 'REPORT_SNAPSHOT_NOT_FOUND') {
            if (snapshotId && !reloadedSnapshots.current.has(snapshotId)) {
                reloadedSnapshots.current.add(snapshotId);
                setExpiredId(snapshotId); setReload(value => value + 1); return '';
            }
            return 'The report is no longer available. Reload to try again.';
        }
        if (result.errorCode === 'UNAUTHORIZED_ACCESS') return 'Reporting access was denied. Ask your administrator for access.';
        if (result.errorCode === 'REPORT_TOO_LARGE') return 'The report is too large to load. Choose a shorter period and try again.';
        if (result.errorCode === 'REPORT_TIMEOUT') return 'The report took too long to load. Choose a shorter period and try again.';
        return "Couldn't load the report. Check your connection and try again.";
    }

    useEffect(() => {
        if (!allowed || validation) {setSummaryBusy(false); return;}
        let current = true;
        setSummaryBusy(true);
        setSummaryError('');
        setRowError('');
        setExportError('');
        Promise.all([client.getSummary(JSON.parse(periodKey)), client.getInsights({...JSON.parse(periodKey), ...(username ? {username} : {})}),
            username ? client.getInsights(JSON.parse(periodKey)) : Promise.resolve(undefined)]).then(([result, insights, unfiltered]) => {
            if (!current) return;
            setExpiredId('');
            setTimezone(result.timezone);
            const options = unfiltered ?? insights;
            const users = Array.from(new Set([options.jobs.by_user, options.desktops.by_user, options.storage.by_user].flatMap(rows => (rows ?? []).map(row => row.name))))
                .filter(name => name !== 'System' && name !== 'Other').sort((a, b) => a.localeCompare(b));
            setSnapshot({key: requestKey, data: result, insights, users});
        }).catch(error => {if (current) setSummaryError(errorText(error));})
            .finally(() => {if (current) setSummaryBusy(false);});
        return () => {current = false;};
    }, [client, allowed, periodKey, username, reload, validation]);

    useEffect(() => {
        if (!allowed || !summary || !tableVisible || expired || validation) {setRowsBusy(false); return;}
        let current = true;
        setRowsBusy(true);
        setRowError('');
        const loadRows = async () => {
            let next = cursor;
            do {
                const result = await client.listRows({snapshot_id: summary.snapshot_id, table, sort_by: sortBy, descending, paginator: {page_size: username ? 200 : effectivePageSize, ...(next ? {cursor: next} : {})}});
                if (!username || !current) return result;
                const listing = result.listing.filter(row => row.key === username);
                next = result.paginator.cursor ?? undefined;
                if (listing.length || !next) return {...result, listing, total_rows: listing.length, paginator: {page_size: effectivePageSize}};
            } while (next);
        };
        loadRows().then(result => {
                if (!current || !result) return;
                setRows({key: rowKey, table, page, data: result});
                setPaging(previous => {
                    const cursors = previous.key === rowKey ? [...previous.cursors] : [undefined];
                    cursors[page] = result.paginator.cursor ?? undefined;
                    return {key: rowKey, page, cursors};
                });
            }).catch(error => {if (current) setRowError(errorText(error, summary.snapshot_id));})
            .finally(() => {if (current) setRowsBusy(false);});
        return () => {current = false;};
    }, [client, allowed, summary, tableVisible, table, expired, validation, rowKey, page, cursor, retryRows]);

    const changePeriod = (value: ReportingSummaryRequest) => {
        const next = new URLSearchParams(query);
        next.set('period', value.period);
        next.delete('start_date'); next.delete('end_date');
        if (value.start_date) next.set('start_date', value.start_date);
        if (value.end_date) next.set('end_date', value.end_date);
        setQuery(next);
    };
    const changeUser = (value: string) => {
        const next = new URLSearchParams(query);
        if (value) next.set('user', value); else next.delete('user');
        setQuery(next);
    };
    const exportCsv = async () => {
        if (!summary || expired || exportBusy || summaryBusy || rowsBusy || !allowed || !columns.some(column => column.visible)) {
            setExportStatus('CSV download is not ready. Wait for the report and select at least one column.'); return;
        }
        const selected = selectionKey;
        setExportBusy(true); setExportError(''); setExportStatus('');
        try {
            const result = await client.exportCsv({snapshot_id: summary.snapshot_id, ...(username ? {username} : {}), table, sort_by: sortBy, descending, columns: columns.filter(column => column.visible && REPORTING_COLUMNS.some(item => item.id === column.id)).map(column => column.id as ReportingColumn)});
            if (!mounted.current) return;
            if (activeSelection.current !== selected) {setExportStatus('CSV download cancelled because the report selection changed. Try again.'); return;}
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
            if (!mounted.current) return;
            if (activeSelection.current !== selected) {setExportStatus('CSV download cancelled because the report selection changed. Try again.'); return;}
            setExportError(errorText(error, summary.snapshot_id));
            setExportStatus('CSV download failed. Reload the report and try again.');
        } finally {
            if (mounted.current) setExportBusy(false);
        }
    };

    if (!auth.isReportingResolved()) return <StatusIndicator type="loading">Checking access</StatusIndicator>;
    if (!allowed) return <Alert type="error">Access denied. Ask your administrator for reporting access.</Alert>;
    const error = validation || summaryError || exportError || rowError;
    return <ContentLayout header={<SpaceBetween size="m"><Header variant="h1" description="Estimated costs and resource use" actions={<SpaceBetween direction="horizontal" size="s">
        <Button onClick={() => setReload(value => value + 1)} disabled={summaryBusy}>Reload</Button>
        {tableVisible && <Button onClick={exportCsv} loading={exportBusy} disabled={exportBusy}>Export CSV</Button>}
    </SpaceBetween>}>Cost and activity overview</Header>
        <SpaceBetween direction="horizontal" size="l">
            <ReportingPeriodPicker value={period} timezone={timezone} onChange={changePeriod}/>
            <FormField label="User">
                <div style={{width: '24ch'}}><Select filteringType="auto" filteringAriaLabel="Find users" filteringPlaceholder="Find users" empty="No users in this period" noMatch="No matching users"
                    selectedOption={{value: username, label: username || 'All users'}}
                    options={[{value: '', label: 'All users'}, ...(summary ? snapshot?.users ?? [] : []).map(name => ({value: name, label: name}))]}
                    onChange={({detail}) => changeUser(detail.selectedOption.value ?? '')}/></div>
            </FormField>
            {username && <Button onClick={() => changeUser('')}>Clear filter</Button>}
        </SpaceBetween>
        {summary && insights && <Box>{date(insights.period.start, summary.timezone)} – {date(insights.period.end, summary.timezone)} · {username && <>{username} · </>}{updated(insights.updated_at, summary.timezone)} {insights.notes.length > 0 && <InfoTitle title="Report details">{insights.notes.slice(0, 3).join(' ')}</InfoTitle>}</Box>}
    </SpaceBetween>}>
        <SpaceBetween size="l">
            {summaryBusy && <StatusIndicator type="loading">Loading report</StatusIndicator>}
            {error && <Alert type="error" action={<Button onClick={() => rowError ? setRetryRows(value => value + 1) : setReload(value => value + 1)}>Try again</Button>}>{error}</Alert>}
            {summary && insights && <SpaceBetween size="m">
                <Tabs activeTabId={tab} onChange={({detail}) => {
                    const next = new URLSearchParams(query); next.set('table', detail.activeTabId);
                    navigate({pathname: tabPaths[detail.activeTabId], search: next.toString()});
                }} tabs={[{id: 'overview', label: 'Overview'}, {id: 'jobs', label: 'Jobs'}, {id: 'desktops', label: 'Desktops'}, {id: 'storage', label: 'Storage'}, {id: 'breakdown', label: 'Breakdown'}]}/>
                {tableVisible && !username && <SegmentedControl label="Breakdown by" selectedId={table} options={[{id: 'user', text: 'User'}, {id: 'project', text: 'Project'}]} onChange={({detail}) => {
                    const next = new URLSearchParams(query); next.set('table', 'breakdown'); next.set('group', detail.selectedId); setQuery(next);
                }}/>}
                {tableVisible ? <ReportingTable key={`${table}:${username}`} table={table} data={data} currency={summary.currency} timezone={summary.timezone} loading={!data?.listing.length && (rowsBusy || summaryBusy || (!data && !rowError))} disabled={expired || summaryBusy || rowsBusy || exportBusy}
                    sortBy={sortBy} descending={descending} page={page} pageSize={pageSize} columns={columns}
                    onPage={page => setPaging(previous => ({...previous, page}))}
                    onSort={(column, descending) => {const next = new URLSearchParams(query); next.set('sort_by', column); next.set('descending', String(descending)); setQuery(next);}}
                    onPreferences={(pageSize, columns) => {setPageSize(pageSize); setColumns(columns); savePreferences(`reporting.breakdown.${table}`, {pageSize, columns});}}/>
                    : <InsightTab username={username} loading={summaryBusy} tab={tab} insights={insights} summary={summary} timezone={summary.timezone}/>}
            </SpaceBetween>}
            <div role="status" aria-label="CSV download status" aria-live="polite">{exportStatus}</div>
        </SpaceBetween>
    </ContentLayout>;
}

function Reporting(props: IdeaAppLayoutProps) {
    return <IdeaAppLayout {...props} content={<ReportingContent/>}/>;
}

export default withRouter(Reporting);
