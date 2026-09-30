import {createContext, ReactNode, useContext, useMemo} from 'react';
import {AreaChart, BarChart, Box, Button, ColumnLayout, Container, Grid, Header, Link, LineChart, Popover, ProgressBar, Spinner, SpaceBetween, StatusIndicator} from '@cloudscape-design/components';
import {JobRow, Ranked, ReportingBudget, ReportingCoverage, ReportingInsights, ReportingNumber, ReportingSummary} from '../../client/reporting-model';
import {budgetPresentation, bytes, palette, calendarDays, cappedSeries, colorByName, rankedColors, date, efficiencyLabel, efficiencyStatus, hours, measuredTierPoints, metricInfo, money, NamedPoint, numeric, percent} from './reporting-format';
import InsightsTable, {InsightColumn, ReportLoading, ReportUser} from './insights-table';

export const Missing = () => <span role="img" aria-label="No data">—</span>;
export function InfoTitle({title, children}: {title: string; children: ReactNode}) {
    return <span>{title} <span onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}><Popover header={title} content={children} dismissAriaLabel="Close information" triggerType="custom"><Button variant="inline-icon" iconName="status-info" ariaLabel={`About ${title}`}/></Popover></span></span>;
}
export function MetricTile({title, value, info, children}: {title: string; value: ReactNode; info: string; children?: ReactNode}) {
    if (value == null) return null;
    return <Container fitHeight header={<Header variant="h3"><InfoTitle title={title}>{info}</InfoTitle></Header>}><Box fontSize="display-l">{value}</Box>{children}</Container>;
}
const metricDefinitions = {
    cpu_efficiency_pct: 'CPU time used as a share of requested cores multiplied by elapsed time.',
    cpu_efficiency_weighted_pct: "CPU efficiency weighted by each job's requested core-hours.",
    memory_efficiency_pct: "Peak memory used as a share of memory requested, or of the instance's memory when a job on its own instance didn't request any.",
    walltime_efficiency_pct: 'Elapsed time as a share of requested time.',
    wasted_core_hours: 'Requested cores multiplied by elapsed hours, minus CPU hours used.',
    wasted_cost: 'Job cost multiplied by the share of requested core time not used, measured against the cores requested, not the instance's vCPUs.'
};
type EfficiencyKey = 'cpu_efficiency_pct' | 'memory_efficiency_pct' | 'walltime_efficiency_pct' | 'wasted_core_hours' | 'wasted_cost';
export function EfficiencyTiles({jobs, currency, weightedInfo = false, only}: {weightedInfo?: boolean; jobs: ReportingInsights['jobs']; currency: string; only?: EfficiencyKey[]}) {
    const tiles = ([
        ['cpu_efficiency_pct', 'CPU efficiency'], ['memory_efficiency_pct', 'Memory efficiency'],
        ['walltime_efficiency_pct', 'Walltime efficiency'], ['wasted_core_hours', 'Unused core-hours'], ['wasted_cost', 'Cost of unused core-hours']
    ] as const).filter(([key]) => !only || only.includes(key));
    return <ColumnLayout columns={3}>{tiles.filter(([key]) => jobs[key] != null).map(([key, title]) => <MetricTile key={key} title={title}
        value={key === 'wasted_cost' ? money(jobs[key], currency) : key === 'wasted_core_hours' ? hours(jobs[key]) : percent(jobs[key])}
        info={`${metricDefinitions[key]}${['cpu_efficiency_pct', 'memory_efficiency_pct', 'walltime_efficiency_pct'].includes(key) ? ' Averages jobs with the needed measurements.' : ''}${key === 'cpu_efficiency_pct' ? ` ${weightedInfo && jobs.cpu_efficiency_weighted_pct != null ? `CPU efficiency by core-hours: ${percent(jobs.cpu_efficiency_weighted_pct)}.` : `Uses ${jobs.jobs_with_efficiency} of ${jobs.count} finished jobs.`}` : ''}`}>{key.endsWith('_pct') && <StatusIndicator type={efficiencyStatus(Number(jobs[key]))}>{efficiencyLabel(Number(jobs[key]), key)}</StatusIndicator>}</MetricTile>)}</ColumnLayout>;
}
export function BudgetTable({budgets, currency}: {budgets: ReportingBudget[]; currency: string}) {
    if (!budgets.length) return null;
    const columns: InsightColumn<ReportingBudget>[] = [
        {id: 'project', label: 'Project', header: 'Project', value: row => row.project, cell: row => row.project},
        {id: 'budget_name', label: 'Budget', header: 'Budget', value: row => row.budget_name, cell: row => row.budget_name},
        ...(['limit', 'spent', 'forecast', 'headroom'] as const).map((key, i) => ({id: key, label: ['Budget limit', 'Spent', 'Forecast', 'Headroom'][i],
            header: <InfoTitle title={['Budget limit', 'Spent', 'Forecast', 'Headroom'][i]}>{["The project's budget for its current budget period.", 'Spending so far in the current budget period.', 'Expected spending by the end of the current budget period.', 'Budget limit minus forecast spending.'][i]}</InfoTitle>, value: (row: ReportingBudget) => numeric(row[key]), cell: (row: ReportingBudget) => row[key] == null ? <Missing/> : money(row[key], currency)})),
        {id: 'pct_at_forecast', label: 'Forecast used', header: <InfoTitle title="Forecast used">Forecast as a share of the budget: near budget at 85%, over budget at 100%.</InfoTitle>, value: row => row.pct_at_forecast,
            cell: row => row.pct_at_forecast == null ? <Missing/> : <ProgressBar value={Math.min(100, Math.max(0, row.pct_at_forecast))} ariaLabel={`${row.project} forecast used`}
                status={row.pct_at_forecast > 100 ? 'error' : 'in-progress'} resultText={percent(row.pct_at_forecast)}/>},
        {id: 'status', label: 'Status', header: 'Status', value: row => row.status, cell: row => <StatusIndicator type={budgetPresentation[row.status].type}>{budgetPresentation[row.status].label}</StatusIndicator>}
    ];
    return <InsightsTable title="Project budgets" description="Spending and forecasts use each project's current budget period." rows={budgets} columns={columns} defaultSort="pct_at_forecast" empty="No project budgets"/>;
}
export function Coaching({jobs, currency, personal = false}: {jobs: ReportingInsights['jobs']; currency: string; personal?: boolean}) {
    if (!jobs.jobs_with_efficiency || jobs.wasted_cost == null || jobs.cost == null) return null;
    return <Box>{personal ? `About ${money(jobs.wasted_cost, currency)} of ${money(jobs.cost, currency)} in job spend paid for cores your jobs didn't use.` : `About ${money(jobs.wasted_cost, currency)} of ${money(jobs.cost, currency)} in job spend this period paid for unused cores.`}</Box>;
}
const gib = (value: number) => new Intl.NumberFormat('en-US', {maximumFractionDigits: value < 10 ? 1 : 0}).format(value);
export function jobHint(job: JobRow) {
    const hints = [];
    const nodes = job.nodes ?? 1;
    if (job.requested_cores != null && job.requested_cores > 0 && job.used_cores != null && job.used_cores / job.requested_cores < 0.5)
        { const suggest = Math.max(1, Math.ceil(job.used_cores / nodes * 1.25)); if (suggest < job.requested_cores / nodes) hints.push(`Requested ${job.requested_cores} cores, used ${job.used_cores < 1 ? 'less than 1' : `about ${hours(job.used_cores)}`}. Try ncpus=${suggest}${nodes > 1 ? ' per node' : ''}.`); }
    const memory = job.requested_memory_gib ?? (job.instance_memory_gib != null ? job.instance_memory_gib * nodes : null);
    if (memory != null && job.peak_memory_gib != null && memory > 0 && job.peak_memory_gib / memory < 0.5)
        hints.push(job.requested_memory_gib != null ? `Requested ${gib(memory)} GiB, peak ${gib(job.peak_memory_gib)} GiB.`
            : `Peak ${gib(job.peak_memory_gib)} GiB of ${gib(memory)} GiB${nodes > 1 ? ` total across ${nodes} nodes` : ''}${job.instance_type ? ` on ${job.instance_type}` : ''}. A smaller instance type would do.`);
    return hints.join(' ') || null;
}
export function mergeJobs(jobs: ReportingInsights['jobs']) {
    return Array.from(new Map([...jobs.costliest, ...jobs.least_efficient].map(job => [job.job_id, job])).values());
}
export function JobsTable({title, rows, currency, timezone, personal = false}: {title: string; rows: JobRow[]; currency: string; timezone: string; personal?: boolean}) {
    const columns: InsightColumn<JobRow>[] = [
        {id: 'name', label: 'Job name', header: 'Job name', width: 200, minWidth: 140, value: row => row.name ?? row.job_id,
            cell: row => <span title={row.name ?? row.job_id}><Link href={`#/${personal ? 'home' : 'soca'}/completed-jobs?job_id=${encodeURIComponent(row.job_id)}`}>{row.name ?? row.job_id}</Link></span>},
        {id: 'cost', label: 'Cost', header: 'Cost', value: row => numeric(row.cost), cell: row => row.cost == null ? <Missing/> : money(row.cost, currency)},
        {id: 'cpu_efficiency_pct', label: 'CPU efficiency', header: <InfoTitle title="CPU efficiency">{metricDefinitions.cpu_efficiency_pct}</InfoTitle>, value: row => row.cpu_efficiency_pct,
            cell: row => row.cpu_efficiency_pct == null ? <Missing/> : <StatusIndicator type={efficiencyStatus(row.cpu_efficiency_pct)}>{percent(row.cpu_efficiency_pct)}</StatusIndicator>},
        {id: 'wasted_core_hours', label: 'Unused core-hours', header: <InfoTitle title="Unused core-hours">{metricDefinitions.wasted_core_hours}</InfoTitle>, value: row => row.wasted_core_hours, cell: row => row.wasted_core_hours == null ? <Missing/> : hours(row.wasted_core_hours)},
        {id: 'hint', label: 'Hint', header: 'Hint', minWidth: 320, value: jobHint, cell: row => <span title={jobHint(row) ?? undefined}>{jobHint(row) ?? <Missing/>}</span>},
        {id: 'elapsed_hours', label: 'Elapsed hours', header: 'Elapsed hours', value: row => row.elapsed_hours, cell: row => row.elapsed_hours == null ? <Missing/> : hours(row.elapsed_hours)},
        ...(['queue', 'instance_type'] as const).map(key => ({id: key, label: key === 'queue' ? 'Queue' : 'Instance type', header: key === 'queue' ? 'Queue' : 'Instance type', value: (row: JobRow) => row[key], cell: (row: JobRow) => row[key] ?? <Missing/>})),
        {id: 'finished_at', label: 'Finished', header: 'Finished', value: row => row.finished_at, cell: row => date(row.finished_at, timezone, true)},
        ...(['job_id', 'nodes', 'project', 'owner'] as const).map((key, i) => ({id: key, label: ['Job ID', 'Nodes', 'Project', 'User'][i], header: ['Job ID', 'Nodes', 'Project', 'User'][i], value: (row: JobRow) => row[key], cell: (row: JobRow) => row[key] ?? <Missing/>}))
    ];
    return <InsightsTable title={title} tableId={personal ? 'my-jobs' : 'jobs'} rows={rows} columns={columns} hidden={['job_id', 'nodes', 'project', ...(personal ? ['owner'] : [])]} defaultSort="cost" jobSort empty="No jobs finished in this period"/>;
}
const niceMax = (value: number) => {const step = 10 ** Math.floor(Math.log10(value)); return Math.ceil(value / step) * step;};
const ChartColors = createContext(new Map<string, number>());
export function CostBars({title, rows, currency, entity, info, storage = false}: {title: string; rows: (Omit<Ranked, 'cost'> & {cost: ReportingNumber})[]; currency: string; entity: string; info?: string; storage?: boolean}) {
    const ranks = useContext(ChartColors);
    const loading = useContext(ReportLoading);
    const nonzero = rows.filter(row => Number(row.cost) > 0);
    const format = (value: number) => storage ? bytes(value * 2 ** 30) : money(value, currency);
    if (nonzero.length < 2) return <MetricTile title={title} value={nonzero.length ? format(Number(nonzero[0].cost)) : loading ? <Spinner/> : storage ? 'No storage measurements in this period' : 'No costs in this period'} info={info ?? (storage ? 'Most recent measured storage use in this period.' : 'Costs for this period.')}>
        {nonzero.length === 1 && <Box>{nonzero[0].name}</Box>}
    </MetricTile>;
    return <Container header={<Header variant="h2">{info ? <InfoTitle title={title}>{info}</InfoTitle> : title}</Header>}><BarChart
        series={[{title, type: 'bar', color: palette[0], data: nonzero.map(row => ({x: row.name, y: Number(row.cost)})), valueFormatter: format}]}
        xDomain={nonzero.map(row => row.name)} xScaleType="categorical" yScaleType="linear" horizontalBars
        xTitle={entity} yTitle={storage ? 'Stored data (GiB)' : `Cost (${currency})`} yTickFormatter={format} ariaLabel={title} hideFilter hideLegend
        height={Math.max(200, nonzero.length * 28)} empty={<Box>No costs in this period</Box>}/></Container>;
}
export function DailyChart({title, points, currency, timezone, storage = false, empty, period}: {title: string; points: NamedPoint[]; currency: string; timezone: string; storage?: boolean; empty: string; period?: ReportingInsights['period']}) {
    const ranks = useContext(ChartColors);
    const loading = useContext(ReportLoading);
    const measured = storage ? measuredTierPoints(points) : points;
    const dates = measured.map(point => point.x).sort();
    const start = period?.start ?? dates[0], end = period?.end ?? dates.at(-1);
    const series = cappedSeries(measured, ranks, !storage && start && end ? calendarDays(start, end) : undefined).map(series => ({...series, data: series.data.map(point => ({x: new Date(`${point.x}T12:00:00Z`), y: point.y}))}));
    const format = (value: number) => storage ? `${new Intl.NumberFormat('en-US', {maximumSignificantDigits: 3}).format(value)} GiB` : money(value, currency);
    const common = {xDomain: start && end ? [new Date(`${start}T12:00:00Z`), new Date(`${end}T12:00:00Z`)] : undefined, xScaleType: 'time' as const, yScaleType: 'linear' as const,
        xTitle: 'Date', yTitle: storage ? 'Stored data (GiB)' : `Cost (${currency})`, xTickFormatter: (value: Date) => new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'short', day: 'numeric', ...(start?.slice(0, 4) !== end?.slice(0, 4) ? {year: 'numeric' as const} : {})}).format(value), yTickFormatter: format,
        height: 280, hideFilter: true, hideLegend: series.length < 2, ariaLabel: title, statusType: loading && !points.length ? 'loading' as const : 'finished' as const, empty: loading ? null : <Box>{empty}</Box>};
    return <Container header={<Header variant="h2">{storage ? <InfoTitle title={title}>Shows dates with measurements for all displayed tiers.</InfoTitle> : title}</Header>}>{storage
        ? <LineChart {...common} yDomain={[0, niceMax(Math.max(1, ...measured.map(point => point.value)))]} series={series.map(series => ({...series, type: 'line', valueFormatter: format}))}/>
        : <AreaChart {...common} detailTotalFormatter={format} series={series.map(series => ({...series, type: 'area', valueFormatter: format}))}/>}</Container>;
}
export function InsightTab(props: {username?: string; loading?: boolean; tab: string; insights: ReportingInsights; summary: ReportingSummary; timezone: string}) {
    const data = props.insights;
    const ranks = useMemo(() => rankedColors([
        ...[data.jobs.by_user ?? [], data.jobs.by_project, data.jobs.by_queue, data.jobs.by_instance_family, data.desktops.by_user ?? [], data.desktops.by_project].flat().map(row => ({name: row.name, value: Number(row.cost)})),
        ...data.storage.tier_daily.map(row => ({name: row.tier === 'ssd' ? 'SSD' : 'Capacity pool', value: row.bytes})),
        ...['Other', 'User', 'Project', 'Queue', 'Instance family'].map(name => ({name, value: 0}))
    ]), [data]);
    return <ReportLoading.Provider value={props.loading ?? false}><ChartColors.Provider value={ranks}><ReportUser.Provider value={props.username ?? ''}><InsightView {...props}/></ReportUser.Provider></ChartColors.Provider></ReportLoading.Provider>;
}
function InsightView({tab, insights: data, summary, timezone}: {tab: string; insights: ReportingInsights; summary: ReportingSummary; timezone: string}) {
    const currency = data.currency;
    const username = useContext(ReportUser);
    const costs = [data.jobs.cost, data.desktops.cost, data.storage.cost].filter(value => value != null);
    const total = costs.length ? costs.reduce<number>((sum, value) => sum + Number(value), 0) : null;
    const tile = (title: string, value: ReportingNumber | null | undefined, details?: ReportingCoverage, definition?: string, children?: ReactNode) => value == null ? null : <MetricTile title={title} value={money(value, currency)} info={metricInfo(details, timezone, definition)}>{children}</MetricTile>;
    if (tab === 'overview') return <SpaceBetween size="l">
        <Coaching jobs={data.jobs} currency={currency}/>
        <ColumnLayout columns={4}>
            {tile('Total spend', total, summary.coverage.spend_total, 'Adds the available job, desktop and storage costs for this period.')}
            {tile('Job spend', data.jobs.cost, summary.coverage.jobs, 'Adds the costs of jobs that finished in this period. Reserved prices are compared with on-demand prices for the same job resources.',
                Number(data.jobs.savings) > 0 && <Box color="text-body-secondary">{money(data.jobs.savings!, currency)} less than on-demand</Box>)}
            {tile('Desktop spend', data.desktops.cost, summary.coverage.desktops, 'Desktop hours multiplied by instance prices.')}
            {tile('Storage spend', data.storage.cost, summary.coverage.shared_storage, 'Storage cost based on measured use.')}
        </ColumnLayout>
        <EfficiencyTiles jobs={data.jobs} currency={currency} only={['cpu_efficiency_pct', 'wasted_core_hours', 'wasted_cost']}/>
        <DailyChart period={data.period} title="Daily job cost by project" points={data.jobs.daily_by_project.map(row => ({name: row.project, x: row.date, value: Number(row.cost)}))} currency={currency} timezone={timezone} empty="No jobs finished in this period"/>
        {!username && <CostBars title="Spend by user" rows={data.jobs.by_user ?? []} info="Finished job costs for the top 15 users." currency={currency} entity="User"/>}
        <BudgetTable budgets={data.budgets} currency={currency}/>
    </SpaceBetween>;
    if (tab === 'jobs') return <SpaceBetween size="l">
        <EfficiencyTiles jobs={data.jobs} currency={currency} weightedInfo/>
        <Grid gridDefinition={[1, 2, 3].map(() => ({colspan: {default: 12, m: 4}}))}>
        <CostBars title="Job cost by queue" rows={data.jobs.by_queue} currency={currency} entity="Queue"/>
        <CostBars title="Job cost by project" rows={data.jobs.by_project} currency={currency} entity="Project"/>
        <CostBars title="Job cost by instance family" rows={data.jobs.by_instance_family} currency={currency} entity="Instance family"/>
        </Grid>
        <JobsTable title="Top jobs" rows={mergeJobs(data.jobs)} currency={currency} timezone={timezone}/>
    </SpaceBetween>;
    if (tab === 'desktops') return <SpaceBetween size="l">
        <ColumnLayout columns={2}>{tile('Desktop spend', data.desktops.cost)}<MetricTile title="Desktop hours" value={data.desktops.hours == null ? null : hours(data.desktops.hours)} info="Time between desktop creation and stopping within this period."/></ColumnLayout>
        <DailyChart period={data.period} title={username ? "Daily desktop cost" : "Daily desktop cost by user"} points={data.desktops.daily_top_users.map(row => ({name: row.user, x: row.date, value: Number(row.cost)}))} currency={currency} timezone={timezone} empty="No desktop costs in this period"/>
        {!username && <CostBars title="Desktop cost by user" rows={data.desktops.by_user ?? []} currency={currency} entity="User"/>}
        <CostBars title="Desktop cost by project" rows={data.desktops.by_project} currency={currency} entity="Project"/>
    </SpaceBetween>;
    return <SpaceBetween size="l">
        <ColumnLayout columns={2}>{tile('Storage spend', data.storage.cost)}<MetricTile title="Stored data" value={data.storage.used_bytes == null ? null : bytes(data.storage.used_bytes)} info="Most recent measured storage use in this period."/></ColumnLayout>
        {!username && <CostBars title="Stored data by user" rows={(data.storage.by_user ?? []).map(row => ({name: row.name, cost: row.bytes / 2 ** 30, count: null, share_pct: 0}))} currency={currency} entity="User" storage/>}
        {!username && <InsightsTable title="Storage by user" rows={(data.storage.by_user ?? []).filter(row => row.bytes > 0)} defaultSort="bytes" empty="No storage measurements in this period" columns={[
            {id: 'name', label: 'User', header: 'User', value: row => row.name, cell: row => row.name},
            {id: 'bytes', label: 'Stored data', header: 'Stored data', value: row => row.bytes, cell: row => bytes(row.bytes)},
            {id: 'cost', label: 'Cost', header: 'Cost', value: row => numeric(row.cost), cell: row => row.cost == null ? <Missing/> : money(row.cost, currency)}
        ]}/>}
        {!username && <DailyChart period={data.period} title="Storage by tier" points={data.storage.tier_daily.map(row => ({name: row.tier === 'ssd' ? 'SSD' : 'Capacity pool', x: row.date, value: row.bytes / 2 ** 30}))} currency={currency} timezone={timezone} storage empty="No storage measurements in this period"/>}
    </SpaceBetween>;
}
