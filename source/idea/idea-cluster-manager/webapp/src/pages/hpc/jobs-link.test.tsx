import {render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {CompletedJobs} from './jobs';
import {initTestAppContext} from '../../test-support';

it('opens a report-linked finished job detail panel beyond the default date range', async () => {
    const context = initTestAppContext();
    const get = vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockImplementation(async request => ({
        listing: [{job_id: '2345', name: 'Protein study', state: 'finished', owner: 'scientist-a',
            queue_time: '2024-01-01T00:00:00Z', start_time: '2024-01-01T00:00:00Z', end_time: '2024-01-01T01:00:00Z',
            params: {cpus: 36, nodes: 1, walltime: '02:00:00'}, execution_hosts: []}],
        paginator: request.paginator
    }));
    const props: any = {ideaPageId: 'completed-jobs', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};
    render(<MemoryRouter initialEntries={['/home/completed-jobs?job_id=2345']}><CompletedJobs {...props}/></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.objectContaining({filters: [{key: 'job_id', value: '2345'}], date_range: undefined})));
    expect(await screen.findByText('JobId: 2345')).toBeInTheDocument();
    expect(screen.getByRole('tab', {name: 'Job Info'})).toBeInTheDocument();
});

it('exports the report-linked job the listing shows', async () => {
    const context = initTestAppContext();
    const get = vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockResolvedValue({listing: [], paginator: {}} as any);
    (URL as any).createObjectURL = vi.fn(() => 'blob:csv');
    (URL as any).revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const props: any = {ideaPageId: 'completed-jobs', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};
    render(<MemoryRouter initialEntries={['/home/completed-jobs?job_id=2345']}><CompletedJobs {...props}/></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalled());
    get.mockClear();
    await userEvent.click(await screen.findByRole('button', {name: 'CSV Export'}));
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(get.mock.calls[0][0]).toMatchObject({filters: [{key: 'job_id', value: '2345'}], date_range: undefined});
});
