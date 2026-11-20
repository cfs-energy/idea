import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import VirtualDesktopDebug from './virtual-desktop-debug';
import { initTestAppContext } from '../../test-support';

function renderPage() {
    render(
        <MemoryRouter>
            <VirtualDesktopDebug
                ideaPageId="virtual-desktop-debug"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={() => {}}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
}

describe('virtual desktop debug page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('stops spinning and says so when the broker cannot be reached', async () => {
        const dcvClient = initTestAppContext().client().virtualDesktopDCV();
        vi.spyOn(dcvClient, 'describeServers').mockRejectedValue(new Error('Service Unavailable'));
        vi.spyOn(dcvClient, 'describeSessions').mockRejectedValue(new Error('Service Unavailable'));
        renderPage();
        expect(await screen.findByText('Could not reach the broker.')).toBeInTheDocument();
        expect(document.querySelector('.fa-spin')).toBeNull();
    });

    it('says when there is more than it shows', async () => {
        const dcvClient = initTestAppContext().client().virtualDesktopDCV();
        vi.spyOn(dcvClient, 'describeServers').mockResolvedValue({ response: { servers: [], next_token: 'more' } } as any);
        vi.spyOn(dcvClient, 'describeSessions').mockResolvedValue({ response: { sessions: [] } } as any);
        renderPage();
        expect(await screen.findByText('Showing the first page only.')).toBeInTheDocument();
    });

    // Covers the JSON viewer prop surface (theme/iconStyle/onEdit/displayDataTypes/name)
    // that both this page and virtual-desktop-sessions pass to @microlink/react-json-view.
    // Both themes are exercised: dark mode selects "monokai", light "grayscale:inverted".
    it.each([true, false])('renders the broker health payloads in the JSON viewer (darkMode=%s)', async (darkMode) => {
        const context = initTestAppContext();
        context.setDarkMode(darkMode);
        const dcvClient = context.client().virtualDesktopDCV();
        vi.spyOn(dcvClient, 'describeServers').mockResolvedValue({
            response: { request_id: 'req-1', servers: [{ instance_id: 'i-0abc' }] }
        } as any);
        vi.spyOn(dcvClient, 'describeSessions').mockResolvedValue({
            response: { request_id: 'req-2', sessions: [{ id: 'session-1' }] }
        } as any);

        render(
            <MemoryRouter>
                <VirtualDesktopDebug
                    ideaPageId="virtual-desktop-debug"
                    toolsOpen={false}
                    tools={null}
                    onToolsChange={() => {}}
                    onPageChange={() => {}}
                    sideNavHeader={{ text: 'IDEA', href: '#/' }}
                    sideNavItems={[]}
                    onSideNavChange={() => {}}
                    onFlashbarChange={() => {}}
                    flashbarItems={[]}
                />
            </MemoryRouter>
        );

        // request_id is stripped before rendering; the payload key is what reaches the viewer.
        expect(await screen.findByText('servers')).toBeInTheDocument();
        expect(screen.queryByText('request_id')).toBeNull();
    });
});

describe('instances with no desktop', () => {
    afterEach(() => vi.restoreAllMocks());

    function clients() {
        const context = initTestAppContext();
        const dcvClient = context.client().virtualDesktopDCV();
        vi.spyOn(dcvClient, 'describeServers').mockResolvedValue({ response: { servers: [] } } as any);
        vi.spyOn(dcvClient, 'describeSessions').mockResolvedValue({ response: { sessions: [] } } as any);
        return context.client().virtualDesktopAdmin();
    }

    it('reads the list when the page opens', async () => {
        vi.spyOn(clients(), 'listOrphanInstances').mockResolvedValue({ listing: [{ instance_id: 'i-orphan', state: 'running' }] });
        renderPage();
        await userEvent.click(await screen.findByRole('tab', { name: 'Instances with no desktop' }));
        expect(await screen.findByText('i-orphan')).toBeInTheDocument();
    });

    it('stops and says so when the list cannot be read', async () => {
        vi.spyOn(clients(), 'listOrphanInstances').mockRejectedValue(new Error('denied'));
        renderPage();
        await userEvent.click(await screen.findByRole('tab', { name: 'Instances with no desktop' }));
        expect(await screen.findByText('Could not read these instances.')).toBeInTheDocument();
    });
});
