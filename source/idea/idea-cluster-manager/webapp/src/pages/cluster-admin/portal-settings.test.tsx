import React, {useState} from 'react';
import {DesktopScheduleTable, NotificationsTable, SCHEDULE_DAYS} from './settings-tables';
import {render, screen, within, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import {SettingDefinition} from '../../client/data-model';
import PortalSettings, {legacySettingsGroup, SETTINGS_CARDS, SETTINGS_GROUPS, SettingsGroups} from './portal-settings';

vi.mock('./email-templates', () => ({default: () => <button>Edit templates</button>}));

const definition = (key: string, group: string, value_type: SettingDefinition['value_type'], effect: SettingDefinition['effect'], advanced = false): SettingDefinition => ({
    key, module: key.split('.')[0], path: key.split('.').slice(1).join('.'), group, value_type, effect, advanced,
    label: key.split('.').at(-1)!.replaceAll('_', ' '), description: 'Configure this setting.', section: key.includes('web_portal') ? 'Portal' : key.includes('server.') ? 'Network and connectivity' : key.includes('.ses.') ? 'Email delivery' : key.includes('backups') ? 'Backup policy' : key.includes('client_ip') ? 'Encryption and access' : key.startsWith('directoryservice') ? 'Directory connection' : key.includes('bedrock') ? 'Amazon Bedrock' : 'Notifications', validation: {required: false}, choices: [],
});
const catalog: SettingDefinition[] = [
    definition('cluster-manager.web_portal.title', 'appearance', 'string', 'runtime'),
    definition('cluster-manager.server.max_workers', 'network', 'integer', 'restart', true),
    definition('cluster.ses.sender_email', 'email', 'string', 'runtime'),
    definition('cluster.ses.enabled', 'email', 'boolean', 'runtime'),
    definition('cluster.backups.enabled', 'backup', 'boolean', 'deployment'),
    definition('cluster.network.client_ip', 'network', 'list', 'deployment'),
    definition('directoryservice.root_password_secret_arn', 'sign-in', 'secret', 'runtime'),
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
    vi.spyOn(context.client().emailTemplates(), 'listEmailTemplates').mockResolvedValue({listing: []});
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
    await userEvent.click(within(nav).getByRole('link', {name: 'Notifications'}));
    expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/email?project=sample');
    expect(screen.getByRole('region', {name: 'Notifications'})).toBeInTheDocument();
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
    expect(await screen.findByText('Saved: cluster-manager.')).toBeVisible();
    expect(screen.getByRole('button', {name: 'Edit'})).toBeEnabled();
});

it('cancels a section edit and returns to key-value view mode', async () => {
    await renderGroups('/cluster/settings/appearance');
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.clear(screen.getByRole('textbox', {name: 'title'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'title'}), 'Discarded');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(screen.queryByRole('textbox', {name: 'title'})).not.toBeInTheDocument();
    expect(screen.getByText('Portal', {selector: 'dd'})).toBeVisible();
});

it('keeps deployment inputs editable and uses the upgrade effect on save', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().updateModuleSettings).mockResolvedValue({success: true, effects: {'backups.enabled': 'deployment'}});
    await renderGroups('/cluster/settings/backup', context);
    expect(screen.getByText('Applies on next upgrade')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.click(screen.getByRole('checkbox', {name: 'enabled'}));
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(await screen.findByText('Saved: cluster. Applies on next upgrade.')).toBeVisible();
    expect(context.client().clusterSettings().updateModuleSettings).toHaveBeenCalledWith({module_id: 'cluster', settings: {backups: {enabled: true}}});
});

it('keeps Advanced collapsed until requested and provides a numeric editor', async () => {
    await renderGroups('/cluster/settings/network');
    expect(screen.getByRole('button', {name: 'Advanced'})).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    expect(screen.getByText('Applies after restart')).toBeVisible();
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
    expect(await screen.findByText(/failed: Save failed/)).toBeVisible();
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
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockResolvedValue({settings: {root_password_secret_arn: secret}});
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
    expect(within(nav).getByRole('link', {name: 'Desktops'})).toBeInTheDocument();
    expect(within(nav).queryByRole('link', {name: 'Account synchronization'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Edit templates'})).not.toBeInTheDocument();
});

it('defines unique groups and card destinations', () => {
    expect(SETTINGS_GROUPS).toHaveLength(12);
    expect(Object.keys(SETTINGS_CARDS)).toHaveLength(12);
    expect(Object.values(SETTINGS_CARDS).flat()).toHaveLength(36);
    expect(new Set(Object.values(SETTINGS_CARDS).flat()).size).toBe(Object.values(SETTINGS_CARDS).flat().length);
});

it.each([
    ['/cluster/settings', 'general', 'appearance'], ['/cluster/settings', 'network', 'network'],
    ['/cluster/settings', 'shared-storage', 'storage'], ['/cluster/settings', 'identity-provider', 'sign-in'], ['/cluster/settings', 'directory-service', 'sign-in'],
    ['/cluster/settings', 'analytics', 'monitoring'], ['/cluster/settings', 'metrics', 'monitoring'], ['/cluster/settings', 'cloudwatch-logs', 'monitoring'],
    ['/cluster/settings', 'maintenance', 'appearance'], ['/cluster/settings', 'account-reconciliation', 'sign-in'], ['/cluster/settings', 'bedrock', 'ai-access'],
    ['/cluster/settings', 'ses', 'email'], ['/cluster/settings', 'ec2', 'network'], ['/cluster/settings', 'backups', 'backup'], ['/cluster/settings', 'route-53', 'network'], ['/cluster/settings', 'aws-account', 'deployment'],
    ['/virtual-desktop/settings', 'general', 'desktop-lifecycle'], ['/virtual-desktop/settings', 'notifications', 'email'], ['/virtual-desktop/settings', 'schedule', 'desktop-lifecycle'], ['/virtual-desktop/settings', 'server', 'desktop-lifecycle'],
    ['/virtual-desktop/settings', 'broker', 'network'], ['/virtual-desktop/settings', 'connection-gateway', 'network'], ['/virtual-desktop/settings', 'backups', 'backup'], ['/virtual-desktop/settings', 'cloudwatch-logs', 'monitoring'],
    ['/soca/settings', 'general', 'job-placement'], ['/soca/settings', 'cloudwatch-logs', 'monitoring'], ['/cluster/email-templates', '', 'email'],
])('maps %s?tab=%s to its section', (path, tab, group) => {
    expect(legacySettingsGroup(path, tab)).toBe(group);
});

it('isolates normal and advanced fields in one card with one strongest effect badge', async () => {
    const context = setup();
    const definitions = [
        {...definition('cluster.network.client_ip', 'network', 'list', 'deployment'), section: 'Encryption and access'},
        {...definition('cluster-manager.server.max_workers', 'network', 'integer', 'restart', true), section: 'Encryption and access'},
    ];
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: definitions});
    await renderGroups('/cluster/settings/network', context);
    expect(screen.getAllByText('Applies on next upgrade')).toHaveLength(1);
    expect(screen.queryByText('Applies after restart')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', {name: 'Edit'})).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
    expect(screen.getAllByRole('button', {name: 'Save'})).toHaveLength(1);
    expect(screen.getByRole('spinbutton', {name: 'max workers'})).toBeEnabled();
});

it('shows read-only advanced values without inputs or badges and excludes hidden search results', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: [
        {...definition('cluster-manager.server.max_workers', 'network', 'integer', 'deployment', true), section: 'Network and connectivity', ...{read_only: true}},
        {...definition('cluster.network.client_ip', 'network', 'list', 'deployment'), ...{hidden: true}},
    ]});
    await renderGroups('/cluster/settings/network?key=cluster-manager.server.max_workers', context);
    expect(screen.getByText('16')).toBeVisible();
    expect(screen.queryByRole('button', {name: 'Edit'})).not.toBeInTheDocument();
    expect(screen.queryByText('Applies on next upgrade')).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole('searchbox'), 'cluster.network.client_ip');
    expect(screen.queryByRole('table', {name: 'Matching settings'})).not.toBeInTheDocument();
});

it('resolves hidden key links to an explanation', async () => {
    const context = setup();
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: [
        {...definition('cluster.network.client_ip', 'network', 'list', 'deployment'), ...{hidden: true}},
    ]});
    await renderGroups('/cluster/settings/network?key=cluster.network.client_ip', context);
    expect(screen.getByText(/This setting is managed outside Settings/)).toBeVisible();
    expect(screen.queryByRole('button', {name: 'Edit'})).not.toBeInTheDocument();
});

function ScheduleHarness() {
    const [editing, setEditing] = useState(false);
    const [values, setValues] = useState({dcv_session: {working_hours: {start_up_time: '09:00', shut_down_time: '17:00'}, schedule: Object.fromEntries(SCHEDULE_DAYS.map(day => [day, {type: 'CUSTOM_SCHEDULE', start_up_time: '10:00', shut_down_time: '16:00'}]))}});
    return <DesktopScheduleTable values={values} moduleId="vdc" timezone="UTC" editing={editing} editDisabled={false} onEdit={() => setEditing(true)} onEditingEnd={() => setEditing(false)} onSaved={setValues}/>;
}

it('saves all twenty-three schedule leaves together and discards cancelled edits', async () => {
    const context = setup();
    render(<ScheduleHarness/>);
    expect(within(screen.getByRole('table', {name: 'Desktop schedule'})).getAllByRole('row')).toHaveLength(8);
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    const start = screen.getByRole('textbox', {name: 'Monday start'});
    await userEvent.clear(start);
    await userEvent.type(start, '11:00');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    expect(screen.getByRole('textbox', {name: 'Monday start'})).toHaveValue('10:00');
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    const update = vi.mocked(context.client().clusterSettings().updateModuleSettings);
    await waitFor(() => expect(update).toHaveBeenCalledOnce());
    const patch = (update.mock.calls[0][0].settings as any).dcv_session;
    expect(Object.keys(patch.working_hours)).toHaveLength(2);
    expect(Object.values(patch.schedule).flatMap(row => Object.keys(row as object))).toHaveLength(21);
});

it('rejects equal custom times and retains a failed schedule draft', async () => {
    const context = setup();
    const update = vi.mocked(context.client().clusterSettings().updateModuleSettings).mockRejectedValue(new Error('Save unavailable'));
    render(<ScheduleHarness/>);
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    const start = screen.getByRole('textbox', {name: 'Monday start'});
    await userEvent.clear(start); await userEvent.type(start, '16:00');
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(screen.getByText(/monday: custom hours require/)).toBeVisible();
    expect(update).not.toHaveBeenCalled();
    await userEvent.clear(start); await userEvent.type(start, '11:00');
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(await screen.findByText('Save unavailable')).toBeVisible();
    expect(screen.getByRole('textbox', {name: 'Monday start'})).toHaveValue('11:00');
});

const desktopEvents = ['provisioning', 'creating', 'initializing', 'resuming', 'ready', 'stopping', 'stopped', 'deleting', 'error', 'deleted', 'cleanup_warning', 'session-shared', 'session-permission-updated', 'session-permission-expired'];
function NotificationHarness() {
    const [editing, setEditing] = useState(false);
    const settings = [
        definition('cluster-manager.notifications.email.enabled', 'email', 'boolean', 'runtime'),
        definition('scheduler.notifications.enabled', 'email', 'boolean', 'runtime'),
        ...desktopEvents.flatMap(event => [definition(`virtual-desktop-controller.dcv_session.notifications.${event}.enabled`, 'email', 'boolean', 'runtime'), definition(`virtual-desktop-controller.dcv_session.notifications.${event}.email_template`, 'email', 'string', 'runtime')]),
        ...['job_started', 'job_completed'].map(event => definition(`scheduler.notifications.${event}.email_template`, 'email', 'string', 'runtime')),
        definition('virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template', 'email', 'string', 'runtime'),
    ].map(setting => ({...setting, label: setting.path, section: setting.path.split('.').at(-2)!}));
    const [values, setValues] = useState<any>({
        'cluster-manager': {notifications: {email: {enabled: true}}},
        scheduler: {notifications: {enabled: true, job_started: {email_template: 'mail'}, job_completed: {email_template: 'mail'}}},
        'virtual-desktop-controller': {dcv_session: {notifications: Object.fromEntries(desktopEvents.map(event => [event, {enabled: true, email_template: 'mail'}])), stopped_session_cleanup: {email_template: 'mail'}}},
    });
    return <NotificationsTable settings={settings} values={values} moduleId={module => module} editing={editing} editDisabled={false} onEdit={() => setEditing(true)} onEditingEnd={() => setEditing(false)} onSaved={(module, patch) => setValues((current: any) => {
        const updated = structuredClone(current[module]);
        Object.entries(patch).forEach(([path, value]) => {const parts = path.split('.'); const leaf = parts.pop()!; parts.reduce((node, part) => node[part] ??= {}, updated)[leaf] = value;});
        return {...current, [module]: updated};
    })}/>;
}

it('renders sixteen notification rows and one distinct cleanup reference, retaining failed module drafts', async () => {
    const context = setup();
    vi.mocked(context.client().emailTemplates().listEmailTemplates).mockResolvedValue({listing: [{name: 'mail'}]});
    const update = vi.mocked(context.client().clusterSettings().updateModuleSettings).mockResolvedValueOnce({success: true}).mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValue({success: true});
    render(<NotificationHarness/>);
    expect(within(screen.getByRole('table', {name: 'Notifications'})).getAllByRole('row')).toHaveLength(17);
    expect(screen.getAllByText('Follows Job notifications')).toHaveLength(2);
    expect(screen.getByText('dcv_session.stopped_session_cleanup.email_template')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    const toggles = screen.getAllByRole('checkbox');
    expect(toggles).toHaveLength(16);
    await userEvent.click(toggles[0]); await userEvent.click(toggles[1]);
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(await screen.findByText(/Saved: cluster-manager. scheduler failed: Unavailable/)).toBeVisible();
    expect(toggles[1]).not.toBeChecked();
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(3));
    expect(update.mock.calls.map(([request]) => request.module_id)).toEqual(['cluster-manager', 'scheduler', 'scheduler']);
});


