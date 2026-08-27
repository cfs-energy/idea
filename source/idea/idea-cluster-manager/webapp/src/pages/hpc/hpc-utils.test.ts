import {
    formatBlockingLimit,
    formatDurationMinutes,
    formatProvisioningAttempt,
    getJobWaitingSignals,
    JobUtils,
    parseWalltimeSeconds
} from './hpc-utils';
import {SocaJob} from '../../client/data-model';

const NOW = new Date('2026-08-19T12:00:00Z');

describe('parseWalltimeSeconds', () => {
    it('parses HH:MM:SS', () => {
        expect(parseWalltimeSeconds('05:00:00')).toBe(18000);
        expect(parseWalltimeSeconds('00:23:30')).toBe(1410);
        expect(parseWalltimeSeconds('120:00:00')).toBe(432000);
    });

    it('returns null for values it cannot parse', () => {
        expect(parseWalltimeSeconds(undefined)).toBeNull();
        expect(parseWalltimeSeconds('')).toBeNull();
        expect(parseWalltimeSeconds('5h')).toBeNull();
        expect(parseWalltimeSeconds('05:00')).toBeNull();
        expect(parseWalltimeSeconds('05:00:00:00')).toBeNull();
        expect(parseWalltimeSeconds('aa:bb:cc')).toBeNull();
        expect(parseWalltimeSeconds('-1:00:00')).toBeNull();
    });
});

describe('formatDurationMinutes', () => {
    it('renders to the nearest minute and never to the second', () => {
        expect(formatDurationMinutes(0)).toBe('less than 1 min');
        expect(formatDurationMinutes(59)).toBe('less than 1 min');
        expect(formatDurationMinutes(60)).toBe('1 min');
        expect(formatDurationMinutes(119)).toBe('1 min');
        expect(formatDurationMinutes(2700)).toBe('45 min');
        expect(formatDurationMinutes(3600)).toBe('1 hr');
        expect(formatDurationMinutes(5430)).toBe('1 hr 30 min');
    });

    it('renders a dash when there is no duration to show', () => {
        expect(formatDurationMinutes(null)).toBe('-');
        expect(formatDurationMinutes(undefined)).toBe('-');
        expect(formatDurationMinutes(-1)).toBe('-');
        expect(formatDurationMinutes(Number.NaN)).toBe('-');
    });
});

describe('JobUtils job timings', () => {
    const job = (overrides: SocaJob): SocaJob => ({job_id: '100', ...overrides});

    it('reports elapsed time from start_time, ending at end_time once set', () => {
        const running = new JobUtils(job({start_time: '2026-08-19T11:30:00Z'}));
        expect(running.getElapsedSeconds(NOW)).toBe(1800);

        const finished = new JobUtils(job({
            start_time: '2026-08-19T09:00:00Z',
            end_time: '2026-08-19T10:00:00Z'
        }));
        expect(finished.getElapsedSeconds(NOW)).toBe(3600);
    });

    it('never falls back to the requested walltime for a job that has not started', () => {
        const queued = new JobUtils(job({queue_time: '2026-08-19T11:00:00Z', params: {walltime: '05:00:00'}}));
        expect(queued.getElapsedSeconds(NOW)).toBeNull();
        expect(queued.getTotalTimeSeconds()).toBeNull();
        expect(queued.getElapsedSummary(NOW)).toEqual({state: 'not-started', text: 'Not started'});
    });

    it('reports total time only from the recorded value', () => {
        expect(new JobUtils(job({total_time_secs: 3720})).getTotalTimeSeconds()).toBe(3720);
        expect(new JobUtils(job({total_time_secs: 0})).getTotalTimeSeconds()).toBe(0);
        expect(new JobUtils(job({total_time_secs: -5})).getTotalTimeSeconds()).toBeNull();
        expect(new JobUtils(job({params: {walltime: '05:00:00'}})).getTotalTimeSeconds()).toBeNull();
    });

    it('reports queue wait, stopping the clock once the job starts', () => {
        const waiting = new JobUtils(job({queue_time: '2026-08-19T11:15:00Z'}));
        expect(waiting.getQueuedSeconds(NOW)).toBe(2700);

        const started = new JobUtils(job({
            queue_time: '2026-08-19T09:00:00Z',
            start_time: '2026-08-19T09:20:00Z'
        }));
        expect(started.getQueuedSeconds(NOW)).toBe(1200);
        expect(new JobUtils(job({})).getQueuedSeconds(NOW)).toBeNull();
    });

    it('ends the queue wait of a job that never started at end_time', () => {
        const record = job({
            state: 'finished',
            queue_time: '2026-08-19T10:00:00Z',
            end_time: '2026-08-19T10:12:00Z'
        });
        expect(new JobUtils(record).getQueuedSeconds(NOW)).toBe(720);
        // the browser clock keeps moving; a terminal record must not
        const muchLater = new Date('2026-09-06T12:00:00Z');
        expect(new JobUtils(record).getQueuedSeconds(muchLater)).toBe(720);
    });

    it('reports no queue wait for a terminal job with neither timestamp', () => {
        const record = job({state: 'finished', queue_time: '2026-08-19T10:00:00Z'});
        expect(new JobUtils(record).getQueuedSeconds(NOW)).toBeNull();
    });

    it('does not extrapolate a terminal job past the time it was recorded', () => {
        const record = job({
            state: 'finished',
            start_time: '2026-08-19T10:00:00Z',
            total_time_secs: 7200,
            params: {walltime: '05:00:00'}
        });
        const muchLater = new Date('2026-09-06T12:00:00Z');
        expect(new JobUtils(record).getElapsedSeconds(muchLater)).toBe(7200);
        expect(new JobUtils(record).getElapsedSummary(muchLater)).toEqual({
            state: 'within-limit',
            text: '2 hr of 5 hr requested'
        });
    });

    it('reports nothing elapsed for a terminal job with no recorded duration', () => {
        const record = job({state: 'finished', start_time: '2026-08-19T10:00:00Z'});
        expect(new JobUtils(record).getElapsedSeconds(NOW)).toBeNull();
    });

    it('grades elapsed against the requested walltime', () => {
        const summaryFor = (startTime: string, walltime?: string) =>
            new JobUtils(job({start_time: startTime, params: {walltime: walltime}})).getElapsedSummary(NOW);

        expect(summaryFor('2026-08-19T11:00:00Z', '05:00:00')).toEqual({
            state: 'within-limit',
            text: '1 hr of 5 hr requested'
        });
        expect(summaryFor('2026-08-19T07:15:00Z', '05:00:00')).toEqual({
            state: 'near-limit',
            text: '4 hr 45 min of 5 hr requested'
        });
        expect(summaryFor('2026-08-19T07:00:00Z', '05:00:00')).toEqual({
            state: 'over-limit',
            text: '5 hr of 5 hr requested'
        });
        expect(summaryFor('2026-08-19T11:00:00Z', undefined)).toEqual({
            state: 'no-walltime',
            text: '1 hr elapsed'
        });
    });
});

