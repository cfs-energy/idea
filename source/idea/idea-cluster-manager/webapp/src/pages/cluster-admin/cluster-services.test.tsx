import {render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import ClusterServices from './cluster-services';
import ClusterStatus from './cluster-status';
import {SettingsServiceDetails} from './portal-settings';

vi.mock('../../components/app-layout', () => ({default: (props: any) => <>{props.header}{props.content}</>}));

afterEach(() => vi.restoreAllMocks());

function setup(container = true) {
    const context = initTestAppContext();
    const settings = context.getClusterSettingsService();
    vi.spyOn(settings, 'getModuleId').mockImplementation(name => name === 'ecs' ? container ? 'ecs' : null : name);
    vi.spyOn(settings, 'getModuleInfo').mockReturnValue({name: 'service', module_id: 'service'});
    vi.spyOn(settings, 'getModuleSettings').mockResolvedValue({cluster_name: 'configured'});
    const client = context.client().clusterSettings();
    vi.spyOn(client, 'getModuleInfo').mockResolvedValue({});
    vi.spyOn(client, 'listClusterModules').mockResolvedValue({listing: []});
    vi.spyOn(client, 'listClusterHosts').mockResolvedValue({listing: []});
    vi.spyOn(client, 'listClusterServices').mockResolvedValue({listing: [
        {name: 'scheduler-service-full-name', desired: 2, running: 1, pending: 1, images: ['example/service:26.09.4', 'example/service:26.09.4'], rollout_state: 'IN_PROGRESS', updated_at: '2026-09-21T00:00:00Z', tasks: [{task_id: 'task-a', started_at: '2026-09-21T00:00:00Z', health: 'HEALTHY'}]},
        {name: 'dcv-broker-service-full-name', desired: 1, running: 1, pending: 0, images: ['example/service@sha256:abc']},
        {name: 'additional-service', desired: 0, running: 0, pending: 0, images: ['example/service:first', 'example/service:second'], rollout_state: 'FAILED'},
    ], errors: []});
    return client;
}

it.each([true, false])('shows all services with the tab subset first: desktop=%s', async desktop => {
    const client = setup();
    render(<ClusterServices desktop={desktop}/>);
    expect(await screen.findByText('26.09.4')).toBeVisible();
    expect(screen.getByText('In progress')).toBeVisible();
    expect(screen.getByText('Failed')).toBeVisible();
    expect(screen.getByText('digest…')).toBeVisible();
    expect(screen.getByText('2 tags')).toBeVisible();
    await userEvent.click(screen.getByText('1 task'));
    expect(await screen.findByText('task-a')).toBeVisible();
    expect(screen.getByText('HEALTHY')).toBeVisible();
    expect(screen.getByText('additional-service')).toBeVisible();
    expect(within(screen.getAllByRole('row')[1]).getByText(desktop ? 'broker' : 'scheduler')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(client.listClusterServices).toHaveBeenCalledTimes(2);
});

it('shows partial errors, request errors and recovery', async () => {
    const client = setup();
    vi.mocked(client.listClusterServices).mockResolvedValueOnce({listing: [{name: 'scheduler'}], errors: ['Some tasks could not be read.']}).mockRejectedValueOnce(new Error('private')).mockResolvedValue({listing: [], errors: []});
    render(<ClusterServices desktop={false}/>);
    expect(await screen.findByText('Some tasks could not be read.')).toBeVisible();
    expect(screen.getByText('scheduler')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    expect(await screen.findByText('Could not load services. Refresh to try again.')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Refresh'}));
    await waitFor(() => expect(screen.queryByText('Could not load services. Refresh to try again.')).not.toBeInTheDocument());
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

it.each(['/virtual-desktop/settings?view=services', '/soca/settings?view=service'])('uses live service content at %s', async path => {
    setup();
    render(<MemoryRouter initialEntries={[path]}><SettingsServiceDetails/></MemoryRouter>);
    expect(await screen.findByText('26.09.4')).toBeVisible();
    expect(screen.queryByText(/These are deployment settings/)).not.toBeInTheDocument();
});

it('keeps deployment content without containers', async () => {
    const client = setup(false);
    render(<MemoryRouter initialEntries={['/soca/settings?view=service']}><SettingsServiceDetails/></MemoryRouter>);
    expect(await screen.findByText(/These are deployment settings/)).toBeVisible();
    expect(client.listClusterServices).not.toHaveBeenCalled();
});

it('keeps hosts visible when the host request fails', async () => {
    const client = setup();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(client.listClusterHosts).mockRejectedValue(new Error('Unavailable'));
    render(<MemoryRouter><ClusterStatus {...{} as any}/></MemoryRouter>);
    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(screen.getByText('Infrastructure Hosts')).toBeVisible();
});
