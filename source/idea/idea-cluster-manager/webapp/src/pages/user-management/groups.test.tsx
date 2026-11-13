import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import Groups, { memberCountLabel } from './groups';
import {initTestAppContext} from '../../test-support';

const pageProps: any = {
    ideaPageId: 'groups', toolsOpen: false, tools: null, onToolsChange: vi.fn(), onPageChange: vi.fn(), sideNavItems: [],
    sideNavHeader: {text: '', href: '#/'}, onSideNavChange: vi.fn(), onFlashbarChange: vi.fn(), flashbarItems: []
};
const group = {name: 'group-a', title: 'Group A', group_type: 'project', enabled: true};
let accounts: any;

beforeEach(() => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    accounts = context.client().accounts();
    vi.spyOn(accounts, 'listGroups').mockResolvedValue({listing: [group], paginator: {}});
    vi.spyOn(accounts, 'listUsersInGroup').mockResolvedValue({listing: [{username: 'person-a', enabled: true}, {username: 'person-b', enabled: true}], paginator: {}});
});
afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});
const mount = async () => {
    render(<MemoryRouter><Groups {...pageProps}/></MemoryRouter>);
    await screen.findByText('group-a');
};
const select = () => userEvent.click(within(screen.getByText('group-a').closest('tr')!).getByRole('radio'));

describe('the group member count', () => {
    it('does not present one full page as the whole group', () => {
        expect(memberCountLabel(12)).toBe('12');
        expect(memberCountLabel(1000)).toBe('1000+');
    });
});

describe('group members', () => {
    it('opens the members panel on selection, with a count, fetching once', async () => {
        await mount();
        await select();
        expect(await screen.findByText('person-a')).toBeVisible();
        expect(await screen.findByText('Users in group-a (2)')).toBeInTheDocument();
        expect(accounts.listUsersInGroup).toHaveBeenCalledTimes(1);
    });
});

describe('selection follows the list', () => {
    it('disables Actions and closes the panel once a refetch has cleared the row', async () => {
        await mount();
        await select();
        await screen.findByText('Users in group-a (2)');
        const actions = screen.getByRole('button', {name: 'Actions'});
        expect(actions).toBeEnabled();
        await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
        await waitFor(() => expect(accounts.listGroups).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(actions).toBeDisabled());
        expect(screen.queryByText(/Users in group-a/)).not.toBeInTheDocument();
    });

    it('stays disabled after creating a group', async () => {
        vi.spyOn(accounts, 'createGroup').mockResolvedValue({});
        await mount();
        await select();
        await userEvent.click(screen.getByRole('button', {name: 'Create Group'}));
        const dialog = (await screen.findAllByRole('heading', {name: 'Add New Group'}))[0].closest<HTMLElement>('[role="dialog"]')!;
        const [title, name] = within(dialog).getAllByRole('textbox');
        await userEvent.type(title, 'Group B');
        await userEvent.type(name, 'group-b');
        await userEvent.click(within(dialog).getByRole('button', {name: 'Submit'}));
        await waitFor(() => expect(accounts.listGroups).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(screen.getByRole('button', {name: 'Actions'})).toBeDisabled());
    });
});

describe('searching', () => {
    it('says which field search looks at when nothing matches', async () => {
        accounts.listGroups.mockResolvedValue({listing: [], paginator: {}});
        render(<MemoryRouter><Groups {...pageProps}/></MemoryRouter>);
        expect(await screen.findByText('No groups found. Search looks at group names, not titles.')).toBeInTheDocument();
    });
});

describe('create form', () => {
    it('shows progress and sends once however it is submitted again', async () => {
        let finish!: (value: any) => void;
        const create = vi.spyOn(accounts, 'createGroup').mockImplementation(() => new Promise(resolve => {finish = resolve;}));
        await mount();
        await userEvent.click(screen.getByRole('button', {name: 'Create Group'}));
        const dialog = (await screen.findAllByRole('heading', {name: 'Add New Group'}))[0].closest<HTMLElement>('[role="dialog"]')!;
        const [title, name] = within(dialog).getAllByRole('textbox');
        await userEvent.type(title, 'Group B');
        await userEvent.type(name, 'group-b');
        const submit = within(dialog).getByRole('button', {name: 'Submit'});
        await userEvent.click(submit);
        await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
        await waitFor(() => expect(submit).toHaveAttribute('aria-disabled', 'true'));
        fireEvent.keyDown(name, {key: 'Enter', keyCode: 13});
        await userEvent.click(submit);
        expect(create).toHaveBeenCalledTimes(1);
        finish({});
    });
});
