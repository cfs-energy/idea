import {useEffect, useRef, useState} from 'react';
import {useSearchParams} from 'react-router-dom';
import {Alert, Box, Button, ColumnLayout, ExpandableSection, Header, SpaceBetween, StatusIndicator, Tabs} from '@cloudscape-design/components';
import {GetMyCostsResult} from '../../client/data-model';
import {ReportingInsights, ReportingSummaryRequest} from '../../client/reporting-model';
import {IdeaSideNavigationProps} from '../../components/side-navigation';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {AppContext} from '../../common';
import {withRouter} from '../../navigation/navigation-utils';
import {collectingLabel, costCaveat, costGaps, localToday} from '../../components/monthly-costs';
import {FACETS, facetAmount} from '../../components/cost-charts';
import {personalCostsCache} from '../../client/personal-costs-cache';
import {InfoTitle, InsightTab, MetricTile, TileRow} from '../reporting/insights-components';
import ReportingPeriodPicker, {datesPicked, REPORTING_PERIODS, validateReportingPeriod} from '../reporting/reporting-period-picker';
import {computeRateNote, costsDescription, jobRateNote, date, STORAGE_EXCLUSIONS, wholeMoney} from '../reporting/reporting-format';
import StorageUsage from './storage-usage';

export interface MyCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {}
const tabs = [{id: 'overview', label: 'Overview'}, {id: 'jobs', label: 'Jobs'}, {id: 'desktops', label: 'Desktops'}, {id: 'storage', label: 'Storage'}];
const facetTabs: Record<string, string> = {'cost-jobs': 'jobs', 'cost-desktops': 'desktops', 'cost-disks': 'overview', 'cost-storage': 'storage', 'cost-ai': 'overview'};
const titles = {jobs: 'Job spend', desktops: 'Desktop spend', desktop_disks: 'Desktop disks', shared_storage: 'Storage spend', ai: 'AI'};
function MonthTiles({costs, previous = false, computeRate = 1, costError = false}: {costs: GetMyCostsResult | null; previous?: boolean; computeRate?: number; costError?: boolean}) {
    const month = previous ? costs?.previous : costs?.current;
    const currency = costs?.currency ?? 'USD';
    const currentTotal = costs?.current?.total, previousTotal = costs?.previous?.total;
    if (!costs && !costError) return <StatusIndicator type="loading">Loading this month's costs</StatusIndicator>;
    const today = localToday(costs?.timezone);
    // The amount is the headline. What it leaves out goes in the line under it.
    const caveat = (key: typeof FACETS[number]['key']) => costCaveat(costGaps(month?.[key], today));
    return <TileRow>
        <MetricTile title="Total spend" value={!costs?.current ? collectingLabel(costs) : wholeMoney(month?.total, currency)} info="Adds the available job, desktop, desktop disk, shared storage and AI costs for the calendar month.">
            {costs?.current && <Box color="text-body-secondary">{previous ? `This month so far ${wholeMoney(currentTotal, currency)}` : `Last month ${wholeMoney(previousTotal, currency)}`}
                {currentTotal != null && previousTotal != null && <> · {wholeMoney(Math.abs(currentTotal - previousTotal), currency)} {currentTotal >= previousTotal ? 'more' : 'less'} (this month so far vs full last month)</>}
            </Box>}
            {costs?.current && FACETS.some(({key}) => caveat(key)) && <Box color="text-body-secondary">So far. Some days are not fully counted yet.</Box>}
        </MetricTile>
        {/* AI is left out when it costs nothing, and when Bedrock is off: there is no next update to wait for. */}
        {costs?.current && FACETS.filter(({key}) => key !== 'ai' || (month?.[key]?.status !== 'not_applicable' && (facetAmount(month?.[key]) == null || Number(facetAmount(month?.[key])) !== 0))).map(({key}, index) => {
            const line = month?.[key];
            return <MetricTile key={key} title={titles[key]} value={facetAmount(line) == null ? 'Not available' : wholeMoney(facetAmount(line), currency)} info={key === 'jobs' ? `Recorded job costs for this calendar month. ${jobRateNote(computeRate)}` : key === 'desktops' ? `Recorded desktop costs from stored daily costs for this calendar month. ${computeRateNote(computeRate)}` : key === 'shared_storage' ? `Uses the available daily costs for this calendar month. ${STORAGE_EXCLUSIONS}` : 'Uses the available daily costs for this calendar month.'}>
                {facetAmount(line) == null ? <Box color="text-body-secondary">Not calculated yet. Check back after the next update.</Box>
                    : caveat(key) ? <Box color="text-body-secondary">{caveat(key)}</Box>
                    : line?.status === 'estimated_share' ? <Box color="text-body-secondary">Your share of a shared file system.</Box> : null}
            </MetricTile>;
        })}
    </TileRow>;
}
function MyCosts(props: MyCostsProps) {
    const context = AppContext.get();
    const client = context.client().myCosts();
    const [query, setQuery] = useSearchParams();
    const period: ReportingSummaryRequest = {
        period: REPORTING_PERIODS.find(option => option.value === query.get('period'))?.value as ReportingSummaryRequest['period'] ?? 'this_month',
        ...(query.get('period') === 'custom' ? {start_date: query.get('start_date') ?? '', end_date: query.get('end_date') ?? ''} : {})
    };
    const periodKey = JSON.stringify(period);
    const tab = tabs.find(tab => tab.id === query.get('tab'))?.id ?? facetTabs[query.get('facet') ?? ''] ?? 'overview';
    const [costs, setCosts] = useState<GetMyCostsResult | null>(null);
    const [snapshot, setSnapshot] = useState<{key: string; data: ReportingInsights}>();
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState('');
    const [costError, setCostError] = useState('');
    const [refreshing, setRefreshing] = useState(false);
    const [refreshState, setRefreshState] = useState<'idle' | 'running' | 'slow' | 'done'>('idle');
    const [reload, setReload] = useState(0);
    // the next report read skips the short server cache: someone just asked for new numbers
    const fresh = useRef(false);
    const [storageOpen, setStorageOpen] = useState(false);
    const mounted = useRef(true);
    useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
    useEffect(() => personalCostsCache().subscribe(result => {setCosts(result); setCostError('');},
        () => setCostError('Could not load monthly costs. Use Refresh to try again.')), [client]);
    const timezone = costs?.timezone ?? context.getClusterSettingsService().getClusterTimeZone();
    const computeRate = context.getClusterSettingsService().getComputeRate();
    const validation = validateReportingPeriod(period, timezone);
    const insights = snapshot?.key === periodKey && !validation ? snapshot.data : undefined;
    useEffect(() => {
        if (validation) {setBusy(false); return;}
        let current = true;
        setBusy(true); setError('');
        const request = {...JSON.parse(periodKey), ...(fresh.current ? {refresh: true} : {})};
        fresh.current = false;
        client.getInsights(request).then(data => {if (current) setSnapshot({key: periodKey, data});})
            .catch((failure: {errorCode?: string}) => {if (current) setError(failure?.errorCode === 'REPORT_TIMEOUT' ? 'The report took too long to load. Choose a shorter period and try again.' : failure?.errorCode === 'REPORT_TOO_LARGE' ? 'The report is too large to load. Choose a shorter period and try again.' : "Couldn't load the report. Check your connection and try again.");})
            .finally(() => {if (current) setBusy(false);});
        return () => {current = false;};
    }, [client, periodKey, reload, validation]);
    const refresh = async () => {
        setRefreshing(true); setRefreshState('idle'); fresh.current = true; setReload(value => value + 1);
        try {
            const result = await personalCostsCache().refresh();
            if (mounted.current) setRefreshState(result.refresh_acknowledged === true ? 'running' : 'idle');
        } catch {
            if (mounted.current) setCostError('Could not refresh costs. Try Refresh again.');
        } finally {
            if (mounted.current) setRefreshing(false);
        }
    };
    // The collector works in the background: when it finishes, the numbers on screen are out of date.
    const pending = costs?.refresh_pending === true;
    useEffect(() => {
        if (refreshState === 'running' || refreshState === 'slow') {
            if (!pending) {
                setRefreshState('done');
                fresh.current = true;
                setReload(value => value + 1);
            } else if (refreshState === 'running') {
                const timer = setTimeout(() => setRefreshState('slow'), 300000);
                return () => clearTimeout(timer);
            }
        }
    }, [pending, refreshState]);
    const refreshText = {idle: '', running: 'Recalculating your costs, usually a minute or two', slow: 'Taking longer than usual. Your earlier numbers are still shown.', done: 'Costs updated'}[refreshState];
    const calendarMonth = period.period === 'this_month' || period.period === 'last_month';
    // Neither of these needs the report, so a slow or failed report does not hold them back.
    const monthTiles = <MonthTiles costs={costs} previous={period.period === 'last_month'} computeRate={computeRate} costError={!!costError}/>;
    const storagePane = <ExpandableSection headerText="Storage usage: folders and quotas" expanded={storageOpen} onChange={({detail}) => setStorageOpen(detail.expanded)}>
        {storageOpen && <StorageUsage compact refreshSignal={reload}/>}
    </ExpandableSection>;
    return <IdeaAppLayout {...props} breadcrumbItems={[{text: 'IDEA', href: '#/'}, {text: 'My costs', href: ''}]}
        header={<Header variant="h1" description={costsDescription(computeRate)} actions={<SpaceBetween direction="horizontal" size="s">
            <span role="status">{refreshText}</span>
            <Button loading={refreshing} onClick={refresh}>Refresh</Button>
        </SpaceBetween>}>My costs</Header>}
        contentType="default" content={<SpaceBetween size="m">
            <ReportingPeriodPicker value={period} timezone={timezone} onChange={value => {
                const next = new URLSearchParams(query);
                next.set('period', value.period); next.delete('start_date'); next.delete('end_date');
                if (value.start_date) next.set('start_date', value.start_date);
                if (value.end_date) next.set('end_date', value.end_date);
                setQuery(next);
            }}/>
            {insights && <Box>{date(insights.period.start, timezone)} – {date(insights.period.end, timezone)}{costs?.refreshed_at && <> · Costs last calculated {date(costs.refreshed_at, timezone, true)}</>} {insights.notes.length > 0 && <InfoTitle title="Report details">{insights.notes.slice(0, 3).join(' ')}</InfoTitle>}</Box>}
            <SpaceBetween size="l">
                {busy && <StatusIndicator type="loading">Loading report</StatusIndicator>}
                {datesPicked(period) && validation ? <Alert type="error">{validation}</Alert> : error ? <Alert type="error" action={<Button onClick={() => setReload(value => value + 1)}>Try again</Button>}>{error}</Alert> : null}
                {costError && <Alert type="error">{costError}</Alert>}
                <Tabs activeTabId={tab} onChange={({detail}) => {const next = new URLSearchParams(query); next.set('tab', detail.activeTabId); setQuery(next);}} tabs={tabs}/>
                {insights ? <InsightTab personal loading={busy} tab={tab} insights={insights} timezone={timezone} computeRate={computeRate} desktopEmpty={!costs?.current ? 'We are still collecting your desktop costs. Check back within the hour.' : undefined}
                    overviewTiles={calendarMonth ? monthTiles : undefined}
                    overviewExtra={!calendarMonth && <SpaceBetween size="m"><Header variant="h2">This month</Header><MonthTiles costs={costs} computeRate={computeRate} costError={!!costError}/></SpaceBetween>}
                    storageExtra={storagePane}/>
                    : tab === 'overview' ? monthTiles : tab === 'storage' ? storagePane : null}
            </SpaceBetween>
        </SpaceBetween>}/>;
}
export default withRouter(MyCosts);