const fixtureSections = ["AD automation","Account synchronization","Amazon Bedrock","Analytics","Backup policy","Collection","Cost estimation","Desktop placement","Desktop policy","Desktop schedule","Directory connection","Directory mapping","Email delivery","Email templates","Encryption and access","Fair-share scheduling","File systems","Installed deployment","Job limits and placement","Load balancers and certificates","Logs","Maintenance notice","Metrics","Network and connectivity","Notifications","Portal","Regional defaults","Resource tags","Scratch storage","Sign-in","Stopped desktop cleanup","Storage measurement","Cost header","Dashboard link","GPU policy"];
const fieldManifest = `
analytics.kinesis.kms_key_id|3|Kinesis KMS key ID|string|deployment|1|read_only|80
analytics.kinesis.removal_policy|3|Removal policy|enum|deployment|1|read_only|80
analytics.kinesis.shard_count|3|Shard count|integer|deployment|0|editable|80
analytics.kinesis.stream_mode|3|Stream mode|enum|deployment|0|editable|80
analytics.opensearch.data_node_instance_type|3|Data node instance type|string|deployment|1|editable|81
analytics.opensearch.data_nodes|3|Data nodes|integer|deployment|1|editable|81
analytics.opensearch.default_number_of_replicas|3|Default number of replicas|integer|restart|1|editable|81
analytics.opensearch.default_number_of_shards|3|Default number of shards|integer|restart|1|read_only|81
analytics.opensearch.domain_vpc_endpoint_url|3|Domain VPC endpoint URL|string|deployment|1|hidden|81
analytics.opensearch.ebs_volume_size|3|EBS volume size|integer|deployment|1|read_only|81
analytics.opensearch.endpoints.external.path_patterns|3|Path patterns|list|deployment|1|hidden|82
analytics.opensearch.endpoints.external.priority|3|Priority|integer|deployment|1|hidden|82
analytics.opensearch.kms_key_id|3|OpenSearch KMS key ID|string|deployment|1|read_only|81
analytics.opensearch.logging.app_log_enabled|3|OpenSearch application logs|boolean|deployment|1|editable|83
analytics.opensearch.logging.app_log_removal_policy|3|App log removal policy|string|deployment|1|editable|83
analytics.opensearch.logging.search_log_removal_policy|3|Search log removal policy|string|deployment|1|editable|83
analytics.opensearch.logging.slow_index_log_enabled|3|Slow indexing logs|boolean|deployment|1|editable|83
analytics.opensearch.logging.slow_index_log_removal_policy|3|Slow index log removal policy|string|deployment|1|editable|83
analytics.opensearch.logging.slow_search_log_enabled|3|Slow search logs|boolean|deployment|1|editable|83
analytics.opensearch.node_to_node_encryption|3|OpenSearch node-to-node encryption|boolean|deployment|1|read_only|81
analytics.opensearch.removal_policy|3|Removal policy|enum|deployment|1|read_only|81
analytics.opensearch.use_existing|3|Use an existing OpenSearch domain|boolean|deployment|1|read_only|81
cluster-manager.accounts.reconcile.check_cognito|1|Check Cognito accounts|boolean|runtime|0|editable|0
cluster-manager.accounts.reconcile.dry_run|1|Preview account changes|boolean|runtime|0|editable|0
cluster-manager.accounts.reconcile.enabled|1|Account synchronization|boolean|runtime|0|editable|0
cluster-manager.accounts.reconcile.interval_minutes|1|Interval (minutes)|integer|runtime|1|editable|1
cluster-manager.accounts.reconcile.max_disable_fraction|1|Max disable fraction|number|runtime|0|editable|0
cluster-manager.accounts.reconcile.okta.api_token_secret_arn|1|API token secret ARN|secret|runtime|0|editable|2
cluster-manager.accounts.reconcile.okta.org_url|1|Org URL|string|runtime|0|editable|2
cluster-manager.accounts.reconcile.reenable|1|Restore synchronized accounts|boolean|runtime|0|editable|0
cluster-manager.bedrock.budgets.action|2|Action|enum|runtime|0|editable|4
cluster-manager.bedrock.budgets.enabled|2|Budget enforcement|boolean|runtime|0|editable|4
cluster-manager.bedrock.budgets.warning_percent|2|Warning (%)|integer|runtime|0|editable|4
cluster-manager.bedrock.enabled|2|Amazon Bedrock|boolean|deployment|0|editable|3
cluster-manager.bedrock.invocation_logging.include_request_response_data|2|Log Bedrock requests and responses|boolean|deployment|0|editable|5
cluster-manager.bedrock.invocation_logging.log_retention_in_days|2|Log retention in (days)|integer|deployment|1|editable|6
cluster-manager.bedrock.invocation_logging.manage_configuration|2|Manage Bedrock invocation logging|boolean|deployment|0|editable|5
cluster-manager.bedrock.model_ids|2|Approved models|list|runtime|0|editable|3
cluster-manager.bedrock.usage.enabled|2|Bedrock usage collection|boolean|runtime|1|editable|7
cluster-manager.bedrock.usage.interval_minutes|2|Interval (minutes)|integer|runtime|1|editable|7
cluster-manager.bedrock.usage.lookback_days|2|Lookback (days)|integer|runtime|1|editable|7
cluster-manager.bedrock.usage.max_query_results|2|Max query results|integer|runtime|1|editable|7
cluster-manager.bedrock.usage.max_users_per_project|2|Max users per project|integer|runtime|1|editable|7
cluster-manager.bedrock.usage.query_timeout_seconds|2|Query timeout (seconds)|integer|runtime|1|editable|7
cluster-manager.bedrock.usage.retention_days|2|Retention (days)|integer|runtime|1|editable|7
cluster-manager.cache.long_term.max_size|17|Max size|integer|restart|1|hidden|127
cluster-manager.cache.long_term.ttl_seconds|17|TTL (seconds)|integer|restart|1|hidden|127
cluster-manager.cache.short_term.max_size|17|Max size|integer|restart|1|hidden|128
cluster-manager.cache.short_term.ttl_seconds|17|TTL (seconds)|integer|restart|1|hidden|128
cluster-manager.cloudwatch_logs.enabled|20|Cluster manager CloudWatch Logs|boolean|deployment|0|editable|88
cluster-manager.ec2.autoscaling.base_os|17|Base OS|string|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.cooldown_minutes|17|Cooldown (minutes)|integer|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes|17|Estimated instance warmup (minutes)|integer|deployment|1|hidden|130
cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent|17|Target utilization (%)|integer|deployment|1|hidden|130
cluster-manager.ec2.autoscaling.elb_healthcheck.grace_time_minutes|17|Grace time (minutes)|integer|deployment|1|hidden|131
cluster-manager.ec2.autoscaling.enable_detailed_monitoring|17|Cluster manager detailed monitoring|boolean|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.instance_ami|17|Instance AMI|string|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.instance_type|17|Instance type|string|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.max_capacity|17|Max capacity|integer|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.metadata_http_tokens|17|Metadata HTTP tokens|enum|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.min_capacity|17|Min capacity|integer|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.new_instances_protected_from_scale_in|17|Cluster manager scale-in protection|boolean|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.public|17|Cluster manager public access|boolean|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.rolling_update_policy.max_batch_size|17|Max batch size|integer|deployment|1|hidden|132
cluster-manager.ec2.autoscaling.rolling_update_policy.min_instances_in_service|17|Min instances in service|integer|deployment|1|hidden|132
cluster-manager.ec2.autoscaling.rolling_update_policy.pause_time_minutes|17|Pause time (minutes)|integer|deployment|1|hidden|132
cluster-manager.ec2.autoscaling.volume_size|17|Volume size|integer|deployment|1|hidden|129
cluster-manager.ec2.autoscaling.volume_type|17|Volume type|enum|deployment|1|hidden|129
cluster-manager.endpoints.external.path_patterns|17|Path patterns|list|deployment|1|hidden|133
cluster-manager.endpoints.external.priority|17|Priority|integer|deployment|1|hidden|133
cluster-manager.endpoints.internal.path_patterns|17|Path patterns|list|deployment|1|hidden|134
cluster-manager.endpoints.internal.priority|17|Priority|integer|deployment|1|hidden|134
cluster-manager.logging.default_log_file_name|3|Default log file name|string|restart|1|editable|89
cluster-manager.logging.logs_directory|3|Logs directory|string|restart|1|editable|89
cluster-manager.logging.profile|3|Profile|string|restart|1|editable|89
cluster-manager.maintenance.enabled|21|Maintenance notice|boolean|runtime|0|editable|79
cluster-manager.maintenance.ends_at|21|Ends at|string|runtime|0|editable|79
cluster-manager.maintenance.message|21|Message|string|runtime|0|editable|79
cluster-manager.metrics.cost.by_account|5|Costs by linked account|boolean|runtime|1|editable|22
cluster-manager.metrics.cost.enabled|5|Cost collection|boolean|restart|0|editable|22
cluster-manager.metrics.cost.interval_hours|5|Interval (hours)|integer|runtime|0|editable|23
cluster-manager.metrics.cost.lookback_days|5|Lookback (days)|integer|runtime|0|editable|22
cluster-manager.metrics.cost.module_tag|5|Module tag key|string|runtime|0|editable|22
cluster-manager.metrics.cost.owner_tag|5|Owner tag key|string|runtime|0|editable|22
cluster-manager.metrics.cost.project_tag|5|Project tag key|string|runtime|0|editable|22
cluster-manager.metrics.storage.enabled|31|Storage measurement|boolean|restart|0|editable|24
cluster-manager.metrics.storage.interval_minutes|31|Interval (minutes)|integer|runtime|1|editable|25
cluster-manager.metrics.storage.verify_tls|31|Verify storage TLS certificates|boolean|runtime|1|editable|24
cluster-manager.notifications.email.enabled|24|Portal email notifications|boolean|runtime|0|editable|46
cluster-manager.oauth2_client.refresh_token_validity_hours|29|Refresh token validity (hours)|integer|deployment|0|editable|171
cluster-manager.server.enable_http|17|Cluster manager HTTP|boolean|restart|1|hidden|135
cluster-manager.server.enable_metrics|17|Cluster manager metrics|boolean|restart|1|hidden|135
cluster-manager.server.enable_tls|19|Cluster manager TLS|boolean|restart|1|read_only|135
cluster-manager.server.enable_unix_socket|17|Cluster manager Unix socket|boolean|restart|1|hidden|135
cluster-manager.server.graceful_shutdown_timeout|17|Graceful shutdown timeout|integer|restart|1|hidden|135
cluster-manager.server.hostname|17|Hostname|string|restart|1|hidden|135
cluster-manager.server.max_workers|17|Max workers|integer|restart|1|hidden|135
cluster-manager.server.port|17|Port|integer|restart|1|hidden|135
cluster-manager.server.tls_certificate_file|19|Cluster manager TLS certificate path|string|restart|1|read_only|135
cluster-manager.server.tls_key_file|19|Cluster manager TLS private key path|string|restart|1|read_only|135
cluster-manager.server.unix_socket_file|17|Unix socket file|string|restart|1|hidden|135
cluster-manager.task_manager.debug|17|Debug|boolean|restart|1|hidden|136
cluster-manager.task_manager.max_workers|17|Max workers|integer|restart|1|hidden|136
cluster-manager.task_manager.min_workers|17|Min workers|integer|restart|1|hidden|136
cluster-manager.task_manager.polling_messages_max|17|Polling messages max|integer|restart|1|hidden|136
cluster-manager.task_manager.polling_visibility_timeout|17|Polling visibility timeout|integer|restart|1|hidden|136
cluster-manager.task_manager.sqs_wait_time|17|SQS wait time|integer|restart|1|hidden|136
cluster-manager.task_manager.task_timeout_seconds|17|Task timeout (seconds)|integer|restart|1|hidden|136
cluster-manager.web_portal.copyright_text|25|Copyright text|string|runtime|0|hidden|12
cluster-manager.web_portal.cost_ticker.enabled|32|Cost header|boolean|runtime|0|editable|13
cluster-manager.web_portal.cost_ticker.period|32|Period|enum|runtime|0|editable|13
cluster-manager.web_portal.custom_dashboard.enabled|33|Dashboard link|boolean|runtime|0|editable|14
cluster-manager.web_portal.custom_dashboard.title|33|Title|string|runtime|0|editable|14
cluster-manager.web_portal.custom_dashboard.url|33|URL|string|runtime|0|editable|14
cluster-manager.web_portal.default_landing_page|25|Default landing page|enum|runtime|0|editable|12
cluster-manager.web_portal.default_log_level|25|Default log level|integer|runtime|0|hidden|12
cluster-manager.web_portal.logo|25|Logo URL|string|runtime|0|editable|12
cluster-manager.web_portal.session_management|25|Session management|enum|runtime|0|editable|172
cluster-manager.web_portal.subtitle|25|Subtitle|string|runtime|0|editable|12
cluster-manager.web_portal.title|25|Title|string|runtime|0|editable|12
cluster.administrator_email|26|Initial administrator email|string|runtime|0|editable|11
cluster.aws.fsx_lustre_version|17|FSx Lustre version|string|runtime|0|hidden|106
cluster.aws.pricing_region|6|Pricing region|string|restart|1|hidden_read_only|106
cluster.backups.backup_plan.rules.default.completion_window_minutes|4|Cluster default: Completion window (minutes)|integer|deployment|0|editable|16
cluster.backups.backup_plan.rules.default.delete_after_days|4|Cluster default: Delete after (days)|integer|deployment|0|editable|16
cluster.backups.backup_plan.rules.default.move_to_cold_storage_after_days|4|Cluster default: Move to cold storage after (days)|integer|deployment|0|editable|16
cluster.backups.backup_plan.rules.default.schedule_expression|4|Cluster default: Schedule expression|string|deployment|0|editable|16
cluster.backups.backup_plan.rules.default.start_window_minutes|4|Cluster default: Start window (minutes)|integer|deployment|0|editable|16
cluster.backups.backup_plan.rules.weekly.completion_window_minutes|4|Cluster weekly: Completion window (minutes)|integer|deployment|0|editable|later
cluster.backups.backup_plan.rules.weekly.delete_after_days|4|Cluster weekly: Delete after (days)|integer|deployment|0|editable|later
cluster.backups.backup_plan.rules.weekly.move_to_cold_storage_after_days|4|Cluster weekly: Move to cold storage after (days)|integer|deployment|0|editable|later
cluster.backups.backup_plan.rules.weekly.schedule_expression|4|Cluster weekly: Schedule expression|string|deployment|0|editable|later
cluster.backups.backup_plan.rules.weekly.start_window_minutes|4|Cluster weekly: Start window (minutes)|integer|deployment|0|editable|later
cluster.backups.backup_plan.selection.tags|4|Tags|list|deployment|0|editable|17
cluster.backups.backup_vault.kms_key_id|4|Backup vault KMS key ID|string|deployment|1|read_only|18
cluster.backups.backup_vault.removal_policy|4|Removal policy|enum|deployment|1|read_only|18
cluster.backups.enable_restore|4|Backup restore|boolean|deployment|0|editable|15
cluster.backups.enabled|4|Cluster backups|boolean|deployment|0|editable|15
cluster.cloudwatch_logs.enabled|20|Cluster CloudWatch Logs|boolean|deployment|0|editable|84
cluster.cloudwatch_logs.force_flush_interval|20|Force flush interval|integer|deployment|1|editable|85
cluster.cloudwatch_logs.retention_in_days|20|Retention in (days)|integer|deployment|1|editable|85
cluster.dynamodb.kms_key_id|14|DynamoDB KMS key ID|string|runtime|0|editable|107
cluster.ebs.kms_key_id|14|EBS KMS key ID|string|deployment|0|editable|108
cluster.encoding|26|Encoding|string|runtime|1|read_only|11
cluster.iam.compute_node_iam_policy_arns|14|Compute node IAM policy ARNs|list|deployment|0|editable|109
cluster.iam.ec2_managed_policy_arns|14|EC2 managed policy ARNs|list|deployment|0|editable|109
cluster.iam.scheduler_iam_policy_arns|14|Scheduler IAM policy ARNs|list|deployment|0|editable|109
cluster.kms.key_type|14|Key type|enum|deployment|0|editable|110
cluster.load_balancers.external_alb.access_logs|19|External ALB access logs|boolean|deployment|0|editable|111
cluster.load_balancers.external_alb.certificates.acm_certificate_arn|19|Acm certificate ARN|string|deployment|0|editable|113
cluster.load_balancers.external_alb.certificates.custom_dns_name|19|Custom DNS name|string|deployment|0|editable|113
cluster.load_balancers.external_alb.certificates.provided|19|Use a provided ALB certificate|boolean|deployment|0|editable|113
cluster.load_balancers.external_alb.idle_timeout_seconds|19|Idle timeout (seconds)|integer|deployment|1|editable|112
cluster.load_balancers.external_alb.public|19|Public external ALB|boolean|deployment|1|read_only|111
cluster.load_balancers.external_alb.ssl_policy|19|SSL policy|string|deployment|0|editable|111
cluster.load_balancers.external_alb.waf.bot_control.enabled|19|Bot protection|boolean|deployment|0|editable|115
cluster.load_balancers.external_alb.waf.enabled|19|Web application firewall|boolean|deployment|0|editable|114
cluster.load_balancers.external_alb.waf.logging.drop_allow_actions|19|Exclude allowed requests from firewall logs|boolean|deployment|1|editable|86
cluster.load_balancers.internal_alb.access_logs|19|Internal ALB access logs|boolean|deployment|0|editable|116
cluster.load_balancers.internal_alb.idle_timeout_seconds|19|Idle timeout (seconds)|integer|deployment|1|editable|117
cluster.load_balancers.internal_alb.ssl_policy|19|SSL policy|string|deployment|0|editable|116
cluster.locale|26|Locale|string|restart|0|editable|11
cluster.logging.audit_logs.enable_payload_tracing|20|Audit payload tracing|boolean|restart|1|editable|87
cluster.logging.audit_logs.tags|20|Tags|list|restart|1|editable|87
cluster.network.client_ip|23|Client ip|list|deployment|1|read_only|118
cluster.network.cluster_prefix_list_max_entries|23|Cluster prefix list max entries|integer|deployment|0|editable|118
cluster.network.https_proxy|23|HTTPS proxy|string|deployment|0|editable|118
cluster.network.max_azs|23|Max AZs|integer|deployment|1|read_only|118
cluster.network.nat_gateways|23|NAT gateways|integer|deployment|1|read_only|118
cluster.network.no_proxy|23|No proxy|string|deployment|0|editable|118
cluster.network.preferred_subnet_id|23|Preferred subnet ID|string|runtime|0|editable|118
cluster.network.prefix_list_ids|23|Prefix list IDs|list|deployment|0|editable|118
cluster.network.private_subnets|23|Private subnets|list|deployment|1|read_only|118
cluster.network.public_subnets|23|Public subnets|list|deployment|1|read_only|118
cluster.network.ssh_key_pair|23|SSH key pair|string|deployment|0|editable|118
cluster.network.subnet_config.isolated.cidr_mask|23|CIDR mask|integer|deployment|1|read_only|119
cluster.network.subnet_config.private.cidr_mask|23|CIDR mask|integer|deployment|1|read_only|120
cluster.network.subnet_config.public.cidr_mask|23|CIDR mask|integer|deployment|1|read_only|121
cluster.network.use_vpc_endpoints|23|Use VPC endpoints|boolean|deployment|0|editable|118
cluster.network.vpc_cidr_block|23|VPC CIDR|string|deployment|1|read_only|118
cluster.network.vpc_flow_logs_removal_policy|23|VPC flow logs removal policy|string|deployment|1|read_only|118
cluster.network.vpc_flow_logs|23|VPC flow logs|boolean|deployment|0|editable|118
cluster.network.vpc_gateway_endpoints|23|VPC gateway endpoints|list|deployment|0|editable|118
cluster.network.vpc_interface_endpoints.logs.enabled|23|CloudWatch Logs endpoint|boolean|deployment|0|editable|122
cluster.network.vpc_interface_endpoints.logs.endpoint_url|23|Endpoint URL|string|deployment|0|editable|122
cluster.network.vpc_interface_endpoints.monitoring.enabled|23|CloudWatch monitoring endpoint|boolean|deployment|0|editable|123
cluster.network.vpc_interface_endpoints.monitoring.endpoint_url|23|Endpoint URL|string|deployment|0|editable|123
cluster.secretsmanager.kms_key_id|14|Secrets Manager KMS key ID|string|deployment|0|editable|124
cluster.ses.account_id|12|Account ID|string|restart|0|editable|78
cluster.ses.enabled|12|Amazon SES delivery|boolean|runtime|0|editable|78
cluster.ses.max_sending_rate|12|Max sending rate|integer|runtime|0|editable|78
cluster.ses.region|12|Region|string|runtime|0|editable|78
cluster.ses.sender_email|12|Sender email|string|runtime|0|editable|78
cluster.sns.kms_key_id|14|SNS KMS key ID|string|deployment|0|editable|125
cluster.sqs.kms_key_id|14|SQS KMS key ID|string|deployment|0|editable|126
cluster.timezone|26|Timezone|string|runtime|0|editable|11
directoryservice.ad_automation.domain_discovery_ttl_seconds|0|Domain discovery TTL (seconds)|integer|runtime|0|editable|174
directoryservice.ad_automation.enable_root_password_reset|0|Rotate service account password|boolean|runtime|0|editable|174
directoryservice.ad_automation.entry_ttl_seconds|0|Entry TTL (seconds)|integer|restart|0|editable|174
directoryservice.ad_automation.hostname_prefix|0|Hostname prefix|string|runtime|0|editable|174
directoryservice.ad_automation.sqs_visibility_timeout_seconds|0|SQS visibility timeout (seconds)|integer|runtime|1|editable|175
directoryservice.ad_edition|10|AD edition|enum|deployment|1|read_only|173
directoryservice.ad_short_name|10|AD short name (NetBIOS)|string|deployment|1|read_only|173
directoryservice.base_os|10|Base OS|string|deployment|1|read_only|173
directoryservice.cloudwatch_logs.enabled|20|Directory CloudWatch Logs|boolean|deployment|0|editable|176
directoryservice.computers.ou|11|Computer OU|string|runtime|0|editable|177
directoryservice.ec2.enable_detailed_monitoring|10|Directory detailed monitoring|boolean|deployment|1|read_only|178
directoryservice.ec2.enable_termination_protection|10|Directory termination protection|boolean|deployment|1|read_only|178
directoryservice.ec2.metadata_http_tokens|10|Metadata HTTP tokens|enum|deployment|1|read_only|178
directoryservice.group_mapping.default-project-group|11|Default project group|string|runtime|0|editable|179
directoryservice.groups.ou|11|Group OU|string|runtime|0|editable|180
directoryservice.instance_ami|10|Instance AMI|string|deployment|1|read_only|173
directoryservice.instance_type|10|Instance type|string|deployment|1|read_only|173
directoryservice.ldap_base|10|LDAP base DN|string|deployment|1|read_only|173
directoryservice.ldap_connection_uri|10|LDAP connection URI|string|runtime|0|editable|173
directoryservice.ldap_options|10|LDAP options|list|restart|0|editable|173
directoryservice.name|10|Directory domain|string|deployment|1|read_only|173
directoryservice.password_max_age|10|Maximum password age (days)|integer|runtime|0|editable|173
directoryservice.provider|10|Provider|enum|deployment|1|read_only|173
directoryservice.public|10|Public directory access|boolean|deployment|1|read_only|173
directoryservice.root_credentials_provided|10|Use provided directory credentials|boolean|deployment|1|read_only|173
directoryservice.root_password_secret_arn|10|Service account password secret ARN|secret|deployment|0|editable|173
directoryservice.root_username_secret_arn|10|Service account username secret ARN|secret|deployment|0|editable|173
directoryservice.sssd.ldap_id_mapping|11|SSSD LDAP ID mapping|boolean|deployment|0|editable|181
directoryservice.sudoers.group_name|11|Sudo group|string|deployment|0|editable|182
directoryservice.sudoers.ou|11|Sudo group OU|string|runtime|0|editable|182
directoryservice.tls_certificate_secret_arn|10|TLS certificate secret ARN|secret|deployment|0|editable|173
directoryservice.tls_private_key_secret_arn|10|TLS private key secret ARN|secret|deployment|0|editable|173
directoryservice.use_existing|10|Use an existing directory|boolean|deployment|1|read_only|173
directoryservice.users.ou|11|User OU|string|runtime|0|editable|183
directoryservice.volume_size|10|Volume size|integer|deployment|1|read_only|173
directoryservice.volume_type|10|Volume type|enum|deployment|1|read_only|173
global-settings.custom_tags|27|Custom tags|list|deployment|0|editable|170
global-settings.gpu_settings.amd.linux.rhel_rocky8_installer_url|17|Rhel rocky8 installer URL|string|deployment|1|hidden|188
global-settings.gpu_settings.amd.linux.rhel_rocky9_installer_url|17|Rhel rocky9 installer URL|string|deployment|1|hidden|188
global-settings.gpu_settings.amd.linux.s3_bucket_path|17|S3 bucket path|string|deployment|1|hidden|188
global-settings.gpu_settings.amd.linux.s3_bucket_url|17|S3 bucket URL|string|deployment|1|hidden|188
global-settings.gpu_settings.amd.windows.s3_bucket_path|17|S3 bucket path|string|deployment|1|hidden|189
global-settings.gpu_settings.amd.windows.s3_bucket_url|17|S3 bucket URL|string|deployment|1|hidden|189
global-settings.gpu_settings.fail_on_missing_driver|34|Require a GPU driver|boolean|deployment|1|editable|187
global-settings.gpu_settings.instance_families|34|GPU instance families|list|deployment|1|editable|187
global-settings.gpu_settings.nvidia.linux.s3_bucket_path|17|S3 bucket path|string|deployment|1|hidden|190
global-settings.gpu_settings.nvidia.linux.s3_bucket_url|17|S3 bucket URL|string|deployment|1|hidden|190
global-settings.gpu_settings.nvidia.windows.s3_bucket_path|17|S3 bucket path|string|deployment|1|hidden|191
global-settings.gpu_settings.nvidia.windows.s3_bucket_url|17|S3 bucket URL|string|deployment|1|hidden|191
global-settings.gpu_settings.nvidia_public_driver_versions.g2|17|G2|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g3s|17|G3s|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g3|17|G3|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g4dn|17|G4dn|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g5g|17|G5g|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g5|17|G5|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g6e|17|G6e|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g6f|17|G6f|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g6|17|G6|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.g7e|17|G7e|string|deployment|1|hidden|later
global-settings.gpu_settings.nvidia_public_driver_versions.g7|17|G7|string|deployment|1|hidden|later
global-settings.gpu_settings.nvidia_public_driver_versions.gr6|17|Gr6|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.ltsb_version|17|Ltsb version|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p2|17|P2|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p3|17|P3|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p4de|17|P4de|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p4d|17|P4d|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p5en|17|P5en|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p5e|17|P5e|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p5|17|P5|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p6-b200|17|P6 b200|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.p6e-gb200|17|P6e gb200|string|deployment|1|hidden|192
global-settings.gpu_settings.nvidia_public_driver_versions.production_version|17|Production version|string|deployment|1|hidden|192
global-settings.package_config.amazon_cloudwatch_agent.download_link_pattern|17|Download link pattern|string|deployment|1|hidden|193
global-settings.package_config.amazon_cloudwatch_agent.download_link|17|Download link|string|deployment|1|hidden|193
global-settings.package_config.aws_ssm.aarch64|17|Aarch64|string|deployment|1|hidden|194
global-settings.package_config.aws_ssm.x86_64|17|X86 64|string|deployment|1|hidden|194
global-settings.package_config.dcv.aarch64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|196
global-settings.package_config.dcv.aarch64.linux.al2023.url|17|URL|string|deployment|1|hidden|196
global-settings.package_config.dcv.aarch64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|197
global-settings.package_config.dcv.aarch64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|197
global-settings.package_config.dcv.aarch64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|198
global-settings.package_config.dcv.aarch64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|198
global-settings.package_config.dcv.aarch64.linux.ubuntu2204.sha256sum|17|Sha256sum|string|deployment|1|hidden|199
global-settings.package_config.dcv.aarch64.linux.ubuntu2204.url|17|URL|string|deployment|1|hidden|199
global-settings.package_config.dcv.aarch64.linux.ubuntu2404.sha256sum|17|Sha256sum|string|deployment|1|hidden|200
global-settings.package_config.dcv.aarch64.linux.ubuntu2404.url|17|URL|string|deployment|1|hidden|200
global-settings.package_config.dcv.agent.aarch64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|201
global-settings.package_config.dcv.agent.aarch64.linux.al2023.url|17|URL|string|deployment|1|hidden|201
global-settings.package_config.dcv.agent.aarch64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|202
global-settings.package_config.dcv.agent.aarch64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|202
global-settings.package_config.dcv.agent.aarch64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|203
global-settings.package_config.dcv.agent.aarch64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|203
global-settings.package_config.dcv.agent.aarch64.linux.ubuntu2204.sha256sum|17|Sha256sum|string|deployment|1|hidden|204
global-settings.package_config.dcv.agent.aarch64.linux.ubuntu2204.url|17|URL|string|deployment|1|hidden|204
global-settings.package_config.dcv.agent.aarch64.linux.ubuntu2404.sha256sum|17|Sha256sum|string|deployment|1|hidden|205
global-settings.package_config.dcv.agent.aarch64.linux.ubuntu2404.url|17|URL|string|deployment|1|hidden|205
global-settings.package_config.dcv.agent.x86_64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|206
global-settings.package_config.dcv.agent.x86_64.linux.al2023.url|17|URL|string|deployment|1|hidden|206
global-settings.package_config.dcv.agent.x86_64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|207
global-settings.package_config.dcv.agent.x86_64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|207
global-settings.package_config.dcv.agent.x86_64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|208
global-settings.package_config.dcv.agent.x86_64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|208
global-settings.package_config.dcv.agent.x86_64.ubuntu.ubuntu2204.sha256sum|17|Sha256sum|string|deployment|1|hidden|209
global-settings.package_config.dcv.agent.x86_64.ubuntu.ubuntu2204.url|17|URL|string|deployment|1|hidden|209
global-settings.package_config.dcv.agent.x86_64.ubuntu.ubuntu2404.sha256sum|17|Sha256sum|string|deployment|1|hidden|210
global-settings.package_config.dcv.agent.x86_64.ubuntu.ubuntu2404.url|17|URL|string|deployment|1|hidden|210
global-settings.package_config.dcv.broker.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|211
global-settings.package_config.dcv.broker.linux.al2023.url|17|URL|string|deployment|1|hidden|211
global-settings.package_config.dcv.broker.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|212
global-settings.package_config.dcv.broker.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|212
global-settings.package_config.dcv.broker.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|213
global-settings.package_config.dcv.broker.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|213
global-settings.package_config.dcv.clients.linux.rhel_centos_rocky8.label|17|Label|string|deployment|1|hidden|214
global-settings.package_config.dcv.clients.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|214
global-settings.package_config.dcv.clients.linux.rhel_centos_rocky9.label|17|Label|string|deployment|1|hidden|215
global-settings.package_config.dcv.clients.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|215
global-settings.package_config.dcv.clients.linux.suse15.label|17|Label|string|deployment|1|hidden|216
global-settings.package_config.dcv.clients.linux.suse15.url|17|URL|string|deployment|1|hidden|216
global-settings.package_config.dcv.clients.linux.ubuntu2004.label|17|Label|string|deployment|1|hidden|217
global-settings.package_config.dcv.clients.linux.ubuntu2004.url|17|URL|string|deployment|1|hidden|217
global-settings.package_config.dcv.clients.linux.ubuntu2204.label|17|Label|string|deployment|1|hidden|218
global-settings.package_config.dcv.clients.linux.ubuntu2204.url|17|URL|string|deployment|1|hidden|218
global-settings.package_config.dcv.clients.linux.ubuntu2404.label|17|Label|string|deployment|1|hidden|219
global-settings.package_config.dcv.clients.linux.ubuntu2404.url|17|URL|string|deployment|1|hidden|219
global-settings.package_config.dcv.clients.macos.intel.label|17|Label|string|deployment|1|hidden|220
global-settings.package_config.dcv.clients.macos.intel.url|17|URL|string|deployment|1|hidden|220
global-settings.package_config.dcv.clients.macos.m1.label|17|Label|string|deployment|1|hidden|221
global-settings.package_config.dcv.clients.macos.m1.url|17|URL|string|deployment|1|hidden|221
global-settings.package_config.dcv.clients.windows.msi.label|17|Label|string|deployment|1|hidden|222
global-settings.package_config.dcv.clients.windows.msi.url|17|URL|string|deployment|1|hidden|222
global-settings.package_config.dcv.clients.windows.zip.label|17|Label|string|deployment|1|hidden|223
global-settings.package_config.dcv.clients.windows.zip.url|17|URL|string|deployment|1|hidden|223
global-settings.package_config.dcv.connection_gateway.aarch64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|224
global-settings.package_config.dcv.connection_gateway.aarch64.linux.al2023.url|17|URL|string|deployment|1|hidden|224
global-settings.package_config.dcv.connection_gateway.aarch64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|225
global-settings.package_config.dcv.connection_gateway.aarch64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|225
global-settings.package_config.dcv.connection_gateway.aarch64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|226
global-settings.package_config.dcv.connection_gateway.aarch64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|226
global-settings.package_config.dcv.connection_gateway.x86_64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|227
global-settings.package_config.dcv.connection_gateway.x86_64.linux.al2023.url|17|URL|string|deployment|1|hidden|227
global-settings.package_config.dcv.connection_gateway.x86_64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|228
global-settings.package_config.dcv.connection_gateway.x86_64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|228
global-settings.package_config.dcv.connection_gateway.x86_64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|229
global-settings.package_config.dcv.connection_gateway.x86_64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|229
global-settings.package_config.dcv.gpg_key|17|Gpg key|string|deployment|1|hidden|195
global-settings.package_config.dcv.x86_64.debian.ubuntu2204.sha256sum|17|Sha256sum|string|deployment|1|hidden|230
global-settings.package_config.dcv.x86_64.debian.ubuntu2204.url|17|URL|string|deployment|1|hidden|230
global-settings.package_config.dcv.x86_64.debian.ubuntu2404.sha256sum|17|Sha256sum|string|deployment|1|hidden|231
global-settings.package_config.dcv.x86_64.debian.ubuntu2404.url|17|URL|string|deployment|1|hidden|231
global-settings.package_config.dcv.x86_64.linux.al2023.sha256sum|17|Sha256sum|string|deployment|1|hidden|232
global-settings.package_config.dcv.x86_64.linux.al2023.url|17|URL|string|deployment|1|hidden|232
global-settings.package_config.dcv.x86_64.linux.rhel_centos_rocky8.sha256sum|17|Sha256sum|string|deployment|1|hidden|233
global-settings.package_config.dcv.x86_64.linux.rhel_centos_rocky8.url|17|URL|string|deployment|1|hidden|233
global-settings.package_config.dcv.x86_64.linux.rhel_centos_rocky9.sha256sum|17|Sha256sum|string|deployment|1|hidden|234
global-settings.package_config.dcv.x86_64.linux.rhel_centos_rocky9.url|17|URL|string|deployment|1|hidden|234
global-settings.package_config.efa.checksum_method|17|Checksum method|string|deployment|1|hidden|235
global-settings.package_config.efa.checksum|17|Checksum|string|deployment|1|hidden|235
global-settings.package_config.efa.url|17|URL|string|deployment|1|hidden|235
global-settings.package_config.efa.version|17|Version|string|deployment|1|hidden|235
global-settings.package_config.efs_utils.version|17|Version|string|deployment|1|hidden|236
global-settings.package_config.linux_packages.application_7|17|Application 7|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.application_8|17|Application 8|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.application_deb|17|Application deb|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.application|17|Application|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_client_deb|17|Openldap client deb|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_client|17|Openldap client|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_server_10|17|Openldap server 10|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_server_7|17|Openldap server 7|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_server_8|17|Openldap server 8|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_server_9|17|Openldap server 9|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.openldap_server|17|Openldap server|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.putty|17|Putty|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.sssd_7|17|SSSD 7|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.sssd_deb|17|SSSD deb|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.sssd|17|SSSD|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system_10|17|System 10|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system_7|17|System 7|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system_8|17|System 8|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system_9|17|System 9|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system_deb|17|System deb|list|deployment|1|hidden|237
global-settings.package_config.linux_packages.system|17|System|list|deployment|1|hidden|237
global-settings.package_config.nodejs.npm_version|17|Npm version|string|deployment|1|hidden|238
global-settings.package_config.nodejs.nvm_version|17|Nvm version|string|deployment|1|hidden|238
global-settings.package_config.nodejs.url|17|URL|string|deployment|1|hidden|238
global-settings.package_config.nodejs.version|17|Version|string|deployment|1|hidden|238
global-settings.package_config.openmpi.checksum_method|17|Checksum method|string|deployment|1|hidden|239
global-settings.package_config.openmpi.checksum|17|Checksum|string|deployment|1|hidden|239
global-settings.package_config.openmpi.url|17|URL|string|deployment|1|hidden|239
global-settings.package_config.openmpi.version|17|Version|string|deployment|1|hidden|239
global-settings.package_config.openpbs.checksum_method|17|Checksum method|string|deployment|1|hidden|240
global-settings.package_config.openpbs.checksum|17|Checksum|string|deployment|1|hidden|240
global-settings.package_config.openpbs.commit|17|Commit|string|deployment|1|hidden|240
global-settings.package_config.openpbs.packages|17|Packages|list|deployment|1|hidden|240
global-settings.package_config.openpbs.repo_url|17|Repo URL|string|deployment|1|hidden|240
global-settings.package_config.openpbs.type|17|Type|string|deployment|1|hidden|240
global-settings.package_config.openpbs.url|17|URL|string|deployment|1|hidden|240
global-settings.package_config.openpbs.version|17|Version|string|deployment|1|hidden|240
global-settings.package_config.prometheus.exporters.node_exporter.linux.aarch64|17|Aarch64|string|deployment|1|hidden|241
global-settings.package_config.prometheus.exporters.node_exporter.linux.x86_64|17|X86 64|string|deployment|1|hidden|241
global-settings.package_config.prometheus.installer.linux.aarch64|17|Aarch64|string|deployment|1|hidden|242
global-settings.package_config.prometheus.installer.linux.x86_64|17|X86 64|string|deployment|1|hidden|242
global-settings.package_config.prometheus.installer.windows.aarch64|17|Aarch64|string|deployment|1|hidden|243
global-settings.package_config.prometheus.installer.windows.x86_64|17|X86 64|string|deployment|1|hidden|243
global-settings.package_config.putty.checksum_method|17|Checksum method|string|deployment|1|hidden|244
global-settings.package_config.putty.checksum|17|Checksum|string|deployment|1|hidden|244
global-settings.package_config.putty.url|17|URL|string|deployment|1|hidden|244
global-settings.package_config.putty.version|17|Version|string|deployment|1|hidden|244
global-settings.package_config.python.checksum_method|17|Checksum method|string|deployment|1|hidden|245
global-settings.package_config.python.checksum|17|Checksum|string|deployment|1|hidden|245
global-settings.package_config.python.url|17|URL|string|deployment|1|hidden|245
global-settings.package_config.python.version|17|Version|string|deployment|1|hidden|245
identity-provider.cognito.administrators_group_name|29|Administrators group name|string|runtime|0|editable|185
identity-provider.cognito.advanced_security_mode|29|Advanced security mode|enum|deployment|0|editable|185
identity-provider.cognito.email_provider|29|Email provider|enum|deployment|0|editable|185
identity-provider.cognito.managers_group_name|29|Managers group name|string|runtime|0|editable|185
identity-provider.cognito.operations_leads_group_name|29|Operations leads group name|string|runtime|0|editable|later
identity-provider.cognito.removal_policy|29|Removal policy|enum|deployment|1|read_only|185
identity-provider.cognito.ses.configuration_set|29|Configuration set|string|deployment|0|editable|186
identity-provider.cognito.ses.from_email|29|From email|string|deployment|0|editable|186
identity-provider.cognito.ses.from_name|29|From name|string|deployment|0|editable|186
identity-provider.cognito.ses.reply_to_address|29|Reply to address|string|deployment|0|editable|186
identity-provider.cognito.ses.ses_region|29|SES region|string|deployment|0|editable|186
identity-provider.provider|29|Provider|enum|deployment|1|read_only|184
metrics.cloudwatch.force_flush_interval|22|Force flush interval|integer|deployment|1|editable|91
metrics.cloudwatch.metrics_collection_interval|22|Metrics collection interval|integer|deployment|1|editable|91
metrics.dogstatsd.url|22|URL|string|restart|0|editable|92
metrics.prometheus.remote_read.url|22|URL|string|restart|0|editable|94
metrics.prometheus.remote_write.queue_config.capacity|22|Capacity|integer|deployment|0|editable|96
metrics.prometheus.remote_write.queue_config.max_samples_per_send|22|Max samples per send|integer|deployment|0|editable|96
metrics.prometheus.remote_write.queue_config.max_shards|22|Max shards|integer|deployment|0|editable|96
metrics.prometheus.remote_write.url|22|URL|string|deployment|0|editable|95
metrics.prometheus.scrape_interval|22|Scrape interval|string|deployment|1|editable|93
metrics.prometheus.scrape_timeout|22|Scrape timeout|string|deployment|1|editable|93
metrics.provider|22|Provider|enum|deployment|0|editable|90
scheduler.base_os|18|Base OS|string|deployment|1|read_only|137
scheduler.bedrock.enabled|2|Bedrock for jobs|boolean|deployment|0|editable|8
scheduler.cache.instance_types_refresh_interval|18|Instance types refresh interval|integer|restart|1|hidden|65
scheduler.cache.long_term.max_size|18|Max size|integer|restart|1|hidden|66
scheduler.cache.long_term.ttl_seconds|18|TTL (seconds)|integer|restart|1|hidden|66
scheduler.cache.short_term.max_size|18|Max size|integer|restart|1|hidden|67
scheduler.cache.short_term.ttl_seconds|18|TTL (seconds)|integer|restart|1|hidden|67
scheduler.cloudwatch_logs.enabled|20|Scheduler CloudWatch Logs|boolean|deployment|0|editable|97
scheduler.compute_node_ami|18|Compute node AMI|string|runtime|0|editable|64
scheduler.compute_node_iam_policy_arns|18|Compute node IAM policy ARNs|list|deployment|0|editable|137
scheduler.compute_node_os|18|Compute node OS|string|runtime|0|editable|64
scheduler.cost_estimation.default_fsx_lustre_size|6|Default Lustre capacity (GB)|integer|runtime|0|editable|26
scheduler.cost_estimation.ebs_gp3_storage|6|EBS gp3 storage (USD/GB-month)|number|runtime|0|editable|26
scheduler.cost_estimation.ebs_io1_storage|6|EBS io1 storage (USD/GB-month)|number|runtime|0|editable|26
scheduler.cost_estimation.ec2_boot_penalty_seconds|6|EC2 boot penalty (seconds)|integer|runtime|0|editable|26
scheduler.cost_estimation.fsx_lustre|6|FSx for Lustre (USD/GB-hour)|number|runtime|0|editable|26
scheduler.cost_estimation.provisioned_iops|6|io1 provisioned IOPS (USD/IOPS-month)|number|runtime|0|editable|26
scheduler.ec2.enable_detailed_monitoring|18|Scheduler detailed monitoring|boolean|deployment|1|read_only|138
scheduler.ec2.enable_termination_protection|18|Scheduler termination protection|boolean|deployment|1|read_only|138
scheduler.ec2.metadata_http_tokens|18|Metadata HTTP tokens|enum|deployment|1|read_only|138
scheduler.efa.max_interfaces|18|Max interfaces|integer|runtime|0|editable|68
scheduler.efa.multi_rail_enabled|18|EFA multi-rail|boolean|runtime|0|editable|68
scheduler.endpoints.external.path_patterns|18|Path patterns|list|deployment|1|hidden|139
scheduler.endpoints.external.priority|18|Priority|integer|deployment|1|hidden|139
scheduler.endpoints.internal.path_patterns|18|Path patterns|list|deployment|1|hidden|140
scheduler.endpoints.internal.priority|18|Priority|integer|deployment|1|hidden|140
scheduler.fair_share.c1|15|C1|integer|restart|0|hidden|69
scheduler.fair_share.c2|15|C2|integer|restart|0|hidden|69
scheduler.fair_share.running_job_penalty|15|Running job penalty|integer|restart|0|hidden|69
scheduler.fair_share.score_type|15|Score type|enum|restart|0|hidden|69
scheduler.fair_share.start_score|15|Start score|integer|restart|0|hidden|69
scheduler.instance_ami|18|Instance AMI|string|deployment|1|read_only|137
scheduler.instance_type|18|Instance type|string|deployment|1|read_only|137
scheduler.job_provisioning.batch_provisioning_wait_interval_seconds|18|Batch provisioning wait interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.dry_run_cache_ttl_seconds|18|Dry run cache TTL (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.finished_job_processing_interval_seconds|18|Finished job processing interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.job_provisioning_interval_seconds|18|Job provisioning interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.job_reconciler_interval_seconds|18|Job reconciler interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.job_reconciler_queued_retry_interval_seconds|18|Job reconciler queued retry interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.job_reconciler_queued_window_seconds|18|Job reconciler queued window (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.job_submission_queue_interval_seconds|18|Job submission queue interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.license_availability_check_timeout_seconds|18|License availability check timeout (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.max_nodes_per_job|18|Maximum nodes per job|integer|runtime|0|editable|70
scheduler.job_provisioning.max_provisioning_retries|18|Maximum provisioning retries|integer|runtime|0|editable|70
scheduler.job_provisioning.mixed_instances_policy.on_demand_allocation_strategy|18|On demand allocation strategy|enum|runtime|1|editable|71
scheduler.job_provisioning.node_housekeeping_interval_seconds|18|Node housekeeping interval (seconds)|integer|runtime|1|hidden|70
scheduler.job_provisioning.node_unavailable_timeout_seconds|18|Node unavailable timeout (seconds)|integer|runtime|1|hidden|later
scheduler.job_provisioning.placement_group.strategy|18|Strategy|enum|runtime|1|editable|72
scheduler.job_provisioning.queue_mode.fair_share.c1|15|C1|number|runtime|1|editable|73
scheduler.job_provisioning.queue_mode.fair_share.c2|15|C2|number|runtime|1|editable|73
scheduler.job_provisioning.queue_mode.fair_share.running_job_penalty|15|Running job penalty|integer|runtime|1|editable|73
scheduler.job_provisioning.queue_mode.fair_share.score_type|15|Score type|enum|runtime|1|editable|73
scheduler.job_provisioning.queue_mode.fair_share.start_score|15|Start score|integer|runtime|1|editable|73
scheduler.job_provisioning.service_quotas|18|Check service quotas for jobs|boolean|runtime|1|editable|70
scheduler.job_provisioning.spot_fleet_request.excess_capacity_termination_policy|18|Excess capacity termination policy|enum|runtime|1|editable|74
scheduler.job_provisioning.spot_fleet_request.instance_interruption_behavior|18|Instance interruption behavior|enum|runtime|1|editable|74
scheduler.job_provisioning.spot_fleet_request.request_type|18|Request type|enum|runtime|1|editable|74
scheduler.job_provisioning.spot_fleet_request.spot_maintenance_strategies|18|Spot maintenance strategies|enum|runtime|1|editable|74
scheduler.job_provisioning.stack_provisioning_timeout_seconds|18|Stack provisioning timeout (seconds)|integer|runtime|1|editable|70
scheduler.logging.default_log_file_name|20|Default log file name|string|restart|1|hidden|98
scheduler.logging.logs_directory|20|Logs directory|string|restart|1|hidden|98
scheduler.logging.profile|20|Profile|string|restart|1|hidden|98
scheduler.notifications.enabled|24|Job notifications|boolean|runtime|0|editable|47
scheduler.notifications.job_completed.email_template|24|Job completed email template|string|runtime|0|editable|48
scheduler.notifications.job_started.email_template|24|Job started email template|string|runtime|0|editable|49
scheduler.opensearch.jobs.index_suffix|3|Node index suffix|string|runtime|1|editable|99
scheduler.opensearch.jobs.number_of_replicas|3|Number of replicas|integer|restart|1|editable|99
scheduler.opensearch.jobs.number_of_shards|3|Number of shards|integer|restart|1|read_only|99
scheduler.opensearch.jobs_index.suffix|3|Job index suffix|string|runtime|1|editable|100
scheduler.opensearch.nodes.number_of_replicas|3|Number of replicas|integer|restart|1|editable|101
scheduler.opensearch.nodes.number_of_shards|3|Number of shards|integer|restart|1|read_only|101
scheduler.provider|18|Provider|enum|deployment|0|hidden|64
scheduler.provisioning_lifecycle_events.enabled|20|Job provisioning events|boolean|runtime|0|editable|102
scheduler.provisioning_lifecycle_events.retention_days|20|Retention (days)|integer|runtime|1|editable|103
scheduler.public|18|Public scheduler access|boolean|deployment|1|read_only|137
scheduler.retain_dns_record|18|Retain DNS record|boolean|deployment|0|hidden|64
scheduler.scheduler_iam_policy_arns|18|Scheduler IAM policy ARNs|list|deployment|0|editable|137
scheduler.scratch_storage.ebs.mount_point|28|EBS scratch mount path|string|runtime|0|editable|75
scheduler.scratch_storage.fsx_lustre.mount_point|28|Lustre scratch mount path|string|runtime|0|editable|76
scheduler.scratch_storage.instance_store.mount_point|28|Instance store scratch mount path|string|runtime|0|editable|77
scheduler.server.enable_http|18|Scheduler HTTP|boolean|restart|1|hidden|141
scheduler.server.enable_metrics|18|Scheduler metrics|boolean|restart|1|hidden|141
scheduler.server.enable_tls|19|Scheduler TLS|boolean|restart|1|read_only|141
scheduler.server.enable_unix_socket|18|Scheduler Unix socket|boolean|restart|1|hidden|141
scheduler.server.graceful_shutdown_timeout|18|Graceful shutdown timeout|integer|restart|1|hidden|141
scheduler.server.hostname|18|Hostname|string|restart|1|hidden|141
scheduler.server.max_workers|18|Max workers|integer|restart|1|hidden|141
scheduler.server.port|18|Port|integer|restart|1|hidden|141
scheduler.server.tls_certificate_file|19|Scheduler TLS certificate path|string|restart|1|read_only|141
scheduler.server.tls_key_file|19|Scheduler TLS private key path|string|restart|1|read_only|141
scheduler.server.unix_socket_file|18|Unix socket file|string|restart|1|hidden|141
scheduler.use_stable_server_name|18|Use stable server name|boolean|deployment|0|hidden|64
scheduler.volume_size|18|Volume size|integer|deployment|1|read_only|137
scheduler.volume_type|18|Volume type|enum|deployment|1|read_only|137
shared-storage.apps.efs.cloudwatch_monitoring|16|apps EFS CloudWatch monitoring|boolean|deployment|1|editable|248
shared-storage.apps.efs.encrypted|16|apps EFS Encrypted|boolean|deployment|1|read_only|248
shared-storage.apps.efs.kms_key_id|16|apps EFS KMS key ID|string|deployment|1|read_only|248
shared-storage.apps.efs.performance_mode|16|apps EFS Performance mode|enum|deployment|1|read_only|248
shared-storage.apps.efs.provisioned_throughput_in_mibps|16|apps EFS Provisioned throughput (MiB/s)|number|deployment|1|editable|248
shared-storage.apps.efs.removal_policy|16|apps EFS Removal policy|enum|deployment|1|read_only|248
shared-storage.apps.efs.throughput_mode|16|apps EFS Throughput mode|enum|deployment|1|editable|248
shared-storage.apps.efs.transition_to_ia|16|apps EFS Transition to ia|string|deployment|1|editable|248
shared-storage.apps.fsx_lustre.deployment_type|16|apps FSx Lustre Deployment type|enum|deployment|1|read_only|249
shared-storage.apps.fsx_lustre.drive_cache_type|16|apps FSx Lustre Drive cache type|enum|deployment|1|read_only|249
shared-storage.apps.fsx_lustre.kms_key_id|16|apps FSx Lustre KMS key ID|string|deployment|1|read_only|249
shared-storage.apps.fsx_lustre.per_unit_storage_throughput|16|apps FSx Lustre Per unit storage throughput|integer|deployment|1|editable|249
shared-storage.apps.fsx_lustre.storage_capacity|16|apps FSx Lustre Storage capacity|integer|deployment|1|editable|249
shared-storage.apps.fsx_lustre.storage_type|16|apps FSx Lustre Storage type|enum|deployment|1|read_only|249
shared-storage.apps.fsx_netapp_ontap.metrics.password_secret_arn|31|apps FSx NetApp ONTAP Password secret ARN|secret|runtime|0|editable|27
shared-storage.apps.fsx_netapp_ontap.metrics.username|31|apps FSx NetApp ONTAP Username|string|runtime|0|editable|27
shared-storage.apps.mount_dir|16|apps Mount dir|string|deployment|0|editable|246
shared-storage.apps.mount_drive|16|apps Mount drive|string|deployment|1|editable|247
shared-storage.apps.mount_options|16|apps Mount options|string|deployment|0|editable|246
shared-storage.apps.provider|16|apps Provider|enum|deployment|1|read_only|246
shared-storage.apps.scope|16|apps Scope|list|deployment|1|read_only|246
shared-storage.apps.title|16|apps Title|string|deployment|0|editable|246
shared-storage.archive.efs.cloudwatch_monitoring|16|archive EFS CloudWatch monitoring|boolean|deployment|1|editable|later
shared-storage.archive.efs.encrypted|16|archive EFS Encrypted|boolean|deployment|1|read_only|later
shared-storage.archive.efs.kms_key_id|16|archive EFS KMS key ID|string|deployment|1|read_only|later
shared-storage.archive.efs.performance_mode|16|archive EFS Performance mode|enum|deployment|1|read_only|later
shared-storage.archive.efs.provisioned_throughput_in_mibps|16|archive EFS Provisioned throughput (MiB/s)|number|deployment|1|editable|later
shared-storage.archive.efs.removal_policy|16|archive EFS Removal policy|enum|deployment|1|read_only|later
shared-storage.archive.efs.throughput_mode|16|archive EFS Throughput mode|enum|deployment|1|editable|later
shared-storage.archive.efs.transition_to_ia|16|archive EFS Transition to ia|string|deployment|1|editable|later
shared-storage.archive.fsx_lustre.deployment_type|16|archive FSx Lustre Deployment type|enum|deployment|1|read_only|later
shared-storage.archive.fsx_lustre.drive_cache_type|16|archive FSx Lustre Drive cache type|enum|deployment|1|read_only|later
shared-storage.archive.fsx_lustre.kms_key_id|16|archive FSx Lustre KMS key ID|string|deployment|1|read_only|later
shared-storage.archive.fsx_lustre.per_unit_storage_throughput|16|archive FSx Lustre Per unit storage throughput|integer|deployment|1|editable|later
shared-storage.archive.fsx_lustre.storage_capacity|16|archive FSx Lustre Storage capacity|integer|deployment|1|editable|later
shared-storage.archive.fsx_lustre.storage_type|16|archive FSx Lustre Storage type|enum|deployment|1|read_only|later
shared-storage.archive.fsx_netapp_ontap.metrics.password_secret_arn|31|archive FSx NetApp ONTAP Password secret ARN|secret|runtime|0|editable|later
shared-storage.archive.fsx_netapp_ontap.metrics.username|31|archive FSx NetApp ONTAP Username|string|runtime|0|editable|later
shared-storage.archive.mount_dir|16|archive Mount dir|string|deployment|0|editable|later
shared-storage.archive.mount_drive|16|archive Mount drive|string|deployment|1|editable|later
shared-storage.archive.mount_options|16|archive Mount options|string|deployment|0|editable|later
shared-storage.archive.provider|16|archive Provider|enum|deployment|1|read_only|later
shared-storage.archive.scope|16|archive Scope|list|deployment|1|read_only|later
shared-storage.archive.title|16|archive Title|string|deployment|0|editable|later
shared-storage.data.efs.cloudwatch_monitoring|16|data EFS CloudWatch monitoring|boolean|deployment|1|editable|252
shared-storage.data.efs.encrypted|16|data EFS Encrypted|boolean|deployment|1|read_only|252
shared-storage.data.efs.kms_key_id|16|data EFS KMS key ID|string|deployment|1|read_only|252
shared-storage.data.efs.performance_mode|16|data EFS Performance mode|enum|deployment|1|read_only|252
shared-storage.data.efs.provisioned_throughput_in_mibps|16|data EFS Provisioned throughput (MiB/s)|number|deployment|1|editable|252
shared-storage.data.efs.removal_policy|16|data EFS Removal policy|enum|deployment|1|read_only|252
shared-storage.data.efs.throughput_mode|16|data EFS Throughput mode|enum|deployment|1|editable|252
shared-storage.data.efs.transition_to_ia|16|data EFS Transition to ia|string|deployment|1|editable|252
shared-storage.data.fsx_lustre.deployment_type|16|data FSx Lustre Deployment type|enum|deployment|1|read_only|253
shared-storage.data.fsx_lustre.drive_cache_type|16|data FSx Lustre Drive cache type|enum|deployment|1|read_only|253
shared-storage.data.fsx_lustre.kms_key_id|16|data FSx Lustre KMS key ID|string|deployment|1|read_only|253
shared-storage.data.fsx_lustre.per_unit_storage_throughput|16|data FSx Lustre Per unit storage throughput|integer|deployment|1|editable|253
shared-storage.data.fsx_lustre.storage_capacity|16|data FSx Lustre Storage capacity|integer|deployment|1|editable|253
shared-storage.data.fsx_lustre.storage_type|16|data FSx Lustre Storage type|enum|deployment|1|read_only|253
shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn|31|data FSx NetApp ONTAP Password secret ARN|secret|runtime|0|editable|28
shared-storage.data.fsx_netapp_ontap.metrics.username|31|data FSx NetApp ONTAP Username|string|runtime|0|editable|28
shared-storage.data.mount_dir|16|data Mount dir|string|deployment|0|editable|250
shared-storage.data.mount_drive|16|data Mount drive|string|deployment|1|editable|251
shared-storage.data.mount_options|16|data Mount options|string|deployment|0|editable|250
shared-storage.data.provider|16|data Provider|enum|deployment|1|read_only|250
shared-storage.data.scope|16|data Scope|list|deployment|1|read_only|250
shared-storage.data.title|16|data Title|string|deployment|0|editable|250
virtual-desktop-controller.bedrock.claude_code.permission_mode|2|Permission mode|enum|runtime|0|editable|10
virtual-desktop-controller.bedrock.enabled|2|Bedrock for desktops|boolean|runtime|0|editable|9
virtual-desktop-controller.cache.long_term.max_size|17|Max size|integer|restart|1|hidden|143
virtual-desktop-controller.cache.long_term.ttl_seconds|17|TTL (seconds)|integer|restart|1|hidden|143
virtual-desktop-controller.cache.short_term.max_size|17|Max size|integer|restart|1|hidden|144
virtual-desktop-controller.cache.short_term.ttl_seconds|17|TTL (seconds)|integer|restart|1|hidden|144
virtual-desktop-controller.cloudwatch_logs.enabled|20|Desktop CloudWatch Logs|boolean|deployment|0|editable|104
virtual-desktop-controller.controller.autoscaling.base_os|17|Base OS|string|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.cooldown_minutes|17|Cooldown (minutes)|integer|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes|17|Estimated instance warmup (minutes)|integer|deployment|1|hidden|147
virtual-desktop-controller.controller.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent|17|Target utilization (%)|integer|deployment|1|hidden|147
virtual-desktop-controller.controller.autoscaling.elb_healthcheck.grace_time_minutes|17|Grace time (minutes)|integer|deployment|1|hidden|148
virtual-desktop-controller.controller.autoscaling.enabled_detailed_monitoring|17|Desktop controller detailed monitoring|boolean|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.instance_ami|17|Instance AMI|string|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.instance_type|17|Instance type|string|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.max_capacity|17|Max capacity|integer|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.metadata_http_tokens|17|Metadata HTTP tokens|enum|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.min_capacity|17|Min capacity|integer|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.new_instances_protected_from_scale_in|17|Desktop controller scale-in protection|boolean|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.public|17|Desktop controller public access|boolean|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.rolling_update_policy.max_batch_size|17|Max batch size|integer|deployment|1|hidden|149
virtual-desktop-controller.controller.autoscaling.rolling_update_policy.min_instances_in_service|17|Min instances in service|integer|deployment|1|hidden|149
virtual-desktop-controller.controller.autoscaling.rolling_update_policy.pause_time_minutes|17|Pause time (minutes)|integer|deployment|1|hidden|149
virtual-desktop-controller.controller.autoscaling.volume_size|17|Volume size|integer|deployment|1|hidden|146
virtual-desktop-controller.controller.autoscaling.volume_type|17|Volume type|enum|deployment|1|hidden|146
virtual-desktop-controller.controller.endpoints.external.path_patterns|17|Path patterns|list|deployment|1|hidden|150
virtual-desktop-controller.controller.endpoints.external.priority|17|Priority|integer|deployment|1|hidden|150
virtual-desktop-controller.controller.endpoints.internal.path_patterns|17|Path patterns|list|deployment|1|hidden|151
virtual-desktop-controller.controller.endpoints.internal.priority|17|Priority|integer|deployment|1|hidden|151
virtual-desktop-controller.controller.enforce_project_budgets|8|Block desktops for projects over budget|boolean|runtime|0|editable|145
virtual-desktop-controller.controller.request_handler_threads.max|17|Max|integer|runtime|0|hidden|152
virtual-desktop-controller.controller.request_handler_threads.min|17|Min|integer|runtime|0|hidden|152
virtual-desktop-controller.dcv_broker.agent_communication_port|17|Agent communication port|integer|deployment|0|hidden|153
virtual-desktop-controller.dcv_broker.autoscaling.base_os|17|Base OS|string|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.cooldown_minutes|17|Cooldown (minutes)|integer|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes|17|Estimated instance warmup (minutes)|integer|deployment|1|hidden|155
virtual-desktop-controller.dcv_broker.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent|17|Target utilization (%)|integer|deployment|1|hidden|155
virtual-desktop-controller.dcv_broker.autoscaling.elb_healthcheck.grace_time_minutes|17|Grace time (minutes)|integer|deployment|1|hidden|156
virtual-desktop-controller.dcv_broker.autoscaling.enabled_detailed_monitoring|17|Desktop broker detailed monitoring|boolean|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.instance_ami|17|Instance AMI|string|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.instance_type|17|Instance type|string|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.max_capacity|17|Max capacity|integer|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.metadata_http_tokens|17|Metadata HTTP tokens|enum|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.min_capacity|17|Min capacity|integer|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.new_instances_protected_from_scale_in|17|Desktop broker scale-in protection|boolean|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.public|17|Desktop broker public access|boolean|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.rolling_update_policy.max_batch_size|17|Max batch size|integer|deployment|1|hidden|157
virtual-desktop-controller.dcv_broker.autoscaling.rolling_update_policy.min_instances_in_service|17|Min instances in service|integer|deployment|1|hidden|157
virtual-desktop-controller.dcv_broker.autoscaling.rolling_update_policy.pause_time_minutes|17|Pause time (minutes)|integer|deployment|1|hidden|157
virtual-desktop-controller.dcv_broker.autoscaling.volume_size|17|Volume size|integer|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.autoscaling.volume_type|17|Volume type|enum|deployment|1|hidden|154
virtual-desktop-controller.dcv_broker.client_communication_port|17|Client communication port|integer|deployment|0|hidden|153
virtual-desktop-controller.dcv_broker.dynamodb_table.autoscaling.enabled|17|Broker table autoscaling|boolean|deployment|1|hidden|159
virtual-desktop-controller.dcv_broker.dynamodb_table.on_demand|17|Broker table on-demand capacity|boolean|deployment|0|hidden|158
virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.max_units|17|Max units|integer|deployment|0|hidden|160
virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.min_units|17|Min units|integer|deployment|0|hidden|160
virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.scale_in_cooldown|17|Scale in cooldown|integer|deployment|0|hidden|160
virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.scale_out_cooldown|17|Scale out cooldown|integer|deployment|0|hidden|160
virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.target_utilization|17|Target utilization|integer|deployment|0|hidden|160
virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.max_units|17|Max units|integer|deployment|0|hidden|161
virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.min_units|17|Min units|integer|deployment|0|hidden|161
virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.scale_in_cooldown|17|Scale in cooldown|integer|deployment|0|hidden|161
virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.scale_out_cooldown|17|Scale out cooldown|integer|deployment|0|hidden|161
virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.target_utilization|17|Target utilization|integer|deployment|0|hidden|161
virtual-desktop-controller.dcv_broker.gateway_communication_port|17|Gateway communication port|integer|deployment|0|hidden|153
virtual-desktop-controller.dcv_broker.session_token_validity|17|Session token validity|integer|deployment|1|read_only|153
virtual-desktop-controller.dcv_broker.ssl_policy|17|SSL policy|string|deployment|1|read_only|153
virtual-desktop-controller.dcv_connection_gateway.autoscaling.base_os|17|Base OS|string|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.cooldown_minutes|17|Cooldown (minutes)|integer|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes|17|Estimated instance warmup (minutes)|integer|deployment|1|hidden|163
virtual-desktop-controller.dcv_connection_gateway.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent|17|Target utilization (%)|integer|deployment|1|hidden|163
virtual-desktop-controller.dcv_connection_gateway.autoscaling.elb_healthcheck.grace_time_minutes|17|Grace time (minutes)|integer|deployment|1|hidden|164
virtual-desktop-controller.dcv_connection_gateway.autoscaling.enabled_detailed_monitoring|17|Desktop gateway detailed monitoring|boolean|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.instance_ami|17|Instance AMI|string|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.instance_type|17|Instance type|string|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.max_capacity|17|Max capacity|integer|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.metadata_http_tokens|17|Metadata HTTP tokens|enum|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.min_capacity|17|Min capacity|integer|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.new_instances_protected_from_scale_in|17|Desktop gateway scale-in protection|boolean|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.public|17|Desktop gateway public access|boolean|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.rolling_update_policy.max_batch_size|17|Max batch size|integer|deployment|1|hidden|165
virtual-desktop-controller.dcv_connection_gateway.autoscaling.rolling_update_policy.min_instances_in_service|17|Min instances in service|integer|deployment|1|hidden|165
virtual-desktop-controller.dcv_connection_gateway.autoscaling.rolling_update_policy.pause_time_minutes|17|Pause time (minutes)|integer|deployment|1|hidden|165
virtual-desktop-controller.dcv_connection_gateway.autoscaling.volume_size|17|Volume size|integer|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.autoscaling.volume_type|17|Volume type|enum|deployment|1|hidden|162
virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn|19|Certificate secret ARN|secret|deployment|0|editable|166
virtual-desktop-controller.dcv_connection_gateway.certificate.custom_dns_name|19|Custom DNS name|string|deployment|0|editable|166
virtual-desktop-controller.dcv_connection_gateway.certificate.private_key_secret_arn|19|Private key secret ARN|secret|deployment|0|editable|166
virtual-desktop-controller.dcv_connection_gateway.certificate.provided|19|Use a provided desktop gateway certificate|boolean|deployment|0|editable|166
virtual-desktop-controller.dcv_session.additional_security_groups|8|Additional security groups|list|runtime|0|editable|29
virtual-desktop-controller.dcv_session.allowed_sessions_per_user|8|Allowed sessions per user|integer|runtime|0|editable|29
virtual-desktop-controller.dcv_session.cpu_utilization_threshold|8|Idle CPU threshold (%)|number|runtime|0|editable|29
virtual-desktop-controller.dcv_session.default_profiles.admin|8|Admin|string|runtime|0|editable|31
virtual-desktop-controller.dcv_session.default_profiles.owner|8|Owner|string|runtime|0|editable|31
virtual-desktop-controller.dcv_session.first_boot_dnf_update|8|Update desktop packages at first boot|boolean|runtime|0|hidden|29
virtual-desktop-controller.dcv_session.history.retention_days|8|Retention (days)|integer|runtime|1|editable|32
virtual-desktop-controller.dcv_session.idle_autostop_delay_max|8|Idle autostop delay max|integer|runtime|0|editable|29
virtual-desktop-controller.dcv_session.idle_autostop_delay|8|Idle autostop delay|integer|runtime|0|editable|29
virtual-desktop-controller.dcv_session.idle_timeout_warning|8|Idle timeout warning|integer|runtime|1|editable|30
virtual-desktop-controller.dcv_session.idle_timeout|8|Idle timeout|integer|runtime|1|editable|30
virtual-desktop-controller.dcv_session.instance_types.allow|7|Allow|list|runtime|0|editable|33
virtual-desktop-controller.dcv_session.instance_types.deny|7|Deny|list|runtime|0|editable|33
virtual-desktop-controller.dcv_session.max_root_volume_memory|8|Max root volume memory|integer|runtime|0|editable|29
virtual-desktop-controller.dcv_session.metadata_http_tokens|8|Metadata HTTP tokens|enum|runtime|0|hidden|29
virtual-desktop-controller.dcv_session.network.private_subnets|7|Private subnets|list|runtime|0|editable|34
virtual-desktop-controller.dcv_session.network.randomize_subnets|7|Randomize desktop subnets|boolean|runtime|0|editable|34
virtual-desktop-controller.dcv_session.network.subnet_autoretry|7|Retry desktop subnet placement|boolean|runtime|0|editable|34
virtual-desktop-controller.dcv_session.notifications.cleanup_warning.email_template|24|Cleanup warning email template|string|runtime|0|editable|50
virtual-desktop-controller.dcv_session.notifications.cleanup_warning.enabled|24|Cleanup warning|boolean|runtime|0|editable|50
virtual-desktop-controller.dcv_session.notifications.creating.email_template|24|Desktop creating email template|string|runtime|0|editable|51
virtual-desktop-controller.dcv_session.notifications.creating.enabled|24|Desktop creating|boolean|runtime|0|editable|51
virtual-desktop-controller.dcv_session.notifications.deleted.email_template|24|Desktop deleted email template|string|runtime|0|editable|52
virtual-desktop-controller.dcv_session.notifications.deleted.enabled|24|Desktop deleted|boolean|runtime|0|editable|52
virtual-desktop-controller.dcv_session.notifications.deleting.email_template|24|Desktop deleting email template|string|runtime|0|editable|53
virtual-desktop-controller.dcv_session.notifications.deleting.enabled|24|Desktop deleting|boolean|runtime|0|editable|53
virtual-desktop-controller.dcv_session.notifications.error.email_template|24|Desktop error email template|string|runtime|0|editable|54
virtual-desktop-controller.dcv_session.notifications.error.enabled|24|Desktop error|boolean|runtime|0|editable|54
virtual-desktop-controller.dcv_session.notifications.initializing.email_template|24|Desktop initializing email template|string|runtime|0|editable|55
virtual-desktop-controller.dcv_session.notifications.initializing.enabled|24|Desktop initializing|boolean|runtime|0|editable|55
virtual-desktop-controller.dcv_session.notifications.provisioning.email_template|24|Desktop provisioning email template|string|runtime|0|editable|56
virtual-desktop-controller.dcv_session.notifications.provisioning.enabled|24|Desktop provisioning|boolean|runtime|0|editable|56
virtual-desktop-controller.dcv_session.notifications.ready.email_template|24|Desktop ready email template|string|runtime|0|editable|57
virtual-desktop-controller.dcv_session.notifications.ready.enabled|24|Desktop ready|boolean|runtime|0|editable|57
virtual-desktop-controller.dcv_session.notifications.resuming.email_template|24|Desktop resuming email template|string|runtime|0|editable|58
virtual-desktop-controller.dcv_session.notifications.resuming.enabled|24|Desktop resuming|boolean|runtime|0|editable|58
virtual-desktop-controller.dcv_session.notifications.session-permission-expired.email_template|24|Session permission expired email template|string|runtime|0|editable|59
virtual-desktop-controller.dcv_session.notifications.session-permission-expired.enabled|24|Session permission expired|boolean|runtime|0|editable|59
virtual-desktop-controller.dcv_session.notifications.session-permission-updated.email_template|24|Session permission updated email template|string|runtime|0|editable|60
virtual-desktop-controller.dcv_session.notifications.session-permission-updated.enabled|24|Session permission updated|boolean|runtime|0|editable|60
virtual-desktop-controller.dcv_session.notifications.session-shared.email_template|24|Session shared email template|string|runtime|0|editable|61
virtual-desktop-controller.dcv_session.notifications.session-shared.enabled|24|Session shared|boolean|runtime|0|editable|61
virtual-desktop-controller.dcv_session.notifications.stopped.email_template|24|Desktop stopped email template|string|runtime|0|editable|62
virtual-desktop-controller.dcv_session.notifications.stopped.enabled|24|Desktop stopped|boolean|runtime|0|editable|62
virtual-desktop-controller.dcv_session.notifications.stopping.email_template|24|Desktop stopping email template|string|runtime|0|editable|63
virtual-desktop-controller.dcv_session.notifications.stopping.enabled|24|Desktop stopping|boolean|runtime|0|editable|63
virtual-desktop-controller.dcv_session.provisioning_timeout_seconds|8|Provisioning timeout (seconds)|integer|runtime|1|editable|30
virtual-desktop-controller.dcv_session.quic_support|8|QUIC support|boolean|deployment|0|editable|29
virtual-desktop-controller.dcv_session.schedule.friday.shut_down_time|9|Friday stop time|string|runtime|0|editable|35
virtual-desktop-controller.dcv_session.schedule.friday.start_up_time|9|Friday start time|string|runtime|0|editable|35
virtual-desktop-controller.dcv_session.schedule.friday.type|9|Friday schedule|enum|runtime|0|editable|35
virtual-desktop-controller.dcv_session.schedule.monday.shut_down_time|9|Monday stop time|string|runtime|0|editable|36
virtual-desktop-controller.dcv_session.schedule.monday.start_up_time|9|Monday start time|string|runtime|0|editable|36
virtual-desktop-controller.dcv_session.schedule.monday.type|9|Monday schedule|enum|runtime|0|editable|36
virtual-desktop-controller.dcv_session.schedule.saturday.shut_down_time|9|Saturday stop time|string|runtime|0|editable|37
virtual-desktop-controller.dcv_session.schedule.saturday.start_up_time|9|Saturday start time|string|runtime|0|editable|37
virtual-desktop-controller.dcv_session.schedule.saturday.type|9|Saturday schedule|enum|runtime|0|editable|37
virtual-desktop-controller.dcv_session.schedule.sunday.shut_down_time|9|Sunday stop time|string|runtime|0|editable|38
virtual-desktop-controller.dcv_session.schedule.sunday.start_up_time|9|Sunday start time|string|runtime|0|editable|38
virtual-desktop-controller.dcv_session.schedule.sunday.type|9|Sunday schedule|enum|runtime|0|editable|38
virtual-desktop-controller.dcv_session.schedule.thursday.shut_down_time|9|Thursday stop time|string|runtime|0|editable|39
virtual-desktop-controller.dcv_session.schedule.thursday.start_up_time|9|Thursday start time|string|runtime|0|editable|39
virtual-desktop-controller.dcv_session.schedule.thursday.type|9|Thursday schedule|enum|runtime|0|editable|39
virtual-desktop-controller.dcv_session.schedule.tuesday.shut_down_time|9|Tuesday stop time|string|runtime|0|editable|40
virtual-desktop-controller.dcv_session.schedule.tuesday.start_up_time|9|Tuesday start time|string|runtime|0|editable|40
virtual-desktop-controller.dcv_session.schedule.tuesday.type|9|Tuesday schedule|enum|runtime|0|editable|40
virtual-desktop-controller.dcv_session.schedule.wednesday.shut_down_time|9|Wednesday stop time|string|runtime|0|editable|41
virtual-desktop-controller.dcv_session.schedule.wednesday.start_up_time|9|Wednesday start time|string|runtime|0|editable|41
virtual-desktop-controller.dcv_session.schedule.wednesday.type|9|Wednesday schedule|enum|runtime|0|editable|41
virtual-desktop-controller.dcv_session.stopped_session_cleanup.dry_run|30|Preview desktop cleanup|boolean|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template|30|Email template|string|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.enabled|30|Stopped desktop cleanup|boolean|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.keep_tags|30|Keep tags|list|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.max_per_pass|30|Max per pass|integer|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.stopped_after_days|30|Stopped after (days)|integer|runtime|0|editable|42
virtual-desktop-controller.dcv_session.stopped_session_cleanup.warn_days_before|30|Warn days before|integer|runtime|0|editable|42
virtual-desktop-controller.dcv_session.validation_timeout_minutes|8|Validation timeout (minutes)|integer|runtime|1|hidden|30
virtual-desktop-controller.dcv_session.working_hours.shut_down_time|9|Shut down time|string|runtime|0|editable|43
virtual-desktop-controller.dcv_session.working_hours.start_up_time|9|Start up time|string|runtime|0|editable|43
virtual-desktop-controller.events.max_receive_count|17|Max receive count|integer|runtime|0|hidden|167
virtual-desktop-controller.external_nlb.access_logs|17|Desktop NLB access logs|boolean|deployment|1|read_only|168
virtual-desktop-controller.instance_storage.mount_point|17|Mount point|string|runtime|0|hidden|44
virtual-desktop-controller.logging.default_log_file_name|20|Default log file name|string|restart|1|hidden|105
virtual-desktop-controller.logging.logs_directory|20|Logs directory|string|restart|1|hidden|105
virtual-desktop-controller.logging.profile|20|Profile|string|restart|1|hidden|105
virtual-desktop-controller.server.enable_http|17|Desktop HTTP|boolean|restart|1|hidden|169
virtual-desktop-controller.server.enable_metrics|17|Desktop metrics|boolean|restart|1|hidden|169
virtual-desktop-controller.server.enable_tls|19|Desktop TLS|boolean|restart|1|read_only|169
virtual-desktop-controller.server.enable_unix_socket|17|Desktop Unix socket|boolean|restart|1|hidden|169
virtual-desktop-controller.server.graceful_shutdown_timeout|17|Graceful shutdown timeout|integer|restart|1|hidden|169
virtual-desktop-controller.server.hostname|17|Hostname|string|restart|1|hidden|169
virtual-desktop-controller.server.max_workers|17|Max workers|integer|restart|1|hidden|169
virtual-desktop-controller.server.port|17|Port|integer|restart|1|hidden|169
virtual-desktop-controller.server.tls_certificate_file|19|Desktop TLS certificate path|string|restart|1|read_only|169
virtual-desktop-controller.server.tls_key_file|19|Desktop TLS private key path|string|restart|1|read_only|169
virtual-desktop-controller.server.unix_socket_file|17|Unix socket file|string|restart|1|hidden|169
virtual-desktop-controller.server.usb_remotization|8|USB device forwarding|list|runtime|1|editable|45
virtual-desktop-controller.vdi_host_backup.backup_plan.rules.default.completion_window_minutes|4|Desktop default: Completion window (minutes)|integer|deployment|0|editable|20
virtual-desktop-controller.vdi_host_backup.backup_plan.rules.default.delete_after_days|4|Desktop default: Delete after (days)|integer|deployment|0|editable|20
virtual-desktop-controller.vdi_host_backup.backup_plan.rules.default.move_to_cold_storage_after_days|4|Desktop default: Move to cold storage after (days)|integer|deployment|0|editable|20
virtual-desktop-controller.vdi_host_backup.backup_plan.rules.default.schedule_expression|4|Desktop default: Schedule expression|string|deployment|0|editable|20
virtual-desktop-controller.vdi_host_backup.backup_plan.rules.default.start_window_minutes|4|Desktop default: Start window (minutes)|integer|deployment|0|editable|20
virtual-desktop-controller.vdi_host_backup.backup_plan.selection.tags|4|Tags|list|deployment|0|editable|21
virtual-desktop-controller.vdi_host_backup.enabled|4|Desktop backups|boolean|deployment|0|editable|19
virtual-desktop-controller.volume_type|17|Volume type|enum|deployment|1|read_only|142
`.trim().split("\n").map(row => {
    const [key, section, label, value_type, effect, advanced, disposition, originalSection] = row.split('|');
    return {...definition(key, Object.entries(SETTINGS_CARDS).find(([, cards]) => cards.includes(fixtureSections[Number(section)]))![0], value_type as SettingDefinition['value_type'], effect as SettingDefinition['effect'], advanced === '1'),
        section: fixtureSections[Number(section)], label, originalSection: originalSection === 'later' ? null : Number(originalSection), read_only: disposition.includes('read_only'), hidden: disposition.startsWith('hidden')};
});

