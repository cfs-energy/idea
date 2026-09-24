import {act, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AccountReconcileSettings from './account-reconcile-settings';
import {initTestAppContext} from '../../test-support';

const savedValues = {enabled: true, interval_minutes: 15, dry_run: false, reenable: false, max_disable_fraction: 0.1, check_cognito: true, okta: {org_url: '', api_token_secret_arn: ''}, last_saved: 1700000000, last_completed: 1700000000};
const report = {dry_run: true, checked: 4, would_disable: 2, would_reenable: 1, eligible_enabled: 4, max_disable_fraction: 0.25, disabled: 0, reenabled: 0, missing: 2, errors: 0, refused: 1, reason: 'max_disable_fraction exceeded', changes: [{username: 'user0', action: 'disable', upstream: {directory: 'missing'}}]};

describe('account reconciliation settings', () => {
    afterEach(() => vi.restoreAllMocks());

    const setup = async (settings: any = {}, identityProvider: any = {}) => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockImplementation(name => name === 'cluster-manager' ? 'cluster-manager' : null);
        let stored = {...savedValues, ...settings};
        const read = vi.spyOn(context.client().clusterSettings(), 'getModuleSettings').mockImplementation(async () => ({settings: {accounts: {reconcile: stored}}}));
        const save = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockImplementation(async request => {
            stored = {...stored, ...(request.settings as any).accounts.reconcile};
            return {success: true};
        });
        const run = vi.spyOn(context.client().accounts(), 'reconcileUsers').mockResolvedValue(report);
        const view = render(<AccountReconcileSettings active identityProvider={identityProvider}/>);
        await screen.findByRole('checkbox', {name: 'Reconciliation on'});
        return {context, read, save, run, ...view};
    };
    const advanced = () => userEvent.click(screen.getByRole('button', {name: 'Advanced'}));

    it('shows saved state, schedule and two run buttons with advanced collapsed', async () => {
        await setup({last_run: {at: 1700000000, report: {...report, refused: 0}}});
        expect(screen.getByRole('checkbox', {name: 'Reconciliation on'})).toBeChecked();
        expect(screen.getAllByRole('checkbox')).toHaveLength(1);
        expect(screen.getByRole('button', {name: 'Run now (dry run)'})).toBeEnabled();
        expect(screen.getByRole('button', {name: 'Run now (apply)'})).toBeEnabled();
        expect(screen.getByText(/Last saved:.*Saved values loaded/)).toBeInTheDocument();
        expect(screen.getByText(`Next run at: ${new Date(1700000900 * 1000).toLocaleString()}`)).toBeInTheDocument();
        expect(screen.getByText(/Last run at \/ result:.*Completed \(dry run\)/)).toBeInTheDocument();
        expect(screen.queryByRole('textbox', {name: 'Interval (minutes)'})).not.toBeInTheDocument();
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Interval (minutes)'})).toHaveValue('15');
        expect(screen.getByRole('textbox', {name: 'Maximum disable fraction'})).toHaveValue('0.1');
        expect(screen.getByRole('checkbox', {name: 'Periodic dry run'})).not.toBeChecked();
        expect(screen.getByRole('checkbox', {name: 'Re-enable restored users'})).not.toBeChecked();
        expect(screen.getByRole('checkbox', {name: 'Check Cognito'})).toBeChecked();
        expect(screen.queryByRole('textbox', {name: 'Okta org URL'})).not.toBeInTheDocument();
    });

    it('saves the main switch immediately without submitting hidden fields', async () => {
        const {save} = await setup();
        await userEvent.click(screen.getByRole('checkbox', {name: 'Reconciliation on'}));
        expect(save).toHaveBeenCalledWith({module_id: 'cluster-manager', settings: {accounts: {reconcile: {enabled: false}}}});
        expect(await screen.findByText(/Reconciliation settings saved/)).toBeInTheDocument();
        expect(screen.getByText('Next run at: Off')).toBeInTheDocument();
    });

    it('reads again on reopening and disables editing after a failed read', async () => {
        const {read, rerender} = await setup();
        read.mockResolvedValueOnce({settings: {accounts: {reconcile: {...savedValues, enabled: false, interval_minutes: 120}}}});
        rerender(<AccountReconcileSettings active={false} identityProvider={{}}/>);
        rerender(<AccountReconcileSettings active identityProvider={{}}/>);
        expect(await screen.findByRole('checkbox', {name: 'Reconciliation on'})).not.toBeChecked();
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Interval (minutes)'})).toHaveValue('120');
        read.mockRejectedValueOnce(new Error('Settings unavailable'));
        rerender(<AccountReconcileSettings active={false} identityProvider={{}}/>);
        rerender(<AccountReconcileSettings active identityProvider={{}}/>);
        expect(await screen.findByText('Settings unavailable')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Run now (apply)'})).not.toBeInTheDocument();
        expect(screen.queryByRole('checkbox', {name: 'Reconciliation on'})).not.toBeInTheDocument();
    });

    it('saves advanced values without sending checkpoint rows', async () => {
        const {save} = await setup();
        await advanced();
        await userEvent.clear(screen.getByRole('textbox', {name: 'Interval (minutes)'}));
        await userEvent.type(screen.getByRole('textbox', {name: 'Interval (minutes)'}), '30');
        expect(screen.getByText(/Unsaved advanced changes/)).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(save).toHaveBeenCalledWith({module_id: 'cluster-manager', settings: {accounts: {reconcile: {
            enabled: true, interval_minutes: 30, dry_run: false, reenable: false, max_disable_fraction: 0.1, check_cognito: true, okta: {org_url: '', api_token_secret_arn: ''}
        }}}});
        expect(await screen.findByText(/Reconciliation settings saved/)).toBeInTheDocument();
        expect(screen.queryByText(/Unsaved advanced changes/)).not.toBeInTheDocument();
    });

    it.each([
        ['Interval (minutes)', '0', 'Interval must be'], ['Interval (minutes)', '1441', 'Interval must be'],
        ['Interval (minutes)', '1.5', 'Interval must be'], ['Maximum disable fraction', '1.1', 'Maximum disable fraction must be'],
        ['Maximum disable fraction', '-1', 'Maximum disable fraction must be'],
    ])('rejects invalid %s %s before saving', async (label, value, message) => {
        const {save} = await setup();
        await advanced();
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
        await advanced();
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect(await screen.findByText(new RegExp(message))).toBeInTheDocument();
        expect(save).not.toHaveBeenCalled();
    });

    it('shows Okta fields when requested and clears the pair when unchecked', async () => {
        const {save} = await setup();
        await advanced();
        await userEvent.click(screen.getByRole('checkbox', {name: 'Also check Okta'}));
        await userEvent.type(screen.getByRole('textbox', {name: 'Okta org URL'}), 'https://id.example.invalid');
        await userEvent.click(screen.getByRole('checkbox', {name: 'Also check Okta'}));
        await userEvent.click(screen.getByRole('button', {name: 'Save reconciliation settings'}));
        expect((save.mock.calls[0][0].settings as any).accounts.reconcile.okta).toEqual({org_url: '', api_token_secret_arn: ''});
    });

    it.each([{provider: 'okta'}, {provider: 'cognito-idp', cognito: {sso_idp_provider_name: 'Okta'}}])('shows Okta fields for the identity provider %o', async provider => {
        await setup({}, provider);
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Okta org URL'})).toBeInTheDocument();
        expect(screen.queryByRole('checkbox', {name: 'Also check Okta'})).not.toBeInTheDocument();
    });

    it('refreshes the identity provider when its cached settings are empty', async () => {
        const {context, read, rerender} = await setup();
        vi.mocked(context.getClusterSettingsService().getModuleId).mockImplementation(name => name);
        read.mockImplementation(async request => ({settings: request.module_id === 'identity-provider'
            ? {provider: 'cognito-idp', cognito: {sso_idp_provider_name: 'Okta'}}
            : {accounts: {reconcile: savedValues}}}));
        rerender(<AccountReconcileSettings active={false} identityProvider={{}}/>);
        rerender(<AccountReconcileSettings active identityProvider={{}}/>);
        await screen.findByRole('checkbox', {name: 'Reconciliation on'});
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Okta org URL'})).toBeInTheDocument();
        expect(read).toHaveBeenCalledWith({module_id: 'identity-provider'});
    });

    it.each([true, false])('retains the refused mode %s when overriding', async dryRun => {
        const {run} = await setup();
        run.mockResolvedValueOnce({...report, dry_run: dryRun}).mockResolvedValueOnce({...report, dry_run: dryRun, refused: 0});
        await userEvent.click(screen.getByRole('button', {name: `Run now (${dryRun ? 'dry run' : 'apply'})`}));
        expect(run).toHaveBeenLastCalledWith({dry_run: dryRun, override_max_disable_fraction: false});
        expect(await screen.findByText('Reconciliation refused')).toBeInTheDocument();
        expect(screen.getByText(/Proposed disables: 2 of 4/)).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Proceed anyway'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: dryRun, override_max_disable_fraction: true});
        await waitFor(() => expect(screen.queryByRole('button', {name: 'Proceed anyway'})).not.toBeInTheDocument());
    });

    it.each(['upstream error fraction exceeded', 'directory unreachable'])('does not offer an override for %s', async reason => {
        const {run} = await setup();
        run.mockResolvedValue({...report, errors: 1, reason});
        await userEvent.click(screen.getByRole('button', {name: 'Run now (apply)'}));
        expect(await screen.findByText('Reconciliation refused')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Proceed anyway'})).not.toBeInTheDocument();
    });

    it('polls the last report without overwriting advanced edits', async () => {
        const poll = vi.spyOn(window, 'setInterval');
        const {read} = await setup();
        await advanced();
        await userEvent.clear(screen.getByRole('textbox', {name: 'Interval (minutes)'}));
        await userEvent.type(screen.getByRole('textbox', {name: 'Interval (minutes)'}), '40');
        read.mockResolvedValue({settings: {accounts: {reconcile: {...savedValues, last_run: {at: 1700000100, report: {...report, refused: 0}}}}}});
        const callback = poll.mock.calls[poll.mock.calls.length - 1][0];
        expect(typeof callback).toBe('function');
        await act(async () => {if (typeof callback === 'function') callback();});
        expect(read).toHaveBeenCalledTimes(2);
        expect(screen.getByText(/Last run at \/ result:.*Completed/)).toBeInTheDocument();
        expect(screen.getByRole('textbox', {name: 'Interval (minutes)'})).toHaveValue('40');
    });

    it('shows save and run failures', async () => {
        const {save, run} = await setup();
        save.mockResolvedValue({success: false});
        await userEvent.click(screen.getByRole('checkbox', {name: 'Reconciliation on'}));
        expect(await screen.findByText('Failed to update reconciliation settings.')).toBeInTheDocument();
        expect(screen.getByRole('checkbox', {name: 'Reconciliation on'})).toBeChecked();
        run.mockRejectedValue(new Error('Run unavailable'));
        await userEvent.click(screen.getByRole('button', {name: 'Run now (dry run)'}));
        expect(await screen.findByText('Run unavailable')).toBeInTheDocument();
    });
});
