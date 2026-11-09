import {render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {formatBytes, modeText, ScratchCleanupContent} from './scratch-cleanup';
import {initTestAppContext} from '../../test-support';

const settings = {enabled: true, dry_run: false, kill_switch: false, file_systems: ['scratch'], success_retention_days: 30, failed_retention_days: 60, digest_weekday: 0, max_folders_per_run: 25, max_run_bytes_percent: 25, max_run_share_percent: 50, };
const fileSystem = {name: 'scratch', title: 'Lustre Scratch', mount_dir: '/lustre', selected: true, capacity_bytes: 1000, used_bytes: 970, used_percent: 97};
const preview = {
    preview_id: 'p1', file_system: 'scratch', created_at: '2026-11-06T10:00:00Z', expires_at: '2026-11-06T11:00:00Z',
    action_counts: {schedule: 2, scheduled: 1, keep: 3}, action_bytes: {schedule: 2048, scheduled: 1024},
    reason_counts: {eligible: 3, job_active: 3}, reason_text: {eligible: 'ok', job_active: 'The job is still running, queued, held or suspended.'},
    confirm_phrase: 'schedule 2',
};
const row = {path: '/lustre/alice/123', owner: 'alice', job_id: '123', job_end_time: '2026-10-01T00:00:00Z', size_bytes: 2048, action: 'schedule', reason_text: 'The job finished and its folder has not changed since.'};

function setup(status: any = {}) {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isAdmin').mockReturnValue(true);
    const admin = context.client().schedulerAdmin();
    const calls = {
        status: vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: [], ...status}),
        preview: vi.spyOn(admin, 'previewScratchCleanup').mockResolvedValue({started: true}),
        list: vi.spyOn(admin, 'listScratchCleanupPreview').mockResolvedValue({listing: [row], paginator: {total: 1}}),
        run: vi.spyOn(admin, 'backfillScratchCleanup').mockResolvedValue({run_id: 'r1'}),
        upcoming: vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({listing: [], paginator: {total: 0}}),
        audit: vi.spyOn(admin, 'listScratchCleanupAudit').mockResolvedValue({listing: []}),
        update: vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true}),
    };
    vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('scheduler');
    render(<ScratchCleanupContent/>);
    return calls;
}

