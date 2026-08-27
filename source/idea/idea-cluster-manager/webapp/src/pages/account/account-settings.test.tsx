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
