import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MetricsHistorySettings} from './metrics-history-settings';
import {initTestAppContext} from '../../test-support';
import {MetricsBackfillStatus} from '../../client/metrics-backfill';

const status: MetricsBackfillStatus = {state: 'completed', jobs_scanned: 7, points_built: 14, points_sent: 14, points_skipped: 2, errors: 1, started_at: null, finished_at: null, dry_run: false, last_error: null};

function setup() {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
    const jobs = vi.spyOn(context.client().schedulerAdmin(), 'getJobMetricsBackfill').mockResolvedValue(status);
    const cost = vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill').mockResolvedValue({...status, errors: 0});
    const runJobs = vi.spyOn(context.client().schedulerAdmin(), 'backfillJobMetrics').mockResolvedValue({...status, state: 'running'});
    const runCost = vi.spyOn(context.client().clusterSettings(), 'backfillCostMetrics').mockResolvedValue({...status, state: 'running'});
    return {context, jobs, cost, runJobs, runCost, ...render(<MetricsHistorySettings active/>)};
}

describe('operations metrics history', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads both statuses on open, shows counts, and starts both with the selected defaults', async () => {
        const {jobs, cost, runJobs, runCost} = setup();
        await screen.findByText(/Jobs: completed, 7 scanned, 14 points built, 14 sent, 2 skipped, 1 errors/);
        expect(jobs).toHaveBeenCalledOnce();
        expect(cost).toHaveBeenCalledOnce();
        expect(screen.getByRole('checkbox', {name: 'Dry run'})).toBeChecked();
        expect(screen.queryByRole('textbox', {name: 'Cost days'})).not.toBeInTheDocument();
        expect(screen.getByRole('textbox', {name: 'End date (UTC, inclusive)'})).toHaveValue(new Date().toISOString().slice(0, 10).replaceAll('-', '/'));
        await userEvent.type(screen.getByRole('textbox', {name: 'Start date (UTC)'}), '2026-09-01');
        await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
        expect(screen.getByRole('textbox', {name: 'Cost days'})).toHaveValue('400');
        await userEvent.click(screen.getByRole('button', {name: 'Run jobs'}));
        await waitFor(() => expect(runJobs).toHaveBeenCalledWith({start_date: '2026-09-01', end_date: new Date().toISOString().slice(0, 10), dry_run: true}));
        await userEvent.click(screen.getByRole('button', {name: 'Run cost'}));
        expect(runCost).toHaveBeenCalledWith({days: 400, dry_run: true});
        expect(screen.getByRole('button', {name: 'Run jobs'})).toBeDisabled();
    });

    it('refreshes on reopening and shows a partial start failure', async () => {
        const {jobs, runJobs, runCost, rerender} = setup();
        await screen.findByText(/Jobs: completed/);
        rerender(<MetricsHistorySettings active={false}/>);
        rerender(<MetricsHistorySettings active/>);
        await waitFor(() => expect(jobs).toHaveBeenCalledTimes(2));
        runCost.mockRejectedValue(new Error('Cost unavailable'));
        await userEvent.type(screen.getByRole('textbox', {name: 'Start date (UTC)'}), '2026-09-01');
        await userEvent.click(screen.getByRole('checkbox', {name: 'Dry run'}));
        await userEvent.click(screen.getByRole('button', {name: 'Advanced'}));
        await userEvent.clear(screen.getByRole('textbox', {name: 'Cost days'}));
        await userEvent.type(screen.getByRole('textbox', {name: 'Cost days'}), '30');
        await userEvent.click(screen.getByRole('button', {name: 'Run jobs'}));
        await userEvent.click(screen.getByRole('button', {name: 'Run cost'}));
        expect(runCost).toHaveBeenCalledWith({days: 30, dry_run: false});
        expect(runJobs).toHaveBeenCalledWith(expect.objectContaining({dry_run: false}));
        expect(await screen.findByText('Cost: Cost unavailable')).toBeInTheDocument();
        expect(screen.getByText(/Jobs: running/)).toBeInTheDocument();
    });

    it.each(['jobs', 'cost'] as const)('isolates a failed %s status from the other half', async kind => {
        const setupResult = setup();
        await screen.findByText(/Jobs: completed/);
        setupResult[kind].mockRejectedValue(new Error('Status unavailable'));
        setupResult.rerender(<MetricsHistorySettings active={false}/>);
        setupResult.rerender(<MetricsHistorySettings active/>);
        await screen.findByText(`${kind === 'jobs' ? 'Jobs' : 'Cost'}: Status unavailable`);
        await userEvent.type(screen.getByRole('textbox', {name: 'Start date (UTC)'}), '2026-09-01');
        expect(screen.getByRole('button', {name: `Run ${kind}`})).toBeDisabled();
        const other = kind === 'jobs' ? 'cost' : 'jobs';
        await userEvent.click(screen.getByRole('button', {name: `Run ${other}`}));
        expect(kind === 'jobs' ? setupResult.runCost : setupResult.runJobs).toHaveBeenCalledOnce();
        expect(kind === 'jobs' ? setupResult.runJobs : setupResult.runCost).not.toHaveBeenCalled();
    });

    it.each(['unauthorized', 'absent'])('allows cost when scheduler is %s', async reason => {
        const {context, jobs, runCost, rerender} = setup();
        await screen.findByText(/Jobs: completed/);
        jobs.mockClear();
        if (reason === 'absent') vi.mocked(context.getClusterSettingsService().isSchedulerDeployed).mockReturnValue(false);
        else vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'cluster-manager');
        rerender(<MetricsHistorySettings active={false}/>);
        rerender(<MetricsHistorySettings active/>);
        await waitFor(() => expect(screen.getByRole('button', {name: 'Run cost'})).toBeEnabled());
        expect(screen.queryByRole('button', {name: 'Run jobs'})).not.toBeInTheDocument();
        expect(screen.queryByText(/Jobs:/)).not.toBeInTheDocument();
        expect(screen.queryByRole('textbox', {name: 'Start date (UTC)'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Run cost'}));
        expect(runCost).toHaveBeenCalledOnce();
        expect(jobs).not.toHaveBeenCalled();
    });

    it('enables a new run after an interrupted lease', async () => {
        const {cost, rerender} = setup();
        await screen.findByText(/Cost rows: completed/);
        cost.mockResolvedValue({...status, state: 'interrupted'});
        rerender(<MetricsHistorySettings active={false}/>);
        rerender(<MetricsHistorySettings active/>);
        await screen.findByText(/Cost rows: interrupted/);
        expect(screen.getByRole('button', {name: 'Run cost'})).toBeEnabled();
    });
});