it('accounts for every baseline, later and dynamic field in one destination or hidden disposition', () => {
    const baseline = fieldManifest.filter(setting => setting.originalSection !== null);
    expect(baseline).toHaveLength(753);
    expect(new Set(baseline.map(setting => setting.key)).size).toBe(753);
    expect(new Set(baseline.map(setting => setting.originalSection)).size).toBe(254);
    expect(fieldManifest.length).toBeGreaterThan(baseline.length);
    expect(new Set(fieldManifest.map(setting => setting.key)).size).toBe(fieldManifest.length);
    for (const setting of fieldManifest) {
        expect(Object.values(SETTINGS_CARDS).flat().filter(title => title === setting.section)).toHaveLength(1);
        if (setting.value_type === 'boolean') expect(setting.label).not.toMatch(/\bEnable(?:d)?\b/i);
    }
    expect(fieldManifest.find(setting => setting.key === 'cluster.aws.pricing_region')).toMatchObject({hidden: true, read_only: true, advanced: true});
    expect(fieldManifest.some(setting => setting.key.includes('shared-storage.archive.'))).toBe(true);
    expect(fieldManifest.some(setting => setting.key.includes('.rules.weekly.'))).toBe(true);
    const synthetic = ['Email templates', 'History backfill'];
    for (const title of synthetic) expect(Object.values(SETTINGS_CARDS).flat().filter(card => card === title)).toHaveLength(1);
});

