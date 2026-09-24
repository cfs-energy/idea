import {render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import MyCosts from './my-costs';
import {initTestAppContext} from '../../test-support';
import {FACETS} from '../../components/cost-charts';

const props: any = {ideaPageId: 'my-costs', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};
const open = () => render(<MemoryRouter><MyCosts {...props}/></MemoryRouter>);
const month: any = {start_date: '2026-09-01', end_date: '2026-09-02', total: 50, incomplete: true,
    ...Object.fromEntries(FACETS.map(({key}) => [key, {cost: 10, status: 'partial', note: 'Stored calculation rule.', daily: [{date: '2026-09-01', day: 1, amount: 10, status: 'ready'}, {date: '2026-09-02', day: 2, status: 'unavailable'}]}]))};
afterEach(() => vi.restoreAllMocks());
it('renders five daily charts and keeps rules and folders collapsed', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue({currency: 'USD', state: 'ready', current: month});
    const usage = vi.spyOn(context.client().fileBrowser(), 'getStorageUsage').mockResolvedValue({state: 'ready', home: '/home/user-a', folders: []});
    open();
    expect(await screen.findByText('$50.00')).toBeInTheDocument();
    expect(screen.getAllByText('This month: 1 of 2 days without data')).toHaveLength(5);
    expect(usage).not.toHaveBeenCalled();
    expect(screen.getByRole('button', {name: 'How it is calculated'})).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(screen.getByRole('button', {name: 'How it is calculated'}));
    expect(screen.getAllByText('Stored calculation rule.')).toHaveLength(5);
    await userEvent.click(screen.getByRole('button', {name: 'Storage usage: folders and quotas'}));
    await waitFor(() => expect(usage).toHaveBeenCalledOnce());
});
it('acknowledges refresh without clearing the visible generation', async () => {
    const context = initTestAppContext();
    const snapshot = {currency: 'USD', state: 'ready', current: month};
    vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue(snapshot);
    const refresh = vi.spyOn(context.client().myCosts(), 'refresh').mockResolvedValue({...snapshot, state: 'refreshing', refresh_pending: true, refresh_acknowledged: true});
    open();
    await screen.findByText('$50.00');
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(await screen.findByText('Refresh requested')).toBeInTheDocument();
    expect(screen.getByText('$50.00')).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledOnce();
});
it('shows the collecting ETA on a cold user', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue({currency: 'USD', state: 'collecting', expected_ready_at: new Date(Date.now() + 300000).toISOString()});
    open();
    expect(await screen.findAllByText('Collecting · about 5 min')).toHaveLength(2);
    expect(document.querySelector('.personal-costs-tiles')).not.toHaveTextContent('$0.00');
});
