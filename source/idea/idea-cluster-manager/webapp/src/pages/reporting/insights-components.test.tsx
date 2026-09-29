import {render, screen, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {csvCell} from './insights-table';
import {BudgetTable, EfficiencyTiles, JobsTable} from './insights-components';
import {exampleJob, insightsFixture} from './insights-fixture';
import {budgetPresentation} from './reporting-format';
import MyJobEfficiency from '../home/my-job-efficiency';
import {initTestAppContext} from '../../test-support';

it.each(['ok', 'watch', 'over'] as const)('colors the %s budget bar and shows its status', status => {
    const {container} = render(<BudgetTable budgets={[{...insightsFixture().budgets[0], status}]} currency="USD"/>);
    expect(screen.getByText(budgetPresentation[status].label)).toBeInTheDocument();
    expect(screen.getByRole('progressbar', {name: 'Project Cedar forecast used'})).toHaveAttribute('value', '90');
    expect(container.querySelector(`[style*="${budgetPresentation[status].color}"]`)).not.toBeNull();
});
it('hides the budget section without budgets and hides missing forecast columns', () => {
    const {rerender} = render(<BudgetTable budgets={[]} currency="USD"/>);
    expect(screen.queryByText('Project budgets')).toBeNull();
    rerender(<BudgetTable budgets={[{...insightsFixture().budgets[0], forecast: null, pct_at_forecast: null, headroom: null}]} currency="USD"/>);
    expect(screen.queryByRole('columnheader', {name: /Forecast/})).toBeNull();
    expect(screen.queryByRole('columnheader', {name: /Headroom/})).toBeNull();
});
it('hides efficiency tiles with no data and keeps measured zero', () => {
    render(<EfficiencyTiles jobs={{...insightsFixture().jobs, cpu_efficiency_pct: 0, cpu_efficiency_weighted_pct: null, memory_efficiency_pct: null, walltime_efficiency_pct: null, wasted_core_hours: null, wasted_cost: null}} currency="USD"/>);
    expect(screen.getByText('0%')).toBeInTheDocument();
    expect(screen.queryByText('Memory efficiency')).toBeNull();
    expect(screen.queryByText('Unused core-hours')).toBeNull();
});
it('sorts job costs numerically, filters, and paginates at 25 rows', async () => {
    const rows = Array.from({length: 30}, (_, i) => ({...exampleJob, job_id: `job-${i}`, cost: String(i), name: `Study ${i}`}));
    render(<JobsTable title="Costliest jobs" rows={rows} currency="USD" timezone="America/New_York"/>);
    const table = screen.getByRole('table', {name: 'Costliest jobs'});
    expect(within(table).getAllByRole('row')).toHaveLength(26);
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('job-29');
    await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
    expect(within(table).getAllByRole('row')).toHaveLength(6);
    await userEvent.type(screen.getByRole('searchbox', {name: 'Find in Costliest jobs'}), 'Study 28');
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('job-28')).toBeInTheDocument();
});
it('uses the scoped client for personal efficiency and shows only its returned budgets', async () => {
    const context = initTestAppContext();
    const fixture = insightsFixture();
    delete fixture.jobs.by_user; delete fixture.desktops.by_user; delete fixture.storage.by_user;
    const get = vi.spyOn(context.client().myCosts(), 'getInsights').mockResolvedValue(fixture);
    render(<MyJobEfficiency timezone="America/New_York" reload={0}/>);
    expect(await screen.findByText('Used about 14% of requested CPU time')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith({period: 'this_month'});
    expect(screen.queryByRole('columnheader', {name: 'User'})).toBeNull();
    expect(screen.getByText('Project budgets')).toBeInTheDocument();
});

it('exports numeric corrections and safely quotes text cells', () => {
    expect(csvCell(-2.5)).toBe('"-2.5"');
    expect(csvCell('Study "A"')).toBe('"Study ""A"""');
    expect(csvCell('=2+2')).toBe('"\'=2+2"');
});
