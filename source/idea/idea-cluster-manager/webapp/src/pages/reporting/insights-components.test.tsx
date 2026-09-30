import {render, screen, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {csvCell, readPreferences, savePreferences} from './insights-table';
import {BudgetTable, Coaching, EfficiencyTiles, JobsTable, jobHint, MetricTile, mergeJobs} from './insights-components';
import {exampleJob, insightsFixture} from './insights-fixture';
import {budgetPresentation} from './reporting-format';

it.each(['ok', 'watch', 'over'] as const)('colors the %s budget bar and shows its status', status => {
    const {container} = render(<BudgetTable budgets={[{...insightsFixture().budgets[0], status}]} currency="USD"/>);
    expect(screen.getByText(budgetPresentation[status].label)).toBeInTheDocument();
    expect(screen.getByRole('progressbar', {name: 'Project Cedar forecast used'})).toHaveAttribute('value', '90');
    expect(container.querySelector('[style*="background-color"]')).toBeNull();
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
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('Study 29');
    await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
    expect(within(table).getAllByRole('row')).toHaveLength(6);
    await userEvent.type(screen.getByRole('searchbox', {name: 'Find in Costliest jobs'}), 'Study 28');
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('Study 28')).toBeInTheDocument();
});

it('exports numeric corrections and safely quotes text cells', () => {
    expect(csvCell(-2.5)).toBe('"-2.5"');
    expect(csvCell('Study "A"')).toBe('"Study ""A"""');
    expect(csvCell('=2+2')).toBe('"\'=2+2"');
});

beforeEach(() => localStorage.clear());
it.each([
    [0, 36, 'Requested 36 cores, used less than 1. Try ncpus=1.'],
    [1, 36, 'Requested 36 cores, used about 1. Try ncpus=2.'],
    [2.41, 36, 'Requested 36 cores, used about 2.4. Try ncpus=4.'],
    [17.99, 36, 'Requested 36 cores, used about 18. Try ncpus=23.'],
    [18, 36, null], [19, 36, null], [null, 36, null], [1, null, null], [0, 0, null]
])('coaches CPU use %s of %s cores at the strict threshold', (used, requested, hint) => {
    expect(jobHint({...exampleJob, used_cores: used as number | null, requested_cores: requested as number | null, peak_memory_gib: null})).toBe(hint);
});
it('coaches memory only with both values and low use', () => {
    expect(jobHint({...exampleJob, used_cores: null})).toBe('Requested 64 GiB, peak 3 GiB.');
    expect(jobHint({...exampleJob, used_cores: null, peak_memory_gib: 32})).toBeNull();
    expect(jobHint({...exampleJob, used_cores: null, requested_memory_gib: null})).toBeNull();
    expect(jobHint({...exampleJob, used_cores: null, requested_memory_gib: null, instance_memory_gib: 72, instance_type: 'c5.9xlarge'})).toBe('Peak 3 GiB of 72 GiB on c5.9xlarge. A smaller instance type would do.');
    expect(jobHint({...exampleJob, used_cores: null, requested_memory_gib: null, instance_memory_gib: 4})).toBeNull();
});
it('leads with money and hides coaching without efficiency data', () => {
    const jobs = {...insightsFixture().jobs, cost: '3.46', wasted_cost: '2.23'};
    const {rerender} = render(<Coaching jobs={jobs} currency="USD" personal/>);
    expect(screen.getByText("About $2.23 of $3.46 on your finished jobs paid for cores your jobs didn't use.")).toBeInTheDocument();
    rerender(<Coaching jobs={jobs} currency="USD"/>);
    expect(screen.getByText('About $2.23 of $3.46 spent on finished jobs paid for unused cores.')).toBeInTheDocument();
    rerender(<Coaching jobs={{...jobs, jobs_with_efficiency: 0}} currency="USD"/>);
    expect(screen.queryByText(/About/)).toBeNull();
});
it('uses a heading for the tile label and ordinary text for its value', () => {
    render(<MetricTile title="Job spend" value="$3.46" info="Costs for this period."/>);
    expect(screen.getByRole('heading', {level: 3, name: /Job spend/})).toBeInTheDocument();
    expect(screen.queryByRole('heading', {name: '$3.46'})).toBeNull();
});
it('keeps job columns in coaching order and links to the existing completed-job detail panel', () => {
    render(<JobsTable title="My top jobs" rows={[exampleJob]} currency="USD" timezone="UTC" personal/>);
    expect(screen.getAllByRole('columnheader').map(cell => cell.textContent?.trim())).toEqual(['Job name', 'Cost', 'CPU efficiency', 'Unused core-hours', 'Hint', 'Elapsed hours', 'Queue', 'Instance type', 'Finished']);
    const link = screen.getByRole('link', {name: exampleJob.name!});
    expect(link).toHaveAttribute('href', '#/home/completed-jobs?job_id=job-1');
    expect(link.parentElement).toHaveAttribute('title', exampleJob.name);
    expect(mergeJobs(insightsFixture().jobs)).toHaveLength(1);
});
it('sorts the single job table by unused cores and retains preferences on remount', async () => {
    const rows = [exampleJob, {...exampleJob, job_id: 'job-2', name: 'Study Elm', cost: '1', wasted_core_hours: 100}];
    const {unmount} = render(<JobsTable title="Top jobs" rows={rows} currency="USD" timezone="UTC"/>);
    await userEvent.click(screen.getByRole('button', {name: 'Sort jobs Highest cost'}));
    await userEvent.click(screen.getByRole('option', {name: 'Most unused cores'}));
    expect(screen.getAllByRole('row')[1]).toHaveTextContent('Study Elm');
    await userEvent.click(screen.getByRole('button', {name: 'Table preferences'}));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('checkbox', {name: 'Job ID'}));
    await userEvent.click(within(dialog).getByRole('button', {name: 'Confirm'}));
    expect(screen.getByRole('columnheader', {name: 'Job ID'})).toBeInTheDocument();
    unmount();
    render(<JobsTable title="Top jobs" rows={rows} currency="USD" timezone="UTC"/>);
    expect(screen.getByRole('columnheader', {name: 'Job ID'})).toBeInTheDocument();
    expect(localStorage.getItem('reporting.table.my-jobs')).toBeNull();
});
it('tolerates unavailable browser storage', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {throw new Error('Disabled');});
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {throw new Error('Disabled');});
    expect(readPreferences('table', [])).toEqual([]);
    expect(() => savePreferences('table', [])).not.toThrow();
    get.mockRestore(); set.mockRestore();
});
it('shows budget percentage once and states the budget period', () => {
    render(<BudgetTable budgets={insightsFixture().budgets} currency="USD"/>);
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '90');
    // Cloudscape has separate visual and screen-reader copies; no custom percentage label.
    expect(screen.getAllByText('90%').every(element => element.tagName !== 'LABEL')).toBe(true);
    expect(screen.getByText("Spending and forecasts use each project's current budget period.")).toBeInTheDocument();
});

