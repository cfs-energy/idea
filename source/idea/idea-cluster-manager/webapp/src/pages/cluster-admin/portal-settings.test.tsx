import {render, screen, within, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import {SettingDefinition} from '../../client/data-model';
import PortalSettings, {legacySettingsGroup, SETTINGS_GROUPS, SettingsGroups} from './portal-settings';

vi.mock('./email-templates', () => ({default: () => <button>Edit templates</button>}));

const definition = (key: string, group: string, value_type: SettingDefinition['value_type'], effect: SettingDefinition['effect'], advanced = false): SettingDefinition => ({
    key, module: key.split('.')[0], path: key.split('.').slice(1).join('.'), group, value_type, effect, advanced,
    label: key.split('.').at(-1)!.replaceAll('_', ' '), description: 'Configure this setting.', section: advanced ? 'Tuning' : 'General', validation: {required: false}, choices: [],
});
const catalog: SettingDefinition[] = [
    definition('cluster-manager.web_portal.title', 'appearance', 'string', 'runtime'),
    definition('cluster-manager.server.max_workers', 'network', 'integer', 'restart', true),
    definition('cluster.ses.sender_email', 'email', 'string', 'runtime'),
    definition('cluster.ses.enabled', 'email', 'boolean', 'runtime'),
    definition('cluster.backups.enabled', 'backup', 'boolean', 'deployment'),
    definition('cluster.network.client_ip', 'network', 'list', 'deployment'),
    definition('cluster-manager.accounts.reconcile.okta.api_token_secret_arn', 'account-synchronization', 'secret', 'runtime'),
    {...definition('cluster-manager.bedrock.budgets.action', 'ai-access', 'enum', 'runtime'), choices: ['block', 'warn']},
];
function Location() {const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>;}
function setup() {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    const service = context.getClusterSettingsService();
    vi.spyOn(service, 'isModuleEnabled').mockReturnValue(true);
    vi.spyOn(service, 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(service, 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(service, 'getModuleId').mockImplementation(module => module === 'virtual-desktop-controller' ? 'vdc' : module);
    const client = context.client().clusterSettings();
    vi.spyOn(client, 'describeSettingsCatalog').mockResolvedValue({settings: catalog});
    vi.spyOn(client, 'getModuleSettings').mockResolvedValue({settings: {web_portal: {title: 'Portal'}, server: {max_workers: 16}, network: {client_ip: []}}});
    vi.spyOn(client, 'updateModuleSettings').mockResolvedValue({success: true, effects: {'web_portal.title': 'runtime'}});
    return context;
}
async function renderGroups(path = '/cluster/settings', context = setup()) {
    render(<MemoryRouter initialEntries={[path]}><SettingsGroups pageProps={{}}/><Location/></MemoryRouter>);
    await waitFor(() => expect(screen.queryByText('Loading settings…')).not.toBeInTheDocument());
    return context;
}

it('lists every group and opens only the selected nested subpage', async () => {
    await renderGroups('/cluster/settings?project=sample');
    const nav = screen.getByRole('navigation', {name: 'Settings groups'});
    for (const [, title] of SETTINGS_GROUPS) expect(within(nav).getByRole('link', {name: title})).toBeInTheDocument();
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    await userEvent.click(within(nav).getByRole('link', {name: 'Email and notifications'}));
    expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/email?project=sample');
    expect(screen.getByRole('region', {name: 'Email and notifications'})).toBeInTheDocument();
    expect(screen.queryByRole('region', {name: 'Storage'})).not.toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
});

it('redirects a legacy tab to its subpage while preserving unrelated query state', async () => {
    await renderGroups('/cluster/settings?tab=ses&project=sample');
    expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/email?project=sample');
});

it('search opens the matching subpage, expands Advanced and scrolls to the key', async () => {
    const scroll = vi.fn();
    const original = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scroll;
    try {
        await renderGroups();
        const search = screen.getByRole('searchbox', {name: 'Search settings'});
        await userEvent.click(search);
        await userEvent.paste('cluster-manager.server.max_workers');
        const matches = screen.getByRole('table', {name: 'Matching settings'});
        expect(within(matches).getByRole('columnheader', {name: 'Setting'})).toBeVisible();
        expect(within(matches).getByRole('columnheader', {name: 'Group'})).toBeVisible();
        expect(within(matches).queryByRole('columnheader', {name: 'Key'})).toBeNull();
        expect(within(matches).getByText('cluster-manager.server.max_workers')).toBeVisible();
        expect(within(matches).queryByRole('radio')).toBeNull();
        await userEvent.click(within(matches).getByRole('link', {name: 'max workers'}));
        expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/network?key=cluster-manager.server.max_workers');
        expect(screen.getByRole('button', {name: 'Advanced'})).toHaveAttribute('aria-expanded', 'true');
        expect(document.getElementById('setting-cluster-manager.server.max_workers')).toBeVisible();
        expect(scroll).toHaveBeenCalled();
    } finally {HTMLElement.prototype.scrollIntoView = original;}
});

it('saves a text editor and reports the effect returned by the server', async () => {
    const context = await renderGroups('/cluster/settings/appearance');
    expect(screen.queryByText('Applies now')).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole('button', {name: 'Edit'}).at(-1)!);
    const field = screen.getByRole('textbox', {name: 'title'});
    await userEvent.clear(field); await userEvent.type(field, 'New portal');
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(context.client().clusterSettings().updateModuleSettings).toHaveBeenCalledWith({module_id: 'cluster-manager', settings: {web_portal: {title: 'New portal'}}});
    expect(await screen.findByText('Saved.')).toBeVisible();
    expect(screen.getByRole('button', {name: 'Edit'})).toBeEnabled();
});

it('cancels a section edit and returns to key-value view mode', async () => {
    await renderGroups('/cluster/settings/appearance');
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.clear(screen.getByRole('textbox', {name: 'title'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'title'}), 'Discarded');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(screen.queryByRole('textbox', {name: 'title'})).not.toBeInTheDocument();
    expect(screen.getByText('Portal')).toBeVisible();
});

it('keeps deployment inputs editable and uses the upgrade effect on save', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().updateModuleSettings).mockResolvedValue({success: true, effects: {'backups.enabled': 'deployment'}});
    await renderGroups('/cluster/settings/backup', context);
    expect(screen.getByText('Applies on next upgrade')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.click(screen.getByRole('checkbox', {name: 'enabled'}));
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(await screen.findByText('Saved. Applies on next upgrade.')).toBeVisible();
    expect(context.client().clusterSettings().updateModuleSettings).toHaveBeenCalledWith({module_id: 'cluster', settings: {backups: {enabled: true}}});
});

it('keeps Advanced collapsed until requested and provides a numeric editor', async () => {
    await renderGroups('/cluster/settings/network');
    expect(screen.getByRole('button', {name: 'Advanced'})).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    expect(screen.getByText('Restart required')).toBeVisible();
    await userEvent.click(screen.getAllByRole('button', {name: 'Edit'}).at(-1)!);
    expect(screen.getByRole('spinbutton', {name: 'max workers'})).toHaveValue(16);
});

it('allows only one settings section to be edited at a time', async () => {
    await renderGroups('/cluster/settings/network');
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    const edits = screen.getAllByRole('button', {name: 'Edit'});
    await userEvent.click(edits[0]);
    expect(screen.getByRole('button', {name: 'Cancel'})).toBeEnabled();
    expect(screen.getByRole('button', {name: 'Edit'})).toBeDisabled();
});

it('edits lists as strings and retains pending edits on a failed save', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().updateModuleSettings).mockRejectedValue(new Error('Save failed'));
    await renderGroups('/cluster/settings/network', context);
    await userEvent.click(screen.getAllByRole('button', {name: 'Edit'})[0]);
    await userEvent.click(screen.getByRole('button', {name: 'Add client ip'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'client ip 1'}), '192.0.2.0/24');
    await userEvent.click(screen.getAllByRole('button', {name: 'Save'})[0]);
    expect(await screen.findByText('Save failed')).toBeVisible();
    expect(screen.getByRole('textbox', {name: 'client ip 1'})).toHaveValue('192.0.2.0/24');
    expect(context.client().clusterSettings().updateModuleSettings).toHaveBeenCalledWith({module_id: 'cluster', settings: {network: {client_ip: ['192.0.2.0/24']}}});
});

