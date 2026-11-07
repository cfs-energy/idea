import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { initTestAppContext } from '../../test-support';
import DeleteFolderDialog, { removeCommand } from './delete-folder-dialog';

const HOME = '/data/home/person';

function renderDialog(usage: any, props: { path?: string; home?: string } = {}) {
    const context = initTestAppContext();
    const getStorageUsage = vi.spyOn(context.client().fileBrowser(), 'getStorageUsage').mockResolvedValue(usage);
    render(<DeleteFolderDialog path={props.path ?? `${HOME}/runs`} name="runs" home={props.home} onClose={() => {}} onDeleted={() => {}} />);
    return getStorageUsage;
}

describe('delete folder dialog', () => {
    afterEach(() => vi.restoreAllMocks());

    it('explains a folder outside home without asking for a name it would never act on', async () => {
        const getStorageUsage = renderDialog({ state: 'ready', home: HOME }, { path: '/data/shared/runs', home: HOME });

        expect(await screen.findByText(/Folders outside your home can't be deleted here/)).toBeInTheDocument();
        expect(screen.queryByText(/to confirm deletion/)).toBeNull();
        expect(getStorageUsage).not.toHaveBeenCalled();
    });

    it('explains a folder the server cannot measure', async () => {
        renderDialog({ state: 'unavailable', home: HOME, message: 'storage analytics, which is not on' });

        expect(await screen.findByText(/Folders outside your home can't be deleted here/)).toBeInTheDocument();
        expect(screen.queryByText(/storage analytics/)).toBeNull();
    });

    it('says a folder has not been measured yet, rather than too large', async () => {
        renderDialog({
            state: 'ready', home: HOME, measured_at: 1,
            folder: { identity: { inode: '2' }, bytes: 0, files: 0, newest_mtime: null, partial: true, measured: false }
        });
        expect(await screen.findByText(/has not been measured yet/)).toBeInTheDocument();
        expect(screen.queryByText(/too large to measure/)).toBeNull();
        expect(screen.queryByText(/At least 0/)).toBeNull();
    });

    it('gives a next step when a folder is too large to measure', async () => {
        renderDialog({
            state: 'ready', home: HOME, measured_at: 1,
            folder: { identity: { device: '1', inode: '2' }, bytes: 1, files: 1, newest_mtime: null, partial: true }
        });

        expect(await screen.findByText(/too large to measure, so it can't be deleted here/)).toBeInTheDocument();
        expect(screen.getByText(`rm -r '${HOME}/runs'`)).toBeInTheDocument();
    });

    it('says a delete that ran out of time is a size problem, not a permission problem', async () => {
        const user = userEvent.setup();
        const context = initTestAppContext();
        const client = context.client().fileBrowser();
        vi.spyOn(client, 'getStorageUsage').mockResolvedValue({
            state: 'ready', home: HOME, measured_at: 1,
            folder: { identity: { device: '1', inode: '2' }, bytes: 1, files: 1, newest_mtime: null, partial: false }
        } as any);
        vi.spyOn(client, 'deleteFolder').mockRejectedValue({ errorCode: 'FILE_OPERATION_TIMED_OUT', message: 'This took too long and was stopped.' });
        render(<DeleteFolderDialog path={`${HOME}/runs`} name="runs" onClose={() => {}} onDeleted={() => {}} />);

        await user.type(await screen.findByLabelText(/Type runs to confirm/), 'runs');
        await user.click(screen.getByRole('button', { name: 'Delete folder' }));

        expect(await screen.findByText(/took too long to delete here/)).toBeInTheDocument();
        expect(screen.getByText(`rm -r '${HOME}/runs'`)).toBeInTheDocument();
    });

    it('quotes the path so the command is safe to paste', () => {
        expect(removeCommand("/data/o'brien's run")).toBe(`rm -r '/data/o'\\''brien'\\''s run'`);
    });
});