it('suggests CPU per node and compares against the per-node request', () => {
    expect(jobHint({...exampleJob, nodes: 4, requested_cores: 32, used_cores: 8, peak_memory_gib: null})).toBe('Requested 32 cores, used about 8. Try ncpus=3 per node.');
    expect(jobHint({...exampleJob, nodes: 4, requested_cores: 4, used_cores: 0, peak_memory_gib: null})).toBeNull();
});
it('compares peak memory with total instance memory across nodes', () => {
    expect(jobHint({...exampleJob, nodes: 2, used_cores: null, requested_memory_gib: null, instance_memory_gib: 16, peak_memory_gib: 12})).toBe('Peak 12 GiB of 32 GiB total across 2 nodes on c6i.large. A smaller instance type would do.');
    expect(jobHint({...exampleJob, nodes: 2, used_cores: null, requested_memory_gib: null, instance_memory_gib: 16, peak_memory_gib: 16})).toBeNull();
});
it.each([
    [70, 'Well sized', 'Well sized', 'Close to requested'],
    [40, 'Some cores sat idle', 'Some memory unused', 'Finished well early'],
    [0, 'Most cores sat idle', 'Most memory unused', 'Finished far earlier than requested']
])('uses metric-specific efficiency labels at %s percent', (value, cpu, memory, walltime) => {
    render(<EfficiencyTiles jobs={{...insightsFixture().jobs, cpu_efficiency_pct: Number(value), memory_efficiency_pct: Number(value), walltime_efficiency_pct: Number(value)}} currency="USD"/>);
    for (const label of [cpu, memory, walltime]) expect(screen.getAllByText(label).length).toBeGreaterThan(0);
});
it('defines unused cost against requested cores', async () => {
    render(<EfficiencyTiles jobs={insightsFixture().jobs} currency="USD"/>);
    await userEvent.click(screen.getByRole('button', {name: 'About Cost of unused core-hours'}));
    expect(screen.getByText("Job cost multiplied by the share of requested core time not used, measured against the cores requested, not the instance's vCPUs.")).toBeInTheDocument();
});