it.each(SETTINGS_GROUPS)('renders only the curated destinations in %s from the complete catalog fixture', async (group) => {
    const context = fullCatalogContext();
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: fieldManifest});
    render(<MemoryRouter initialEntries={[`/cluster/settings/${group}`]}><SettingsGroups pageProps={{}} cluster={{values: {}, sections: [
        {id: 'maintenance', label: 'Maintenance notice', content: <h2>Maintenance notice</h2>},
        {id: 'account-reconciliation', label: 'Account synchronization', content: <h2>Account synchronization</h2>},
    ]}}/></MemoryRouter>);
    await waitFor(() => expect(screen.queryByText('Loading settings…')).not.toBeInTheDocument());
    for (const title of SETTINGS_CARDS[group]) {
        if (title === 'Email templates') expect(screen.getByRole('button', {name: title})).toBeVisible();
        else expect(screen.getAllByRole('heading', {name: new RegExp(`^${title}( Applies.*)?$`)}).length).toBeGreaterThan(0);
    }
    const anchors = Array.from(document.querySelectorAll('[id^="setting-"]')).map(element => element.id);
    expect(new Set(anchors).size).toBe(anchors.length);
    for (const setting of fieldManifest.filter(item => (item.path === 'dcv_session.stopped_session_cleanup.email_template' ? 'email' : item.group) === group && !item.hidden && item.section !== 'Maintenance notice')) {
        if (setting.section === 'Storage measurement' && setting.module === 'shared-storage' && !setting.path.startsWith('archive.')) continue;
        expect(document.getElementById('setting-' + setting.key), setting.key).not.toBeNull();
    }
    expect(screen.queryByText('Backfill history')).not.toBeInTheDocument();
});

