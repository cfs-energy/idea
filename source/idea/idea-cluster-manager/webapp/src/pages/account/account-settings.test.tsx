import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import AccountSettings from './account-settings';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const PROJECT = {
    project_id: 'project-1',
    name: 'project-a',
    title: 'Project A',
    enabled: true,
    bedrock: {
        enabled: true,
        model_ids: ['vendor.model-a']
    }
};

const stubAccountPage = (context: AppContext, clusterManagerSettings: any) => {
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue(clusterManagerSettings);
    vi.spyOn(context.client().projects(), 'getUserProjects').mockResolvedValue({projects: [PROJECT]});
    vi.spyOn(context.client().auth(), 'listUsersInGroup').mockResolvedValue({listing: []});
    vi.spyOn(context.auth(), 'getUser').mockResolvedValue({username: 'user-a', group_name: 'user-a-group'});
    vi.spyOn(context.auth(), 'getUsername').mockReturnValue('user-a');
    vi.spyOn(context.auth(), 'isPasswordExpirationApplicable').mockReturnValue(false);
    vi.spyOn(context.client().auth(), 'listApiTokens').mockResolvedValue({listing: []});
};

const renderAccountSettings = () => {
    render(
        <MemoryRouter>
            <AccountSettings
                ideaPageId="account-settings"
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

describe('account settings my projects models', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('lists the project models when the settings projection exposes the feature flag', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {bedrock: {enabled: true}});
        renderAccountSettings();
        await userEvent.click(await screen.findByText('My Projects'));
        expect(await screen.findByText('AI Models')).toBeInTheDocument();
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
    });

    it('hides the models column when the projection does not expose the feature flag', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        renderAccountSettings();
        await userEvent.click(await screen.findByText('My Projects'));
        expect(await screen.findByText('Project A')).toBeInTheDocument();
        expect(screen.queryByText('AI Models')).toBeNull();
        expect(screen.queryByText('vendor.model-a')).toBeNull();
    });

    it('shows a stored home preference as My costs and omits Home from landing options', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        vi.mocked(context.auth().getUser).mockResolvedValue({username: 'user-a', landing_page: 'home'});
        renderAccountSettings();
        await userEvent.click(await screen.findByRole('button', {name: /Landing page/}));
        expect(screen.getByRole('option', {name: 'My costs', selected: true})).toBeInTheDocument();
        expect(screen.getByRole('option', {name: 'Cluster default'})).toBeInTheDocument();
        expect(screen.queryByRole('option', {name: 'Home'})).not.toBeInTheDocument();
    });

    it('never shows Reports as chosen when it is not offered', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        vi.spyOn(context.getClusterSettingsService(), 'isCustomDashboardEnabled').mockReturnValue(false);
        vi.mocked(context.auth().getUser).mockResolvedValue({username: 'user-a', landing_page: 'reports'});
        renderAccountSettings();
        await userEvent.click(await screen.findByRole('button', {name: /Landing page/}));
        expect(screen.queryByRole('option', {name: 'Reports'})).not.toBeInTheDocument();
        expect(screen.getByRole('option', {name: 'Cluster default', selected: true})).toBeInTheDocument();
    });
});


describe('account settings model invocation guidance', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    const PROVISIONED_PROJECT = {
        project_id: 'project-1',
        name: 'project-a',
        title: 'Project A',
        enabled: true,
        bedrock: {
            enabled: true,
            model_ids: ['vendor.model-a'],
            inference_profile_arns: {
                'vendor.model-a': 'arn:aws:bedrock:us-east-2:111122223333:application-inference-profile/abcd1234'
            }
        }
    };

    const stubWithProject = (project: any) => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {enabled: true}
        });
        vi.spyOn(context.client().projects(), 'getUserProjects').mockResolvedValue({projects: [project]});
        vi.spyOn(context.client().auth(), 'listUsersInGroup').mockResolvedValue({listing: []});
        vi.spyOn(context.auth(), 'getUser').mockResolvedValue({username: 'user-a', group_name: 'user-a-group'});
        vi.spyOn(context.auth(), 'getUsername').mockReturnValue('user-a');
        vi.spyOn(context.auth(), 'isPasswordExpirationApplicable').mockReturnValue(false);
    vi.spyOn(context.client().auth(), 'listApiTokens').mockResolvedValue({listing: []});
    };

    it('says the profile is what to pass, because the model name alone is denied', async () => {
        // a user who copies the model id gets AccessDenied naming a resource they never
        // referenced. the page shows both strings, so it has to say which one works.
        stubWithProject(PROVISIONED_PROJECT);
        renderAccountSettings();
        await userEvent.click(await screen.findByText('My Projects'));

        expect(await screen.findByText(/on its own is refused/i)).toBeInTheDocument();
    });

    it('shows the profile arn alongside the model id', async () => {
        stubWithProject(PROVISIONED_PROJECT);
        renderAccountSettings();
        await userEvent.click(await screen.findByText('My Projects'));

        expect(await screen.findByText(/application-inference-profile\/abcd1234/)).toBeInTheDocument();
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
    });
});

describe('account settings profile', () => {
    afterEach(() => {
        vi.restoreAllMocks();
        delete (window as any).idea.app.sso;
    });

    it('says when the profile did not load, and retries', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        const getUser = vi.spyOn(context.auth(), 'getUser').mockRejectedValueOnce(new Error('down'));
        renderAccountSettings();

        expect(await screen.findByText('Could not load your profile.')).toBeInTheDocument();

        getUser.mockResolvedValue({username: 'user-a', group_name: 'user-a-group'} as any);
        await userEvent.click(screen.getByRole('button', {name: 'Retry'}));
        expect(await screen.findByText('user-a-group')).toBeInTheDocument();
    });

    it('says when group members did not load, instead of showing none', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        vi.spyOn(context.client().auth(), 'listUsersInGroup').mockRejectedValue(new Error('down'));
        renderAccountSettings();

        expect(await screen.findByText('Could not load group members.')).toBeInTheDocument();
    });

    it('uses plain labels and leaves you out of your own group list', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        vi.spyOn(context.client().auth(), 'listUsersInGroup').mockResolvedValue({listing: [{username: 'user-a'}, {username: 'user-b'}]});
        renderAccountSettings();

        expect(await screen.findByText('Other people in my group')).toBeInTheDocument();
        expect(await screen.findByText('user-b')).toBeInTheDocument();
        expect(screen.getByText('Linux account')).toBeInTheDocument();
        expect(screen.queryByText('LDAP Info')).toBeNull();
        expect(screen.queryByText('Is Administrator?')).toBeNull();
        const group = screen.getByText('Other people in my group').parentElement!;
        expect(group.textContent).not.toContain('user-a');
    });

    it('offers no password change with single sign-on', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        (window as any).idea.app.sso = true;
        renderAccountSettings();

        await screen.findByText('Linux account');
        expect(screen.queryByRole('button', {name: 'Change Password'})).toBeNull();
    });

    it('offers a password change without single sign-on', async () => {
        const context = initTestAppContext();
        stubAccountPage(context, {});
        (window as any).idea.app.sso = false;
        renderAccountSettings();

        expect(await screen.findByRole('button', {name: 'Change Password'})).toBeInTheDocument();
    });
});
