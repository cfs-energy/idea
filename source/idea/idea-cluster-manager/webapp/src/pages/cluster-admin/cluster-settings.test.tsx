import {AppContext} from '../../common';
import {render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import ClusterSettings from './portal-settings';
import {initTestAppContext} from '../../test-support';

const CATALOG_WARNING = 'Review model terms and pricing';
const REDEPLOY_NOTICE = 'Bedrock is enabled, but the cluster-manager module has not been redeployed';
const VDC_REDEPLOY_NOTICE = 'Bedrock is enabled, but the virtual-desktop-controller module has not been redeployed';
const LOG_ROLE_ARN = 'arn:aws:iam::111122223333:role/idea-test-bedrock-invocation-logging-us-east-2';
const PROJECT_ROLE_ARN = 'arn:aws:iam::111122223333:role/idea/idea-test/projects/*';
const LOG_GROUP_NAME = '/idea-test/cluster-manager/bedrock-invocations';
const LOGGING_NOTICE = 'IDEA is not managing Bedrock model invocation logging';

vi.mock('./email-templates', () => ({default: () => <div>Email templates</div>}));

const renderClusterSettings = () => {
    const context = AppContext.get();
    vi.spyOn(context.client().clusterSettings(), 'describeSettingsCatalog').mockResolvedValue({settings: ['enabled', 'model_ids'].map(path => ({key: `cluster-manager.bedrock.${path}`, module: 'cluster-manager', path: `bedrock.${path}`, group: 'ai-access', section: 'Amazon Bedrock', label: path === 'enabled' ? 'Amazon Bedrock' : 'Approved models', description: '', value_type: path === 'enabled' ? 'boolean' : 'list', advanced: false, effect: 'runtime', validation: {}}))});
    if (!vi.isMockFunction(context.getClusterSettingsService().getModuleId)) vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('cluster-manager');
    if (!vi.isMockFunction(context.client().clusterSettings().getModuleSettings)) vi.spyOn(context.client().clusterSettings(), 'getModuleSettings').mockImplementation(async () => ({settings: await context.getClusterSettingsService().getModuleSettings('cluster-manager')}));
    vi.spyOn(context.auth(), 'isModuleAdmin').mockImplementation(module => module === 'cluster-manager');
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(await screen.findByText('vendor.model-b')).toBeInTheDocument();
        await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
        expect(await screen.findByText(CATALOG_WARNING)).toBeInTheDocument();
        expect(await screen.findByRole('button', {name: 'Add model'})).toBeInTheDocument();
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
        await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
        await userEvent.type(await screen.findByPlaceholderText('vendor.model-name'), 'vendor.model-b');
        await userEvent.click(await screen.findByRole('button', {name: 'Add model'}));
        await userEvent.click(screen.getByRole('button', {name: 'Save'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
        expect(await screen.findByText('vendor.model-a')).toBeInTheDocument();
        expect(screen.queryByText(LOGGING_NOTICE)).not.toBeInTheDocument();
    });

    it('renders the group without the notice when the feature is off', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: false,
                model_ids: []
            }
        });
        renderClusterSettings();
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
        await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
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
        await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
        await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
        await userEvent.click(await screen.findByRole('button', {name: 'Add model'}));
        expect(await screen.findByText('Enter a model id.')).toBeInTheDocument();
        expect(updateModuleSettings).not.toHaveBeenCalled();
    });
});

describe('account reconciliation mount', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads reconciliation directly when opened even if the module cache is restricted', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({});
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockImplementation(name => name === 'cluster-manager' ? 'cluster-manager' : null);
        const read = vi.spyOn(context.client().clusterSettings(), 'getModuleSettings').mockResolvedValue({settings: {
            accounts: {reconcile: {enabled: true, interval_minutes: 15, dry_run: false, reenable: false, max_disable_fraction: 0.1, check_cognito: true}}
        }});
        renderClusterSettings();
        await userEvent.click(await screen.findByRole('link', {name: 'Users and sign-in'}));
        expect(await screen.findByRole('checkbox', {name: 'Account synchronization'})).toBeChecked();
        expect(read).toHaveBeenCalledWith({module_id: 'cluster-manager'});
        expect(screen.queryByRole('button', {name: 'Run now (apply)'})).not.toBeInTheDocument();
        expect(screen.getByRole('link', {name: 'View reconciliation history'})).toHaveAttribute('href', '#/cluster/reconciliation-runs');
    });
});

describe('settings read states', () => {
    afterEach(() => vi.restoreAllMocks());

    it('shows an empty state when no shared file systems are configured', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({});
        vi.spyOn(context.getClusterSettingsService(), 'getSharedStorageSettings').mockResolvedValue({});
        renderClusterSettings();
        await userEvent.click(await screen.findByRole('link', {name: 'Storage'}));
        expect(await screen.findByText('No file systems configured.')).toBeInTheDocument();
    });

    it('shows a visible error when a settings read fails', async () => {
        const context = initTestAppContext();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({});
        vi.spyOn(context.getClusterSettingsService(), 'getSharedStorageSettings').mockRejectedValue(new Error('denied'));
        renderClusterSettings();
        expect(await screen.findByText('Some settings could not be loaded')).toBeInTheDocument();
        expect(screen.getByText('Could not read shared storage settings.')).toBeInTheDocument();
    });
});

it('stages model revocation and cancels without writing', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({bedrock: {enabled: true, model_ids: ['vendor.model']}});
    const update = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true});
    renderClusterSettings();
    await userEvent.click(await screen.findByRole('link', {name: 'AI access'}));
    await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
    await userEvent.click(screen.getByRole('button', {name: 'Remove vendor.model'}));
    expect(screen.getByText(/Every project that lists vendor.model loses access/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Remove model'}));
    expect(update).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(screen.getByText('vendor.model')).toBeVisible();
    expect(update).not.toHaveBeenCalled();
    vi.restoreAllMocks();
});

it('keeps the maintenance end time informational and discards a cancelled notice', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({maintenance: {enabled: false, message: 'Saved notice', ends_at: ''}});
    const update = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true});
    renderClusterSettings();
    await userEvent.click(await screen.findByRole('link', {name: 'General'}));
    expect(await screen.findByText('Turn this notice off when maintenance is complete. The end time is informational.')).toBeVisible();
    expect(screen.getByText('This does not automatically reopen submissions.')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.clear(screen.getByRole('textbox', {name: 'Message'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'Message'}), 'Draft notice');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(screen.getByText('Saved notice')).toBeVisible();
    expect(screen.queryByText('Draft notice')).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
    vi.restoreAllMocks();
});

it('keeps the maintenance message full width with an accessible feature toggle', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({maintenance: {enabled: false, message: 'Saved notice', ends_at: ''}});
    renderClusterSettings();
    await userEvent.click(await screen.findByRole('link', {name: 'General'}));
    await userEvent.click(await screen.findByRole('button', {name: 'Edit'}));
    expect(screen.getByRole('checkbox', {name: 'Maintenance notice'})).toBeVisible();
    expect(screen.getByRole('textbox', {name: 'Message'}).tagName).toBe('TEXTAREA');
    expect(screen.getAllByRole('textbox').map(node => node.getAttribute('placeholder'))).toContain('2026-09-15T18:00:00Z');
    expect(screen.queryByRole('checkbox', {name: /^Enable(?:d)?$/})).toBeNull();
});
