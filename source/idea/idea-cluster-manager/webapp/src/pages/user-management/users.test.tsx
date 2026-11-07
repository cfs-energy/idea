import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import Users from './users';
import {initTestAppContext} from '../../test-support';

const pageProps: any = {
    ideaPageId: 'users', toolsOpen: false, tools: null, onToolsChange: vi.fn(), onPageChange: vi.fn(), sideNavItems: [],
    sideNavHeader: {text: '', href: '#/'}, onSideNavChange: vi.fn(), flashbarItems: []
};
const person = {username: 'person-a', enabled: true, sudo: false, additional_groups: ['group-a']};

let accounts: ReturnType<ReturnType<typeof initTestAppContext>['client']>['accounts'] extends () => infer R ? R : never;
let flash: ReturnType<typeof vi.fn>;

beforeEach(() => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    accounts = context.client().accounts();
    vi.spyOn(accounts, 'listUsers').mockResolvedValue({listing: [person], paginator: {}} as any);
    flash = vi.fn();
});
afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

const mount = async () => {
    render(<MemoryRouter><Users {...pageProps} onFlashbarChange={flash}/></MemoryRouter>);
    await screen.findByText('person-a');
};
const select = async () => {
    await userEvent.click(within(screen.getByText('person-a').closest('tr')!).getByRole('radio'));
};
const actions = () => screen.getByRole('button', {name: 'Actions'});
// every modal stays in the document, so reach the open one by its heading
const dialog = async (title: string) => within((await screen.findAllByRole('heading', {name: title}))[0].closest<HTMLElement>('[role="dialog"]')!);
const pick = async (name: string) => {
    await userEvent.click(actions());
    await userEvent.click(await screen.findByRole('menuitem', {name}));
};

describe('selection follows the list', () => {
    it('disables Actions once a refetch has cleared the row', async () => {
        vi.spyOn(accounts, 'disableUser').mockResolvedValue({} as any);
        await mount();
        expect(actions()).toBeDisabled();
        await select();
        expect(actions()).toBeEnabled();
        await pick('Disable User');
        await userEvent.click((await dialog('Disable User')).getByRole('button', {name: 'Yes'}));
        await waitFor(() => expect(accounts.listUsers).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(actions()).toBeDisabled());
    });
});

describe('reset password', () => {
    it('says what it does before and after', async () => {
        vi.spyOn(accounts, 'resetPassword').mockResolvedValue({} as any);
        await mount();
        await select();
        await pick('Reset Password');
        expect(await screen.findByText(/a password reset and revokes their API tokens/)).toBeInTheDocument();
        await userEvent.click((await dialog('Reset Password')).getByRole('button', {name: 'Yes'}));
        await waitFor(() => expect(flash).toHaveBeenCalled());
        expect(flash.mock.calls[0][0].items[0].content).toBe('Reset email sent to person-a. Their API tokens were revoked.');
    });
});

describe('searching', () => {
    it('says which field search looks at when nothing matches', async () => {
        vi.mocked(accounts.listUsers).mockResolvedValue({listing: [], paginator: {}} as any);
        render(<MemoryRouter><Users {...pageProps} onFlashbarChange={flash}/></MemoryRouter>);
        expect(await screen.findByText('No users found. Search looks at usernames only.')).toBeInTheDocument();
    });
});

describe('add to group lookup', () => {
    it('looks up in lowercase and says when choices could not load', async () => {
        const list = vi.spyOn(accounts, 'listGroups').mockRejectedValue({message: 'nope'});
        await mount();
        await select();
        await pick('Add user to Group');
        const input = await screen.findByRole('combobox');
        fireEvent.focus(input);
        fireEvent.change(input, {target: {value: 'Group-A'}});
        await waitFor(() => expect(list.mock.calls.at(-1)![0]!.filters).toEqual([{key: 'group_name', like: 'group-a'}]));
        expect(await screen.findByText('Choices could not be loaded. Try again.')).toBeInTheDocument();
    });
});
