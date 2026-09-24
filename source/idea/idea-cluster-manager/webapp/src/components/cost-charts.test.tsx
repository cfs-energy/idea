import {render, screen} from '@testing-library/react';
import {comparisonSeries, facetAmount, DailyCostCharts, dailyPoints, dailySeries, FACETS, missingDays, missingSummary, tickMoney} from './cost-charts';
import {GetMyCostsResult, MyCostsDaily} from '../client/data-model';

const days: MyCostsDaily[] = [
    {date: '2024-02-01', day: 1, amount: 0, status: 'ready'},
    {date: '2024-02-02', day: 2, status: 'unavailable'},
    {date: '2024-02-03', day: 3, amount: -2, status: 'partial'},
    {date: '2024-02-29', day: 29, amount: 5, status: 'ready'}
];
it('uses stored amounts, preserves zeros and corrections, and omits unknown days', () => {
    expect(dailyPoints(days)).toEqual([{x: '1', y: 0}, {x: '3', y: -2}, {x: '29', y: 5}]);
    expect(missingDays(days)).toBe('2');
    const series = dailySeries(days, [], 'EUR');
    expect(series.map(s => s.title)).toEqual(['Last month', 'This month']);
    expect(series[0].data).toEqual([]);
    expect(series[1].data).toEqual([{x: '1', y: 0}, {x: '3', y: -2}, {x: '29', y: 5}]);
    expect(series[1].valueFormatter(2)).toBe('€2.00');
    expect(series[1]).not.toHaveProperty('color');
});
it('compares all five facets without converting missing amounts into zeros', () => {
    const series = comparisonSeries({currency: 'USD', state: 'ready', current: {jobs: {cost: 0}, ai: {cost: -1}} as any});
    expect(series.map(s => s.title)).toEqual(['Last month', 'This month']);
    expect(series[0].data).toEqual([]);
    expect(series[1].data).toEqual([{x: 'Jobs', y: 0}, {x: 'AI', y: -1}]);
});
it('renders all five daily charts and missing-day notes from the arrays', () => {
    const month = {start_date: '2024-02-01', end_date: '2024-02-29', ...Object.fromEntries(FACETS.map(f => [f.key, {daily: days}]))};
    render(<DailyCostCharts costs={{currency: 'USD', state: 'ready', current: month} as GetMyCostsResult}/>);
    expect(screen.getAllByText('This month: 1 of 4 days without data')).toHaveLength(5);
    for (const {label} of FACETS) expect(screen.getByRole('heading', {name: label})).toBeInTheDocument();
    expect(screen.getAllByRole('button', {name: 'View daily values'})).toHaveLength(5);
});

it('prefers the snapshot amount and never replaces an explicit unknown with a legacy value', () => {
    expect(facetAmount({amount: 3, cost: 2})).toBe(3);
    expect(facetAmount({amount: null, cost: 2} as any)).toBeNull();
});

it('keeps small axis ticks distinct and summarises missing days by count', () => {
    const ticks = [0, 0.005, 0.01, 0.015, 0.02].map(tickMoney(0.02, 'USD'));
    expect(new Set(ticks).size).toBe(ticks.length);
    expect(tickMoney(40, 'USD')(4)).toBe('$4.00');
    expect(missingSummary(days)).toBe('1 of 4 days without data');
    expect(missingSummary(days.filter(d => d.amount == null))).toBe('No data for any of the 1 days');
    expect(missingSummary(days.filter(d => d.amount != null))).toBe('');
});

it('shows the empty state with its reason instead of a bare axis when a facet has no data', () => {
    const none = [{date: '2024-02-01', day: 1, status: 'unavailable'}] as MyCostsDaily[];
    const month = {start_date: '2024-02-01', end_date: '2024-02-29', ...Object.fromEntries(FACETS.map(f => [f.key, {daily: f.key === 'shared_storage' ? none : days, reason: f.key === 'shared_storage' ? 'No complete measurement.' : undefined}]))};
    render(<DailyCostCharts costs={{currency: 'USD', state: 'ready', current: month} as GetMyCostsResult}/>);
    expect(screen.getAllByText('No data this month or last month')).toHaveLength(1);
    expect(screen.getByText('No complete measurement.')).toBeInTheDocument();
    expect(screen.getAllByText('This month: 1 of 4 days without data')).toHaveLength(4);
});