describe('formatProvisioningAttempt', () => {
    it('renders the attempt against the configured cap', () => {
        expect(formatProvisioningAttempt(1, 3)).toBe('attempt 1 of 3');
        expect(formatProvisioningAttempt(3, 3)).toBe('attempt 3 of 3');
    });

    it('drops the cap when the deployment has none', () => {
        expect(formatProvisioningAttempt(4, null)).toBe('attempt 4');
        expect(formatProvisioningAttempt(4, undefined)).toBe('attempt 4');
    });

    it('drops the cap rather than render an attempt beyond it', () => {
        expect(formatProvisioningAttempt(4, 3)).toBe('attempt 4');
    });

    it('renders nothing without an attempt', () => {
        expect(formatProvisioningAttempt(undefined, 3)).toBeNull();
        expect(formatProvisioningAttempt(0, 3)).toBeNull();
        expect(formatProvisioningAttempt(NaN, 3)).toBeNull();
    });

    it('separates a held job from a live final attempt', () => {
        expect(formatProvisioningAttempt(3, 3, true)).toBe('held after 3 of 3 attempts');
        expect(formatProvisioningAttempt(3, 3, false)).toBe('attempt 3 of 3');
        expect(formatProvisioningAttempt(4, null, true)).toBe('held after 4 attempts');
    });
});

describe('formatBlockingLimit', () => {
    it('names the limit type', () => {
        expect(formatBlockingLimit('max_provisioned_instances')).toBe('queue limit: max_provisioned_instances');
    });

    it('renders nothing without a limit', () => {
        expect(formatBlockingLimit(undefined)).toBeNull();
        expect(formatBlockingLimit('')).toBeNull();
    });
});

describe('getJobWaitingSignals', () => {
    const queuedJob = (overrides: Partial<SocaJob> = {}): SocaJob => ({
        job_id: '2345',
        state: 'queued',
        queue_time: '2026-08-19T11:15:00Z',
        params: {compute_stack: 'tbd'},
        ...overrides
    });

    it('reports the wait, the attempt and the blocking limit', () => {
        const signals = getJobWaitingSignals(queuedJob({
            provisioning_attempt: 2,
            max_provisioning_attempts: 3,
            blocking_limit_type: 'max_provisioned_instances'
        }), NOW);
        expect(signals).toEqual([
            'waiting 45 min',
            'attempt 2 of 3',
            'queue limit: max_provisioned_instances'
        ]);
    });

    it('reports the wait alone when nothing else is set', () => {
        expect(getJobWaitingSignals(queuedJob(), NOW)).toEqual(['waiting 45 min']);
    });

    it('measures the wait from the same clock as the detail panel', () => {
        const job = queuedJob();
        const [signal] = getJobWaitingSignals(job, NOW);
        expect(signal).toBe(`waiting ${formatDurationMinutes(new JobUtils(job).getQueuedSeconds(NOW))}`);
    });

    it('stops reporting a wait once the job has started', () => {
        const signals = getJobWaitingSignals(queuedJob({
            state: 'running',
            start_time: '2026-08-19T11:45:00Z'
        }), NOW);
        expect(signals).toEqual([]);
    });

    it('reports nothing at all for a job with no waiting signals', () => {
        expect(getJobWaitingSignals({job_id: '2346', state: 'running', start_time: '2026-08-19T11:00:00Z'}, NOW))
            .toEqual([]);
    });

    it('reports a held job as held rather than as another attempt', () => {
        const signals = getJobWaitingSignals(queuedJob({
            state: 'held',
            provisioning_attempt: 3,
            max_provisioning_attempts: 3
        }), NOW);
        expect(signals).toEqual(['waiting 45 min', 'held after 3 of 3 attempts']);
    });

    it('never derives a queue position or a predicted start', () => {
        const signals = getJobWaitingSignals(queuedJob({
            provisioning_attempt: 1,
            max_provisioning_attempts: 3,
            blocking_limit_type: 'max_running_jobs'
        }), NOW);
        const joined = signals.join(' ');
        expect(joined).not.toMatch(/position|ahead|rank|estimat|start(s|ing)? (at|in|by)/i);
    });
});
