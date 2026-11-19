import {
    prepareQueueProfileForSave,
    allowNewFsxLustreInitialValue,
    clearQueueDefault,
    applyFsxThroughputForSave,
    fsxThroughputFormValues,
    formatBlockingLimit,
    jobFailureNextStep,
    listAllProjects,
    formatDurationMinutes,
    formatProvisioningAttempt,
    getJobWaitingSignals,
    JobUtils,
    ncpusExceedsCoresMessage,
    parseWalltimeSeconds
} from './hpc-utils';
import {SocaJob} from '../../client/data-model';

const NOW = new Date('2026-08-19T12:00:00Z');

describe('ncpusExceedsCoresMessage', () => {
    it('names the instance cores and form controls', () => {
        expect(ncpusExceedsCoresMessage('c7i.4xlarge', 8)).toBe(
            'c7i.4xlarge has 8 cores with hyper-threading off; set CPUs per instance to 8 or enable hyper-threading'
        );
    });
});

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
    it('says what the limit is in words, and that nothing is needed', () => {
        expect(formatBlockingLimit('max_provisioned_instances')).toBe('The queue is at its limit for machines. Your job starts when others finish. Nothing to do.');
        expect(formatBlockingLimit('max_running_jobs')).toBe('The queue is at its limit for running jobs. Your job starts when others finish. Nothing to do.');
    });

    it('never shows a limit name it does not know', () => {
        expect(formatBlockingLimit('max_new_thing')).not.toContain('max_new_thing');
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
            'The queue is at its limit for machines. Your job starts when others finish. Nothing to do.'
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

describe('jobFailureNextStep', () => {
    it('sends a script failure to the output files', () => {
        expect(jobFailureNextStep({job_id: '2345', name: 'plasma', exit_status: 2})).toBe(
            "Exit code 2 came from your script. Check the job's output files (plasma.o2345 and plasma.e2345 in the folder it was submitted from, unless the script sets -o or -e).");
    });

    it('names the per-subjob files of an array job', () => {
        expect(jobFailureNextStep({job_id: '123[]', name: 'sweep', exit_status: 1})).toContain('(sweep.o123.<index> and sweep.e123.<index> ');
    });

    it('names the signal case above 256', () => {
        expect(jobFailureNextStep({job_id: '2345', name: 'plasma', exit_status: 271})).toBe('Stopped by a signal, often the walltime limit.');
    });

    it('has nothing to say without a script exit code', () => {
        expect(jobFailureNextStep({job_id: '2345', name: 'plasma'})).toBeNull();
        expect(jobFailureNextStep({job_id: '2345', name: 'plasma', exit_status: 0})).toBeNull();
        expect(jobFailureNextStep({job_id: '2345', name: 'plasma', exit_status: -1})).toBeNull();
    });
});

describe('listAllProjects', () => {
    it('follows the cursor to the last project', async () => {
        const project = (index: number) => ({project_id: `p-${index}`, name: `project-${index}`});
        const listProjects = vi.fn()
            .mockResolvedValueOnce({listing: Array.from({length: 100}, (_, index) => project(index)), paginator: {cursor: 'next'}})
            .mockResolvedValueOnce({listing: Array.from({length: 30}, (_, index) => project(100 + index)), paginator: {}});
        const projects = await listAllProjects({listProjects});
        expect(projects).toHaveLength(130);
        expect(listProjects).toHaveBeenCalledTimes(2);
        expect(listProjects.mock.calls[1][0].paginator.cursor).toBe('next');
    });
});

describe('allowNewFsxLustreInitialValue', () => {
    it('starts unchecked for a new queue profile', () => {
        expect(allowNewFsxLustreInitialValue(true, {})).toBe(false);
    });
    it('shows a profile saved before the setting existed as allowed, so saving it changes nothing', () => {
        expect(allowNewFsxLustreInitialValue(false, {queue_management_params: {}})).toBe(true);
        expect(allowNewFsxLustreInitialValue(false, {})).toBe(true);
    });
    it('shows the stored value of an existing profile', () => {
        expect(allowNewFsxLustreInitialValue(false, {queue_management_params: {allow_new_fsx_lustre: false}})).toBe(false);
        expect(allowNewFsxLustreInitialValue(false, {queue_management_params: {allow_new_fsx_lustre: true}})).toBe(true);
    });
});

describe('FSx per-unit throughput on the queue form', () => {
    const save = (fsx: any, throughput: any) => {
        const profile: any = {default_job_params: {fsx_lustre: fsx}, fsx_lustre_throughput: throughput}
        applyFsxThroughputForSave(profile)
        return profile
    }
    it('saves a new PERSISTENT_2 profile left at the shown default with a value PERSISTENT_2 takes', () => {
        const profile = save({deployment_type: 'persistent_2'}, fsxThroughputFormValues(undefined))
        expect(profile.default_job_params.fsx_lustre.per_unit_throughput).toBe(125)
        expect(profile.fsx_lustre_throughput).toBeUndefined()
    });
    it('saves the value chosen for the chosen type', () => {
        expect(save({deployment_type: 'persistent_1'}, {persistent_1: '100', persistent_2: '500'})
            .default_job_params.fsx_lustre.per_unit_throughput).toBe(100);
        expect(save({deployment_type: 'persistent_2'}, {persistent_1: '100', persistent_2: '500'})
            .default_job_params.fsx_lustre.per_unit_throughput).toBe(500);
    });
    it('drops the throughput for a scratch type', () => {
        expect(save({deployment_type: 'scratch_2', per_unit_throughput: 50}, {persistent_1: '200'})
            .default_job_params.fsx_lustre.per_unit_throughput).toBeUndefined();
    });
    it('fills the field of the stored type with the stored value', () => {
        expect(fsxThroughputFormValues({deployment_type: 'persistent_2', per_unit_throughput: 500}))
            .toEqual({persistent_1: '200', persistent_2: '500'});
    });
});

describe('clearQueueDefault', () => {
    it('sends a cleared default as null, so the server removes it and keeps the rest', () => {
        const profile: any = {default_job_params: {fsx_lustre: {existing_fsx: 'fs-1'}, walltime: '01:00:00'}}
        clearQueueDefault(profile, 'fsx_lustre')
        expect(JSON.parse(JSON.stringify(profile)).default_job_params).toEqual({fsx_lustre: null, walltime: '01:00:00'})
    });
});

describe('prepareQueueProfileForSave (the queue form save)', () => {
    // the stored profile as the API returns it, which the wizard starts from (A-S7)
    const stored = () => ({
        name: 'q',
        project_ids: ['p1'],
        scaling_mode: 'batch',
        default_job_params: {
            walltime: '02:00:00',
            enable_scratch: true,
            scratch_provider: 'fsx-lustre-existing',
            fsx_lustre: {enabled: true, existing_fsx: 'fs-1.fsx.us-east-1.amazonaws.com'},
            spot: false,
            spot_price: {amount: 0.1},
        },
        fsx_lustre_throughput: {persistent_1: '200', persistent_2: '125'},
    })
    const sent = (profile: any, before: any = stored()) =>
        JSON.parse(JSON.stringify(prepareQueueProfileForSave(profile, before)))

    it('switching scratch to EBS clears the FSx settings and keeps the rest', () => {
        const profile: any = stored()
        profile.default_job_params.scratch_provider = 'ebs'
        const params = sent(profile).default_job_params
        expect(params.fsx_lustre).toBeNull()
        expect(params.walltime).toBe('02:00:00')
        // Spot was already off when loaded: its stored price stays (verify-scheduler #13)
        expect(params.spot_price).toEqual({amount: 0.1})
    });
    it('turning Spot off in this edit clears its stored defaults', () => {
        const before: any = stored()
        before.default_job_params.spot = true
        const profile: any = stored()
        profile.default_job_params.spot = false
        const params = sent(profile, before).default_job_params
        expect(params.spot_price).toBeNull()
        expect(params.spot_allocation_count).toBeNull()
        expect(params.spot_allocation_strategy).toBeNull()
    });
    it('an unrelated edit keeps the stored provider settings as stored', () => {
        const before = (): any => ({
            name: 'q', project_ids: ['p1'],
            default_job_params: {
                enable_scratch: true, scratch_provider: 'ebs',
                scratch_storage_size: {value: 60, unit: 'gb'},
                fsx_lustre: {enabled: true, existing_fsx: 'fs-1.fsx.us-east-1.amazonaws.com'},
            },
        })
        const profile = before()
        profile.title = 'renamed'
        const params = sent(profile, before()).default_job_params
        expect(params.fsx_lustre).toEqual(before().default_job_params.fsx_lustre)
        expect(params.scratch_storage_size).toEqual({value: 60, unit: 'gb'})
    });
    it('switching to a new FSx clears the stored existing one', () => {
        const profile: any = stored()
        profile.default_job_params.scratch_provider = 'fsx-lustre-new'
        const params = sent(profile).default_job_params
        expect(params.fsx_lustre.existing_fsx).toBeNull()
        expect(params.fsx_lustre.enabled).toBe(true)
        expect(params.scratch_storage_size).toBeNull()
    });
    it('turning scratch off clears its settings and disables FSx', () => {
        const profile: any = stored()
        profile.default_job_params.enable_scratch = false
        const params = sent(profile).default_job_params
        expect(params.scratch_provider).toBeNull()
        // every stored FSx setting goes as null so the server removes it, not only enabled
        expect(params.fsx_lustre).toEqual({
            enabled: false, existing_fsx: null, s3_backend: null, import_path: null,
            export_path: null, deployment_type: null, per_unit_throughput: null, size: null,
        })
    });
    it('an unrelated edit of a profile stored with scratch off sends its FSx values as stored', () => {
        // rereview3 S3: a profile written through the API, scratch unset, FSx default kept
        const apiWritten = (): any => ({
            name: 'q',
            project_ids: ['p1'],
            default_job_params: {
                walltime: '02:00:00',
                fsx_lustre: {enabled: true, existing_fsx: 'fs-1.fsx.us-east-1.amazonaws.com', s3_backend: 's3://b'},
            },
        })
        const profile = apiWritten()
        profile.default_job_params.walltime = '03:00:00'
        const params = sent(profile, apiWritten()).default_job_params
        expect(params.fsx_lustre).toEqual(apiWritten().default_job_params.fsx_lustre)
        expect('scratch_provider' in params).toBe(false)
        expect(params.walltime).toBe('03:00:00')
    });
    it('a profile created with scratch off sends no clears', () => {
        const profile: any = {name: 'q', project_ids: ['p1'], default_job_params: {enable_scratch: false}}
        const params = sent(profile, {}).default_job_params
        expect('fsx_lustre' in params || 'scratch_provider' in params).toBe(false)
    });
    it('sends the projects and no form-only fields', () => {
        const profile: any = stored()
        profile.scratch_off_notice = undefined
        const body = sent(profile)
        expect('scratch_off_notice' in body).toBe(false)
        expect(body.projects).toEqual([{project_id: 'p1'}])
        expect(body.fsx_lustre_throughput).toBeUndefined()
    });
});
