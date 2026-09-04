import {render, screen, waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import moment from 'moment';
import {vi} from 'vitest';
import IdeaAppLayout from './index';
import {initTestAppContext} from '../../test-support';
import {MaintenanceSettings} from '../../service/cluster-settings-service';

// Every signed-in page routes through this layout, which is where the banner is rendered.

const layoutProps = {
    ideaPageId: 'test-page',
    toolsOpen: false,
    tools: null,
    onToolsChange: () => {},
    onPageChange: () => {},
    sideNavHeader: {text: 'IDEA', href: '#/'},
    sideNavItems: [],
    onSideNavChange: () => {},
    onFlashbarChange: () => {},
    flashbarItems: [],
    content: <p>page body</p>
} as any;

function renderLayout(maintenance: MaintenanceSettings) {
    const context = initTestAppContext();
    const clusterSettings = context.getClusterSettingsService();
    // What the page was served with, and what the poll reads back.
    clusterSettings.maintenance = maintenance;
    vi.spyOn(clusterSettings, 'fetchMaintenance').mockResolvedValue(maintenance);
    return render(
        <MemoryRouter>
            <IdeaAppLayout {...layoutProps} />
        </MemoryRouter>
    );
}

describe('maintenance banner', () => {
    it('shows the message while the window is open', async () => {
        renderLayout({enabled: true, message: 'Scheduler closed for the 26.09 upgrade.', ends_at: ''});

        expect(await screen.findByText('Cluster maintenance')).not.toBeNull();
        expect(screen.getByText('Scheduler closed for the 26.09 upgrade.')).not.toBeNull();
    });

    it('shows nothing while the window is closed', async () => {
        renderLayout({enabled: false, message: 'Scheduler closed for the 26.09 upgrade.', ends_at: ''});

        // The page itself still renders; only the banner is absent.
        expect(await screen.findByText('page body')).not.toBeNull();
        expect(screen.queryByText('Cluster maintenance')).toBeNull();
        expect(screen.queryByText('Scheduler closed for the 26.09 upgrade.')).toBeNull();
    });

    it('adds the end of the window in the reader timezone', async () => {
        renderLayout({enabled: true, message: 'Scheduler closed.', ends_at: '2026-09-15T18:00:00Z'});

        // Rendered in the browser timezone, so the expected text is derived the same way.
        const localEnd = moment.utc('2026-09-15T18:00:00Z').local().format('lll');
        expect(await screen.findByText(`Scheduler closed. until ${localEnd}`)).not.toBeNull();
    });

    it('falls back to a generic message when the window is opened without one', async () => {
        renderLayout({enabled: true, message: '', ends_at: ''});

        expect(await screen.findByText('This cluster is undergoing maintenance.')).not.toBeNull();
    });

    it('drops an unparsable end time instead of rendering "Invalid date"', async () => {
        renderLayout({enabled: true, message: 'Scheduler closed.', ends_at: 'next tuesday'});

        expect(await screen.findByText('Scheduler closed.')).not.toBeNull();
    });

    it('takes the banner down when the poll reports the window closed', async () => {
        const context = initTestAppContext();
        const clusterSettings = context.getClusterSettingsService();
        clusterSettings.maintenance = {enabled: true, message: 'Scheduler closed.', ends_at: ''};
        vi.spyOn(clusterSettings, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});

        render(
            <MemoryRouter>
                <IdeaAppLayout {...layoutProps} />
            </MemoryRouter>
        );

        await waitFor(() => expect(screen.queryByText('Cluster maintenance')).toBeNull());
    });
});