it('filters notification rows, masters and reads for a desktop administrator', async () => {
    const context = setup();
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'virtual-desktop-controller');
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: fieldManifest});
    await renderGroups('/virtual-desktop/settings/email', context);
    expect(within(screen.getByRole('table', {name: 'Notifications'})).getAllByRole('row')).toHaveLength(15);
    expect(screen.queryByText('Follows Job notifications')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Email templates'})).not.toBeInTheDocument();
    expect(vi.mocked(context.client().clusterSettings().getModuleSettings).mock.calls.map(([request]) => request.module_id)).toEqual(['vdc']);
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    expect(screen.getAllByRole('checkbox')).toHaveLength(14);
});

it('keeps unknown configured templates visible and rejects the whole notification draft', async () => {
    const context = setup();
    render(<NotificationHarness/>);
    await waitFor(() => expect(screen.getAllByText(/Unknown template: mail/)).toHaveLength(17));
    await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
    await userEvent.click(screen.getAllByRole('checkbox')[0]);
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(await screen.findByText(/choose an existing email template before saving/)).toBeVisible();
    expect(context.client().clusterSettings().updateModuleSettings).not.toHaveBeenCalled();
    expect(screen.getAllByRole('checkbox')[0]).not.toBeChecked();
});


it('opens the legacy email templates route in Notifications', async () => {
    await renderGroups('/cluster/email-templates?project=sample');
    expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/email?project=sample');
    expect(screen.getByRole('button', {name: 'Email templates'})).toBeVisible();
});

