import {render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import CostSections, {ESTIMATED_NOTE} from '../../components/cost-sections';
import StorageUsage from './storage-usage';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const SUMMARY = {
    username: 'user-a',
    window: 'last_30_days',
    start_date: '2026-08-03',
    end_date: '2026-09-01',
    currency: 'USD',
    ai: {
        invocations: 3,
        total_tokens: 300,
        cost: 12.5,
        estimated: true,
        projects: [
            {
                project_id: 'project-1',
                project_name: 'project-a',
                project_title: 'Project A',
                invocations: 3,
                total_tokens: 300,
                cost: 12.5,
                estimated: true,
                by_model: [
                    {
                        model_id: 'vendor-a.model-1',
                        invocations: 3,
                        total_tokens: 300,
                        cost: 12.5,
                        estimated: true
                    }
                ]
            }
        ]
    },
    jobs: {
        job_count: 2,
        cost: 8.25,
        estimated: true,
        by_project: [{name: 'project-a', job_count: 2, cost: 8.25}],
        by_queue: [{name: 'normal', job_count: 2, cost: 8.25}],
        recent_jobs: [
            {
                job_id: '101',
                name: 'solve',
                queue: 'normal',
                project: 'project-a',
                end_time: '2026-08-30T10:00:00+00:00',
                cost: 4.5
            }
        ]
    },
    desktops: {
        session_count: 2,
        hours: 6.5,
        cost: 0.65,
        estimated: true,
        sessions: [
            {
                idea_session_id: 'sess-1',
                name: 'desktop-one',
                instance_type: 'm5.large',
                state: 'STOPPED',
                hours: 4.5,
                cost: 0.45,
                estimated: true
            },
            {
                idea_session_id: 'sess-2',
                name: 'desktop-two',
                instance_type: 'zz.unknown',
                state: 'STOPPED',
                hours: 2.0,
                estimated: true,
                price_unavailable: true
            }
        ]
    }
};

const EMPTY_SUMMARY = {
    username: 'user-a',
    window: 'last_30_days',
    ai: {invocations: 0, total_tokens: 0, cost: 0, estimated: true, projects: []},
    jobs: {job_count: 0, cost: 0, estimated: true, by_project: [], by_queue: [], recent_jobs: []},
    desktops: {session_count: 0, hours: 0, cost: 0, estimated: true, sessions: []}
};

let currentSummary: any;
const stubMyCosts = (context: AppContext, summary: any) => {
    currentSummary = summary;
    vi.spyOn(context.client().fileBrowser(), 'getStorageUsage').mockResolvedValue({state: 'ready', home: '/home/user-a', folders: []});
    vi.spyOn(context.client().myCosts(), 'getCosts').mockResolvedValue({currency: 'USD', state: 'ready', current: {details: summary} as any});
};

const SECTIONS = ['AI usage', 'Desktops', 'Jobs'];

/** Section headings in DOM order. The side nav header is an h2 too, so filter. */
const sectionOrder = (): (string | null)[] =>
    screen.getAllByRole('heading', {level: 2})
        .map((node) => node.textContent)
        .filter((text) => SECTIONS.includes(text ?? ''));

const renderMyCosts = () => {
    render(<MemoryRouter><h1>My costs</h1><p>{ESTIMATED_NOTE}</p><CostSections summary={currentSummary} loading={false} error={null} onRetry={() => {}} subject="self"/><StorageUsage/></MemoryRouter>);
};

describe('shared cost details and storage usage', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('polls computing storage, renders folder age and old bytes, then the quota report', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        const measured = Date.now() / 1000;
        const folder = {
            path: '/home/user-a/reports & notes', name: 'reports & notes', bytes: 1024, files: 2,
            newest_mtime: measured - 86400, oldest_mtime: measured - 100 * 86400,
            unchanged_90_days_bytes: 512, partial: false
        };
        const usage = vi.mocked(context.client().fileBrowser().getStorageUsage);
        usage.mockResolvedValueOnce({state: 'computing', home: '/home/user-a'}).mockResolvedValue({
            state: 'ready', home: '/home/user-a', measured_at: measured, partial: false,
            total: folder, folders: [folder],
            quotas: [{target: 'data', volume: 'home', qtree: '', used_bytes: 2048, files: 3, limit_bytes: 4096, measured_at: measured}]
        });
        renderMyCosts();
        expect(await screen.findByText('Computing storage usage...')).toBeInTheDocument();
        expect(screen.queryByText(/ONTAP quota/)).toBeNull();
        expect(await screen.findByText('reports & notes', {}, {timeout: 10000})).toBeInTheDocument();
        expect(screen.getByText(/512 B has not changed in 90 days\./)).toBeInTheDocument();
        expect(screen.getByText(/1 day ago/)).toBeInTheDocument();
        expect(screen.getByRole('link', {name: 'Open in Files'})).toHaveAttribute('href', '#/home/file-browser?cwd=%2Fhome%2Fuser-a%2Freports%20%26%20notes');
        expect(screen.getByText('2 KiB in 3 files on home (limit 4 KiB)')).toBeInTheDocument();
        expect(screen.getByText('Your ONTAP quota counts everything you own on this volume, including outside your home folder.')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: /Delete/})).toBeNull();
        await waitFor(() => expect(usage).toHaveBeenCalledTimes(2));
    });

    it('labels a partial storage scan and explains an unavailable ONTAP quota report', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        vi.mocked(context.client().fileBrowser().getStorageUsage).mockResolvedValue({
            state: 'ready', home: '/home/user-a', measured_at: Date.now() / 1000, partial: true, quota_status: 'unavailable',
            total: {path: '/home/user-a', name: 'user-a', bytes: 0, files: 0, newest_mtime: null, oldest_mtime: null, unchanged_90_days_bytes: 0, partial: true}, folders: []
        });
        renderMyCosts();
        expect(await screen.findByText('Scanned 0 files, then stopped at the time limit. Sizes are lower bounds.')).toBeInTheDocument();
        expect(screen.getByText('ONTAP quota: unavailable. No quota report is available for your account.')).toBeInTheDocument();
        expect(screen.queryByText('Partial')).toBeNull();
    });

    it('says how far a scan got against the quota file count, not a bare badge', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        const measured = Date.now() / 1000;
        vi.mocked(context.client().fileBrowser().getStorageUsage).mockResolvedValue({
            state: 'ready', source: 'scan', home: '/home/user-a', measured_at: measured, partial: true, scanned_files: 8361,
            total: {path: '/home/user-a', name: 'user-a', bytes: 3 * 1024 ** 3, files: 8361, newest_mtime: null, oldest_mtime: null, unchanged_90_days_bytes: 0, partial: true},
            folders: [{path: '/home/user-a/.cache', name: '.cache', bytes: 3 * 1024 ** 3, files: 8361, newest_mtime: measured, oldest_mtime: null, unchanged_90_days_bytes: 0, partial: true},
                {path: '/home/user-a/projects', name: 'projects', bytes: 0, files: 0, newest_mtime: measured, oldest_mtime: null, unchanged_90_days_bytes: 0, partial: true, measured: false}],
            quotas: [{target: 'data', volume: 'profiles', qtree: '', used_bytes: 1.2 * 1024 ** 4, files: 3670000, limit_bytes: null, measured_at: measured}]
        });
        renderMyCosts();
        expect(await screen.findByText('Scanned 8,361 of about 3.67M files, then stopped at the time limit. Sizes are lower bounds.', {}, {timeout: 10000})).toBeInTheDocument();
        expect(screen.getByText('Your ONTAP quota counts everything you own on this volume, including outside your home folder.')).toBeInTheDocument();
        expect(screen.getByText('Not measured yet')).toBeInTheDocument();
        expect(screen.getByText('At least 3 GiB')).toBeInTheDocument();
        expect(screen.queryByText('Partial')).toBeNull();
    }, 20000);

    it('drills into a folder, shows the study progress, and says what is outside home', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        const measured = Date.now() / 1000;
        const row = (path: string, name: string, bytes: number) => ({path, name, bytes, files: 10, newest_mtime: measured, oldest_mtime: measured, unchanged_90_days_bytes: bytes / 2, partial: false});
        const usage = vi.mocked(context.client().fileBrowser().getStorageUsage);
        usage.mockImplementation(async (request: any = {}) => request.folder ? {
            state: 'ready', source: 'analytics', home: '/home/user-a', measured_at: measured, partial: false,
            total: row(request.folder, 'runs', 1024), folder: row(request.folder, 'runs', 1024), folders: [row(`${request.folder}/run-7`, 'run-7', 512)]
        } : {
            state: 'ready', source: 'analytics', home: '/home/user-a', measured_at: measured, partial: false, outside_home_bytes: 900 * 1024 ** 3,
            total: row('/home/user-a', 'user-a', 2048), folders: [row('/home/user-a/runs', 'runs', 1024)],
            shared: [row('/data/shared/models', 'models', 4096)],
            quotas: [{target: 'data', volume: 'profiles', qtree: '', used_bytes: 900 * 1024 ** 3, files: 100, limit_bytes: null, measured_at: measured}],
            progress: {running: false, step: 'Finished', done: 9, total: 9, started_at: measured, steps: []}
        });
        renderMyCosts();
        expect(await screen.findByText(/900 GiB of that is outside your home folder/, {}, {timeout: 10000})).toBeInTheDocument();
        expect(screen.getByText('models')).toBeInTheDocument();
        const user = userEvent.setup();
        await user.click(screen.getByRole('link', {name: 'runs'}));
        expect(await screen.findByRole('link', {name: 'run-7'}, {timeout: 5000})).toBeInTheDocument();
        expect(usage).toHaveBeenCalledWith({folder: '/home/user-a/runs'});
        expect(screen.queryByText(/outside your home folder\. ONTAP/)).toBeNull();
    }, 20000);

    it('shows live study steps and sends one refresh', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        const measured = Date.now() / 1000;
        const usage = vi.mocked(context.client().fileBrowser().getStorageUsage);
        usage.mockResolvedValue({
            state: 'ready', source: 'analytics', home: '/home/user-a', measured_at: measured, partial: false,
            total: {path: '/home/user-a', name: 'user-a', bytes: 1, files: 1, newest_mtime: null, oldest_mtime: null, unchanged_90_days_bytes: 0, partial: false}, folders: [],
            progress: {running: true, step: 'Checking runs/run-7', done: 57, total: 100, started_at: measured - 12,
                steps: [{label: 'Read your quota total', state: 'done'}, {label: 'Find your biggest folders', state: 'running'}]}
        });
        renderMyCosts();
        const status = await screen.findByRole('status', {name: 'Storage study progress'}, {timeout: 10000});
        expect(status).toHaveTextContent('Checking runs/run-7');
        expect(status).toHaveTextContent('57 of 100 folders checked');
        expect(status).toHaveTextContent('Read your quota total (done)');
    }, 20000);

    it('renders the three sections with the measured numbers', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        expect(await screen.findByRole('heading', {name: 'My costs'}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'AI usage'})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'Jobs'})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'Desktops'})).toBeInTheDocument();

        expect(await screen.findByText('Project A')).toBeInTheDocument();
        expect(await screen.findByText('solve')).toBeInTheDocument();
        expect(await screen.findByText('desktop-one')).toBeInTheDocument();
    }, 20000);

    it('marks every section as estimated and says these are not the AWS bill', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        const badges = await screen.findAllByText('Estimated', {}, {timeout: 10000});
        expect(badges.length).toBe(3);
        expect(await screen.findByText(/not the AWS bill/)).toBeInTheDocument();
    });

    it('expands a project row to its models', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        await screen.findByText('Project A', {}, {timeout: 10000});
        expect(screen.queryByText('vendor-a.model-1')).toBeNull();
        await userEvent.click(screen.getByRole('button', {name: 'Show models for Project A'}));
        expect(await screen.findByText('vendor-a.model-1')).toBeInTheDocument();
    });

    it('reports hours without a cost when the instance price is unavailable', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        expect(await screen.findByText('Price not available', {}, {timeout: 10000})).toBeInTheDocument();
    });

    it('shows a per section empty state when nothing was measured', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        expect(await screen.findByText('No desktops', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findAllByText('No completed jobs')).toHaveLength(2);
    });

    it('hides the AI section entirely when the user has no AI usage', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        // An unused section is absent, not shown empty.
        await screen.findByText('No desktops', {}, {timeout: 10000});
        expect(screen.queryByRole('heading', {name: 'AI usage'})).toBeNull();
        expect(screen.queryByText('No AI usage')).toBeNull();
    });

    it('still shows the AI section when the usage read failed', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {...EMPTY_SUMMARY, ai: {is_unavailable: true}});
        renderMyCosts();

        // An unavailable read must not be hidden the way no usage is.
        expect(await screen.findByRole('heading', {name: 'AI usage'}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('AI usage is not available')).toBeInTheDocument();
    });

    it('orders the sections AI, then desktops, then jobs', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        await screen.findByRole('heading', {name: 'AI usage'}, {timeout: 10000});
        expect(sectionOrder()).toEqual(['AI usage', 'Desktops', 'Jobs']);
    });

    it('orders desktops before jobs when the AI section is hidden', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        await screen.findByText('No desktops', {}, {timeout: 10000});
        expect(sectionOrder()).toEqual(['Desktops', 'Jobs']);
    });

    it('reports desktop cost as not available when no session could be priced', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            jobs: {is_unavailable: true},
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0,
                unpriced_sessions: 2,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'one', instance_type: 'm6g.xlarge', state: 'STOPPED', hours: 4.5, price_unavailable: true},
                    {idea_session_id: 's2', name: 'two', instance_type: 'g4dn.2xlarge', state: 'STOPPED', hours: 2.0, price_unavailable: true}
                ]
            }
        });
        renderMyCosts();

        // Real hours with no prices must not read as $0.00.
        expect(await within(screen.getByRole('region', {name: 'Desktop cost details'})).findByText('Not available', {}, {timeout: 10000})).toBeInTheDocument();
        expect(screen.queryByText('$0.00')).toBeNull();
    });

    it('shows a terminated desktop with its hours and cost', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 1,
                hours: 24.0,
                cost: 2.4,
                estimated: true,
                sessions: [
                    {
                        idea_session_id: 's1',
                        name: 'gone-but-billed',
                        instance_type: 'm5.large',
                        state: 'Terminated',
                        hours: 24.0,
                        cost: 2.4
                    }
                ]
            }
        });
        renderMyCosts();

        // A desktop deleted inside the window still cost what it cost while running.
        expect(await screen.findByText('gone-but-billed', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('Terminated')).toBeInTheDocument();
        expect(screen.queryByText('DELETED')).toBeNull();
        // Once in the row and once in the section total, so it counts toward the hours.
        expect(await screen.findAllByText('24.00 h')).toHaveLength(2);
        expect(await screen.findByText('1')).toBeInTheDocument();
    }, 20000);

    it('marks a session whose stop time had to be inferred', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0.65,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'recorded', instance_type: 'm5.large', state: 'STOPPED', hours: 4.5, cost: 0.45},
                    {idea_session_id: 's2', name: 'legacy', instance_type: 'm5.large', state: 'STOPPED', hours: 2.0, cost: 0.2, stop_time_estimated: true}
                ]
            }
        });
        renderMyCosts();

        // A session stopped before the stop time was recorded is counted to its last write, which
        // is an upper bound.
        expect(await screen.findByText('2.00 h (estimated)', {}, {timeout: 10000})).toBeInTheDocument();
        // The session with a recorded stop time is not marked.
        expect(await screen.findByText('4.50 h')).toBeInTheDocument();
    });

    it('marks a partly priced total and says how many rows are missing', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0.45,
                unpriced_sessions: 1,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'one', instance_type: 'm5.large', state: 'STOPPED', hours: 4.5, cost: 0.45},
                    {idea_session_id: 's2', name: 'two', instance_type: 'zz.unknown', state: 'STOPPED', hours: 2.0, price_unavailable: true}
                ]
            }
        });
        renderMyCosts();

        expect(await screen.findByText('$0.45 *', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText(/1 of 2 sessions could not be priced/)).toBeInTheDocument();
    });

    it('says in the one note which unpriced desktops were deleted before usage history', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 2,
                hours: 4.5,
                cost: 0.45,
                unpriced_sessions: 1,
                usage_unknown_sessions: 1,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'one', instance_type: 'm5.large', state: 'STOPPED', hours: 4.5, cost: 0.45},
                    {idea_session_id: 's2', name: 'two', instance_type: 'm5.large', state: 'Terminated', hours: 0, price_unavailable: true, usage_unknown: true}
                ]
            }
        });
        renderMyCosts();

        expect(await screen.findByText(
            '* 1 of 2 sessions could not be priced and are not in this total (1 deleted before usage history existed).',
            {}, {timeout: 10000}
        )).toBeInTheDocument();
        expect(screen.getByText('Not priced')).toBeInTheDocument();
        expect(screen.getByText('Unknown')).toBeInTheDocument();
    });

    it('shows the section as unavailable rather than as zero when a read failed', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            jobs: {is_unavailable: true},
            desktops: {is_unavailable: true}
        });
        renderMyCosts();

        expect(await screen.findByText('Job costs are not available', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('Desktop costs are not available')).toBeInTheDocument();
    });
});
