import {buildBudgetUsage} from '../../components/job-budget-usage';
import {render, screen} from '@testing-library/react';
import {JobInfo, JobWaitingSignals, JobStatus, JobCosts, JobBudgetImpact} from './jobs';
import {SocaJob} from '../../client/data-model';
import {initTestAppContext} from '../../test-support';

const NOW = new Date('2026-08-19T12:00:00Z');

// KeyValue renders <div><Box>{title}</Box>{value}</div>, so the value is what is
// left of the wrapper's text once the title is removed
const valueOf = (title: string): string => {
    const label = screen.getByText(title);
    const text = label.parentElement?.textContent ?? '';
    return text.replace(title, '').trim();
};

describe('job info panel', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('shows the error message on a job that already has a compute stack', () => {
        // the status-column popover only fires while compute_stack is 'tbd', so a
        // job that got a stack and then failed used to show no reason at all
        const job: SocaJob = {
            job_id: '2345',
            state: 'held',
            error_message: 'SERVICE_QUOTA_NOT_AVAILABLE: service quota not available for instance types',
            params: {compute_stack: 'idea-compute-node-2345'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(screen.getByText(/SERVICE_QUOTA_NOT_AVAILABLE/)).toBeInTheDocument();
    });

    it('omits the error message row when the job has no error', () => {
        render(<JobInfo job={{job_id: '2346', state: 'running'}} now={NOW}/>);
        expect(screen.queryByText('Error Message')).toBeNull();
    });

    it('reports a queued job as not started and shows how long it has waited', () => {
        const job: SocaJob = {
            job_id: '2347',
            state: 'queued',
            queue_time: '2026-08-19T11:15:00Z',
            params: {walltime: '05:00:00', compute_stack: 'tbd'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('Not started');
        expect(valueOf('Queued For')).toBe('45 min');
        expect(valueOf('Requested Walltime')).toBe('05:00:00');
        // requested walltime must never be rendered as time the job has used
        expect(valueOf('Total Time')).toBe('-');
    });

    it('reports elapsed against requested for a running job, in minutes', () => {
        const job: SocaJob = {
            job_id: '2348',
            state: 'running',
            queue_time: '2026-08-19T10:00:00Z',
            start_time: '2026-08-19T10:30:00Z',
            params: {walltime: '05:00:00'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('1 hr 30 min of 5 hr requested');
        expect(valueOf('Queued For')).toBe('30 min');
    });

    it('bounds a record for a job that never ran, whenever it is opened', () => {
        // total_time_secs is 0 on these records: rendering it would read as
        // "less than 1 min" beside an "Elapsed vs Requested" of "Not started"
        const job: SocaJob = {
            job_id: '2350',
            state: 'finished',
            queue_time: '2026-08-19T10:00:00Z',
            end_time: '2026-08-19T10:12:00Z',
            total_time_secs: 0,
            params: {walltime: '05:00:00', compute_stack: 'tbd'}
        };
        render(<JobInfo job={job} now={new Date('2026-09-06T12:00:00Z')}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('Not started');
        expect(valueOf('Total Time')).toBe('-');
        // 12 min, not the 18 days since the job was recorded
        expect(valueOf('Queued For')).toBe('12 min');
    });

    it('renders exit status and recorded total time for a completed job', () => {
        const job: SocaJob = {
            job_id: '2349',
            state: 'finished',
            exit_status: 0,
            start_time: '2026-08-19T09:00:00Z',
            end_time: '2026-08-19T10:02:00Z',
            total_time_secs: 3720,
            provisioning_time: '2026-08-19T08:50:00Z',
            params: {walltime: '05:00:00'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Exit Status')).toBe('0');
        expect(valueOf('Total Time')).toBe('1 hr 2 min');
        expect(valueOf('Elapsed vs Requested')).toBe('1 hr 2 min of 5 hr requested');
        expect(valueOf('Provisioning Time')).not.toBe('-');
    });
});

describe('job waiting signals', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    const queuedJob = (overrides: Partial<SocaJob> = {}): SocaJob => ({
        job_id: '2345',
        state: 'queued',
        queue_time: '2026-08-19T11:15:00Z',
        params: {compute_stack: 'tbd'},
        ...overrides
    });

    it('tells a queued job owner how long it has waited and which attempt it is on', () => {
        render(<JobWaitingSignals job={queuedJob({
            provisioning_attempt: 2,
            max_provisioning_attempts: 3
        })} now={NOW}/>);
        expect(screen.getByText(/waiting 45 min/)).toBeInTheDocument();
        expect(screen.getByText(/attempt 2 of 3/)).toBeInTheDocument();
    });

    it('names the queue limit holding the job, and nothing else about the queue', () => {
        const {container} = render(<JobWaitingSignals job={queuedJob({
            blocking_limit_type: 'max_provisioned_instances'
        })} now={NOW}/>);
        // asserted over the whole rendered text: a threshold or a usage count added to
        // the signal would have to show up here
        expect(container.textContent).toBe('waiting 45 min \u00b7 queue limit: max_provisioned_instances');
    });

    it('renders nothing for a running job', () => {
        const {container} = render(<JobWaitingSignals job={{
            job_id: '2346',
            state: 'running',
            queue_time: '2026-08-19T10:00:00Z',
            start_time: '2026-08-19T10:30:00Z'
        }} now={NOW}/>);
        expect(container).toBeEmptyDOMElement();
    });

    it('shows the attempt and the blocking limit in the job info panel', () => {
        render(<JobInfo job={queuedJob({
            provisioning_attempt: 3,
            max_provisioning_attempts: 3,
            blocking_limit_type: 'max_running_jobs'
        })} now={NOW}/>);
        expect(valueOf('Provisioning Attempt')).toBe('attempt 3 of 3');
        expect(valueOf('Blocking Queue Limit')).toBe('max_running_jobs');
    });

    it('omits both rows on a job that is not waiting to be provisioned', () => {
        render(<JobInfo job={{job_id: '2347', state: 'running', start_time: '2026-08-19T11:00:00Z'}} now={NOW}/>);
        expect(screen.queryByText('Provisioning Attempt')).toBeNull();
        expect(screen.queryByText('Blocking Queue Limit')).toBeNull();
    });
});

describe('job status and costs', () => {
    beforeEach(() => initTestAppContext());

    it('shows the scheduler reason for a held job without a command prompt', () => {
        render(<JobStatus job={{state: 'held', status_reason: 'Held after attempt 3 of 3: Capacity unavailable.', comment: 'See qstat -f'}}/>);
        expect(screen.getByText('Held after attempt 3 of 3: Capacity unavailable.')).toBeInTheDocument();
        expect(screen.queryByText(/qstat/)).toBeNull();
    });

    it.each(['ran', 'failed', 'held', 'deleted'] as const)('shows the %s disposition', disposition => {
        render(<JobStatus job={{state: 'finished', disposition, status_reason: 'Recorded outcome.'}}/>);
        expect(screen.getByText(disposition[0].toUpperCase() + disposition.slice(1))).toBeInTheDocument();
        expect(screen.getByText('Recorded outcome.')).toBeInTheDocument();
    });

    it('shows the status reason and disposition in detail', () => {
        render(<JobInfo job={{state: 'finished', disposition: 'deleted', status_reason: 'Cancelled by the owner.'}} now={NOW}/>);
        expect(valueOf('Status Reason')).toBe('Cancelled by the owner.');
        expect(valueOf('Disposition')).toBe('deleted');
    });

    it('does not show an incomplete cost total as a price', () => {
        render(<JobCosts job={{estimated_bom_cost: {price_unavailable: true, total: {amount: 123.45}}}}/>);
        expect(screen.getByText('Price not available')).toBeInTheDocument();
        expect(screen.queryByText(/123.45/)).toBeNull();
    });

    it('shows recorded savings and the estimated total', () => {
        render(<JobCosts job={{estimated_bom_cost: {total: {amount: 12}, savings_total: {amount: 3}, savings: [{title: 'Spot savings', total_price: {amount: 3}}]}}}/>);
        expect(screen.getByText(/Estimated savings:/)).toBeInTheDocument();
        expect(screen.getByText('Spot savings')).toBeInTheDocument();
        expect(screen.getByText(/12.00/)).toBeInTheDocument();
    });

    it('shows recorded budget usage with the shared budget display', () => {
        render(buildBudgetUsage({budget_name: 'Compute budget', budget_limit: {amount: 100}, actual_spend: {amount: 20}, forecasted_spend: {amount: 30}, job_usage_percent: 4}));
        expect(screen.getByText('Compute budget')).toBeInTheDocument();
        expect(screen.getByText('4.00%')).toBeInTheDocument();
    });
});


describe('job budget impact', () => {
    beforeEach(() => initTestAppContext());

    it.each([undefined, {price_unavailable: true}])('hides budget percentages without a price: %j', cost => {
        render(<JobBudgetImpact job={{estimated_bom_cost: cost, estimated_budget_usage: {job_usage_percent: 4}}}/>);
        expect(screen.getByText('Price not available')).toBeInTheDocument();
        expect(screen.queryByText('4.00%')).toBeNull();
    });

    it('shows budget percentages with an available price', () => {
        render(<JobBudgetImpact job={{estimated_bom_cost: {total: {amount: 4}}, estimated_budget_usage: {job_usage_percent: 4}}}/>);
        expect(screen.getByText('4.00%')).toBeInTheDocument();
    });
});
