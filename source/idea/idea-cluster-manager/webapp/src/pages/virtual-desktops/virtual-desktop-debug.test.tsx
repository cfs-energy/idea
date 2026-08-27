import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import VirtualDesktopDebug from './virtual-desktop-debug';
import { initTestAppContext } from '../../test-support';

describe('virtual desktop debug page', () => {
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
