import {render, screen} from '@testing-library/react';
import {ReportLoading} from './insights-table';
import {CostBars, DailyChart, InsightTab} from './insights-components';
import {insightsFixture} from './insights-fixture';
import {ReportingSummary} from '../../client/reporting-model';

const charts = vi.hoisted(() => ({bar: vi.fn(), line: vi.fn(), area: vi.fn()}));
vi.mock('@cloudscape-design/components', async importOriginal => {
    const actual = await importOriginal<typeof import('@cloudscape-design/components')>();
    return {...actual, BarChart: (props: unknown) => {charts.bar(props); return <div data-testid="bar-chart"/>;},
        LineChart: (props: unknown) => {charts.line(props); return <div data-testid="line-chart"/>;},
        AreaChart: (props: unknown) => {charts.area(props); return <div data-testid="area-chart"/>;}};
});
beforeEach(() => {vi.clearAllMocks(); localStorage.clear();});
it.each([[], [{name: 'Project Cedar', cost: '0', count: 1, share_pct: 0}], [{name: 'Project Cedar', cost: '2', count: 1, share_pct: 100}]].map(rows => ({rows})))('uses a tile below two nonzero bars', ({rows}) => {
    render(<CostBars title="Job cost by project" rows={rows} entity="Project" currency="USD"/>);
    expect(screen.queryByTestId('bar-chart')).toBeNull();
    expect(screen.getByRole('heading', {level: 3, name: /Job cost by project/})).toBeInTheDocument();
});
it('uses one series without a legend and drops zero bars', () => {
    render(<CostBars title="Job cost by project" rows={['0', '2', '3'].map((cost, i) => ({name: `Project ${i}`, cost, count: 1, share_pct: 0}))} entity="Project" currency="USD"/>);
    const props = charts.bar.mock.calls[0][0];
    expect(props.series).toHaveLength(1);
    expect(props.series[0].data).toHaveLength(2);
    expect(props.hideLegend).toBe(true);
});
it('uses real dates and zero fills the selected daily money period', () => {
    render(<DailyChart title="Daily job cost by project" points={[{name: 'Project Cedar', x: '2026-09-02', value: 2}]} currency="USD" timezone="America/New_York" empty="No jobs finished in this period" period={{start: '2026-09-01', end: '2026-09-03', label: 'September'}}/>);
    const props = charts.area.mock.calls[0][0];
    expect(props.xScaleType).toBe('time');
    expect(props.series[0].data.map((p: {y: number}) => p.y)).toEqual([0, 2, 0]);
    expect(props.series[0].data[0].x).toBeInstanceOf(Date);
    expect(props.xTickFormatter(props.series[0].data[0].x)).toBe('Sep 1');
});
it('uses a line chart from zero with distinct storage ticks', () => {
    render(<DailyChart title="Storage by tier" points={[{name: 'SSD', x: '2026-09-01', value: 6.78}]} storage currency="USD" timezone="UTC" empty="No storage measurements in this period"/>);
    const props = charts.line.mock.calls[0][0];
    expect(props.yDomain).toEqual([0, 7]);
    expect(props.yTickFormatter(6.78)).toBe('6.78 GiB');
    expect(props.yTickFormatter(6.79)).toBe('6.79 GiB');
});
it('preserves entity colors across tabs and uses the same stored value for a tile and ranking', () => {
    const data = insightsFixture();
    const props = {insights: data, summary: {tiles: {}, coverage: {}} as ReportingSummary, timezone: 'UTC'};
    const {rerender} = render(<InsightTab {...props} tab="overview"/>);
    const projectColor = charts.area.mock.calls[0][0].series[0].color;
    rerender(<InsightTab {...props} tab="desktops"/>);
    rerender(<InsightTab {...props} tab="overview"/>);
    expect(charts.area.mock.calls.at(-1)![0].series[0].color).toBe(projectColor);
    rerender(<InsightTab {...props} tab="storage"/>);
    expect(screen.getAllByText('1.0 TiB')).toHaveLength(3);
});

it('suppresses chart and tile empty states during refresh', () => {
    render(<ReportLoading.Provider value={true}><CostBars title="Job cost by queue" rows={[]} entity="Queue" currency="USD"/>
        <DailyChart title="Daily job cost by project" points={[]} currency="USD" timezone="UTC" empty="No jobs finished in this period"/>
    </ReportLoading.Provider>);
    expect(screen.queryByText('No costs in this period')).toBeNull();
    expect(charts.area.mock.calls[0][0].statusType).toBe('loading');
    expect(charts.area.mock.calls[0][0].empty).toBeNull();
});
