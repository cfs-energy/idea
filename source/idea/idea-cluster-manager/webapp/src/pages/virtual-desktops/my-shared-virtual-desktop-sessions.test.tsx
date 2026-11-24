import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import MySharedVirtualDesktopSessions from './my-shared-virtual-desktop-sessions';
import { POPUP_BLOCKED_MESSAGE } from './desktop-connection';
import { initTestAppContext } from '../../test-support';

const SHARE = {
    idea_session_id: 'session-1',
    idea_session_name: 'beamline',
    idea_session_owner: 'someowner',
    idea_session_base_os: 'rocky9',
    idea_session_instance_type: 'g5.xlarge',
    idea_session_state: 'READY',
    expiry_date: '2030-01-01T00:00:00Z'
};

async function renderPage(listing: any[]) {
    const context = initTestAppContext();
    const virtualDesktop = context.client().virtualDesktop();
    const listSharedPermissions = vi.spyOn(virtualDesktop, 'listSharedPermissions').mockResolvedValue({ listing, paginator: {} } as any);
    const flashbarItems: any[] = [];
    render(
        <MemoryRouter>
            <MySharedVirtualDesktopSessions
                ideaPageId="my-shared-virtual-desktop-sessions"
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
    return { virtualDesktop, listSharedPermissions, flashbarItems };
}

describe('desktops shared with me', () => {
    afterEach(() => vi.restoreAllMocks());

    it('asks for every current share, however old the desktop is', async () => {
        const { listSharedPermissions } = await renderPage([SHARE]);
        expect(listSharedPermissions.mock.calls[0][0].date_range).toBeUndefined();
    });

    it('uses plain column titles', async () => {
        await renderPage([SHARE]);
        for (const title of ['Owner', 'Operating system', 'Size', 'Access ends']) {
            expect(screen.getByRole('columnheader', { name: new RegExp(title) })).toBeInTheDocument();
        }
    });

    it('offers nothing to click on a desktop that is not running, and says who can start it', async () => {
        await renderPage([{ ...SHARE, idea_session_state: 'STOPPED' }]);
        expect(screen.getByText('Not running. Ask someowner to start it.')).toBeInTheDocument();
        expect(screen.queryByText('Download')).not.toBeInTheDocument();
    });

    it.each(['Connect', 'Download'])('%s reports a refused connection instead of failing silently', async (link) => {
        const { virtualDesktop, flashbarItems } = await renderPage([SHARE]);
        vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockRejectedValue(new Error('Permission expired'));
        await userEvent.setup().click(screen.getAllByText(link).pop()!);
        await waitFor(() => expect(flashbarItems.some(item => item.type === 'error' && `${item.content}`.includes('Permission expired'))).toBe(true));
    });

    it('never builds a file or a link without a token', async () => {
        const { virtualDesktop, flashbarItems } = await renderPage([SHARE]);
        vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockResolvedValue({ connection_info: { endpoint: 'https://cluster.example', dcv_session_id: 'dcv-1' } } as any);
        const open = vi.spyOn(window, 'open').mockImplementation(() => null);
        await userEvent.setup().click(screen.getAllByText('Connect').pop()!);
        await waitFor(() => expect(flashbarItems.some(item => `${item.content}`.includes('Connection details are unavailable'))).toBe(true));
        expect(open).not.toHaveBeenCalled();
    });

    it('says when the browser blocked the tab', async () => {
        const { virtualDesktop, flashbarItems } = await renderPage([SHARE]);
        vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockResolvedValue({ connection_info: { endpoint: 'https://cluster.example', web_url_path: '/dcv', access_token: 'token', dcv_session_id: 'dcv-1' } } as any);
        vi.spyOn(window, 'open').mockImplementation(() => null);
        await userEvent.setup().click(screen.getAllByText('Connect').pop()!);
        await waitFor(() => expect(flashbarItems.map(item => item.content)).toContain(POPUP_BLOCKED_MESSAGE));
    });
});
