import {useEffect, useRef, useState} from 'react';
import {useSearchParams} from 'react-router-dom';
import {Alert, Badge, Box, Button, ColumnLayout, ExpandableSection, Header, SpaceBetween, StatusIndicator, Tabs} from '@cloudscape-design/components';
import {GetMyCostsResult} from '../../client/data-model';
import {ReportingInsights, ReportingSummaryRequest} from '../../client/reporting-model';
import {IdeaSideNavigationProps} from '../../components/side-navigation';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {AppContext} from '../../common';
import {withRouter} from '../../navigation/navigation-utils';
import {collectingLabel, costBadge} from '../../components/monthly-costs';
import {FACETS, facetAmount} from '../../components/cost-charts';
import {personalCostsCache} from '../../client/personal-costs-cache';
import {InfoTitle, InsightTab, MetricTile, TileRow} from '../reporting/insights-components';
import ReportingPeriodPicker, {REPORTING_PERIODS, validateReportingPeriod} from '../reporting/reporting-period-picker';
import {date, money, updated} from '../reporting/reporting-format';
import StorageUsage from './storage-usage';

export interface MyCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {}
const tabs = [{id: 'overview', label: 'Overview'}, {id: 'jobs', label: 'Jobs'}, {id: 'desktops', label: 'Desktops'}, {id: 'storage', label: 'Storage'}];
const facetTabs: Record<string, string> = {'cost-jobs': 'jobs', 'cost-desktops': 'desktops', 'cost-disks': 'overview', 'cost-storage': 'storage', 'cost-ai': 'overview'};
const titles = {jobs: 'Job spend', desktops: 'Desktop spend', desktop_disks: 'Desktop disks', shared_storage: 'Storage spend', ai: 'AI'};
function MonthTiles({costs, previous = false}: {costs: GetMyCostsResult | null; previous?: boolean}) {
    const month = previous ? costs?.previous : costs?.current;
    const currency = costs?.currency ?? 'USD';
    const currentTotal = costs?.current?.total, previousTotal = costs?.previous?.total;
    return <TileRow>
        <MetricTile title="Total spend" value={!costs?.current ? collectingLabel(costs) : money(month?.total, currency)} info="Adds the available job, desktop, desktop disk, shared storage and AI costs for the calendar month.">
            <Box color="text-body-secondary">{previous ? `This month so far ${money(currentTotal, currency)}` : `Last month ${money(previousTotal, currency)}`}
                {currentTotal != null && previousTotal != null && <> · {money(Math.abs(currentTotal - previousTotal), currency)} {currentTotal >= previousTotal ? 'more' : 'less'} (this month so far vs full last month)</>}
            </Box>
        </MetricTile>
        {FACETS.filter(({key}) => key !== 'ai' || facetAmount(month?.[key]) == null || Number(facetAmount(month?.[key])) !== 0).map(({key}) => {
            const line = month?.[key];
            const badge = costBadge(line, !costs?.current);
            return <MetricTile key={key} title={titles[key]} value={money(facetAmount(line), currency)} info={line?.reason || line?.note || 'Uses the available daily costs for this calendar month.'}>
                {badge && badge !== 'Partial' && <Badge>{badge}</Badge>}
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
    const [acknowledged, setAcknowledged] = useState(false);
    const [reload, setReload] = useState(0);
    const [storageOpen, setStorageOpen] = useState(false);
    const mounted = useRef(true);
    useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
    useEffect(() => personalCostsCache().subscribe(result => {setCosts(result); setCostError('');},
        () => setCostError('Could not load monthly costs. Use Refresh to try again.')), [client]);
    const timezone = costs?.timezone ?? context.getClusterSettingsService().getClusterTimeZone();
    const validation = validateReportingPeriod(period, timezone);
    const insights = snapshot?.key === periodKey && !validation ? snapshot.data : undefined;
    useEffect(() => {
        if (validation) {setBusy(false); return;}
        let current = true;
        setBusy(true); setError('');
        client.getInsights(JSON.parse(periodKey)).then(data => {if (current) setSnapshot({key: periodKey, data});})
            .catch(() => {if (current) setError("Couldn't load the report. Check your connection and try again.");})
            .finally(() => {if (current) setBusy(false);});
        return () => {current = false;};
    }, [client, periodKey, reload, validation]);
    const refresh = async () => {
        setRefreshing(true); setAcknowledged(false); setReload(value => value + 1);
        try {
            const result = await personalCostsCache().refresh();
            if (mounted.current) setAcknowledged(result.refresh_acknowledged === true);
        } catch {
            if (mounted.current) setCostError('Could not refresh costs. Try Refresh again.');
        } finally {
            if (mounted.current) setRefreshing(false);
        }
    };
    const calendarMonth = period.period === 'this_month' || period.period === 'last_month';
    return <IdeaAppLayout {...props} breadcrumbItems={[{text: 'IDEA', href: '#/'}, {text: 'Home', href: '#/'}, {text: 'My costs', href: ''}]}
        header={<Header variant="h1" description="Estimated costs and resource use" actions={<SpaceBetween direction="horizontal" size="s">
            <span role="status">{acknowledged ? 'Refresh requested' : ''}</span>
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
            {insights && <Box>{date(insights.period.start, timezone)} – {date(insights.period.end, timezone)} · {updated(insights.updated_at, timezone)} {insights.notes.length > 0 && <InfoTitle title="Report details">{insights.notes.slice(0, 3).join(' ')}</InfoTitle>}</Box>}
            <SpaceBetween size="l">
                {busy && <StatusIndicator type="loading">Loading report</StatusIndicator>}
                {(validation || error) && <Alert type="error" action={<Button onClick={() => setReload(value => value + 1)}>Try again</Button>}>{validation || error}</Alert>}
                {costError && <Alert type="error">{costError}</Alert>}
                <Tabs activeTabId={tab} onChange={({detail}) => {const next = new URLSearchParams(query); next.set('tab', detail.activeTabId); setQuery(next);}} tabs={tabs}/>
                {insights && <InsightTab personal loading={busy} tab={tab} insights={insights} timezone={timezone}
                    overviewTiles={calendarMonth ? <MonthTiles costs={costs} previous={period.period === 'last_month'}/> : undefined}
                    overviewExtra={!calendarMonth && <SpaceBetween size="m"><Header variant="h2">This month</Header><MonthTiles costs={costs}/></SpaceBetween>}
                    storageExtra={<ExpandableSection headerText="Storage usage: folders and quotas" expanded={storageOpen} onChange={({detail}) => setStorageOpen(detail.expanded)}>
                        {storageOpen && <StorageUsage compact/>}
                    </ExpandableSection>}/>}
            </SpaceBetween>
        </SpaceBetween>}/>;
}
export default withRouter(MyCosts);
