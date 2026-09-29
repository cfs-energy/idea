import {ReactNode} from 'react';
import {AreaChart, BarChart, Box, Button, ColumnLayout, Container, Header, Popover, ProgressBar, SpaceBetween, StatusIndicator} from '@cloudscape-design/components';
import {JobRow, Ranked, ReportingBudget, ReportingCoverage, ReportingInsights, ReportingNumber, ReportingSummary} from '../../client/reporting-model';
import {budgetPresentation, bytes, cappedSeries, date, efficiencyStatus, hours, measuredTierPoints, metricInfo, money, NamedPoint, numeric, percent} from './reporting-format';
import InsightsTable, {InsightColumn} from './insights-table';

export const Missing = () => <span aria-label="No data">—</span>;
export function InfoTitle({title, children}: {title: string; children: ReactNode}) {
    return <span>{title} <span onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}><Popover header={title} content={children} dismissAriaLabel="Close information" triggerType="custom"><Button variant="inline-icon" iconName="status-info" ariaLabel={`About ${title}`}/></Popover></span></span>;
}
export function MetricTile({title, value, info, children}: {title: string; value: string | null; info: string; children?: ReactNode}) {
    if (value == null) return null;
    return <Container header={<Header variant="h3"><InfoTitle title={title}>{info}</InfoTitle></Header>}><Box variant="h2">{value}</Box>{children}</Container>;
}
const metricDefinitions = {
    cpu_efficiency_pct: 'CPU time used as a share of requested cores multiplied by elapsed time.',
    cpu_efficiency_weighted_pct: "CPU efficiency weighted by each job's requested core-hours.",
    memory_efficiency_pct: 'Peak memory used as a share of memory requested.',
    walltime_efficiency_pct: 'Elapsed time as a share of requested time.',
    wasted_core_hours: 'Requested cores multiplied by elapsed hours, minus CPU hours used.',
    wasted_cost: 'Job cost multiplied by the share of requested core time not used.'
};
export function EfficiencyTiles({jobs, currency}: {jobs: ReportingInsights['jobs']; currency: string}) {
    const tiles = [
        ['cpu_efficiency_pct', 'CPU efficiency'], ['cpu_efficiency_weighted_pct', 'CPU efficiency by core-hours'], ['memory_efficiency_pct', 'Memory efficiency'],
        ['walltime_efficiency_pct', 'Walltime efficiency'], ['wasted_core_hours', 'Unused core-hours'], ['wasted_cost', 'Cost of unused core-hours']
    ] as const;
    return <ColumnLayout columns={3}>{tiles.filter(([key]) => jobs[key] != null).map(([key, title]) => <MetricTile key={key} title={title}
        value={key === 'wasted_cost' ? money(jobs[key], currency) : key === 'wasted_core_hours' ? hours(jobs[key]) : percent(jobs[key])}
        info={`${metricDefinitions[key]}${['cpu_efficiency_pct', 'memory_efficiency_pct', 'walltime_efficiency_pct'].includes(key) ? ' Averages jobs with the needed measurements.' : ''}${key === 'cpu_efficiency_pct' ? ` Uses ${jobs.jobs_with_efficiency} of ${jobs.count} finished jobs.` : ''}`}/>)}</ColumnLayout>;
}
export function BudgetTable({budgets, currency}: {budgets: ReportingBudget[]; currency: string}) {
    if (!budgets.length) return null;
    const columns: InsightColumn<ReportingBudget>[] = [
        {id: 'project', label: 'Project', header: 'Project', value: row => row.project, cell: row => row.project},
        {id: 'budget_name', label: 'Budget', header: 'Budget', value: row => row.budget_name, cell: row => row.budget_name},
        ...(['limit', 'spent', 'forecast', 'headroom'] as const).map((key, i) => ({id: key, label: ['Budget limit', 'Spent', 'Forecast', 'Headroom'][i],
            header: <InfoTitle title={['Budget limit', 'Spent', 'Forecast', 'Headroom'][i]}>{["The project's budget for its current budget period.", 'Spending so far in the current budget period.', 'Expected spending by the end of the current budget period.', 'Budget limit minus forecast spending.'][i]}</InfoTitle>, value: (row: ReportingBudget) => numeric(row[key]), cell: (row: ReportingBudget) => row[key] == null ? <Missing/> : money(row[key], currency)})),
        {id: 'pct_at_forecast', label: 'Forecast used', header: <InfoTitle title="Forecast used">Forecast as a share of the budget: near budget at 85%, over budget at 100%.</InfoTitle>, value: row => row.pct_at_forecast,
            cell: row => row.pct_at_forecast == null ? <Missing/> : <ProgressBar value={Math.min(100, Math.max(0, row.pct_at_forecast))} label={percent(row.pct_at_forecast)} ariaLabel={`${row.project} forecast used`}
                style={{progressValue: {backgroundColor: budgetPresentation[row.status].color}}}/>},
        {id: 'status', label: 'Status', header: 'Status', value: row => row.status, cell: row => <StatusIndicator type={budgetPresentation[row.status].type}>{budgetPresentation[row.status].label}</StatusIndicator>}
    ];
    return <InsightsTable title="Project budgets" rows={budgets} columns={columns} defaultSort="pct_at_forecast" empty="No project budgets"/>;
}
export function jobHint(job: JobRow) {
    return job.cpu_efficiency_pct == null ? null : `Used about ${percent(job.cpu_efficiency_pct)} of requested CPU time`;
}
export function JobsTable({title, rows, currency, timezone, personal = false}: {title: string; rows: JobRow[]; currency: string; timezone: string; personal?: boolean}) {
    const columns: InsightColumn<JobRow>[] = [
        ...(['job_id', 'name', 'owner', 'project', 'queue', 'instance_type', 'nodes'] as const).filter(key => !personal || key !== 'owner').map(key => ({id: key,
            label: ({job_id: 'Job ID', name: 'Job name', owner: 'User', project: 'Project', queue: 'Queue', instance_type: 'Instance type', nodes: 'Nodes'})[key],
            header: ({job_id: 'Job ID', name: 'Job name', owner: 'User', project: 'Project', queue: 'Queue', instance_type: 'Instance type', nodes: 'Nodes'})[key], value: (row: JobRow) => row[key], cell: (row: JobRow) => row[key] ?? <Missing/>})),
        {id: 'finished_at', label: 'Finished', header: 'Finished', value: row => row.finished_at, cell: row => date(row.finished_at, timezone, true)},
        {id: 'elapsed_hours', label: 'Elapsed hours', header: 'Elapsed hours', value: row => row.elapsed_hours, cell: row => row.elapsed_hours == null ? <Missing/> : hours(row.elapsed_hours)},
        {id: 'cost', label: 'Cost', header: 'Cost', value: row => numeric(row.cost), cell: row => row.cost == null ? <Missing/> : money(row.cost, currency)},
        ...(['cpu_efficiency_pct', 'memory_efficiency_pct', 'walltime_efficiency_pct'] as const).map((key, i) => ({id: key, label: ['CPU efficiency', 'Memory efficiency', 'Walltime efficiency'][i],
            header: <InfoTitle title={['CPU efficiency', 'Memory efficiency', 'Walltime efficiency'][i]}>{metricDefinitions[key]}</InfoTitle>, value: (row: JobRow) => row[key],
            cell: (row: JobRow) => row[key] == null ? <Missing/> : <StatusIndicator type={efficiencyStatus(row[key]!)}>{percent(row[key])}</StatusIndicator>})),
        {id: 'wasted_core_hours', label: 'Unused core-hours', header: <InfoTitle title="Unused core-hours">{metricDefinitions.wasted_core_hours}</InfoTitle>, value: row => row.wasted_core_hours, cell: row => row.wasted_core_hours == null ? <Missing/> : hours(row.wasted_core_hours)},
        ...(personal ? [{id: 'hint', label: 'CPU use', header: 'CPU use', value: jobHint, cell: (row: JobRow) => jobHint(row) ?? <Missing/>}] : [])
    ];
    return <InsightsTable title={title} rows={rows} columns={columns} defaultSort={title.includes('Least') ? 'wasted_core_hours' : 'cost'} empty="No jobs finished in this period"/>;
}
export function CostBars({title, rows, currency, entity, info}: {title: string; rows: Ranked[]; currency: string; entity: string; info?: string}) {
    // Categories remain individually readable; series beyond eight share the Other legend entry.
    const points = rows.map(row => ({name: row.name, x: row.name, value: Number(row.cost)}));
    const series = cappedSeries(points).map(series => ({...series, type: 'bar' as const, valueFormatter: (value: number) => money(value, currency)}));
    return <Container header={<Header variant="h2">{info ? <InfoTitle title={title}>{info}</InfoTitle> : title}</Header>}><BarChart series={series} xDomain={rows.map(row => row.name)} xScaleType="categorical" yScaleType="linear" horizontalBars stackedBars
        xTitle={entity} yTitle={`Cost (${currency})`} yTickFormatter={value => money(value, currency)} ariaLabel={title} hideFilter hideLegend={series.length < 2}
        height={Math.max(200, rows.length * 28)} empty={<Box>No costs in this period</Box>}/></Container>;
}
export function DailyChart({title, points, currency, timezone, area = false, storage = false, empty}: {title: string; points: NamedPoint[]; currency: string; timezone: string; area?: boolean; storage?: boolean; empty: string}) {
    const measured = storage ? measuredTierPoints(points) : points;
    const series = cappedSeries(measured);
    const format = (value: number) => storage ? `${hours(value)} GiB` : money(value, currency);
    const common = {xDomain: Array.from(new Set(measured.map(point => point.x))).sort(), xScaleType: 'categorical' as const, yScaleType: 'linear' as const,
        xTitle: 'Date', yTitle: storage ? 'Stored data (GiB)' : `Cost (${currency})`, xTickFormatter: (value: string) => date(value, timezone), yTickFormatter: format,
        height: 280, hideFilter: true, hideLegend: series.length < 2, ariaLabel: title, empty: <Box>{empty}</Box>};
    return <Container header={<Header variant="h2">{storage ? <InfoTitle title={title}>Shows dates with measurements for all displayed tiers.</InfoTitle> : title}</Header>}>{area
        ? <AreaChart {...common} detailTotalFormatter={format} series={series.map(series => ({...series, type: 'area', valueFormatter: format}))}/>
        : <BarChart {...common} stackedBars series={series.map(series => ({...series, type: 'bar', valueFormatter: format}))}/>}</Container>;
}
export function InsightTab({tab, insights: data, summary, timezone}: {tab: string; insights: ReportingInsights; summary: ReportingSummary; timezone: string}) {
    const currency = data.currency;
    const costs = [data.jobs.cost, data.desktops.cost, data.storage.cost].filter(value => value != null);
    const total = costs.length ? costs.reduce<number>((sum, value) => sum + Number(value), 0) : null;
    const tile = (title: string, value: ReportingNumber | null | undefined, details?: ReportingCoverage, definition?: string) => <MetricTile title={title} value={value == null ? null : money(value, currency)} info={metricInfo(details, timezone, definition)}/>;
    if (tab === 'overview') return <SpaceBetween size="l">
        <ColumnLayout columns={3}>
            {tile('Total spend', total, summary.coverage.spend_total, 'Adds the available job, desktop and storage costs for this period.')}
            {tile('Job spend', data.jobs.cost, summary.coverage.jobs, 'Adds the costs of jobs that finished in this period.')}
            {tile('Savings vs on-demand', data.jobs.savings, undefined, 'Compares reserved prices with on-demand prices for the same job resources.')}
            <MetricTile title="CPU efficiency" value={data.jobs.cpu_efficiency_pct == null ? null : percent(data.jobs.cpu_efficiency_pct)} info={`${metricDefinitions.cpu_efficiency_pct} Averages jobs with the needed measurements.`}>
                {data.jobs.wasted_core_hours != null && <Box>{hours(data.jobs.wasted_core_hours)} unused core-hours</Box>}
            </MetricTile>
            {tile('Desktop spend', data.desktops.cost, summary.coverage.desktops, 'Desktop hours multiplied by instance prices.')}
            {tile('Storage spend', data.storage.cost, summary.coverage.shared_storage, 'Storage cost based on measured use.')}
        </ColumnLayout>
        <DailyChart title="Daily job cost by project" points={data.jobs.daily_by_project.map(row => ({name: row.project, x: row.date, value: Number(row.cost)}))} currency={currency} timezone={timezone} empty="No jobs finished in this period"/>
        <CostBars title="Spend by user" rows={data.jobs.by_user ?? []} info="Finished job costs for the top 15 users." currency={currency} entity="User"/>
        <BudgetTable budgets={data.budgets} currency={currency}/>
    </SpaceBetween>;
    if (tab === 'jobs') return <SpaceBetween size="l">
        <EfficiencyTiles jobs={data.jobs} currency={currency}/>
        <CostBars title="Job cost by queue" rows={data.jobs.by_queue} currency={currency} entity="Queue"/>
        <CostBars title="Job cost by project" rows={data.jobs.by_project} currency={currency} entity="Project"/>
        <CostBars title="Job cost by instance family" rows={data.jobs.by_instance_family} currency={currency} entity="Instance family"/>
        <JobsTable title="Costliest jobs" rows={data.jobs.costliest} currency={currency} timezone={timezone}/>
        <JobsTable title="Least efficient jobs" rows={data.jobs.least_efficient} currency={currency} timezone={timezone}/>
    </SpaceBetween>;
    if (tab === 'desktops') return <SpaceBetween size="l">
        <ColumnLayout columns={2}>{tile('Desktop spend', data.desktops.cost)}<MetricTile title="Desktop hours" value={data.desktops.hours == null ? null : hours(data.desktops.hours)} info="Time between desktop creation and stopping within this period."/></ColumnLayout>
        <DailyChart title="Daily desktop cost by user" points={data.desktops.daily_top_users.map(row => ({name: row.user, x: row.date, value: Number(row.cost)}))} currency={currency} timezone={timezone} area empty="No desktop costs in this period"/>
        <CostBars title="Desktop cost by user" rows={data.desktops.by_user ?? []} currency={currency} entity="User"/>
        <CostBars title="Desktop cost by project" rows={data.desktops.by_project} currency={currency} entity="Project"/>
    </SpaceBetween>;
    return <SpaceBetween size="l">
        <ColumnLayout columns={2}>{tile('Storage spend', data.storage.cost)}<MetricTile title="Stored data" value={data.storage.used_bytes == null ? null : bytes(data.storage.used_bytes)} info="Most recent measured storage use in this period."/></ColumnLayout>
        <Container header={<Header variant="h2">Stored data by user</Header>}><BarChart
            series={cappedSeries((data.storage.by_user ?? []).map(row => ({name: row.name, x: row.name, value: row.bytes / 2 ** 30}))).map(series => ({...series, type: 'bar', valueFormatter: (value: number) => `${hours(value)} GiB`}))}
            xDomain={(data.storage.by_user ?? []).map(row => row.name)} xScaleType="categorical" yScaleType="linear" horizontalBars stackedBars
            xTitle="User" yTitle="Stored data (GiB)" yTickFormatter={value => `${hours(value)} GiB`} height={Math.max(200, (data.storage.by_user?.length ?? 0) * 28)}
            hideFilter hideLegend={(data.storage.by_user?.length ?? 0) < 2} ariaLabel="Stored data by user" empty={<Box>No storage measurements in this period</Box>}/></Container>
        <InsightsTable title="Storage by user" rows={data.storage.by_user ?? []} defaultSort="bytes" empty="No storage measurements in this period" columns={[
            {id: 'name', label: 'User', header: 'User', value: row => row.name, cell: row => row.name},
            {id: 'bytes', label: 'Stored data', header: 'Stored data', value: row => row.bytes, cell: row => bytes(row.bytes)},
            {id: 'cost', label: 'Cost', header: 'Cost', value: row => numeric(row.cost), cell: row => row.cost == null ? <Missing/> : money(row.cost, currency)}
        ]}/>
        <DailyChart title="Storage by tier" points={data.storage.tier_daily.map(row => ({name: row.tier === 'ssd' ? 'SSD' : 'Capacity pool', x: row.date, value: row.bytes / 2 ** 30}))} currency={currency} timezone={timezone} area storage empty="No storage measurements in this period"/>
    </SpaceBetween>;
}