function fullCatalogContext() {
    const context = setup();
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: fieldManifest});
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockImplementation(async ({module_id}) => ({settings: module_id === 'shared-storage' ? {
        apps: {provider: 'efs'}, data: {provider: 'fsx_lustre'}, archive: {provider: 'fsx_netapp_ontap', fsx_netapp_ontap: {metrics: {username: '', password_secret_arn: ''}}},
    } : module_id === 'metrics' ? {provider: 'dogstatsd'} : module_id === 'cluster' ? {aws: {region: 'us-west-2', pricing_region: 'us-east-1'}} : {}}));
    vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill').mockResolvedValue({state: 'idle'} as any);
    vi.spyOn(context.client().schedulerAdmin(), 'getJobMetricsBackfill').mockResolvedValue({state: 'idle'} as any);
    return context;
}

it('assembles four Costs cards and groups credentials only for the configured ONTAP attachment', async () => {
    const context = fullCatalogContext();
    await renderGroups('/cluster/settings/cost-collection', context);
    const headings = within(screen.getByRole('region', {name: 'Costs'})).getAllByRole('heading', {level: 2});
    expect(headings.map(node => node.textContent)).toEqual(['Costs', 'Collection Applies after restart', 'Storage measurement Applies after restart', 'Cost estimation', 'History backfill']);
    const credential = document.getElementById('setting-shared-storage.archive.fsx_netapp_ontap.metrics.username')!;
    expect(credential).toBeVisible();
    expect(credential.closest('.settings-subgroup')).toHaveTextContent('archive');
    expect(document.getElementById('setting-shared-storage.apps.fsx_netapp_ontap.metrics.username')).toBeNull();
    expect(document.getElementById('setting-shared-storage.data.fsx_netapp_ontap.metrics.username')).toBeNull();
    expect(screen.getByText(/Grant a read-only SVM REST user/)).toBeVisible();
    expect(screen.queryByRole('textbox', {name: 'Pricing region'})).toBeNull();
    expect(context.client().clusterSettings().getCostMetricsBackfill).toHaveBeenCalledTimes(1);
    expect(context.client().schedulerAdmin().getJobMetricsBackfill).toHaveBeenCalledTimes(1);
    const anchors = Array.from(document.querySelectorAll('[id^="setting-"]')).map(node => node.id);
    expect(new Set(anchors).size).toBe(anchors.length);
});

