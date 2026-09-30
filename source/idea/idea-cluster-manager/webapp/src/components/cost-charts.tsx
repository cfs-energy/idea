import React from 'react';
import {BarChart, Box, Container, ExpandableSection, Grid, Header, LineChart, SpaceBetween} from '@cloudscape-design/components';
import {calendarDays, colorByName, date, money as formatMoney} from '../pages/reporting/reporting-format';
import InsightsTable from '../pages/reporting/insights-table';
import {Missing} from '../pages/reporting/insights-components';
import {GetMyCostsResult, MyCostsAmount, MyCostsDaily, MyCostsMonth} from '../client/data-model';

export const FACETS: {key: keyof Pick<MyCostsMonth, 'jobs' | 'desktops' | 'desktop_disks' | 'shared_storage' | 'ai'>; label: string; target: string}[] = [
    {key: 'jobs', label: 'Jobs', target: 'cost-jobs'},
    {key: 'desktops', label: 'Desktops', target: 'cost-desktops'},
    {key: 'desktop_disks', label: 'Desktop disks', target: 'cost-disks'},
    {key: 'shared_storage', label: 'Shared storage', target: 'cost-storage'},
    {key: 'ai', label: 'AI', target: 'cost-ai'}
];
export const facetAmount = (line?: MyCostsAmount): number | null | undefined => line?.amount === undefined ? line?.cost : line.amount;
export const money = formatMoney;
export const dailyPoints = (days: MyCostsDaily[] = [], start?: string, end?: string) => {
    const byDate = new Map(days.map(day => [day.date, day.amount]));
    const dates = days.map(day => day.date).sort();
    const first = start ?? dates[0], last = end ?? dates.at(-1);
    return first && last ? calendarDays(first, last).flatMap(day => byDate.has(day) && byDate.get(day) == null ? [] : [{x: new Date(`${day}T12:00:00Z`), y: byDate.get(day) ?? 0}]) : [];
};
// Last month is complete and comes first; this month is still growing.
export const dailySeries = (current: MyCostsDaily[] = [], previous: MyCostsDaily[] = [], currency = 'USD', currentPeriod?: MyCostsMonth, previousPeriod?: MyCostsMonth) => [
    {title: 'Last month', color: colorByName('Last month'), type: 'line' as const, data: dailyPoints(previous, previousPeriod?.start_date, previousPeriod?.end_date), valueFormatter: (value: number) => money(value, currency)},
    {title: 'This month', color: colorByName('This month'), type: 'line' as const, data: dailyPoints(current, currentPeriod?.start_date, currentPeriod?.end_date), valueFormatter: (value: number) => money(value, currency)}
];
// Axis ticks keep enough digits to stay distinct when the whole range is cents.
export const tickMoney = (max: number, currency: string) => {
    const digits = max < 0.1 ? 4 : max < 1 ? 3 : 2;
    return (value: number) => new Intl.NumberFormat(undefined, {style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits}).format(value);
};
export const comparisonSeries = (costs: GetMyCostsResult | null) => ['previous', 'current'].map((period, i) => ({
    title: i === 0 ? 'Last month' : 'This month', color: colorByName(i === 0 ? 'Last month' : 'This month'), type: 'bar' as const,
    data: FACETS.flatMap(({key, label}) => {
        const amount = facetAmount(costs?.[period as 'current' | 'previous']?.[key]);
        return amount == null ? [] : [{x: label, y: amount}];
    }), valueFormatter: (value: number) => money(value, costs?.currency || 'USD')
}));
export function FacetComparison({costs}: {costs: GetMyCostsResult | null}) {
    const currency = costs?.currency || 'USD';
    return <BarChart series={comparisonSeries(costs)} xDomain={FACETS.map(f => f.label)}
        xScaleType="categorical" yScaleType="linear" xTitle="Service" yTitle={currency}
        yTickFormatter={value => money(value, currency)} height={120} stackedBars={false}
        hideFilter={true} hideLegend={false} ariaLabel="Costs by service, this month and last month"
        statusType="finished" empty={<span>No daily cost data</span>}/>;
}
export const missingDays = (days: MyCostsDaily[] = []) => days.filter(d => d.amount == null).map(d => d.day).join(', ');
export const missingSummary = (days: MyCostsDaily[] = []) => { const n = days.filter(d => d.amount == null).length; return n === 0 ? '' : n === days.length ? `No data for any of the ${days.length} days` : `${n} of ${days.length} days without data`; };
export function DailyCostCharts({costs}: {costs: GetMyCostsResult | null}) {
    const currency = costs?.currency || 'USD';
    return <Grid gridDefinition={FACETS.map(() => ({colspan: {default: 12, m: 6}}))}>
        {FACETS.filter(({key}) => [...(costs?.current?.[key]?.daily ?? []), ...(costs?.previous?.[key]?.daily ?? [])].some(day => day.amount != null)).map(({key, label, target}) => {
            const current = costs?.current?.[key]?.daily ?? [];
            const previous = costs?.previous?.[key]?.daily ?? [];
            const rows = [...previous.map(d => ({...d, period: 'Last month'})), ...current.map(d => ({...d, period: 'This month'}))];
            const series = dailySeries(current, previous, currency, costs?.current, costs?.previous);
            return <section key={key} id={target} tabIndex={-1} aria-label={`${label} daily costs`}>
                <Container header={<Header variant="h2">{label}</Header>}>
                    <SpaceBetween size="s">
                        <LineChart series={series.some(s => s.data.length) ? series : []}
                            xScaleType="time" yScaleType="linear" xTitle="Date"
                            xTickFormatter={value => new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'short', day: 'numeric', ...(costs?.current?.start_date?.slice(0, 4) !== (costs?.previous?.start_date ?? costs?.current?.start_date)?.slice(0, 4) ? {year: 'numeric' as const} : {})}).format(value)} yTitle={currency}
                            yTickFormatter={value => money(value, currency)} height={200}
                            hideFilter={true} hideLegend={false} ariaLabel={`${label}: daily costs, this month and last month`}
                            detailPopoverSeriesContent={({series, x, y}) => {
                                const day = (series.title === 'This month' ? current : previous).find(d => d.date === x.toISOString().slice(0, 10));
                                return {key: `${series.title} · ${day?.date ? date(day.date, costs?.timezone ?? 'UTC') : date(x.toISOString().slice(0, 10), 'UTC')}`, value: money(y, currency)};
                            }}
                            statusType="finished" empty={<Box textAlign="center" color="inherit">
                                <Box variant="strong" color="inherit">No data this month or last month</Box>
                            </Box>}/>
                        {series.some(s => s.data.length) && (missingSummary(previous) || missingSummary(current)) && <Box variant="small" color="text-body-secondary">
                            {[missingSummary(previous) && `Last month: ${missingSummary(previous)}`, missingSummary(current) && `This month: ${missingSummary(current)}`].filter(Boolean).join(' · ')}
                        </Box>}
                        <ExpandableSection headerText="View daily values">
                            <InsightsTable title={`${label} daily values`} rows={rows} columns={[
                                {id: 'period', label: 'Period', header: 'Period', value: d => d.period, cell: d => d.period},
                                {id: 'date', label: 'Date', header: 'Date', value: d => d.date, cell: d => date(d.date, costs?.timezone ?? 'UTC')},
                                {id: 'amount', label: 'Cost', header: 'Cost', value: d => d.amount, cell: d => d.amount == null ? <Missing/> : money(d.amount, currency)}
                            ]} empty="No daily cost data"/>
                        </ExpandableSection>
                    </SpaceBetween>
                </Container>
            </section>;
        })}
    </Grid>;
}
