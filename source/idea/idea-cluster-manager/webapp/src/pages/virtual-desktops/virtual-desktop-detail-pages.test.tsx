import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import VirtualDesktopSessionDetail from './virtual-desktop-session-detail';
import VirtualDesktopSoftwareStackDetail from './virtual-desktop-software-stack-detail';
import VirtualDesktopPermissionProfileDetail from './virtual-desktop-permission-profile-detail';
import { initTestAppContext } from '../../test-support';

const PAGE_PROPS = {
    toolsOpen: false,
    tools: null,
    onToolsChange: () => {},
    onPageChange: () => {},
    sideNavHeader: { text: 'IDEA', href: '#/' },
    sideNavItems: [],
    onSideNavChange: () => {},
    onFlashbarChange: () => {},
    flashbarItems: []
} as any;

function setup() {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({ dcv_session: { working_hours: {} } });
    const clients = context.client();
    vi.spyOn(clients.virtualDesktopAdmin(), 'listSessionPermissions').mockResolvedValue({ listing: [] } as any);
    return clients;
}

function renderSession() {
    return render(
        <MemoryRouter initialEntries={['/sessions/session-1?owner=someowner&tab=session-health']}>
            <Routes>
                <Route path="/sessions/:idea_session_id" element={<VirtualDesktopSessionDetail ideaPageId="virtual-desktop-session-detail" {...PAGE_PROPS}/>}/>
            </Routes>
        </MemoryRouter>
    );
}

const FAILED_SESSION = { idea_session_id: 'session-1', name: 'beamline', owner: 'someowner', state: 'ERROR', failure_reason: 'The host never started.' };
const READY_SESSION = { idea_session_id: 'session-1', dcv_session_id: 'dcv-1', name: 'beamline', owner: 'someowner', state: 'READY' };

describe('session detail page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('says a failed load failed, and can try again', async () => {
        const clients = setup();
        const getSessionInfo = vi.spyOn(clients.virtualDesktopAdmin(), 'getSessionInfo')
            .mockRejectedValueOnce(new Error('Service Unavailable'))
            .mockResolvedValue({ session: FAILED_SESSION } as any);
        renderSession();
        expect(await screen.findByText('This session could not be loaded.', {}, { timeout: 10000 })).toBeInTheDocument();
        await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }));
        expect(await screen.findByText('The host never started.')).toBeInTheDocument();
        expect(getSessionInfo).toHaveBeenCalledTimes(2);
    });

    it('offers no tab for what does not exist yet, and names the session, not its id', async () => {
        const clients = setup();
        vi.spyOn(clients.virtualDesktopAdmin(), 'getSessionInfo').mockResolvedValue({ session: FAILED_SESSION } as any);
        renderSession();
        await screen.findByText('The host never started.', {}, { timeout: 10000 });
        expect(screen.queryByText(/Coming Soon/)).not.toBeInTheDocument();
        expect(screen.queryByRole('tab', { name: 'Monitoring' })).not.toBeInTheDocument();
        expect(screen.queryByRole('tab', { name: 'Cost Estimates' })).not.toBeInTheDocument();
        expect(screen.queryByText('session-1')).not.toBeInTheDocument();
    });

    it('asks for a full page of who has access and says when that failed', async () => {
        const clients = setup();
        vi.spyOn(clients.virtualDesktopAdmin(), 'getSessionInfo').mockResolvedValue({ session: FAILED_SESSION } as any);
        const list = vi.spyOn(clients.virtualDesktopAdmin(), 'listSessionPermissions').mockRejectedValue(new Error('Service Unavailable'));
        renderSession();
        await screen.findByText('The host never started.', {}, { timeout: 10000 });
        expect(list.mock.calls[0][0].paginator?.page_size).toBe(100);
        await userEvent.setup().click(screen.getByRole('tab', { name: 'Permissions' }));
        expect(await screen.findByText('Could not load who has access.')).toBeInTheDocument();
    });

    it.each([
        ['the broker has no record', () => Promise.resolve({ response: { sessions: {} } }), 'The broker has no record of this session.'],
        ['the request failed', () => Promise.reject(new Error('Service Unavailable')), 'Session health could not be loaded.']
    ])('stops saying loading when %s', async (_name, answer, text) => {
        const clients = setup();
        vi.spyOn(clients.virtualDesktopAdmin(), 'getSessionInfo').mockResolvedValue({ session: READY_SESSION } as any);
        vi.spyOn(clients.virtualDesktopDCV(), 'describeSessions').mockImplementation(answer as any);
        renderSession();
        expect(await screen.findByText(text, {}, { timeout: 10000 })).toBeInTheDocument();
        expect(screen.queryByText('Loading session health')).not.toBeInTheDocument();
    });
});

describe('software stack detail page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('says a failed load failed', async () => {
        const clients = setup();
        vi.spyOn(clients.virtualDesktopAdmin(), 'getSoftwareStackInfo').mockRejectedValue(new Error('Service Unavailable'));
        render(
            <MemoryRouter initialEntries={['/stacks/stack-1']}>
                <Routes>
                    <Route path="/stacks/:software_stack_id" element={<VirtualDesktopSoftwareStackDetail ideaPageId="virtual-desktop-software-stack-detail" {...PAGE_PROPS}/>}/>
                </Routes>
            </MemoryRouter>
        );
        expect(await screen.findByText('This desktop image could not be loaded.', {}, { timeout: 10000 })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    });
});

describe('permission profile detail page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('says a failed load failed', async () => {
        const clients = setup();
        vi.spyOn(clients.virtualDesktopUtils(), 'getPermissionProfile').mockRejectedValue(new Error('Service Unavailable'));
        render(
            <MemoryRouter initialEntries={['/profiles/profile-1']}>
                <Routes>
                    <Route path="/profiles/:profile_id" element={<VirtualDesktopPermissionProfileDetail ideaPageId="virtual-desktop-permission-profile-detail" {...PAGE_PROPS}/>}/>
                </Routes>
            </MemoryRouter>
        );
        await waitFor(() => expect(screen.getByText('This permission profile could not be loaded.')).toBeInTheDocument(), { timeout: 10000 });
    });
});
