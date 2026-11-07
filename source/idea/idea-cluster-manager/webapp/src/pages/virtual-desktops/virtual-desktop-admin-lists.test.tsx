import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import VirtualDesktopPermissionProfiles from './virtual-desktop-permission-profiles';
import VirtualDesktopDashboard from './virtual-desktop-dashboard';
import UpdateSessionPermissionModal from './forms/virtual-desktop-update-session-permissions-form';
import React from 'react';
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

describe('permission profiles', () => {
    afterEach(() => vi.restoreAllMocks());

    it('offers no search box, because the listing cannot search', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({});
        vi.spyOn(context.client().virtualDesktopUtils(), 'getBasePermissions').mockResolvedValue({ permissions: [] } as any);
        vi.spyOn(context.client().virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({ listing: [{ profile_id: 'view', title: 'View only' }], paginator: {} } as any);
        render(<MemoryRouter><VirtualDesktopPermissionProfiles ideaPageId="virtual-desktop-permission-profiles" {...PAGE_PROPS}/></MemoryRouter>);
        await screen.findByText('View only', {}, { timeout: 10000 });
        expect(createWrapper().findTextFilter()).toBeNull();
        expect(createWrapper().findInput()).toBeNull();
    });
});

describe('dashboard', () => {
    afterEach(() => vi.restoreAllMocks());

    it('counts past the first ten values, and refresh reloads every chart', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({ opensearch: { dcv_session: { alias: 'sessions' } } });
        vi.spyOn(context.getClusterSettingsService(), 'getModuleInfo').mockReturnValue({} as any);
        const query = vi.spyOn(context.client().analytics(), 'queryOpenSearch').mockResolvedValue({ data: { hits: { total: { value: 40 } }, aggregations: {} } } as any);
        render(<MemoryRouter><VirtualDesktopDashboard ideaPageId="virtual-desktop-dashboard" {...PAGE_PROPS}/></MemoryRouter>);
        await waitFor(() => expect(query).toHaveBeenCalledTimes(6), { timeout: 10000 });
        for (const [request] of query.mock.calls) {
            const aggregations = Object.values((request as any).data.body.aggs) as any[];
            expect(aggregations[0].terms.size).toBe(100);
        }
        query.mockClear();
        await userEvent.setup().click(screen.getByRole('button', { name: 'Refresh charts' }));
        await waitFor(() => expect(query).toHaveBeenCalledTimes(6));
    });
});

describe('sharing a desktop', () => {
    afterEach(() => vi.restoreAllMocks());

    const SESSION = { idea_session_id: 'session-1', name: 'beamline', owner: 'someowner', project: { project_id: 'p1' } };

    const setup = () => {
        const clients = initTestAppContext().client();
        vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({ project: { ldap_groups: ['group-a'] } } as any);
        vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({ listing: [], paginator: {} } as any);
        vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({ listing: [] } as any);
        return vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions');
    };

    const open = async () => {
        const modal = React.createRef<UpdateSessionPermissionModal>();
        render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={() => Promise.resolve(true)}/>);
        await act(async () => modal.current!.showModal());
    };

    it('reads everyone who has access, not the first ten', async () => {
        const list = setup().mockResolvedValue({ listing: [] } as any);
        await open();
        await waitFor(() => expect(list).toHaveBeenCalled());
        expect(list.mock.calls[0][0].paginator?.page_size).toBe(100);
    });

    it('says when that could not be read', async () => {
        setup().mockRejectedValue(new Error('Service Unavailable'));
        await open();
        expect(await screen.findByText('Could not load who has access. Close this and try again.')).toBeInTheDocument();
    });
});
