import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {CostsBillboard, costBadge} from './monthly-costs';
import {buildBudgetUsage} from './job-budget-usage';
import {GetMyCostsResult, MyCostsMonth} from '../client/data-model';
import {FACETS} from './cost-charts';

const line = {cost: 1, status: 'ready', note: 'Calculation rule.'};
const month: MyCostsMonth = {start_date: '2026-09-01', end_date: '2026-09-02', total: 5, incomplete: false,
    jobs: line, desktops: line, desktop_disks: line, shared_storage: line, ai: line};
const costs: GetMyCostsResult = {state: 'ready', timezone: 'America/New_York', currency: 'EUR', current: month, previous: {...month, total: 10}, refreshed_at: '2026-09-02T12:00:00Z'};

describe('personal costs card', () => {
    it('shows compact headlines and five tiles without calculation prose', () => {
        render(<CostsBillboard costs={costs}/>);
        expect(screen.getByText('€5.00')).toBeInTheDocument();
        expect(screen.getByText('€10.00')).toBeInTheDocument();
        expect(screen.getAllByText('This month')[0]).toBeInTheDocument();
        expect(screen.getAllByText('Last month')[0]).toBeInTheDocument();
        expect(screen.getByText(/^Updated /)).toBeInTheDocument();
        expect(screen.queryByText('Calculation rule.')).toBeNull();
        for (const {label} of FACETS) expect(screen.getByRole('button', {name: label})).toBeInTheDocument();
    });
    it.each([
        [{cost: 1, status: 'ready'}, false, null],
        [{cost: 1, status: 'partial'}, false, 'Partial'],
        // a real desktop disk line before 26.11.2 stored it as counted: an amount on every day, none missing
        [{cost: 0.6245, status: 'partial', coverage: {missing_days: 0}}, false, null],
        [{cost: 1, status: 'ready', daily: [{date: '2020-01-01', day: 1, amount: null, status: 'unavailable'}]}, false, 'Partial'],
        [{cost: 1, status: 'partial', daily: [{date: '2020-01-01', day: 1, amount: 1, status: 'partial'}]}, false, 'Partial'],
        [{status: 'unavailable'}, false, 'No data'],
        [{cost: 0, status: 'ready'}, false, null],
        [{cost: 1, status: 'estimated_share'}, false, 'Shared cost'],
        [undefined, true, 'Collecting']
    ])('uses a badge only for affected coverage', (value, collecting, expected) => {
        expect(costBadge(value as any, collecting)).toBe(expected);
    });
    it('keeps known values during refresh and offers a keyboard coverage explanation', async () => {
        render(<CostsBillboard costs={{...costs, state: 'refreshing', current: {...month, incomplete: true, shared_storage: {status: 'unavailable', reason: 'Missing dated usage.'}}}}/>);
        expect(screen.getByText('This month')).toBeInTheDocument();
        expect(screen.getByText('€5.00')).toBeInTheDocument();
        const badge = screen.getByRole('button', {name: 'Shared storage: No data'});
        badge.focus();
        await userEvent.keyboard('{Enter}');
        expect(screen.getByText(/This month: No data yet/)).toBeInTheDocument();
        expect(screen.getByText('—')).toBeInTheDocument();
    });
    it('shows collecting with the stored ETA and no fabricated zero', () => {
        render(<CostsBillboard costs={{currency: 'USD', state: 'collecting', expected_ready_at: new Date(Date.now() + 1200000).toISOString()}}/>);
        expect(screen.getAllByText('Collecting · about 20 min')).toHaveLength(2);
        expect(screen.queryByText('Jobs')).toBeNull();
        expect(screen.queryByText('$0.00')).toBeNull();
    });
    it('moves focus to the selected daily chart', async () => {
        render(<><CostsBillboard costs={costs}/><section id="cost-jobs" tabIndex={-1}/></>);
        await userEvent.click(screen.getByRole('button', {name: 'Jobs'}));
        expect(document.getElementById('cost-jobs')).toHaveFocus();
    });
});

describe('job budget panel', () => {
    const usage = (limit: number, actual: number, forecast: number): any => ({budget_name: 'Research', budget_limit: {amount: limit, unit: 'USD'}, actual_spend: {amount: actual, unit: 'USD'}, forecasted_spend: {amount: forecast, unit: 'USD'}});
    it('does not show the whole limit as left when there is spending and no forecast', () => {
        render(buildBudgetUsage(usage(100000, 90000, 0)));
        expect(screen.getByText('No forecast yet')).toBeInTheDocument();
        expect(screen.getByText('$10,000.00')).toBeInTheDocument();
        expect(screen.queryByText('Remaining Budget')).toBeNull();
    });
    it('says how far over budget a forecast is', () => {
        render(buildBudgetUsage(usage(100, 50, 130), '2026-11-02T12:00:00Z'));
        expect(screen.getByText('Over budget by')).toBeInTheDocument();
        expect(screen.getByText('$30.00')).toBeInTheDocument();
        expect(screen.getByText(/Project budget when this job finished/)).toBeInTheDocument();
    });
});