it('offers the catalog enum choices', async () => {
    await renderGroups('/cluster/settings/ai-access');
    await userEvent.click(screen.getAllByRole('button', {name: 'Edit'}).at(-1)!);
    await userEvent.click(screen.getByRole('button', {name: 'action'}));
    expect(screen.getByRole('option', {name: 'warn'})).toBeInTheDocument();
});

it('links a secret reference to its regional console', async () => {
    const context = setup();
    const secret = ['arn', 'aws', 'secretsmanager', 'us-east-1', '1'.repeat(12), 'secret', 'reference'].join(':');
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockResolvedValue({settings: {accounts: {reconcile: {okta: {api_token_secret_arn: secret}}}}});
    await renderGroups('/cluster/settings/account-synchronization', context);
    expect(screen.getByRole('link', {name: /Open secret/})).toHaveAttribute('href', expect.stringContaining(encodeURIComponent(secret)));
});

it('does not render blank editable forms after a module load failure', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockRejectedValue(new Error('Load failed'));
    await renderGroups('/cluster/settings/appearance', context);
    expect(screen.getByText('Some settings could not be loaded')).toBeVisible();
    expect(screen.queryByRole('textbox', {name: 'title'})).not.toBeInTheDocument();
});

it('keeps module-only administrators out of cluster policy editors', async () => {
    const context = setup();
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'virtual-desktop-controller');
    await renderGroups('/virtual-desktop/settings', context);
    const nav = screen.getByRole('navigation', {name: 'Settings groups'});
    expect(within(nav).getByRole('link', {name: 'Desktop access and lifecycle'})).toBeInTheDocument();
    expect(within(nav).queryByRole('link', {name: 'Account synchronization'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Edit templates'})).not.toBeInTheDocument();
});

it('preserves the desktop schedule and defaults editors inside their subpage', async () => {
    const context = setup();
    const service = context.getClusterSettingsService();
    vi.spyOn(service, 'getModuleSettings').mockResolvedValue({});
    vi.spyOn(service, 'getClusterSettings').mockResolvedValue({});
    vi.spyOn(service, 'getVirtualDesktopSettings').mockResolvedValue({});
    vi.spyOn(service, 'getSchedulerSettings').mockResolvedValue({});
    render(<MemoryRouter initialEntries={['/cluster/settings/desktop-lifecycle']}><PortalSettings
        ideaPageId="settings" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    expect(await screen.findByRole('button', {name: 'Edit Default Schedules'})).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Edit edit subnet autoretry'})).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
});

it.each([
    ['/cluster/settings', 'general', 'appearance'], ['/cluster/settings', 'network', 'network'],
    ['/cluster/settings', 'shared-storage', 'storage'], ['/cluster/settings', 'identity-provider', 'sign-in'], ['/cluster/settings', 'directory-service', 'sign-in'],
    ['/cluster/settings', 'analytics', 'monitoring'], ['/cluster/settings', 'metrics', 'monitoring'], ['/cluster/settings', 'cloudwatch-logs', 'monitoring'],
    ['/cluster/settings', 'maintenance', 'maintenance'], ['/cluster/settings', 'account-reconciliation', 'account-synchronization'], ['/cluster/settings', 'bedrock', 'ai-access'],
    ['/cluster/settings', 'ses', 'email'], ['/cluster/settings', 'ec2', 'network'], ['/cluster/settings', 'backups', 'backup'], ['/cluster/settings', 'route-53', 'network'], ['/cluster/settings', 'aws-account', 'deployment'],
    ['/virtual-desktop/settings', 'general', 'desktop-lifecycle'], ['/virtual-desktop/settings', 'notifications', 'email'], ['/virtual-desktop/settings', 'schedule', 'desktop-lifecycle'], ['/virtual-desktop/settings', 'server', 'desktop-lifecycle'],
    ['/virtual-desktop/settings', 'broker', 'network'], ['/virtual-desktop/settings', 'connection-gateway', 'network'], ['/virtual-desktop/settings', 'backups', 'backup'], ['/virtual-desktop/settings', 'cloudwatch-logs', 'monitoring'],
    ['/soca/settings', 'general', 'job-placement'], ['/soca/settings', 'cloudwatch-logs', 'monitoring'], ['/cluster/email-templates', '', 'email'],
])('maps %s?tab=%s to its section', (path, tab, group) => {
    expect(legacySettingsGroup(path, tab)).toBe(group);
});
