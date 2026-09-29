import {act, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AccountReconcileSettings from './account-reconcile-settings';
import {initTestAppContext} from '../../test-support';

const savedValues = {enabled: true, interval_minutes: 15, dry_run: false, reenable: false, max_disable_fraction: 0.1, check_cognito: true, okta: {org_url: '', api_token_secret_arn: ''}, last_saved: 1700000000, last_completed: 1700000000};
const report = {dry_run: true, checked: 4, would_disable: 2, would_reenable: 1, eligible_enabled: 4, max_disable_fraction: 0.25, disabled: 0, reenabled: 0, missing: 2, errors: 0, refused: 1, reason: 'max_disable_fraction exceeded', changes: [{username: 'user0', action: 'disable', upstream: {directory: 'missing'}}]};

describe('account reconciliation settings', () => {
    afterEach(() => vi.restoreAllMocks());

    const setup = async (settings: any = {}, identityProvider: any = {}, mode: 'policy' | 'runs' | 'all' = 'all') => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockImplementation(name => name === 'cluster-manager' ? 'cluster-manager' : null);
        let stored = {...savedValues, ...settings};
        const read = vi.spyOn(context.client().clusterSettings(), 'getModuleSettings').mockImplementation(async () => ({settings: {accounts: {reconcile: stored}}}));
        const save = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockImplementation(async request => {
            stored = {...stored, ...(request.settings as any).accounts.reconcile};
            return {success: true};
        });
        const run = vi.spyOn(context.client().accounts(), 'reconcileUsers').mockResolvedValue(report);
        const view = render(<AccountReconcileSettings active identityProvider={identityProvider} mode={mode}/>);
        await screen.findByRole('checkbox', {name: 'Account synchronization'});
        return {context, read, save, run, ...view};
    };
    const advanced = () => userEvent.click(screen.getByRole('button', {name: 'Advanced'}));

    it('shows saved state, schedule and two run buttons with advanced collapsed', async () => {
        await setup({last_run: {at: 1700000000, report: {...report, refused: 0}}});
        expect(screen.getByRole('checkbox', {name: 'Account synchronization'})).toBeChecked();
        expect(screen.getAllByRole('checkbox')).toHaveLength(1);
        expect(screen.getByRole('button', {name: 'Preview changes'})).toBeEnabled();
        expect(screen.getByRole('button', {name: 'Run reconciliation'})).toBeEnabled();
        expect(screen.getByText(/Last saved:.*Saved values loaded/)).toBeInTheDocument();
        expect(screen.getByText(`Scheduled runs: ${new Date(1700000900 * 1000).toLocaleString()}`)).toBeInTheDocument();
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
        await userEvent.click(screen.getByRole('checkbox', {name: 'Account synchronization'}));
        expect(save).toHaveBeenCalledWith({module_id: 'cluster-manager', settings: {accounts: {reconcile: {enabled: false}}}});
        expect(await screen.findByText(/Reconciliation settings saved/)).toBeInTheDocument();
        expect(screen.getByText('Scheduled runs: Off')).toBeInTheDocument();
        expect(screen.getByText('Scheduled reconciliation is off. You can still preview or run reconciliation using the saved policy.')).toBeInTheDocument();
    });

    it('keeps both manual actions available while scheduling is off and confirms apply', async () => {
        const {run} = await setup({enabled: false});
        await userEvent.click(screen.getByRole('button', {name: 'Preview changes'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: true, override_max_disable_fraction: false});

        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        expect(screen.getByRole('dialog', {name: 'Apply reconciliation now?'})).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
        expect(run).toHaveBeenCalledTimes(1);

        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        await userEvent.click(screen.getByRole('button', {name: 'Apply reconciliation'}));
        expect(run).toHaveBeenLastCalledWith({dry_run: false, override_max_disable_fraction: false});
    });

    it('shows both actions in the Settings policy header without adding report tables', async () => {
        await setup({enabled: false}, {}, 'policy');
        expect(screen.getByRole('heading', {name: 'Account synchronization'})).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Preview changes'})).toBeEnabled();
        expect(screen.getByRole('button', {name: 'Run reconciliation'})).toBeEnabled();
        expect(screen.getByRole('link', {name: 'View reconciliation history'})).toHaveAttribute(
            'href', '#/cluster/reconciliation-runs'
        );
        expect(screen.queryByRole('table')).not.toBeInTheDocument();
    });

    it('uses preview counts only when they are newer than the saved policy', async () => {
        const stale = {at: 1700000000, report: {...report, dry_run: true}};
        await setup({last_saved: 1700000100, last_run: stale});
        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        expect(screen.getByText('Run a dry run first to see the changes')).toBeInTheDocument();
        expect(screen.queryByText(/latest dry run would disable/)).not.toBeInTheDocument();
    });

    it('shows fresh dry-run counts in the apply confirmation', async () => {
        const fresh = {at: 1700000200, report: {...report, dry_run: true}};
        await setup({last_saved: 1700000100, last_run: fresh});
        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        expect(screen.getByText('The latest dry run would disable 2 and re-enable 1 accounts.')).toBeInTheDocument();
        expect(screen.queryByText('Run a dry run first to see the changes')).not.toBeInTheDocument();
    });

    it('warns that unsaved policy edits are not applied by a manual run', async () => {
        const {run} = await setup();
        await advanced();
        await userEvent.clear(screen.getByRole('textbox', {name: 'Interval (minutes)'}));
        await userEvent.type(screen.getByRole('textbox', {name: 'Interval (minutes)'}), '30');
        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        expect(screen.getByText('Pending changes are not included. This run uses the saved policy.')).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Apply reconciliation'}));
        expect(run).toHaveBeenCalledWith({dry_run: false, override_max_disable_fraction: false});
    });

    it('reads again on reopening and disables editing after a failed read', async () => {
        const {read, rerender} = await setup();
        read.mockResolvedValueOnce({settings: {accounts: {reconcile: {...savedValues, enabled: false, interval_minutes: 120}}}});
        rerender(<AccountReconcileSettings active={false} identityProvider={{}}/>);
        rerender(<AccountReconcileSettings active identityProvider={{}}/>);
        expect(await screen.findByRole('checkbox', {name: 'Account synchronization'})).not.toBeChecked();
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Interval (minutes)'})).toHaveValue('120');
        read.mockRejectedValueOnce(new Error('Settings unavailable'));
        rerender(<AccountReconcileSettings active={false} identityProvider={{}}/>);
        rerender(<AccountReconcileSettings active identityProvider={{}}/>);
        expect(await screen.findByText('Settings unavailable')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Run reconciliation'})).not.toBeInTheDocument();
        expect(screen.queryByRole('checkbox', {name: 'Account synchronization'})).not.toBeInTheDocument();
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
        await screen.findByRole('checkbox', {name: 'Account synchronization'});
        await advanced();
        expect(screen.getByRole('textbox', {name: 'Okta org URL'})).toBeInTheDocument();
        expect(read).toHaveBeenCalledWith({module_id: 'identity-provider'});
    });

    it.each([true, false])('retains the refused mode %s when overriding', async dryRun => {
        const {run} = await setup();
        run.mockResolvedValueOnce({...report, dry_run: dryRun}).mockResolvedValueOnce({...report, dry_run: dryRun, refused: 0});
        if (dryRun) {
            await userEvent.click(screen.getByRole('button', {name: 'Preview changes'}));
        } else {
            await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
            await userEvent.click(screen.getByRole('button', {name: 'Apply reconciliation'}));
        }
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
        await userEvent.click(screen.getByRole('button', {name: 'Run reconciliation'}));
        await userEvent.click(screen.getByRole('button', {name: 'Apply reconciliation'}));
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

    it('disables both manual actions only while a request is active', async () => {
        const {run} = await setup({enabled: false});
        let finish: (value: any) => void = () => {};
        run.mockReturnValueOnce(new Promise(resolve => {finish = resolve;}));
        await userEvent.click(screen.getByRole('button', {name: 'Preview changes'}));
        expect(screen.getByRole('button', {name: 'Preview changes'})).toBeDisabled();
        expect(screen.getByRole('button', {name: 'Run reconciliation'})).toBeDisabled();
        await act(async () => finish({...report, refused: 0}));
        await waitFor(() => expect(screen.getByRole('button', {name: 'Preview changes'})).toBeEnabled());
        expect(screen.getByRole('button', {name: 'Run reconciliation'})).toBeEnabled();
    });

    it('shows save and run failures', async () => {
        const {save, run} = await setup();
        save.mockResolvedValue({success: false});
        await userEvent.click(screen.getByRole('checkbox', {name: 'Account synchronization'}));
        expect(await screen.findByText('Failed to update reconciliation settings.')).toBeInTheDocument();
        expect(screen.getByRole('checkbox', {name: 'Account synchronization'})).toBeChecked();
        run.mockRejectedValue(new Error('Run unavailable'));
        await userEvent.click(screen.getByRole('button', {name: 'Preview changes'}));
        expect(await screen.findByText('Run unavailable')).toBeInTheDocument();
    });
});

it('keeps advanced field order and cancels drafts without writing', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('cluster-manager');
    vi.spyOn(context.client().clusterSettings(), 'getModuleSettings').mockResolvedValue({settings: {accounts: {reconcile: savedValues}}});
    const save = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings');
    const editing = vi.fn();
    render(<AccountReconcileSettings active identityProvider={{}} mode="policy" onEditingChange={editing}/>);
    await screen.findByRole('checkbox', {name: 'Account synchronization'});
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    expect(screen.getByRole('checkbox', {name: 'Periodic dry run'}).compareDocumentPosition(screen.getByRole('textbox', {name: 'Interval (minutes)'})) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Interval (minutes)'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'Interval (minutes)'}), '30');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel reconciliation changes'}));
    expect(editing).toHaveBeenLastCalledWith(false);
    expect(save).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    expect(screen.getByRole('textbox', {name: 'Interval (minutes)'})).toHaveValue('15');
});