it('keeps collection order and Advanced while locking other card editors', async () => {
    const context = fullCatalogContext();
    await renderGroups('/cluster/settings/cost-collection', context);
    const edits = screen.getAllByRole('button', {name: 'Edit'});
    await userEvent.click(edits[0]);
    expect(edits.slice(1).every(button => (button as HTMLButtonElement).disabled)).toBe(true);
    const keys = Array.from(document.querySelectorAll('.setting-editor')).slice(0, 6).map(node => node.id.split('.').at(-1));
    expect(keys).toEqual(['enabled', 'lookback_days', 'interval_hours', 'module_tag', 'project_tag', 'owner_tag']);
    expect(screen.getByRole('checkbox', {name: 'Cost collection'})).toBeVisible();
    expect(screen.getAllByRole('button', {name: 'Advanced'})[0]).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(screen.getAllByRole('button', {name: 'Edit'}).every(button => !(button as HTMLButtonElement).disabled)).toBe(true);
});

it('admits jobs-only Costs without requesting cluster collection or credentials', async () => {
    const context = fullCatalogContext();
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'scheduler');
    await renderGroups('/soca/settings/costs', context);
    expect(screen.getByRole('heading', {name: 'Cost estimation'})).toBeVisible();
    expect(screen.getByRole('heading', {name: 'History backfill'})).toBeVisible();
    expect(screen.queryByRole('heading', {name: /^Collection/})).toBeNull();
    expect(context.client().clusterSettings().getCostMetricsBackfill).not.toHaveBeenCalled();
    expect(context.client().clusterSettings().getModuleSettings).toHaveBeenCalledExactlyOnceWith({module_id: 'scheduler'});
});

