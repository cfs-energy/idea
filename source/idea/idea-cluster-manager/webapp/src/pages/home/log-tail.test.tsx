import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import { initTestAppContext } from '../../test-support';
import LogTail, { describeTailFailure, IdeaLogTail } from './log-tail';

const written: string[] = [];

vi.mock('@xterm/xterm', () => ({
    Terminal: class {
        options = {};
        open() {}
        onKey() {}
        loadAddon() {}
        writeln(line: string) { written.push(line); }
    }
}));
vi.mock('@xterm/addon-fit', () => ({
    FitAddon: class {
        activate() {}
        fit() {}
    }
}));

function renderTail(tailFile: (...args: any[]) => Promise<any>) {
    const context = initTestAppContext();
    const spy = vi.spyOn(context.client().fileBrowser(), 'tailFile').mockImplementation(tailFile as any);
    render(
        <MemoryRouter initialEntries={['/?file=/home/testuser/run.log&cwd=/home/testuser']}>
            <LogTail {...({} as any)} />
        </MemoryRouter>
    );
    return spy;
}

const POLL = 10000;

/** One poll at a time: React applies state between polls, and the next poll depends on it. */
async function polls(count: number) {
    for (let i = 0; i < count; i++) {
        await act(async () => { await vi.advanceTimersByTimeAsync(POLL); });
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    }
}

describe('watching the end of a file', () => {
    beforeEach(() => {
        written.length = 0;
        vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('says it has paused after a quiet stretch, instead of only unticking the box', async () => {
        renderTail(() => Promise.resolve({ lines: [], next_token: 'token' }));
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
        await polls(11);

        expect(screen.getByText(/Paused: no new lines for 2 minutes/)).toBeInTheDocument();
        expect(screen.getByRole('checkbox')).not.toBeChecked();
    });

    it('runs one poll even when asked to start twice', () => {
        const started = vi.spyOn(globalThis, 'setInterval');
        const already = { interval: 1 };
        IdeaLogTail.prototype.startPolling.call(already as any);
        expect(started).not.toHaveBeenCalled();
    });

    it('stops saying it keeps trying once repeated failures end the polling', async () => {
        let reads = 0;
        renderTail(() => reads++ === 0
            ? Promise.resolve({ lines: [], next_token: 'token' })
            : Promise.reject({ errorCode: 'GENERAL_ERROR', message: 'no' }));
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
        await polls(13);
        expect(screen.getByRole('checkbox')).not.toBeChecked();
        expect(screen.getByText(/Tick Auto-refresh to try again/)).toBeInTheDocument();
    });

    it('keeps polling when a line arrives before the limit', async () => {
        let call = 0;
        renderTail(() => {
            call += 1;
            return Promise.resolve({ lines: call === 6 ? ['more'] : [], next_token: 'token' });
        });
        await polls(11);

        expect(screen.queryByText(/Paused/)).toBeNull();
        expect(screen.getByRole('checkbox')).toBeChecked();
    });

    it('does not claim to be refreshing when the first read failed, and ticking the box tries again', async () => {
        const spy = renderTail(() => Promise.reject({ errorCode: 'FILE_NOT_FOUND', message: 'File not found: /x' }));
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });

        expect(screen.getByText(/This file is gone or was moved/)).toBeInTheDocument();
        expect(screen.queryByText(/File not found: \/x/)).toBeNull();
        expect(screen.getByRole('checkbox')).not.toBeChecked();

        const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
        spy.mockImplementation((() => Promise.resolve({ lines: ['back'], next_token: 'token' })) as any);
        await user.click(screen.getByRole('checkbox'));
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });

        expect(written).toContain('back');
        expect(screen.getByRole('checkbox')).toBeChecked();
    });

    it('keeps the indentation of a line', async () => {
        renderTail(() => Promise.resolve({ lines: ['  at frame.py line 3\n'], next_token: 'token' }));
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });

        expect(written).toEqual(['  at frame.py line 3']);
    });

    it('says when the file is empty, until a line arrives', async () => {
        let call = 0;
        renderTail(() => {
            call += 1;
            return Promise.resolve({ lines: call === 1 ? [] : ['first'], next_token: 'token' });
        });
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
        expect(screen.getByText(/This file is empty/)).toBeInTheDocument();

        await polls(1);
        expect(screen.queryByText(/This file is empty/)).toBeNull();
    });

    it('explains each failure in plain words', () => {
        expect(describeTailFailure({ errorCode: 'TAIL_THROTTLE', message: 'tail file request throttled' }).text).toMatch(/Too many live views/);
        expect(describeTailFailure({ errorCode: 'TAIL_THROTTLE' }).type).toBe('warning');
        expect(describeTailFailure({ errorCode: 'NOT_A_TEXT_FILE', message: 'file is not a text file' }).text).toMatch(/isn't a text file/);
        expect(describeTailFailure({ errorCode: 'NOT_A_TEXT_FILE', message: 'file is too large (11 GB)' }).text).toMatch(/too large to watch/);
    });
});
