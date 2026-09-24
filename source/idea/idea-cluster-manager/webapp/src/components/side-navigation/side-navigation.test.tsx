import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {vi} from 'vitest';
import IdeaSideNavigation from './index';
import {initTestAppContext} from '../../test-support';
import {IdeaSideNavItems} from '../../navigation/side-nav-items';

it('collapses Administration and follows aliases with the task selected', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
    const navigate = vi.fn();
    render(<IdeaSideNavigation sideNavHeader={{text: 'IDEA', href: '#/home/virtual-desktops'}}
        sideNavItems={IdeaSideNavItems(context)} onSideNavChange={() => {}} navigate={navigate}
        location={{pathname: '/cluster/groups', search: ''} as any} params={{}} searchParams={new URLSearchParams()} setSearchParams={() => {}}/>);
    expect(screen.queryByText('ADMIN ZONE')).not.toBeInTheDocument();
    expect(screen.getByRole('link', {name: 'People and access'})).toHaveAttribute('aria-current', 'page');
    await userEvent.click(screen.getByRole('link', {name: 'Projects'}));
    expect(navigate).toHaveBeenCalledWith('/cluster/projects');
    const boundary = screen.getByRole('button', {name: 'Administration'});
    expect(boundary).toHaveAttribute('aria-expanded', 'true');
    await userEvent.click(boundary);
    expect(boundary).toHaveAttribute('aria-expanded', 'false');
});
