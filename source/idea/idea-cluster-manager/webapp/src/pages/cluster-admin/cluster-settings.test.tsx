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

describe('account reconciliation settings', () => {
    afterEach(() => vi.restoreAllMocks());
    const setup = async (settings = {}) => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({accounts: {reconcile: settings}});
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('cluster-manager');
        const save = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true});
        const run = vi.spyOn(context.client().accounts(), 'reconcileUsers');
        renderClusterSettings();
        await userEvent.click(await screen.findByRole('tab', {name: 'Account reconciliation'}));
        return {save, run};
    };
    const report = {dry_run: true, checked: 4, would_disable: 2, would_reenable: 1, eligible_enabled: 4, max_disable_fraction: 0.25, disabled: 0, reenabled: 0, missing: 2, errors: 0, refused: 1, reason: 'max_disable_fraction exceeded', changes: [{username: 'user0', action: 'disable', upstream: {directory: 'missing'}}]};

    it('defaults to restoration and dry runs and saves every setting through module settings', async () => {
        const {save} = await setup({last_completed: 123, okta: {org_url: null, api_token_secret_arn: null}});
        expect(screen.getByRole('checkbox', {name: 'Re-enable restored users'})).toBeChecked();
        expect(screen.getByRole('checkbox', {name: 'Dry run'})).toBeChecked();
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(save).toHaveBeenCalledWith({module_id: 'cluster-manager', settings: {accounts: {reconcile: {enabled: false, interval_minutes: 60, dry_run: true, reenable: true, max_disable_fraction: 0.25, check_cognito: false, okta: {org_url: '', api_token_secret_arn: ''}}}}});
        expect(await screen.findByText(/Reconciliation settings saved/)).toBeInTheDocument();
    });

    it.each([
        ['Interval (minutes)', '0', 'Interval must be'],
        ['Interval (minutes)', '1441', 'Interval must be'],
        ['Interval (minutes)', '1.5', 'Interval must be'],
        ['Maximum disable fraction', '1.1', 'Maximum disable fraction must be'],
        ['Maximum disable fraction', '-1', 'Maximum disable fraction must be'],
    ])('rejects invalid %s %s before saving', async (label, value, message) => {
        const {save} = await setup();
        await userEvent.clear(screen.getByRole('textbox', {name: label}));
        await userEvent.type(screen.getByRole('textbox', {name: label}), value);
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(await screen.findByText(new RegExp(message))).toBeInTheDocument();
        expect(save).not.toHaveBeenCalled();
    });

    it.each([
        ['http://id.example.invalid', 'placeholder', 'Okta org URL must be'],
        ['https://id.example.invalid/path', 'placeholder', 'Okta org URL must be'],
        ['https://id.example.invalid', 'placeholder', 'Okta token must be'],
        ['https://id.example.invalid', '', 'Both Okta settings are required'],
    ])('validates Okta fields %s %s', async (org_url, api_token_secret_arn, message) => {
        const {save} = await setup({okta: {org_url, api_token_secret_arn}});
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(await screen.findByText(new RegExp(message))).toBeInTheDocument();
        expect(save).not.toHaveBeenCalled();
    });

    it.each([true, false])('renders a refusal and preserves dry-run mode %s when overriding', async (dryRun) => {
        const {run} = await setup();
        run.mockResolvedValueOnce({...report, dry_run: dryRun}).mockResolvedValueOnce({...report, dry_run: dryRun, refused: 0});
        if (!dryRun) await userEvent.click(screen.getByRole('checkbox', {name: 'Dry run'}));
        await userEvent.click(screen.getByRole('button', {name: 'Run now'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: dryRun, override_max_disable_fraction: false});
        expect(await screen.findByText('Reconciliation refused')).toBeInTheDocument();
        expect(screen.getByText(/Proposed disables: 2 of 4/)).toBeInTheDocument();
        for (const label of ['Checked', 'Would disable', 'Would re-enable', 'Missing', 'Errors', 'user0']) expect(screen.getByText(label)).toBeInTheDocument();
        await userEvent.click(screen.getByRole('checkbox', {name: 'Dry run'}));
        await userEvent.click(screen.getByRole('button', {name: 'Proceed anyway'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: dryRun, override_max_disable_fraction: true});
        expect(screen.queryByRole('button', {name: 'Proceed anyway'})).not.toBeInTheDocument();
        if (!dryRun) await userEvent.click(screen.getByRole('checkbox', {name: 'Dry run'}));
        run.mockResolvedValue({...report, refused: 0, dry_run: false, disabled: 2});
        await userEvent.click(screen.getByRole('button', {name: 'Run now'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: false, override_max_disable_fraction: false});
        expect(await screen.findByText('Applied-run report')).toBeInTheDocument();
    });

    it('does not offer an override for upstream errors', async () => {
        const {run} = await setup();
        run.mockResolvedValue({...report, errors: 1, reason: 'upstream read failed'});
        await userEvent.click(screen.getByRole('button', {name: 'Run now'}));
        expect(await screen.findByText('Reconciliation refused')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Proceed anyway'})).not.toBeInTheDocument();
    });

    it('shows save and run failures', async () => {
        const {save, run} = await setup();
        save.mockResolvedValue({success: false});
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(await screen.findByText('Failed to update reconciliation settings.')).toBeInTheDocument();
        run.mockRejectedValue(new Error('Run unavailable'));
        await userEvent.click(screen.getByRole('button', {name: 'Run now'}));
        expect(await screen.findByText('Run unavailable')).toBeInTheDocument();
    });
});
