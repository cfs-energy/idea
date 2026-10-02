import {render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import {ActiveJobs, ActiveJobsEmpty, AdminActiveJobs, CompletedJobs, jobColumns, MY_JOBS_PREFERENCES_KEY} from './jobs';
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
