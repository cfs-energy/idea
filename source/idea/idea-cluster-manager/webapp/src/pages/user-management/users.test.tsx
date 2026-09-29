import {render, screen, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {AppContext} from '../../common';
import {initTestAppContext} from '../../test-support';
import Users from './users';

const renderPage = () => {
    render(
        <MemoryRouter>
            <Users
                ideaPageId="users"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{text: 'IDEA', href: '#/'}}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={() => {}}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
};

describe('users page', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('saves virtual desktop instance type exceptions for the selected user', async () => {
        const context: AppContext = initTestAppContext();
        vi.spyOn(context.client().accounts(), 'listUsers').mockResolvedValue({
            listing: [{username: 'desktop-user', enabled: true, instance_type_exceptions: ['m6a.large']}],
            paginator: {total: 1}
        });
        const modifyUser = vi.spyOn(context.client().accounts(), 'modifyUser').mockResolvedValue({
            user: {username: 'desktop-user', instance_type_exceptions: ['g5.xlarge']}
        });
        vi.spyOn(context.getClusterSettingsService(), 'getInstanceTypes').mockResolvedValue([
            {InstanceType: 'g5.xlarge'},
            {InstanceType: 'm6a.large'}
        ]);
        renderPage();

        expect(await screen.findByText('desktop-user')).toBeInTheDocument();
        await userEvent.click(screen.getByRole('radio'));
        await userEvent.click(screen.getByRole('button', {name: 'Actions'}));
        await userEvent.click(await screen.findByText('Set virtual desktop instance types'));

        const dialog = await screen.findByRole('dialog', {name: 'Virtual desktop instance type exceptions'});
        const multiselect = within(dialog).getByRole('button', {name: /Choose instance types|m6a.large/});
        await userEvent.click(multiselect);
        await userEvent.click(await screen.findByText('g5.xlarge'));
        await userEvent.click(within(dialog).getByRole('button', {name: 'Save'}));

        expect(modifyUser).toHaveBeenCalledWith({
            user: {
                username: 'desktop-user',
                instance_type_exceptions: ['m6a.large', 'g5.xlarge']
            }
        });
    });
});
