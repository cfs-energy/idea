import React from 'react';
import {BarChart, Box, Container, ExpandableSection, Grid, Header, SpaceBetween, Table} from '@cloudscape-design/components';
import {GetMyCostsResult, MyCostsAmount, MyCostsDaily, MyCostsMonth} from '../client/data-model';

export const FACETS: {key: keyof Pick<MyCostsMonth, 'jobs' | 'desktops' | 'desktop_disks' | 'shared_storage' | 'ai'>; label: string; target: string}[] = [
    {key: 'jobs', label: 'Jobs', target: 'cost-jobs'},
    {key: 'desktops', label: 'Desktops', target: 'cost-desktops'},
    {key: 'desktop_disks', label: 'Desktop disks', target: 'cost-disks'},
    {key: 'shared_storage', label: 'Shared storage', target: 'cost-storage'},
    {key: 'ai', label: 'AI', target: 'cost-ai'}
];
export const facetAmount = (line?: MyCostsAmount): number | null | undefined => line?.amount === undefined ? line?.cost : line.amount;
export const money = (value: number | null | undefined, currency: string) => value == null ? '--'
    : new Intl.NumberFormat(undefined, {style: 'currency', currency}).format(value);
export const dailyPoints = (days: MyCostsDaily[] = []) => days.filter(day => day.amount != null)
    .map(day => ({x: String(day.day), y: day.amount!}));
// Last month is complete and comes first; this month is still growing.
export const dailySeries = (current: MyCostsDaily[] = [], previous: MyCostsDaily[] = [], currency = 'USD') => [
    {title: 'Last month', type: 'bar' as const, data: dailyPoints(previous), valueFormatter: (value: number) => money(value, currency)},
    {title: 'This month', type: 'bar' as const, data: dailyPoints(current), valueFormatter: (value: number) => money(value, currency)}
];
// Axis ticks keep enough digits to stay distinct when the whole range is cents.
export const tickMoney = (max: number, currency: string) => {
    const digits = max < 0.1 ? 4 : max < 1 ? 3 : 2;
    return (value: number) => new Intl.NumberFormat(undefined, {style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits}).format(value);
};
export const comparisonSeries = (costs: GetMyCostsResult | null) => ['previous', 'current'].map((period, i) => ({
    title: i === 0 ? 'Last month' : 'This month', type: 'bar' as const,
    data: FACETS.flatMap(({key, label}) => {
        const amount = facetAmount(costs?.[period as 'current' | 'previous']?.[key]);
        return amount == null ? [] : [{x: label, y: amount}];
    }), valueFormatter: (value: number) => money(value, costs?.currency || 'USD')
}));
export function FacetComparison({costs}: {costs: GetMyCostsResult | null}) {
    const currency = costs?.currency || 'USD';
    return <BarChart series={comparisonSeries(costs)} xDomain={FACETS.map(f => f.label)}
        xScaleType="categorical" yScaleType="linear" xTitle="Facet" yTitle={currency}
        yTickFormatter={value => money(value, currency)} height={120} stackedBars={false}
        hideFilter={true} hideLegend={false} ariaLabel="Costs by facet, this month and last month"
        statusType="finished" empty={<span>No daily cost data</span>}/>;
}
export const missingDays = (days: MyCostsDaily[] = []) => days.filter(d => d.amount == null).map(d => d.day).join(', ');
export const missingSummary = (days: MyCostsDaily[] = []) => { const n = days.filter(d => d.amount == null).length; return n === 0 ? '' : n === days.length ? `No data for any of the ${days.length} days` : `${n} of ${days.length} days without data`; };
const monthDays = (month?: MyCostsMonth) => month?.start_date
    ? new Date(Number(month.start_date.slice(0, 4)), Number(month.start_date.slice(5, 7)), 0).getDate() : 0;

export function DailyCostCharts({costs}: {costs: GetMyCostsResult | null}) {
    const currency = costs?.currency || 'USD';
    const count = Math.max(monthDays(costs?.current), monthDays(costs?.previous));
    return <Grid gridDefinition={FACETS.map(() => ({colspan: {default: 12, m: 6}}))}>
        {FACETS.map(({key, label, target}) => {
            const current = costs?.current?.[key]?.daily ?? [];
            const previous = costs?.previous?.[key]?.daily ?? [];
            const rows = [...previous.map(d => ({...d, period: 'Last month'})), ...current.map(d => ({...d, period: 'This month'}))];
            const series = dailySeries(current, previous, currency);
            const reason = costs?.current?.[key]?.reason || costs?.previous?.[key]?.reason;
            return <section key={key} id={target} tabIndex={-1} aria-label={`${label} daily costs`}>
                <Container header={<Header variant="h2">{label}</Header>}>
                    <SpaceBetween size="s">
                        <BarChart series={series.some(s => s.data.length) ? series : []}
                            xDomain={Array.from({length: count}, (_, i) => String(i + 1))}
                            xScaleType="categorical" yScaleType="linear" xTitle="Day of month" yTitle={currency}
                            yTickFormatter={tickMoney(Math.max(0, ...current.map(d => d.amount ?? 0), ...previous.map(d => d.amount ?? 0)), currency)} height={200} stackedBars={false}
                            hideFilter={true} hideLegend={false} ariaLabel={`${label}: daily costs, this month and last month`}
                            detailPopoverSeriesContent={({series, x, y}) => {
                                const day = (series.title === 'This month' ? current : previous).find(d => String(d.day) === x);
                                return {key: `${series.title} · ${day?.date ?? x} · ${day?.status ?? ''}`, value: money(y, currency)};
                            }}
                            statusType="finished" empty={<Box textAlign="center" color="inherit">
                                <Box variant="strong" color="inherit">No data this month or last month</Box>
                                {reason && <Box variant="small" color="inherit">{reason}</Box>}
                            </Box>}/>
                        {series.some(s => s.data.length) && (missingSummary(previous) || missingSummary(current)) && <Box variant="small" color="text-body-secondary">
                            {[missingSummary(previous) && `Last month: ${missingSummary(previous)}`, missingSummary(current) && `This month: ${missingSummary(current)}`].filter(Boolean).join(' · ')}
                        </Box>}
                        <ExpandableSection headerText="View daily values">
                            <Table items={rows} columnDefinitions={[
                                {id: 'period', header: 'Period', cell: d => d.period},
                                {id: 'date', header: 'Date', cell: d => d.date},
                                {id: 'amount', header: currency, cell: d => money(d.amount, currency)},
                                {id: 'status', header: 'Coverage', cell: d => d.status}
                            ]} empty="No daily cost data"/>
                        </ExpandableSection>
                    </SpaceBetween>
                </Container>
            </section>;
        })}
    </Grid>;
}
