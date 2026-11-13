import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {beforeEach, expect, it, vi} from 'vitest';
import ClusterStatus from './cluster-status';
import {initTestAppContext} from '../../test-support';

const props: any = {
    ideaPageId: 'cluster-status', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavItems: [],
    sideNavHeader: {text: '', href: '#/'}, onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []
};
let client: any;
beforeEach(() => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    vi.spyOn(context.auth(), 'getAwsRegion').mockReturnValue('us-east-1');
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSetIds').mockReturnValue([]);
    client = context.client().clusterSettings();
    vi.spyOn(client, 'getModuleSettings').mockResolvedValue({settings: {}});
    vi.spyOn(client, 'listClusterModules').mockResolvedValue({listing: []});
    vi.spyOn(client, 'listClusterHosts').mockResolvedValue({listing: []});
});
const mount = () => render(<MemoryRouter><ClusterStatus {...props}/></MemoryRouter>);

it('says modules failed to load, and refresh clears it', async () => {
    client.listClusterModules.mockRejectedValueOnce({message: 'x'});
    mount();
    expect(await screen.findByText('Modules could not be loaded. Use refresh to try again.')).toBeInTheDocument();
});

it('says hosts failed to load', async () => {
    client.listClusterHosts.mockRejectedValueOnce({message: 'x'});
    mount();
    expect(await screen.findByText('Hosts could not be loaded. Use refresh to try again.')).toBeInTheDocument();
});

it('marks a version it could not read as unknown', async () => {
    client.listClusterHosts.mockResolvedValue({listing: [{InstanceId: 'i-1', State: {Name: 'running'}, Placement: {AvailabilityZone: 'a'}, Tags: [{Key: 'idea:ModuleName', Value: 'cluster-manager'}, {Key: 'Name', Value: 'host-a'}]}]});
    vi.spyOn(client, 'getModuleInfo').mockRejectedValue({message: 'x'});
    mount();
    expect(await screen.findByText('Unknown')).toBeInTheDocument();
});
