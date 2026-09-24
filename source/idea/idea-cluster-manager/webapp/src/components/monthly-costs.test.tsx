import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {CostsBillboard, costBadge} from './monthly-costs';
import {GetMyCostsResult, MyCostsMonth} from '../client/data-model';
import {FACETS} from './cost-charts';

const line = {cost: 1, status: 'ready', note: 'Calculation rule.'};
const month: MyCostsMonth = {start_date: '2026-09-01', end_date: '2026-09-02', total: 5, incomplete: false,
    jobs: line, desktops: line, desktop_disks: line, shared_storage: line, ai: line};
const costs: GetMyCostsResult = {state: 'ready', currency: 'EUR', current: month, previous: {...month, total: 10}, refreshed_at: '2026-09-02T12:00:00Z'};

describe('personal costs card', () => {
    it('shows compact headlines, five tiles and a Home chart without calculation prose', () => {
        render(<CostsBillboard costs={costs} home/>);
        expect(screen.getByText('€5.00')).toBeInTheDocument();
        expect(screen.getByText('€10.00')).toBeInTheDocument();
        expect(screen.getByText('Estimated costs · This month')).toBeInTheDocument();
        expect(screen.getByText('Estimated costs · Last month')).toBeInTheDocument();
        expect(screen.getByText(/^As of /)).toBeInTheDocument();
        expect(screen.queryByText('Calculation rule.')).toBeNull();
        for (const {label, target} of FACETS) expect(screen.getByRole('link', {name: label})).toHaveAttribute('href', `#/home/my-costs?facet=${target}`);
        expect(screen.getByRole('link', {name: 'View My costs'})).toBeInTheDocument();
        expect(screen.getByRole('application', {name: 'Costs by facet, this month and last month'})).toBeInTheDocument();
    });
    it.each([
        [{cost: 1, status: 'ready'}, false, null],
        [{cost: 1, status: 'partial'}, false, 'Partial'],
        [{status: 'unavailable'}, false, 'No data'],
        [{cost: 0, status: 'ready'}, false, null],
        [{cost: 1, status: 'estimated_share'}, false, 'Estimated share'],
        [undefined, true, 'Collecting']
    ])('uses a badge only for affected coverage', (value, collecting, expected) => {
        expect(costBadge(value, collecting)).toBe(expected);
    });
    it('keeps known values during refresh and offers a keyboard coverage explanation', async () => {
        render(<CostsBillboard costs={{...costs, state: 'refreshing', current: {...month, incomplete: true, shared_storage: {status: 'unavailable', reason: 'Missing dated usage.'}}}}/>);
        expect(screen.getByText('Known costs · This month')).toBeInTheDocument();
        expect(screen.getByText('€5.00')).toBeInTheDocument();
        const badge = screen.getByRole('button', {name: 'Shared storage: No data'});
        badge.focus();
        await userEvent.keyboard('{Enter}');
        expect(screen.getByText(/Missing dated usage/)).toBeInTheDocument();
        expect(screen.getByText('--')).toBeInTheDocument();
    });
    it('shows collecting with the stored ETA and no fabricated zero', () => {
        render(<CostsBillboard costs={{currency: 'USD', state: 'collecting', expected_ready_at: new Date(Date.now() + 1200000).toISOString()}}/>);
        expect(screen.getAllByText('Collecting · about 20 min')).toHaveLength(2);
        expect(screen.getAllByText('Collecting')).toHaveLength(5);
        expect(screen.queryByText('$0.00')).toBeNull();
    });
    it('moves focus to the selected daily chart', async () => {
        render(<><CostsBillboard costs={costs}/><section id="cost-jobs" tabIndex={-1}/></>);
        await userEvent.click(screen.getByRole('button', {name: 'Jobs'}));
        expect(document.getElementById('cost-jobs')).toHaveFocus();
    });
});
