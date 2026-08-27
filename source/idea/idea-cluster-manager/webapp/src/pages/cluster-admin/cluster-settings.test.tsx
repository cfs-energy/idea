import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import ClusterSettings from './cluster-settings';
import {initTestAppContext} from '../../test-support';

const CATALOG_WARNING = 'Approving a model commits this AWS account';
const REDEPLOY_NOTICE = 'Bedrock is enabled, but the cluster-manager module has not been redeployed';
const VDC_REDEPLOY_NOTICE = 'Bedrock is enabled, but the virtual-desktop-controller module has not been redeployed';
const LOG_ROLE_ARN = 'arn:aws:iam::111122223333:role/idea-test-bedrock-invocation-logging-us-east-2';
const PROJECT_ROLE_ARN = 'arn:aws:iam::111122223333:role/idea/idea-test/projects/*';
const LOG_GROUP_NAME = '/idea-test/cluster-manager/bedrock-invocations';
const LOGGING_NOTICE = 'IDEA is not managing Bedrock model invocation logging';

const renderClusterSettings = () => {
    render(
        <MemoryRouter>
            <ClusterSettings
                ideaPageId="cluster-settings"
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

describe('cluster settings bedrock catalog', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('lists the approved models and warns before a model is added', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a', 'vendor.model-b']
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(await screen.findByText('vendor.model-b')).toBeInTheDocument();
        expect(await screen.findByText(CATALOG_WARNING)).toBeInTheDocument();
        expect(await screen.findByRole('button', {name: 'Add Model'})).toBeInTheDocument();
    });

    it('sends the whole catalog when a model is added', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a']
            }
        });
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('cluster-manager');
        const updateModuleSettings = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings')
            .mockResolvedValue({success: true});
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        await userEvent.type(await screen.findByPlaceholderText('vendor.model-name'), 'vendor.model-b');
        await userEvent.click(await screen.findByRole('button', {name: 'Add Model'}));
        expect(updateModuleSettings).toHaveBeenCalledWith({
            module_id: 'cluster-manager',
            settings: {
                bedrock: {
                    model_ids: ['vendor.model-a', 'vendor.model-b']
                }
            }
        });
    });

    it('tells the admin to redeploy when the module carries no provisioner permissions', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a']
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText(REDEPLOY_NOTICE)).toBeInTheDocument();
    });

    it('does not ask for a redeploy once the module is deployed with bedrock', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_log_role_arn: LOG_ROLE_ARN
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(screen.queryByText(REDEPLOY_NOTICE)).not.toBeInTheDocument();
    });

    it('tells the admin to redeploy the desktop controller when it cannot pass project roles', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_log_role_arn: LOG_ROLE_ARN
            }
        });
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({
            dcv_session: {}
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText(VDC_REDEPLOY_NOTICE)).toBeInTheDocument();
        expect(screen.queryByText(REDEPLOY_NOTICE)).not.toBeInTheDocument();
    });

    it('does not ask for a desktop controller redeploy once it can pass project roles', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_log_role_arn: LOG_ROLE_ARN
            }
        });
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({
            bedrock: {
                project_pass_role_arn: PROJECT_ROLE_ARN
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(screen.queryByText(VDC_REDEPLOY_NOTICE)).not.toBeInTheDocument();
    });

    it('says usage is not collected when idea does not manage invocation logging', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_log_role_arn: LOG_ROLE_ARN,
                invocation_log_group_name: LOG_GROUP_NAME,
                invocation_logging: {
                    manage_configuration: false
                }
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText(LOGGING_NOTICE)).toBeInTheDocument();
        expect(await screen.findByText(LOG_GROUP_NAME)).toBeInTheDocument();
    });

    it('does not mention invocation logging once idea manages it', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_log_role_arn: LOG_ROLE_ARN,
                invocation_log_group_name: LOG_GROUP_NAME,
                invocation_logging: {
                    manage_configuration: true
                }
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(screen.queryByText(LOGGING_NOTICE)).not.toBeInTheDocument();
    });

    it('renders the tab without the notice when the feature is off', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: false,
                model_ids: []
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        expect(await screen.findByText(CATALOG_WARNING)).toBeInTheDocument();
        expect(screen.queryByText(REDEPLOY_NOTICE)).not.toBeInTheDocument();
    });

    it('rejects an empty model id without calling the settings api', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: []
            }
        });
        const updateModuleSettings = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings')
            .mockResolvedValue({success: true});
        renderClusterSettings();
        await userEvent.click(await screen.findByText('Bedrock'));
        await userEvent.click(await screen.findByRole('button', {name: 'Add Model'}));
        expect(await screen.findByText('Enter a model id.')).toBeInTheDocument();
        expect(updateModuleSettings).not.toHaveBeenCalled();
    });
});
