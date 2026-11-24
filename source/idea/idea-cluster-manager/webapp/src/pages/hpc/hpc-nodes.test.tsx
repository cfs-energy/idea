import {render, waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import HpcNodes from './hpc-nodes';
import {initTestAppContext} from '../../test-support';

const props: any = {ideaPageId: 'nodes', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};

it('filters nodes on launch time, not on no field at all', async () => {
    const context = initTestAppContext();
    const listNodes = vi.spyOn(context.client().schedulerAdmin(), 'listNodes').mockResolvedValue({listing: []} as any);
    render(<MemoryRouter><HpcNodes {...props}/></MemoryRouter>);
    await waitFor(() => expect(listNodes).toHaveBeenCalled());
    expect(listNodes.mock.calls[0][0].date_range?.key).toBe('launch_time');
    expect(listNodes.mock.calls[0][0].date_range?.start).toBeTruthy();
});