describe('scratch cleanup page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('gives each upcoming removal column its own name', async () => {
        setup();
        const table = (await screen.findByText('No removals are scheduled.')).closest('table')!;
        const headers = within(table).getAllByRole('columnheader').map(header => header.textContent);
        expect(new Set(headers).size).toBe(headers.length);
        expect(headers).toContain('Job result');
    });

    it('tells a manager the folder list is for administrators instead of asking for it', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isAdmin').mockReturnValue(false);
        const admin = context.client().schedulerAdmin();
        vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: [{preview_id: 'p1', file_system: 'scratch', counts: {}, bytes: {}, reason_counts: {}}]} as any);
        vi.spyOn(admin, 'listScratchCleanupAudit').mockResolvedValue({listing: []});
        vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({listing: []});
        const rows = vi.spyOn(admin, 'listScratchCleanupPreview').mockRejectedValue(new Error('Unauthorized'));
        render(<ScratchCleanupContent/>);
        expect(await screen.findByText('Only a cluster administrator can see the folder list.')).toBeVisible();
        expect(rows).not.toHaveBeenCalled();
    });

    it('shows nothing to do on a cluster with no Lustre scratch', async () => {
        const calls = setup({available: false, file_systems: []});
        expect(await screen.findByText(/no Lustre scratch file system/)).toBeVisible();
        expect(screen.queryByRole('button', {name: 'Preview'})).toBeNull();
        expect(calls.audit).not.toHaveBeenCalled();
    });

    it('shows usage and says plainly when it is off', async () => {
        setup({settings: {...settings, enabled: false, dry_run: true}});
        expect(await screen.findByText(/Automatic cleanup is off/)).toBeVisible();
        expect(screen.getByText('Lustre Scratch')).toBeVisible();
        expect(screen.getByText(/970 B of 1000 B/)).toBeVisible();
    });

    it.each([
        [{kill_switch: true}, 'All removals are stopped'],
        [{enabled: false}, 'off'],
        [{dry_run: true}, 'Dry run'],
        [{}, 'On. A job'],
    ])('describes mode %j', (changes, text) => {
        expect(modeText({settings: {...settings, ...changes}}).text).toContain(text);
    });

    it('starts a preview for the shown file system', async () => {
        const calls = setup();
        await userEvent.click(await screen.findByRole('button', {name: 'Preview'}));
        expect(calls.preview).toHaveBeenCalledWith({file_system: 'scratch'});
    });

    it('shows real progress while working and blocks starting more', async () => {
        setup({progress: {operation: 'preview', phase: 'Checking folders', done: 3, total: 4, current: '/lustre/alice/123'}});
        expect((await screen.findAllByText(/Previewing: Checking folders/))[0]).toBeVisible();
        expect(screen.getByText('/lustre/alice/123')).toBeVisible();
        expect(screen.getByRole('button', {name: 'Preview'})).toBeDisabled();
        expect(screen.getByRole('button', {name: 'Run backfill'})).toBeDisabled();
    });

    it('lists what would go and why others stay', async () => {
        const calls = setup({previews: [preview]});
        expect(await screen.findByText('The job is still running, queued, held or suspended.')).toBeVisible();
        expect(await screen.findByText('/lustre/alice/123')).toBeVisible();
        expect(calls.list).toHaveBeenCalledWith(expect.objectContaining({preview_id: 'p1', action: 'schedule'}));
        await userEvent.click(screen.getByText(/Kept \(3\)/));
        await waitFor(() => expect(calls.list).toHaveBeenLastCalledWith(expect.objectContaining({action: 'keep'})));
    });

    it('runs only after the exact phrase is typed, sending no paths', async () => {
        const calls = setup({previews: [preview]});
        await userEvent.click(await screen.findByRole('button', {name: 'Run backfill'}));
        const dialog = screen.getByRole('dialog');
        const run = within(dialog).getByRole('button', {name: 'Run backfill'});
        expect(run).toBeDisabled();
        expect(within(dialog).getByText(/permanently removed/)).toBeVisible();
        await userEvent.type(within(dialog).getByRole('textbox', {name: 'Confirmation'}), 'schedule 1');
        expect(run).toBeDisabled();
        await userEvent.clear(within(dialog).getByRole('textbox', {name: 'Confirmation'}));
        await userEvent.type(within(dialog).getByRole('textbox', {name: 'Confirmation'}), 'schedule 2');
        await userEvent.click(run);
        expect(calls.run).toHaveBeenCalledWith({preview_id: 'p1', confirm_phrase: 'schedule 2'});
    });

    it.each([
        ['off', {enabled: false}],
        ['in dry run', {dry_run: true}],
        ['stopped', {kill_switch: true}],
    ])('does not offer a run when %s', async (_, changes) => {
        setup({previews: [preview], settings: {...settings, ...changes}});
        expect(await screen.findByRole('button', {name: 'Run backfill'})).toBeDisabled();
    });

    it('does not offer a run for a file system that is not selected', async () => {
        setup({previews: [preview], file_systems: [{...fileSystem, selected: false}]});
        expect(await screen.findByRole('button', {name: 'Run backfill'})).toBeDisabled();
        expect(screen.getByText(/not selected for cleanup/)).toBeVisible();
    });

    it('shows the problem a run would stop on', async () => {
        setup({previews: [{...preview, confirm_phrase: undefined, problem: 'The run would remove 60% of job folders.'}]});
        expect(await screen.findByText('The run would remove 60% of job folders.')).toBeVisible();
        expect(screen.getByRole('button', {name: 'Run backfill'})).toBeDisabled();
    });

    it('counts what it does not name', async () => {
        setup({previews: [{...preview, other_kept: 4}]});
        expect(await screen.findByText(/4 other items are not job folders/)).toBeVisible();
    });

    it('says when owners cannot be told', async () => {
        setup({message: 'The cluster cannot send email.'});
        expect(await screen.findByText('The cluster cannot send email.')).toBeVisible();
    });

    it('shows upcoming removals with their state', async () => {
        const context = initTestAppContext();
        const admin = context.client().schedulerAdmin();
        vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: [], scheduled_folders: 3, failed_folders: 1});
        vi.spyOn(admin, 'listScratchCleanupAudit').mockResolvedValue({listing: []});
        vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({next_token: 'more', listing: [
            {owner: 'alice', job_id: '1', action: 'scheduled', outcome: 'failed', first_notice_at: '2026-11-01T00:00:00Z'},
            {owner: 'alice', job_id: '2', action: 'scheduled', reason_text: 'The owner has an active job.'},
            {owner: 'bob', job_id: '3', action: 'scheduled'},
        ]});
        render(<ScratchCleanupContent/>);
        expect(await screen.findByText('Kept: The owner has an active job.')).toBeVisible();
        expect(screen.getAllByText('Scheduled')[0]).toBeVisible();
        expect(screen.getByText('3 folders, 1 from failed jobs')).toBeVisible();
        expect(screen.getByRole('button', {name: 'Show more'})).toBeVisible();
        expect(screen.getByText('Job result')).toBeVisible();
        expect(screen.getByText('Failed')).toBeVisible();
        expect(screen.getByText('Once')).toBeVisible();
    });

    it('does not let anyone else preview or backfill', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isAdmin').mockReturnValue(false);
        const admin = context.client().schedulerAdmin();
        vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: [preview]});
        vi.spyOn(admin, 'listScratchCleanupAudit').mockResolvedValue({listing: []});
        vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({listing: []});
        vi.spyOn(admin, 'listScratchCleanupPreview').mockResolvedValue({listing: [], paginator: {total: 0}});
        render(<ScratchCleanupContent/>);
        expect(await screen.findByRole('button', {name: 'Preview'})).toBeDisabled();
        expect(screen.getByRole('button', {name: 'Run backfill'})).toBeDisabled();
        expect(screen.getByText('Only a cluster administrator can run a backfill.')).toBeVisible();
    });

    it('asks for a preview of the file system and offers no way past the active owner rule', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isAdmin').mockReturnValue(true);
        const admin = context.client().schedulerAdmin();
        vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: []});
        vi.spyOn(admin, 'listScratchCleanupAudit').mockResolvedValue({listing: []});
        vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({listing: []});
        const start = vi.spyOn(admin, 'previewScratchCleanup').mockResolvedValue({started: true});
        render(<ScratchCleanupContent/>);
        await userEvent.click(await screen.findByRole('button', {name: 'Preview'}));
        expect(start).toHaveBeenCalledWith({file_system: 'scratch'});
        expect(screen.queryByRole('checkbox', {name: /active jobs/})).toBeNull();
        expect(screen.getByText('All their folders stay')).toBeVisible();
    });

    it('puts a stopped last run at the top of the page', async () => {
        setup({runs: [{run_id: 'r', file_system: 'scratch', status: 'failed', message: 'Removing a folder failed.'}]});
        expect(await screen.findByText('The last run stopped')).toBeVisible();
    });

    it('stops and allows removals through the settings', async () => {
        const calls = setup();
        await userEvent.click(await screen.findByRole('button', {name: 'Stop all removals'}));
        expect(calls.update).toHaveBeenCalledWith({module_id: 'scheduler', settings: {scratch_cleanup: {kill_switch: true}}});
    });

    it('lets only an administrator allow removals again', async () => {
        const context = initTestAppContext();
        const admin = vi.spyOn(context.auth(), 'isAdmin').mockReturnValue(false);
        vi.spyOn(context.client().schedulerAdmin(), 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings: {...settings, kill_switch: true}, runs: [], previews: []});
        vi.spyOn(context.client().schedulerAdmin(), 'listScratchCleanupAudit').mockResolvedValue({listing: []});
        vi.spyOn(context.client().schedulerAdmin(), 'listScratchCleanupUpcoming').mockResolvedValue({listing: []});
        render(<ScratchCleanupContent/>);
        expect(await screen.findByRole('button', {name: 'Allow removals'})).toBeDisabled();
        expect(admin).toHaveBeenCalled();
    });

    it('shows last runs in plain words', async () => {
        setup({runs: [{run_id: 'r', file_system: 'scratch', trigger: 'schedule', status: 'stopped', removed_folders: 0, removed_bytes: 0, message: 'PBS lists no active jobs while 3 nodes are running jobs.'}]});
        expect((await screen.findAllByText('PBS lists no active jobs while 3 nodes are running jobs.'))[0]).toBeVisible();
        expect(screen.getByText('Stopped')).toBeVisible();
        expect(screen.getByText('Hourly check')).toBeVisible();
    });

    it('lists the removal log and pages through it', async () => {
        const context = initTestAppContext();
        const admin = context.client().schedulerAdmin();
        vi.spyOn(admin, 'getScratchCleanupStatus').mockResolvedValue({available: true, file_systems: [fileSystem], settings, runs: [], previews: []});
        vi.spyOn(admin, 'listScratchCleanupUpcoming').mockResolvedValue({listing: []});
        const audit = vi.spyOn(admin, 'listScratchCleanupAudit')
            .mockResolvedValueOnce({listing: [{...row, action: 'removed', run_id: 'a'}], next_token: 'next'})
            .mockResolvedValueOnce({listing: [{...row, path: '/lustre/bob/9', action: 'removed', run_id: 'b'}]});
        render(<ScratchCleanupContent/>);
        // the runs table has a Removed column, so wait for the log's own row
        expect(await screen.findByText('/lustre/alice/123')).toBeVisible();
        await userEvent.click(await screen.findByRole('button', {name: 'Show more'}));
        expect(await screen.findByText('/lustre/bob/9')).toBeVisible();
        expect(audit).toHaveBeenLastCalledWith({paginator: {page_size: 50, cursor: 'next'}});
    });

    it('reports an error it cannot recover from', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.client().schedulerAdmin(), 'getScratchCleanupStatus').mockRejectedValue(new Error('Not authorized'));
        render(<ScratchCleanupContent/>);
        expect(await screen.findByText('Not authorized')).toBeVisible();
    });

    it('formats sizes', () => {
        expect(formatBytes(undefined)).toBe('-');
        expect(formatBytes(512)).toBe('512 B');
        expect(formatBytes(1536)).toBe('1.5 KiB');
        expect(formatBytes(17 * 2 ** 40)).toBe('17.0 TiB');
    });
});
