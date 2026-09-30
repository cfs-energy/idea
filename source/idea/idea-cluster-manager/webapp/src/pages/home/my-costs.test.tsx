import {act, render, screen, waitFor, within} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation, useNavigate} from 'react-router-dom';
import MyCosts from './my-costs';
import {initTestAppContext} from '../../test-support';
import {insightsFixture} from '../reporting/insights-fixture';
import {FACETS} from '../../components/cost-charts';
import {GetMyCostsResult, MyCostsMonth} from '../../client/data-model';
import {ReportingInsights} from '../../client/reporting-model';

const props: any = {ideaPageId: 'my-costs', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};
function Location() {
    const location = useLocation();
    const navigate = useNavigate();
    return <><output aria-label="Location">{location.pathname}{location.search}</output><button onClick={() => navigate(-1)}>Back</button></>;
}
const open = (search = '') => render(<MemoryRouter initialEntries={[`/home/my-costs${search}`]}><MyCosts {...props}/><Location/></MemoryRouter>);
const month = {start_date: '2026-09-01', end_date: '2026-09-02', total: 150, incomplete: true,
    ...Object.fromEntries(FACETS.map(({key}, index) => [key, {amount: (index + 1) * 10, cost: 999, status: 'partial', note: 'Stored calculation rule.'}]))} as MyCostsMonth;
