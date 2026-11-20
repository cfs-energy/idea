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

    describe('saving', () => {
        const GRANT = {idea_session_id: 'session-1', actor_name: 'marker-user', actor_type: 'USER', expiry_date: '2030-01-01T00:00:00Z', permission_profile: {profile_id: 'view'}};
        const saveWith = async () => {
            const clients = initTestAppContext().client();
            vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({project: {ldap_groups: ['group-a']}} as any);
            vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({listing: [{username: 'marker-user', additional_groups: ['vdc-users-module-group']}], paginator: {}} as any);
            vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({listing: [{profile_id: 'view', title: 'View only'}, {profile_id: 'full', title: 'Full control'}]} as any);
            vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions').mockResolvedValue({listing: [GRANT], paginator: {total: 1}} as any);
            const submit = vi.fn().mockResolvedValue(true);
            const modal = React.createRef<UpdateSessionPermissionModal>();
            render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={submit}/>);
            await act(async () => modal.current!.showModal());
            await screen.findByText('View only');
            return {submit, modal};
        };

        it('sends nothing for a grant that was not edited', async () => {
            const {submit} = await saveWith();
            await userEvent.setup().click(screen.getByRole('button', {name: 'Save'}));
            await waitFor(() => expect(submit).toHaveBeenCalled());
            expect(submit).toHaveBeenCalledWith([], [], []);
        });

        it('sends the recipient each row shows, and refuses a recipient named twice', async () => {
            const clients = initTestAppContext().client();
            vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({project: {ldap_groups: ['group-a']}} as any);
            vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({listing: ['marker-a', 'marker-b'].map(username => ({username, additional_groups: ['vdc-users-module-group']})), paginator: {}} as any);
            vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({listing: [{profile_id: 'view', title: 'View only'}]} as any);
            vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions').mockResolvedValue({listing: [{...GRANT, actor_name: 'marker-b'}], paginator: {total: 1}} as any);
            const submit = vi.fn().mockResolvedValue(true);
            const modal = React.createRef<UpdateSessionPermissionModal>();
            render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={submit}/>);
            await act(async () => modal.current!.showModal());
            await screen.findByText('View only');
            const newRow = () => Object.keys(modal.current!.rowRegistry).find(key => !modal.current!.state.allRows[key].existing)!;
            await act(async () => modal.current!.createNewRow());
            await waitFor(() => expect(newRow()).toBeDefined());
            const actor = () => modal.current!.rowRegistry[newRow()].registry['actor'];
            const choose = async (username: string) => act(async () => {
                actor().setState({value: username}, () => modal.current!.onStateChange({param: actor().props.param, value: username, ref: actor()} as any));
            });
            await choose('marker-a');
            await choose('marker-b');
            for (const field of Object.values(modal.current!.rowRegistry[newRow()].registry)) {
                if (field.props.param.name !== 'actor') await act(async () => field.setState({value: field.props.param.name === 'expiry-date' ? '2031-01-01' : 'view'}));
            }
            const user = userEvent.setup();
            await user.click(screen.getByRole('button', {name: 'Save'}));
            expect(await screen.findByText('marker-b is listed more than once. Remove one of the rows, then save.')).toBeInTheDocument();
            expect(submit).not.toHaveBeenCalled();
            await act(async () => actor().setState({value: 'marker-a'}));
            await user.click(screen.getByRole('button', {name: 'Save'}));
            await waitFor(() => expect(submit).toHaveBeenCalled());
            expect(submit.mock.calls[0][0].map((permission: any) => permission.actor_name)).toEqual(['marker-a']);
        });

        it('shares with a group as a group, never as the user with the same name', async () => {
            const clients = initTestAppContext().client();
            vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({project: {ldap_groups: ['physics']}} as any);
            vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({listing: [{username: 'physics', additional_groups: ['vdc-users-module-group']}], paginator: {}} as any);
            vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({listing: [{profile_id: 'view', title: 'View only'}]} as any);
            vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions').mockResolvedValue({listing: [{...GRANT, actor_name: 'group:chemistry', actor_type: 'GROUP'}], paginator: {total: 1}} as any);
            const submit = vi.fn().mockResolvedValue(true);
            const modal = React.createRef<UpdateSessionPermissionModal>();
            render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={submit}/>);
            await act(async () => modal.current!.showModal());
            await screen.findByText('View only');
            // the stored group share keeps its group: key on screen, so it cannot read as a user
            expect(await screen.findByDisplayValue('group:chemistry')).toBeInTheDocument();
            await act(async () => modal.current!.createNewRow());
            const newRow = () => Object.keys(modal.current!.rowRegistry).find(key => !modal.current!.state.allRows[key].existing)!;
            await waitFor(() => expect(newRow()).toBeDefined());
            const row = modal.current!.rowRegistry[newRow()];
            const choices = row.registry['actor'].props.param.choices!.map(choice => [choice.title, choice.value]);
            expect(choices).toEqual(expect.arrayContaining([['physics', 'physics'], ['Group: physics', 'group:physics'], ['Group: chemistry', 'group:chemistry']]));
            for (const [name, value] of [['actor', 'group:physics'], ['permission-profile', 'view'], ['expiry-date', '2031-01-01']]) {
                const field = row.registry[name];
                await act(async () => field.setState({value}, () => modal.current!.onStateChange({param: field.props.param, value, ref: field} as any)));
            }
            await userEvent.setup().click(screen.getByRole('button', {name: 'Save'}));
            await waitFor(() => expect(submit).toHaveBeenCalled());
            const [created, updated, deleted] = submit.mock.calls[0];
            expect(created.map((permission: any) => [permission.actor_name, permission.actor_type])).toEqual([['group:physics', 'GROUP']]);
            expect(updated).toEqual([]);
            expect(deleted).toEqual([]);
        });

        it('sends an edited grant as an update', async () => {
            const {submit} = await saveWith();
            const user = userEvent.setup();
            await user.click(screen.getByText('View only'));
            await user.click(await screen.findByText('Full control'));
            await user.click(screen.getByRole('button', {name: 'Save'}));
            await waitFor(() => expect(submit).toHaveBeenCalled());
            const [created, updated, deleted] = submit.mock.calls[0];
            expect(created).toEqual([]);
            expect(deleted).toEqual([]);
            expect(updated.map((permission: any) => permission.permission_profile.profile_id)).toEqual(['full']);
        });
    });

    it('offers the recipients the server accepts, from every page, and no partial list', async () => {
        const clients = initTestAppContext().client();
        vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({project: {ldap_groups: ['group-a']}} as any);
        vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({listing: []} as any);
        vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions').mockResolvedValue({listing: [], paginator: {total: 0}} as any);
        const members = vi.spyOn(clients.auth(), 'listUsersInGroup')
            .mockResolvedValueOnce({listing: [{username: 'clusteradmin-marker', additional_groups: []}, {username: 'marker-a', additional_groups: ['vdc-users-module-group']}], paginator: {cursor: 'next'}} as any)
            .mockResolvedValueOnce({listing: [{username: 'marker-a', additional_groups: ['vdc-users-module-group']}, {username: 'marker-b', additional_groups: ['vdc-administrators-module-group']}], paginator: {}} as any);
        const modal = React.createRef<UpdateSessionPermissionModal>();
        render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={() => Promise.resolve(true)}/>);
        await waitFor(() => expect(modal.current!.state.userListLoaded).toBe(true));
        expect(modal.current!.state.users.map(user => user.username)).toEqual(['marker-a', 'marker-b']);
        expect(members).toHaveBeenCalledTimes(2);
    });

    it('keeps Save unavailable when a page of recipients fails', async () => {
        const clients = initTestAppContext().client();
        vi.spyOn(clients.projects(), 'getProject').mockResolvedValue({project: {ldap_groups: ['group-a']}} as any);
        vi.spyOn(clients.virtualDesktopUtils(), 'listPermissionProfiles').mockResolvedValue({listing: []} as any);
        vi.spyOn(clients.virtualDesktop(), 'listSessionPermissions').mockResolvedValue({listing: [], paginator: {total: 0}} as any);
        vi.spyOn(clients.auth(), 'listUsersInGroup')
            .mockResolvedValueOnce({listing: [{username: 'marker-a', additional_groups: ['vdc-users-module-group']}], paginator: {cursor: 'next'}} as any)
            .mockRejectedValueOnce(new Error('Service Unavailable'));
        const modal = React.createRef<UpdateSessionPermissionModal>();
        render(<UpdateSessionPermissionModal ref={modal} session={SESSION} onCancel={() => {}} onSubmit={() => Promise.resolve(true)}/>);
        await act(async () => modal.current!.showModal());
        expect(await screen.findByText('Could not load who has access. Close this and try again.')).toBeInTheDocument();
        expect(modal.current!.state.userListLoaded).toBe(false);
    });

    it('says when that could not be read', async () => {
        setup().mockRejectedValue(new Error('Service Unavailable'));
        await open();
        expect(await screen.findByText('Could not load who has access. Close this and try again.')).toBeInTheDocument();
    });
});
