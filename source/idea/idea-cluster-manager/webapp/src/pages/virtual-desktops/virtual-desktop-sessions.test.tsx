import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import VirtualDesktopSessions from './virtual-desktop-sessions';
import { initTestAppContext } from '../../test-support';

const SESSION = {
    idea_session_id: 'session-1',
    dcv_session_id: 'dcv-1',
    name: 'beamline',
    owner: 'someowner',
    state: 'READY',
    software_stack: { base_os: 'rocky9' },
    server: { instance_type: 'g5.xlarge' },
    created_on: '2020-01-01T00:00:00Z'
};

async function renderPage(listing: any[] = [SESSION]) {
    const context = initTestAppContext();
    const clients = context.client();
    vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({ dcv_session: { max_root_volume_memory: 500 } });
    vi.spyOn(clients.projects(), 'listProjects').mockResolvedValue({ listing: [] } as any);
    const admin = clients.virtualDesktopAdmin();
    const listSessions = vi.spyOn(admin, 'listSessions').mockResolvedValue({ listing, paginator: {} } as any);
    const flashbarItems: any[] = [];
    render(
        <MemoryRouter>
            <VirtualDesktopSessions
                ideaPageId="virtual-desktop-sessions"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={(event: any) => flashbarItems.push(...event.items)}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
    await screen.findByText('beamline', {}, { timeout: 10000 });
    return { admin, clients, listSessions, flashbarItems, user: userEvent.setup() };
}

async function runAction(user: ReturnType<typeof userEvent.setup>, action: string) {
    createWrapper().findTable()!.findRowSelectionArea(1)!.click();
    await user.click(await screen.findByRole('button', { name: 'Actions' }));
    await user.click(screen.getByRole('menuitem', { name: action }));
}

async function confirm(user: ReturnType<typeof userEvent.setup>, title: string) {
    const dialog = screen.getByText(title).closest('[role="dialog"]') as HTMLElement;
    await user.click(within(dialog).getByRole('button', { name: 'Yes' }));
}

describe('manage desktops', () => {
    afterEach(() => vi.restoreAllMocks());

    it('lists desktops of any age by default', async () => {
        const { listSessions } = await renderPage();
        expect(listSessions.mock.calls[0][0].date_range).toBeUndefined();
    });

    it('names a desktop by name and owner after an action, not by id', async () => {
        const { admin, user, flashbarItems } = await renderPage();
        vi.spyOn(admin, 'stopSessions').mockResolvedValue({ success: [], failed: [{ ...SESSION, failure_reason: 'Not running' }] } as any);
        await runAction(user, 'Stop/Hibernate Session(s)');
        await confirm(user, 'Stop/Hibernate Sessions');
        await waitFor(() => expect(flashbarItems.length).toBeGreaterThan(0));
        const { container } = render(<div>{flashbarItems[0].content}</div>);
        expect(container).toHaveTextContent('beamline (Owner: someowner) - Error: Not running');
        expect(container).not.toHaveTextContent('session-1');
    });

    it('says terminate throughout', async () => {
        const { user } = await renderPage();
        await runAction(user, 'Terminate Session(s)');
        expect(screen.getByText('Terminate Sessions')).toBeInTheDocument();
        expect(screen.getByText('Are you sure you want to terminate below sessions:')).toBeInTheDocument();
        expect(screen.queryByText('Delete Sessions')).not.toBeInTheDocument();
    });

    it('shows why a desktop failed in the table', async () => {
        await renderPage([{ ...SESSION, state: 'ERROR', failure_reason: 'The host never started.' }]);
        expect(screen.getByText('The host never started.')).toBeInTheDocument();
    });

    it.each([
        ['a failed request', () => Promise.reject(new Error('Broker down')), 'Session health could not be loaded: Broker down'],
    ])('reports %s for session health', async (_name, answer, text) => {
        const { clients, user, flashbarItems } = await renderPage();
        vi.spyOn(clients.virtualDesktopDCV(), 'describeSessions').mockImplementation(answer as any);
        await runAction(user, 'Session(s) Health');
        await waitFor(() => expect(flashbarItems.map(item => item.content)).toContain(text));
    });

    it('says the broker has no record instead of an empty object', async () => {
        const { clients, user } = await renderPage();
        vi.spyOn(clients.virtualDesktopDCV(), 'describeSessions').mockResolvedValue({ response: { sessions: {} } } as any);
        await runAction(user, 'Session(s) Health');
        expect(await screen.findByText('The broker has no record of this session.')).toBeInTheDocument();
    });

    it('a plain stop does not force, so the state check still applies', async () => {
        const { admin, user } = await renderPage();
        const stopSessions = vi.spyOn(admin, 'stopSessions').mockResolvedValue({ success: [], failed: [] } as any);
        await runAction(user, 'Stop/Hibernate Session(s)');
        await confirm(user, 'Stop/Hibernate Sessions');
        await waitFor(() => expect(stopSessions).toHaveBeenCalled());
        expect(stopSessions.mock.calls[0][0].sessions![0].force).toBe(false);
    });

    it('a force stop still forces', async () => {
        const { admin, user } = await renderPage();
        const stopSessions = vi.spyOn(admin, 'stopSessions').mockResolvedValue({ success: [], failed: [] } as any);
        await runAction(user, 'Force Stop/Hibernate Session(s)');
        await confirm(user, 'Force Stop/Hibernate Sessions');
        await waitFor(() => expect(stopSessions).toHaveBeenCalled());
        expect(stopSessions.mock.calls[0][0].sessions![0].force).toBe(true);
    });
});
