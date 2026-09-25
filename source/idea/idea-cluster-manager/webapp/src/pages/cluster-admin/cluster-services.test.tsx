import {render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {vi} from 'vitest';
import {AppContext} from '../../common';
import {initTestAppContext} from '../../test-support';
import {ServicesPage} from './services-page';
import ClusterStatus from './cluster-status';

vi.mock('../../components/app-layout', () => ({default: (props: any) => <>{props.header}{props.content}</>}));
vi.mock('../virtual-desktops/virtual-desktop-settings', () => ({default: (props: any) => props.renderSections({sections: [
    {id: 'controller', content: <div>Controller deployment details</div>},
    {id: 'broker', content: <div><p>Broker deployment details</p><button>Broker capacity</button><p>Broker network</p></div>},
    {id: 'connection-gateway', content: <div><p>Gateway deployment details</p><button>Gateway capacity</button><p>Gateway network</p></div>},
]})}));

beforeEach(() => {
    // Some neighbouring suites leave spies registered against the app singleton. Restore those
    // before constructing this test's context so its clients and settings service start clean.
    vi.restoreAllMocks();
    initTestAppContext();
});
afterEach(() => vi.restoreAllMocks());
const renderServices = () => render(<MemoryRouter><ServicesPage {...{} as any}/></MemoryRouter>);

function setup(container = true) {
    const context = AppContext.get();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    const settings = context.getClusterSettingsService();
    vi.spyOn(settings, 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(settings, 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(settings, 'getModuleId').mockImplementation(name => name === 'ecs' ? container ? 'ecs' : null : name);
    vi.spyOn(settings, 'getModuleInfo').mockReturnValue({name: 'service', module_id: 'service', version: '26.09.4'});
    vi.spyOn(settings, 'getModuleSettings').mockResolvedValue({container_enabled: true});
    const client = context.client().clusterSettings();
    vi.spyOn(client, 'getModuleInfo').mockResolvedValue({});
    vi.spyOn(client, 'listClusterModules').mockResolvedValue({listing: []});
    vi.spyOn(client, 'listClusterHosts').mockResolvedValue({listing: []});
    vi.spyOn(client, 'listClusterServices').mockResolvedValue({listing: [
        {name: 'scheduler', desired: 2, running: 1, pending: 1, images: ['example/service:26.09.4', 'example/service:26.09.4'], rollout_state: 'IN_PROGRESS', updated_at: '2026-09-21T00:00:00Z', tasks: [{task_id: 'task-a', started_at: '2026-09-21T00:00:00Z', health: 'HEALTHY'}]},
        {name: 'dcv-broker', desired: 1, running: 1, pending: 0, images: ['example/service@sha256:abc']},
        {name: 'additional-service', desired: 0, running: 0, pending: 0, images: ['example/service:first', 'example/service:second'], rollout_state: 'FAILED'},
    ], errors: []});
    return client;
}

it('partitions one inventory and refreshes it once while preserving service details', async () => {
    const client = setup();
    renderServices();
    expect(await screen.findByText('26.09.4')).toBeVisible();
    expect(client.listClusterServices).toHaveBeenCalledOnce();
    expect(screen.getAllByRole('button', {name: 'Refresh'})).toHaveLength(1);
    expect(screen.queryByRole('button', {name: /capacity/})).not.toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeVisible();
    expect(screen.getByText('Failed')).toBeVisible();
    expect(screen.getByText('digest…')).toBeVisible();
    expect(screen.getByText('2 tags')).toBeVisible();
    await userEvent.click(screen.getByText('1 task'));
    expect(await screen.findByText('task-a')).toBeVisible();
    expect(screen.getByText('HEALTHY')).toBeVisible();
    await userEvent.click(screen.getByText('2 tags'));
    expect(await screen.findByText('example/service:first')).toBeVisible();
    expect(screen.getByText('example/service:second')).toBeVisible();
    const tables = screen.getAllByRole('table');
    expect(tables).toHaveLength(3);
    expect(within(tables[0]).getByText('additional-service')).toBeVisible();
    expect(within(tables[1]).getByText('broker')).toBeVisible();
    expect(within(tables[2]).getByText('scheduler')).toBeVisible();
    expect(screen.getAllByText('additional-service')).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(client.listClusterServices).toHaveBeenCalledTimes(2);
});

it('keeps every authorized role once and filters only Datadog identities', async () => {
    const client = setup();
    const names = ['cluster-manager', 'bastion', 'virtual-desktop-controller', 'dcv-broker', 'dcv-connection-gateway', 'scheduler', 'additional-service', 'monitor', 'agent', 'other-daemon', 'datadog-service-helper'];
    vi.mocked(client.listClusterServices).mockResolvedValue({listing: [
        ...names.map(name => ({name})),
        {name: 'datadogserviceServiceA1B2C3D4-random'}, {name: 'datadogserviceA1B2C3D4-random'}, {name: 'datadog-service'},
        {name: 'telemetry', images: ['public.ecr.aws/datadog/agent:7']},
        {name: 'telemetry-digest', images: ['gcr.io/datadog/agent@sha256:abc']},
        {name: 'other-image', images: ['example/datadog-agent:7']},
        {name: 'application-with-sidecar', images: ['example/service:current', 'datadog/agent:7']},
    ]});
    renderServices();
    await screen.findByText('other-daemon');
    const tables = screen.getAllByRole('table');
    expect(within(tables[0]).getAllByRole('row')).toHaveLength(10);
    expect(within(tables[1]).getAllByRole('row')).toHaveLength(4);
    expect(within(tables[2]).getAllByRole('row')).toHaveLength(2);
    for (const label of ['cluster manager', 'bastion', 'controller', 'broker', 'gateway', 'scheduler', 'additional-service', 'monitor', 'agent', 'other-daemon', 'datadog-service-helper', 'other-image', 'application-with-sidecar']) expect(screen.getAllByText(label)).toHaveLength(1);
    expect(screen.queryByText(/datadogservice|^datadog-service$|^telemetry/)).not.toBeInTheDocument();
});

it.each(['desktop', 'jobs', 'cluster', 'none'])('gates groups for %s administrators', async role => {
    const client = setup();
    vi.mocked(AppContext.get().auth().isModuleAdmin).mockImplementation(module => module === ({desktop: 'virtual-desktop-controller', jobs: 'scheduler', cluster: 'cluster-manager'} as Record<string, string>)[role]);
    renderServices();
    if (role === 'none') {
        expect(screen.getByText('No services available.')).toBeVisible();
        expect(client.listClusterServices).not.toHaveBeenCalled();
        return;
    }
    await waitFor(() => expect(client.listClusterServices).toHaveBeenCalledOnce());
    for (const [name, authorized] of [['Control plane', 'cluster'], ['Desktop services', 'desktop'], ['Job service', 'jobs']]) expect(screen.queryByRole('heading', {name}) !== null).toBe(role === authorized);
});

it('hides absent modules and their rows', async () => {
    setup();
    vi.mocked(AppContext.get().getClusterSettingsService().isVirtualDesktopDeployed).mockReturnValue(false);
    vi.mocked(AppContext.get().getClusterSettingsService().isSchedulerDeployed).mockReturnValue(false);
    renderServices();
    await screen.findByText('additional-service');
    expect(screen.getAllByRole('table')).toHaveLength(1);
    expect(screen.queryByText('broker')).not.toBeInTheDocument();
    expect(screen.queryByText('scheduler')).not.toBeInTheDocument();
});

it('shows loading then empty groups', async () => {
    const client = setup();
    let resolve!: (value: {listing: []}) => void;
    vi.mocked(client.listClusterServices).mockReturnValue(new Promise(done => { resolve = done; }));
    renderServices();
    expect(screen.getByText('Loading service settings')).toBeVisible();
    await screen.findAllByText('Loading services');
    expect(screen.getByRole('button', {name: 'Refresh'})).toBeDisabled();
    resolve({listing: []});
    expect(await screen.findAllByText('No services found.')).toHaveLength(3);
});

it('shows partial errors, request errors and recovery', async () => {
    const client = setup();
    vi.mocked(client.listClusterServices).mockResolvedValueOnce({listing: [{name: 'scheduler'}], errors: ['Some tasks could not be read.']}).mockRejectedValueOnce(new Error('private')).mockResolvedValue({listing: [], errors: []});
    renderServices();
    expect(await screen.findByText('Some tasks could not be read.')).toBeVisible();
    expect(screen.getByText('scheduler')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(await screen.findByText('Could not load services. Refresh to try again.')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    await waitFor(() => expect(screen.queryByText('Could not load services. Refresh to try again.')).not.toBeInTheDocument());
});

it.each(['absent', 'empty', 'failure'])('avoids ECS inventory when settings are %s', async mode => {
    const client = setup(mode !== 'absent');
    const settings = AppContext.get().getClusterSettingsService();
    if (mode === 'empty') vi.mocked(settings.getModuleSettings).mockResolvedValue({});
    if (mode === 'failure') vi.mocked(settings.getModuleSettings).mockRejectedValue(new Error('Unavailable'));
    renderServices();
    if (mode === 'failure') expect(await screen.findByText('Could not read service settings. Reload the page to try again.')).toBeVisible();
    else {
        expect(await screen.findByText(/These are deployment settings/)).toBeVisible();
        for (const label of ['Controller deployment details', 'Broker deployment details', 'Gateway deployment details']) expect(screen.getAllByText(label)).toHaveLength(1);
        expect(screen.getAllByRole('button', {name: 'Broker capacity'})).toHaveLength(1);
        expect(screen.getAllByRole('button', {name: 'Gateway capacity'})).toHaveLength(1);
        expect(screen.getByRole('link', {name: /CLI runbooks/})).toHaveAttribute('href', 'https://docs.idea-hpc.com/first-time-users/cluster-operations');
        expect(screen.queryByText('Broker network')).not.toBeInTheDocument();
    }
    expect(client.listClusterServices).not.toHaveBeenCalled();
    if (mode === 'absent') expect(settings.getModuleSettings).not.toHaveBeenCalled();
});

it.each([true, false])('hides empty hosts only for container clusters: %s', async container => {
    const client = setup(container);
    render(<MemoryRouter><ClusterStatus {...{} as any}/></MemoryRouter>);
    await waitFor(() => expect(client.listClusterHosts).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText('Infrastructure Hosts') !== null).toBe(!container));
});

it('keeps nonempty infrastructure hosts on container clusters', async () => {
    const client = setup();
    vi.mocked(client.listClusterHosts).mockResolvedValue({listing: [{InstanceId: 'i-example', State: {Name: 'running'}, Placement: {AvailabilityZone: 'example'}, Tags: [{Key: 'idea:ModuleName', Value: 'cluster-manager'}]}]});
    render(<MemoryRouter><ClusterStatus {...{} as any}/></MemoryRouter>);
    await waitFor(() => expect(client.listClusterHosts).toHaveBeenCalled());
    expect(await screen.findByText('Infrastructure Hosts')).toBeVisible();
});

it('keeps hosts visible when the host request fails', async () => {
    const client = setup();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(client.listClusterHosts).mockRejectedValue(new Error('Unavailable'));
    render(<MemoryRouter><ClusterStatus {...{} as any}/></MemoryRouter>);
    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(screen.getByText('Infrastructure Hosts')).toBeVisible();
});

it.each(['desktop', 'jobs'])('uses the authorized container capability for %s administrators', async role => {
    const client = setup();
    vi.mocked(AppContext.get().auth().isModuleAdmin).mockImplementation(module => module === (role === 'desktop' ? 'virtual-desktop-controller' : 'scheduler'));
    vi.mocked(AppContext.get().getClusterSettingsService().getModuleSettings).mockResolvedValue({container_enabled: true});
    renderServices();
    expect(await screen.findByText('Live services')).toBeVisible();
    expect(client.listClusterServices).toHaveBeenCalledOnce();
});

it.each(['broker-lab', 'scheduler-lab', 'gateway-lab', 'controller-lab'])('groups configured module identities independently of prefix %s', async prefix => {
    const client = setup();
    const context = AppContext.get();
    vi.spyOn(context.auth(), 'getClusterName').mockReturnValue(prefix);
    vi.mocked(context.getClusterSettingsService().getModuleId).mockImplementation(name => ({'cluster-manager': 'portal', scheduler: 'batch', 'virtual-desktop-controller': 'desk'} as Record<string, string>)[name] ?? name);
    vi.mocked(client.listClusterServices).mockResolvedValue({listing: ['portal', 'batch', 'desk-controller', 'desk-broker', 'desk-gateway'].map(name => ({name: `${prefix}-${name}`}))});
    renderServices();
    await waitFor(() => expect(client.listClusterServices).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryAllByText('Loading services')).toHaveLength(0));
    const tables = screen.getAllByRole('table');
    expect(within(tables[0]).getByText('cluster manager')).toBeVisible();
    expect(within(tables[1]).getByText('controller')).toBeVisible();
    expect(within(tables[1]).getByText('broker')).toBeVisible();
    expect(within(tables[1]).getByText('gateway')).toBeVisible();
    expect(within(tables[2]).getByText('scheduler')).toBeVisible();
});