const snapshot: GetMyCostsResult = {currency: 'USD', state: 'ready', timezone: 'America/New_York', current: month, previous: {...month, total: 200}};
function setup() {
    const context = initTestAppContext();
    const get = vi.spyOn(context.client().myCosts(), 'getInsights').mockResolvedValue(insightsFixture());
    const costs = vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue(snapshot);
    const refresh = vi.spyOn(context.client().myCosts(), 'refresh').mockResolvedValue({...snapshot, state: 'refreshing', refresh_pending: true, refresh_acknowledged: true});
    const reporting = vi.spyOn(context.reporting(), 'getInsights');
    const summary = vi.spyOn(context.reporting(), 'getSummary');
    const usage = vi.spyOn(context.client().fileBrowser(), 'getStorageUsage').mockResolvedValue({state: 'ready', home: '/home/user-a', folders: []});
    return {get, costs, refresh, reporting, summary, usage};
}
const tile = (title: string) => createWrapper().findAllContainers().find(container => container.findHeader()?.getElement().contains(screen.getByRole('heading', {level: 3, name: title})))!.getElement();
beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());
it('uses the personal client and shared overview with all monthly service totals and comparison', async () => {
    const {get, costs, reporting, summary} = setup();
    open('?user=other-user&username=other-user&table=breakdown&tab=breakdown');
    await screen.findByText('$150.00');
    expect(screen.getByRole('heading', {level: 1, name: 'My costs'})).toBeInTheDocument();
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Overview', 'Jobs', 'Desktops', 'Storage']);
    expect(screen.queryByText('User', {exact: true})).toBeNull();
    expect(screen.queryByText('Breakdown', {exact: true})).toBeNull();
    expect(screen.queryByText('Spend by user')).toBeNull();
    expect(screen.getByText("About $120.00 of $1,524.22 on your finished jobs paid for cores your jobs didn't use.")).toBeInTheDocument();
    expect(screen.getByText('Daily job cost by project')).toBeInTheDocument();
    expect(screen.getByText('Project budgets')).toBeInTheDocument();
    for (const [title, value] of [['Total spend', '$150.00'], ['Job spend', '$10.00'], ['Desktop spend', '$20.00'], ['Desktop disks', '$30.00'], ['Storage spend', '$40.00'], ['AI', '$50.00']]) {
        expect(within(tile(title)).getByText(value)).toBeInTheDocument();
    }
    expect(screen.getByText('Last month $200.00 · $50.00 less (this month so far vs full last month)')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith({period: 'this_month'});
    expect(costs).toHaveBeenCalledWith({});
    expect(reporting).not.toHaveBeenCalled();
    expect(summary).not.toHaveBeenCalled();
});
it('uses the shared personal job table, hints, and completed-job links', async () => {
    const {get} = setup();
    open('?tab=jobs&period=last_month&username=other-user');
    const table = await screen.findByRole('table', {name: 'Top jobs'});
    expect(within(table).getByRole('link', {name: 'Protein study'})).toHaveAttribute('href', '#/home/completed-jobs?job_id=job-1');
    expect(screen.queryByRole('columnheader', {name: 'User'})).toBeNull();
    expect(screen.getByRole('heading', {name: 'Top jobs (1)'})).toBeInTheDocument();
    expect(screen.getByText('Requested 36 cores, used about 1. Try ncpus=2. Requested 64 GiB, peak 3 GiB.')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith({period: 'last_month'});
});
it('keeps desktops and storage personal and moves lazy folders and quotas to Storage', async () => {
    const {usage} = setup();
    open();
    await screen.findByText('$150.00');
    expect(screen.queryByText('Storage usage: folders and quotas')).toBeNull();
    await userEvent.click(screen.getByRole('tab', {name: 'Desktops'}));
    expect(screen.getByText('Daily desktop cost')).toBeInTheDocument();
    expect(screen.queryByText('Daily desktop cost by user')).toBeNull();
    expect(screen.queryByText('Desktop cost by user')).toBeNull();
    await userEvent.click(screen.getByRole('tab', {name: 'Storage'}));
    expect(screen.queryByText('Stored data by user')).toBeNull();
    expect(screen.queryByText('Storage by user')).toBeNull();
    expect(usage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', {name: 'Storage usage: folders and quotas'}));
    await waitFor(() => expect(usage).toHaveBeenCalledWith({}));
});
it('preserves URL period and tab state through selection and back navigation', async () => {
    const {get} = setup();
    open('?tab=jobs');
    await screen.findByRole('table', {name: 'Top jobs'});
    await userEvent.click(screen.getByRole('button', {name: 'Reporting period This month'}));
    await userEvent.click(screen.getByRole('option', {name: 'Last month'}));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({period: 'last_month'}));
    expect(screen.getByLabelText('Location')).toHaveTextContent('?tab=jobs&period=last_month');
    await userEvent.click(screen.getByRole('tab', {name: 'Storage'}));
    expect(screen.getByLabelText('Location')).toHaveTextContent('?tab=storage&period=last_month');
    await userEvent.click(screen.getByRole('button', {name: 'Back'}));
    expect(screen.getByRole('tab', {name: 'Jobs'})).toHaveAttribute('aria-selected', 'true');
    await userEvent.click(screen.getByRole('button', {name: 'Back'}));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({period: 'this_month'}));
});
it.each(['last_30_days', 'custom'])('supports %s without sending unrelated URL fields', async period => {
    const {get} = setup();
    open(`?period=${period}&start_date=2026-08-01&end_date=2026-08-10&user=other-user`);
    await screen.findByText('Daily job cost by project');
    expect(get).toHaveBeenCalledWith({period, ...(period === 'custom' ? {start_date: '2026-08-01', end_date: '2026-08-10'} : {})});
    expect(screen.getByRole('heading', {level: 2, name: 'This month'})).toBeInTheDocument();
    expect(screen.getByText('$150.00')).toBeInTheDocument();
});
it('rejects invalid custom dates before requesting insights', async () => {
    const {get} = setup();
    open('?period=custom&start_date=2026-09-10&end_date=2026-09-01');
    expect((await screen.findAllByText('End date must be on or after start date.')).length).toBeGreaterThan(0);
    expect(get).not.toHaveBeenCalled();
});
it('uses last-month totals and retains the comparison', async () => {
    setup();
    open('?period=last_month');
    await waitFor(() => expect(within(tile('Total spend')).getByText('$200.00')).toBeInTheDocument());
    expect(screen.getByText('This month so far $150.00 · $50.00 less (this month so far vs full last month)')).toBeInTheDocument();
});
it('acknowledges refresh without clearing the visible monthly totals', async () => {
    const {get, refresh} = setup();
    open();
    await screen.findByText('$150.00');
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(await screen.findByText('Refresh requested')).toBeInTheDocument();
    expect(screen.getByText('$150.00')).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledWith();
    expect(get).toHaveBeenCalledTimes(2);
});
it('shows collecting without presenting missing amounts as zero', async () => {
    const {costs} = setup();
    costs.mockResolvedValue({currency: 'USD', state: 'collecting', expected_ready_at: new Date(Date.now() + 300000).toISOString()});
    open();
    await screen.findByText('Collecting · about 5 min');
    expect(within(tile('Total spend')).queryByText('$0.00')).toBeNull();
    expect(within(tile('AI')).queryByText('$0.00')).toBeNull();
});
it('shows report errors and retries', async () => {
    const {get} = setup();
    get.mockRejectedValueOnce(new Error('Unavailable'));
    open();
    await screen.findByText("Couldn't load the report. Check your connection and try again.");
    await userEvent.click(screen.getByRole('button', {name: 'Try again'}));
    await screen.findByText('Daily job cost by project');
});
it('does not display late insights for a previous period', async () => {
    const {get} = setup();
    let resolve!: (value: ReportingInsights) => void;
    get.mockReturnValueOnce(new Promise(done => {resolve = done;}));
    open();
    await userEvent.click(screen.getByRole('button', {name: 'Reporting period This month'}));
    await userEvent.click(screen.getByRole('option', {name: 'Last month'}));
    await screen.findByText('Daily job cost by project');
    await act(async () => resolve({...insightsFixture(), notes: ['Old report']}));
    expect(screen.queryByRole('button', {name: 'About Report details'})).toBeNull();
});
it.each([['cost-jobs', 'Jobs'], ['cost-storage', 'Storage'], ['cost-ai', 'Overview']])('preserves existing Home service links for %s', async (facet, tab) => {
    setup();
    open(`?facet=${facet}`);
    await waitFor(() => expect(screen.queryByText('Loading report')).toBeNull());
    expect(screen.getByRole('tab', {name: tab})).toHaveAttribute('aria-selected', 'true');
});
