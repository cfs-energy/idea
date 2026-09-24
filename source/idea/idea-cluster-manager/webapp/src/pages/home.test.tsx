import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, expect, it, vi} from 'vitest';
import Home from './home';
import {initTestAppContext} from '../test-support';

const props: any = {ideaPageId: 'home', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};
const open = () => render(<MemoryRouter><Home {...props}/></MemoryRouter>);

afterEach(() => vi.restoreAllMocks());

it('renders costs, recent jobs and desktops independently', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue({currency: 'USD', state: 'ready', current: {total: 4} as any});
    vi.spyOn(context.client().scheduler(), 'listActiveJobs').mockResolvedValue({listing: [{job_id: '1', name: 'Solve', state: 'running'}]});
    vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockResolvedValue({listing: []});
    vi.spyOn(context.client().virtualDesktop(), 'listSessions').mockResolvedValue({listing: [{idea_session_id: 'd1', name: 'Design', state: 'READY'}]});
    open();
    expect(await screen.findByText('Recent jobs')).toBeInTheDocument();
    expect(await screen.findByText('Recent desktops')).toBeInTheDocument();
    expect(await screen.findByText('Your costs')).toBeInTheDocument();
    expect(screen.getByText('Solve')).toBeInTheDocument();
    expect(screen.getByText('Design')).toBeInTheDocument();
});

it('hides empty sections and shows failures in their own section', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(context.client().myCosts(), 'getCosts').mockRejectedValue(new Error('cost source'));
    vi.spyOn(context.client().scheduler(), 'listActiveJobs').mockResolvedValue({listing: []});
    vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockResolvedValue({listing: []});
    vi.spyOn(context.client().virtualDesktop(), 'listSessions').mockRejectedValue(new Error('desktop source'));
    open();
    expect(await screen.findByText(/Your costs could not be loaded: cost source/)).toBeInTheDocument();
    expect(await screen.findByText(/Recent desktops could not be loaded: desktop source/)).toBeInTheDocument();
    expect(screen.queryByText('Recent jobs')).not.toBeInTheDocument();
});
