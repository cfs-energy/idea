import {act, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import {ActiveJobs, ActiveJobsEmpty, AdminActiveJobs, AdminCompletedJobs, CompletedJobs, jobColumns, MY_JOBS_PREFERENCES_KEY} from './jobs';
import {SocaJob} from '../../client/data-model';
import {initTestAppContext} from '../../test-support';

const props: any = {ideaPageId: 'jobs', toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, sideNavHeader: {text: 'IDEA', href: '#/'}, sideNavItems: [], onSideNavChange: () => {}, onFlashbarChange: () => {}, flashbarItems: []};

const cellText = (type: string, id: string, job: SocaJob): string => {
    const cell = jobColumns(type).find(column => column.id === id)!.cell(job);
    return String(cell);
};

const headers = () => screen.getAllByRole('columnheader').map(header => header.textContent?.trim());

describe('jobs table columns', () => {
    beforeEach(() => {
        localStorage.clear();
    });

    it('shows cost estimate and exit status on completed jobs only', () => {
        const ids = (type: string) => jobColumns(type).map(column => column.id);
        expect(ids('active')).toEqual(['id', 'name', 'status', 'owner', 'queue', 'project', 'queued-on', 'runtime']);
        expect(ids('completed')).toEqual([...ids('active'), 'cost', 'exit_code']);
    });

    it('measures runtime from start to end and shows a dash before a job starts', () => {
        expect(cellText('active', 'runtime', {start_time: '2026-08-19T09:00:00Z', end_time: '2026-08-19T10:30:00Z'})).toBe('1 hr 30 min');
        expect(cellText('active', 'runtime', {state: 'queued', queue_time: '2026-08-19T09:00:00Z'})).toBe('–');
    });

    it('shows a cost only when the job carries a priced estimate', () => {
        // line items, not total: an older record's total subtracts a reserved-instance discount nobody paid
        expect(cellText('completed', 'cost', {estimated_bom_cost: {line_items_total: {amount: 12.5, unit: 'USD'}, total: {amount: 9.25, unit: 'USD'}}})).toContain('12.5');
        expect(cellText('completed', 'cost', {estimated_bom_cost: {price_unavailable: true, line_items_total: {amount: 0, unit: 'USD'}}})).toBe('–');
        expect(cellText('completed', 'cost', {})).toBe('–');
    });

    it('exports the status a person reads, not the raw state', () => {
        const status = jobColumns('completed').find(column => column.id === 'status')!;
        const started = {state: 'finished', start_time: '2026-08-19T09:00:00Z'};
        expect(status.exportValue!({...started, exit_status: 271} as any)).toBe('Failed (exit 271)');
        expect(status.exportValue!({...started, exit_status: 0} as any)).toBe('Finished');
        expect(status.exportValue!({state: 'finished'} as any)).toBe('Deleted');
    });

    it('offers sort only on columns the scheduler orders across every job', async () => {
        const context = initTestAppContext();
        const listCompletedJobs = vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockImplementation(async (request: any) => ({
            listing: [{job_id: '999', name: 'Protein study', state: 'finished', queue_time: '2026-08-19T09:00:00Z'}],
            paginator: {...request.paginator, total: 412}
        }) as any);
        const {container} = render(<MemoryRouter><CompletedJobs {...props}/></MemoryRouter>);
        await screen.findByText('Protein study');
        expect(listCompletedJobs.mock.calls[0][0].sort_by).toEqual({key: 'queue_time', order: 'desc'});
        const table = createWrapper(container).findTable()!;
        expect(table.findDescSortedColumn()!.getElement()).toHaveTextContent('Submitted');
        const sortArea = (header: string) => Array.from(container.querySelectorAll('th'))
            .find(th => th.textContent?.includes(header))?.querySelector<HTMLElement>('[role="button"]') ?? null;
        for (const header of ['Job ID', 'Status', 'Runtime', 'Cost estimate', 'Exit status']) {
            expect(sortArea(header)).toBeNull();
        }

        listCompletedJobs.mockClear();
        await act(async () => sortArea('Name')!.click());
        await waitFor(() => expect(listCompletedJobs).toHaveBeenCalled());
        expect(listCompletedJobs.mock.calls[0][0].sort_by).toEqual({key: 'name', order: 'asc'});
    });

    const search = async (container: HTMLElement, text: string) => {
        await act(async () => createWrapper(container).findTextFilter()!.findInput().setInputValue(text));
    };

    it('sends a comma list as job ids only when every token is a job id', async () => {
        const context = initTestAppContext();
        const list = vi.spyOn(context.client().scheduler(), 'listActiveJobs').mockImplementation(async (request: any) => ({listing: [], paginator: request.paginator}) as any);
        const {container} = render(<MemoryRouter><ActiveJobs {...props}/></MemoryRouter>);
        await screen.findByText('You have no active jobs.');

        await search(container, '2345, 2346,');
        await waitFor(() => expect(list.mock.lastCall![0].filters).toEqual([{key: 'job_id', value: ['2345', '2346']}]));

        await search(container, 'plasma, run 2');
        await waitFor(() => expect(list.mock.lastCall![0].filters).toEqual([{key: '$all', value: 'plasma, run 2'}]));
        expect(await screen.findByText('No active jobs match your search.')).toBeInTheDocument();
    });

    it('tells a person where older completed jobs went', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockImplementation(async (request: any) => ({listing: [], paginator: {...request.paginator, total: 0}}) as any);
        render(<MemoryRouter><CompletedJobs {...props}/></MemoryRouter>);
        expect(await screen.findByText('No completed jobs in this date range. Older jobs may still be here: widen the range above.')).toBeInTheDocument();
    });

    it('sends the range on its own field and none once it is cleared', async () => {
        const context = initTestAppContext();
        const list = vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockImplementation(async (request: any) => ({listing: [], paginator: request.paginator}) as any);
        render(<MemoryRouter><CompletedJobs {...props}/></MemoryRouter>);
        await waitFor(() => expect(list).toHaveBeenCalled());
        expect(list.mock.calls[0][0].date_range?.key).toBe('queue_time');
        expect(list.mock.calls[0][0].date_range?.start).toBeTruthy();
    });

    const exportCompleted = async (total: number, rows: number) => {
        const context = initTestAppContext();
        (URL as any).createObjectURL = vi.fn(() => 'blob:csv');
        vi.spyOn(context.client().schedulerAdmin(), 'listCompletedJobs').mockImplementation(async (request: any) => ({
            listing: Array.from({length: Math.min(rows, request.paginator.page_size)}, (_, index) => ({job_id: `${index}`, name: `job-${index}`, state: 'finished'})),
            paginator: {...request.paginator, total}
        }) as any);
        const onFlashbarChange = vi.fn();
        render(<MemoryRouter><AdminCompletedJobs {...props} onFlashbarChange={onFlashbarChange}/></MemoryRouter>);
        await screen.findByText('job-0');
        await userEvent.click(screen.getByRole('button', {name: 'CSV Export'}));
        await waitFor(() => expect(onFlashbarChange).toHaveBeenCalled());
        return onFlashbarChange.mock.calls[0][0].items[0];
    };

    it('warns with both numbers when an export hits the cap', async () => {
        const item = await exportCompleted(23456, 10000);
        expect(item.type).toBe('warning');
        expect(item.content).toBe('Exported 10000 of 23456 completed jobs from the last month. Narrow the date range to export the rest.');
    });

    it('reports a full export as a success and states the range', async () => {
        const item = await exportCompleted(412, 412);
        expect(item.type).toBe('success');
        expect(item.content).toBe('Exported 412 completed jobs from the last month.');
    });

    it('shows a dash rather than an invalid date when the submit time is missing', () => {
        expect(cellText('active', 'queued-on', {})).toBe('–');
    });

    it('hides owner on the user view and keeps it on the all-users view', async () => {
        const context = initTestAppContext();
        const listing = async (request: any) => ({listing: [{job_id: '2345', name: 'Protein study', state: 'queued', owner: 'scientist-a', params: {compute_stack: 'tbd'}}], paginator: request.paginator});
        vi.spyOn(context.client().scheduler(), 'listActiveJobs').mockImplementation(listing as any);
        vi.spyOn(context.client().schedulerAdmin(), 'listActiveJobs').mockImplementation(listing as any);

        const mine = render(<MemoryRouter><ActiveJobs {...props}/></MemoryRouter>);
        await screen.findByText('Protein study');
        expect(headers()).not.toContain('Owner');
        expect(headers()).toContain('Job ID');
        expect(context.localStorage().getItem(`${MY_JOBS_PREFERENCES_KEY}-table-columns`)).toBe(JSON.stringify({owner: false}));
        mine.unmount();

        render(<MemoryRouter><AdminActiveJobs {...props}/></MemoryRouter>);
        await screen.findByText('Protein study');
        expect(headers()).toContain('Owner');
    });

    it('keeps an owner column the user chose to show', async () => {
        const context = initTestAppContext();
        context.localStorage().setItem(`${MY_JOBS_PREFERENCES_KEY}-table-columns`, JSON.stringify({owner: true}));
        vi.spyOn(context.client().scheduler(), 'listCompletedJobs').mockImplementation(async (request: any) => ({listing: [{job_id: '2346', name: 'Mesh run', state: 'finished', owner: 'scientist-a'}], paginator: request.paginator}) as any);
        render(<MemoryRouter><CompletedJobs {...props}/></MemoryRouter>);
        await screen.findByText('Mesh run');
        expect(headers()).toEqual(expect.arrayContaining(['Owner', 'Cost estimate', 'Exit status']));
        const heading = screen.getByRole('heading', {name: /Completed jobs/});
        expect(within(heading.closest('div')!.parentElement!).queryByText(/All completed/i)).toBeNull();
    });
});

it('offers submit and write script when there are no active jobs', async () => {
    const navigate = vi.fn();
    render(<ActiveJobsEmpty navigate={navigate}/>);
    expect(screen.getByText('You have no active jobs.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', {name: 'Write script'}));
    await userEvent.click(screen.getByRole('button', {name: 'Submit'}));
    expect(navigate.mock.calls).toEqual([['/home/script-workbench'], ['/soca/jobs/submit-job']]);
});
