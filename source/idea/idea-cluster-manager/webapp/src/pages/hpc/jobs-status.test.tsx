import {render, screen} from '@testing-library/react';
import {JOB_TABLE_COLUMN_DEFINITIONS} from './jobs';
import {SocaJob} from '../../client/data-model';
import {initTestAppContext} from '../../test-support';

const ERROR_MESSAGE = 'SERVICE_QUOTA_NOT_AVAILABLE: service quota not available for instance types';

const statusCell = JOB_TABLE_COLUMN_DEFINITIONS.find((column) => column.id === 'status')!.cell;

const renderStatus = (job: SocaJob) => render(<>{statusCell(job)}</>);

describe('job status column', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('separates a job that never ran from a clean run', () => {
        // A legacy terminal record with no disposition or start time uses the
        // deleted fallback rather than looking like a successful run.
        const job: SocaJob = {
            job_id: '2345',
            state: 'finished',
            params: {compute_stack: 'idea-compute-node-2345'}
        };
        renderStatus(job);
        expect(screen.getByText('Deleted')).toBeInTheDocument();
        expect(screen.queryByText('Ran')).toBeNull();
    });

    it('offers the reason on a job that never ran', () => {
        const job: SocaJob = {
            job_id: '2346',
            state: 'finished',
            error_message: ERROR_MESSAGE,
            params: {compute_stack: 'tbd'}
        };
        renderStatus(job);
        expect(screen.queryByText('Queued')).toBeNull();
        expect(screen.getByText('Deleted')).toBeInTheDocument();
        expect(screen.getByText(ERROR_MESSAGE)).toBeInTheDocument();
    });

    it('still reports a completed run as ran', () => {
        const job: SocaJob = {
            job_id: '2347',
            state: 'finished',
            exit_status: 0,
            start_time: '2026-08-19T09:00:00Z',
            end_time: '2026-08-19T10:00:00Z',
            params: {compute_stack: 'idea-compute-node-2347'}
        };
        renderStatus(job);
        expect(screen.getByText('Ran')).toBeInTheDocument();
        expect(screen.queryByText('Deleted')).toBeNull();
    });

    it('leaves an active job waiting for capacity as queued', () => {
        const job: SocaJob = {
            job_id: '2348',
            state: 'queued',
            params: {compute_stack: 'tbd'}
        };
        renderStatus(job);
        expect(screen.getByText('Queued')).toBeInTheDocument();
        expect(screen.queryByText('Deleted')).toBeNull();
    });

    it('does not report a deliberate deletion as an error', () => {
        // qdel after the stack was built: terminal, never started, and nothing failed.
        // The red treatment is reserved for failed and held dispositions.
        const markupFor = (job: SocaJob): string => {
            const {container, unmount} = renderStatus(job);
            const markup = container.innerHTML;
            unmount();
            return markup;
        };

        const failed = markupFor({
            job_id: '2350',
            state: 'finished',
            disposition: 'failed',
            error_message: ERROR_MESSAGE,
            params: {compute_stack: 'idea-compute-node-2350'}
        });
        const deleted = markupFor({
            job_id: '2351',
            state: 'finished',
            disposition: 'deleted',
            params: {compute_stack: 'idea-compute-node-2351'}
        });

        expect(failed).toContain('error');
        expect(deleted).not.toContain('error');
    });

    it('reports a held job as held rather than as queued', () => {
        // a job held before any capacity existed still reports compute_stack 'tbd'
        const job: SocaJob = {
            job_id: '2351',
            state: 'held',
            comment: 'IDEA: provisioning failed 3 times - job held',
            status_reason: 'Held after attempt 3 of 3: provisioning failed 3 times.',
            provisioning_attempt: 3,
            max_provisioning_attempts: 3,
            queue_time: '2026-08-19T09:00:00Z',
            params: {compute_stack: 'tbd'}
        };
        renderStatus(job);
        expect(screen.queryByText('Queued')).toBeNull();
        expect(screen.getByText(/Held after attempt 3 of 3: provisioning failed 3 times/)).toBeInTheDocument();
        expect(screen.getByText(/held after 3 of 3 attempts/)).toBeInTheDocument();
    });

    it('leaves a running job alone', () => {
        const job: SocaJob = {
            job_id: '2349',
            state: 'running',
            start_time: '2026-08-19T09:00:00Z',
            params: {compute_stack: 'idea-compute-node-2349'}
        };
        renderStatus(job);
        expect(screen.getByText('Running')).toBeInTheDocument();
    });
});