it('removes unavailable scheduler editors and denies plain users before catalog requests', async () => {
    const context = fullCatalogContext();
    vi.mocked(context.getClusterSettingsService().isSchedulerDeployed).mockReturnValue(false);
    const mounted = await renderGroups('/cluster/settings/cost-collection', context);
    expect(screen.queryByRole('heading', {name: 'Cost estimation'})).toBeNull();
    expect(mounted.client().schedulerAdmin().getJobMetricsBackfill).not.toHaveBeenCalled();
});

it('denies direct Settings content before privileged requests for a plain user', async () => {
    const context = fullCatalogContext();
    vi.mocked(context.auth().isModuleAdmin).mockReturnValue(false);
    await renderGroups('/cluster/settings/cost-collection', context);
    expect(context.client().clusterSettings().describeSettingsCatalog).not.toHaveBeenCalled();
    expect(context.client().clusterSettings().getModuleSettings).not.toHaveBeenCalled();
    expect(context.client().schedulerAdmin().getJobMetricsBackfill).not.toHaveBeenCalled();
});

it('starts backfill only for active Costs and focuses the bookmark card', async () => {
    const context = fullCatalogContext();
    await renderGroups('/cluster/settings/monitoring?operation=backfill-history', context);
    expect(context.client().clusterSettings().getCostMetricsBackfill).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('link', {name: 'History backfill in Costs'}));
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('backfill-history')));
    expect(document.querySelectorAll('#backfill-history')).toHaveLength(1);
});

it.each(['cloudwatch', 'prometheus'])('explains provider limits for %s and keeps storage setup visible', async provider => {
    const context = fullCatalogContext();
    const read = vi.mocked(context.client().clusterSettings().getModuleSettings).getMockImplementation()!;
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockImplementation(request => request.module_id === 'metrics' ? Promise.resolve({settings: {provider}}) : read(request));
    await renderGroups('/cluster/settings/cost-collection', context);
    const edits = screen.getAllByRole('button', {name: 'Edit'});
    expect(edits[0]).toBeDisabled();
    if (provider === 'cloudwatch') expect(edits[1]).toBeEnabled();
    else {expect(edits[1]).toBeDisabled(); expect(screen.getByText(/The current provider does not support measurement/)).toBeVisible();}
});

it('explains a missing ONTAP attachment without exposing EFS or Lustre credentials', async () => {
    const context = fullCatalogContext();
    const read = vi.mocked(context.client().clusterSettings().getModuleSettings).getMockImplementation()!;
    vi.mocked(context.client().clusterSettings().getModuleSettings).mockImplementation(request => request.module_id === 'shared-storage' ? Promise.resolve({settings: {apps: {provider: 'efs'}, data: {provider: 'fsx_lustre'}}}) : read(request));
    await renderGroups('/cluster/settings/cost-collection', context);
    expect(screen.getByText(/No ONTAP attachments configured/)).toBeVisible();
    expect(document.querySelectorAll('[id*="fsx_netapp_ontap.metrics"]')).toHaveLength(0);
    expect(screen.getAllByRole('button', {name: 'Edit'})[1]).toBeDisabled();
});

it('routes cleanup-template search to its single Notifications editor', async () => {
    const context = fullCatalogContext();
    await renderGroups('/cluster/settings/desktop-lifecycle?key=virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template', context);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/email?key=virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template'));
    expect(document.querySelectorAll('[id="setting-virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template"]')).toHaveLength(1);
});

it('keeps a backfill bookmark focused while retaining an unrelated setting key', async () => {
    const context = fullCatalogContext();
    await renderGroups('/cluster/settings/cost-collection?operation=backfill-history&key=cluster.timezone&project=sample', context);
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('backfill-history')));
    expect(screen.getByTestId('location')).toHaveTextContent('/cluster/settings/cost-collection?operation=backfill-history&key=cluster.timezone&project=sample');
});

it('offers both GPU policies once in General without desktop deployment', async () => {
    const context = setup();
    vi.mocked(context.getClusterSettingsService().isVirtualDesktopDeployed).mockReturnValue(false);
    vi.mocked(context.getClusterSettingsService().isModuleEnabled).mockImplementation(module => module !== 'virtual-desktop-controller');
    const policies = ['instance_families', 'fail_on_missing_driver'].map(path => ({...definition(`global-settings.gpu_settings.${path}`, 'general', path === 'instance_families' ? 'list' : 'boolean', 'deployment'), section: 'GPU policy'}));
    vi.mocked(context.client().clusterSettings().describeSettingsCatalog).mockResolvedValue({settings: policies});
    await renderGroups('/cluster/settings/appearance', context);
    expect(screen.getByRole('heading', {name: /^GPU policy/})).toBeVisible();
    expect(SETTINGS_CARDS.appearance).toContain('GPU policy');
    expect(Object.values(SETTINGS_CARDS).flat().filter(card => card === 'GPU policy')).toHaveLength(1);
    for (const policy of policies) expect(screen.getAllByText(policy.label)).toHaveLength(1);
});