it('does not render the operation or read statuses without authorization', () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(false);
    const jobs = vi.spyOn(context.client().schedulerAdmin(), 'getJobMetricsBackfill');
    const cost = vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill');
    render(<MetricsHistorySettings active/>);
    expect(screen.queryByRole('heading', {name: 'History backfill'})).not.toBeInTheDocument();
    expect(jobs).not.toHaveBeenCalled();
    expect(cost).not.toHaveBeenCalled();
    vi.restoreAllMocks();
});


it('shows only job history to a deployed jobs administrator', async () => {
    const {context, rerender, cost} = setup();
    await screen.findByText(/Jobs: completed/);
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'scheduler');
    cost.mockClear();
    rerender(<MetricsHistorySettings active={false}/>);
    rerender(<MetricsHistorySettings active/>);
    await screen.findByText(/Jobs: completed/);
    expect(screen.queryByRole('button', {name: 'Run cost'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Advanced'})).not.toBeInTheDocument();
    expect(screen.queryByText(/Cost rows:/)).not.toBeInTheDocument();
    expect(cost).not.toHaveBeenCalled();
    expect(screen.getAllByRole('heading', {name: 'History backfill'})).toHaveLength(1);
    expect(document.querySelectorAll('#backfill-history')).toHaveLength(1);
    vi.restoreAllMocks();
});

it('polls every ten seconds only while active and stops on unmount', async () => {
    vi.useFakeTimers();
    try {
        const {jobs, cost, rerender, unmount} = setup();
        await act(async () => {});
        expect(jobs).toHaveBeenCalledOnce();
        await act(async () => { vi.advanceTimersByTime(10000); });
        expect(jobs).toHaveBeenCalledTimes(2);
        expect(cost).toHaveBeenCalledTimes(2);
        rerender(<MetricsHistorySettings active={false}/>);
        expect(screen.queryByText('History backfill')).not.toBeInTheDocument();
        await act(async () => { vi.advanceTimersByTime(30000); });
        expect(jobs).toHaveBeenCalledTimes(2);
        rerender(<MetricsHistorySettings active/>);
        await act(async () => {});
        expect(jobs).toHaveBeenCalledTimes(3);
        unmount();
        await act(async () => { vi.advanceTimersByTime(30000); });
        expect(jobs).toHaveBeenCalledTimes(3);
        expect(cost).toHaveBeenCalledTimes(3);
    } finally { vi.useRealTimers(); vi.restoreAllMocks(); }
});

it.each(['read', 'run'])('ignores stale %s responses after deactivation', async operation => {
    const {cost, runCost, rerender} = setup();
    await screen.findByText(/Cost rows: completed/);
    let resolve!: (value: MetricsBackfillStatus) => void;
    const pending = new Promise<MetricsBackfillStatus>(done => { resolve = done; });
    if (operation === 'read') {
        cost.mockReturnValueOnce(pending);
        rerender(<MetricsHistorySettings active={false}/>);
        rerender(<MetricsHistorySettings active/>);
    } else {
        runCost.mockReturnValueOnce(pending);
        fireEvent.click(screen.getByRole('button', {name: 'Run cost'}));
    }
    rerender(<MetricsHistorySettings active={false}/>);
    rerender(<MetricsHistorySettings active/>);
    await screen.findByText(/Cost rows: completed/);
    await act(async () => { resolve({...status, state: 'running'}); });
    expect(screen.getByText(/Cost rows: completed/)).toBeVisible();
    expect(screen.getByRole('button', {name: 'Run cost'})).toBeEnabled();
    vi.restoreAllMocks();
});

it('disables both operations while their jobs are running', async () => {
    const {jobs, cost, rerender} = setup();
    await screen.findByText(/Cost rows: completed/);
    jobs.mockResolvedValue({...status, state: 'running'});
    cost.mockResolvedValue({...status, state: 'running'});
    rerender(<MetricsHistorySettings active={false}/>);
    rerender(<MetricsHistorySettings active/>);
    await screen.findByText(/Cost rows: running/);
    await userEvent.type(screen.getByRole('textbox', {name: 'Start date (UTC)'}), '2026-09-01');
    expect(screen.getByRole('button', {name: 'Run jobs'})).toBeDisabled();
    expect(screen.getByRole('button', {name: 'Run cost'})).toBeDisabled();
    vi.restoreAllMocks();
});
