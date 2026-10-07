import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import {MemoryRouter, Route, Routes} from 'react-router-dom';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import IdeaAuthenticatedRoute from './auth-route';

beforeEach(() => {
    vi.useFakeTimers();
    // Isolate guard retries from AppContext's independent heartbeat.
    vi.spyOn(globalThis, 'setInterval').mockImplementation(() => 0 as unknown as ReturnType<typeof setInterval>);
});
afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
});

describe.each(['/soca/active-jobs', '/soca/settings'])('access loading at %s', path => {
    it('waits for claims, warns without access, and renders children with access', async () => {
        const context = initTestAppContext();
        const auth = context.auth();
        const client = context.client().auth();
        vi.spyOn(client, 'isLoggedIn').mockResolvedValue(true);
        const claims = {
            username: 'user', groups: [] as string[], issued_at: 1, expires_at: 2,
            auth_time: 1, scope: [], email: '', cluster_name: 'test',
            aws_region: 'us-east-1', email_verified: true
        };
        vi.spyOn(client, 'getClaims').mockImplementation(async () => claims);
        vi.spyOn(context.reporting(), 'getCapabilities').mockResolvedValue({can_read_reporting: false});
        context.getClusterSettingsService().clusterModules = [{name: 'scheduler', status: 'deployed'}];
        // App subscribes to the same notifications, including when a renewal check fails.
        const route = () => <MemoryRouter initialEntries={[path]}>
            <IdeaAuthenticatedRoute isLoggedIn><p>Protected page</p><input aria-label="Draft" defaultValue=""/></IdeaAuthenticatedRoute>
        </MemoryRouter>;
        const {rerender} = render(route());
        const unsubscribe = auth.subscribeReporting(() => rerender(route()));
        try {
            expect(screen.queryByText('Destination unavailable')).not.toBeInTheDocument();
            expect(screen.queryByText('Protected page')).not.toBeInTheDocument();
            expect(screen.getByTestId('access-loading')).toBeInTheDocument();

            await act(async () => { await auth.isLoggedIn(); });
            expect(screen.getByText('Destination unavailable')).toBeInTheDocument();
            expect(screen.queryByText('Protected page')).not.toBeInTheDocument();

            claims.groups = ['scheduler-administrators-module-group'];
            await act(async () => { await auth.isLoggedIn(); });
            expect(screen.queryByText('Destination unavailable')).not.toBeInTheDocument();
            expect(screen.getByText('Protected page')).toBeInTheDocument();
            expect(screen.queryByTestId('access-loading')).not.toBeInTheDocument();

            const draft = screen.getByRole('textbox', {name: 'Draft'});
            fireEvent.change(draft, {target: {value: 'unsaved edits'}});
            vi.mocked(client.isLoggedIn).mockRejectedValue(new Error('Renewal timed out'));
            await act(async () => { await expect(auth.isLoggedIn()).rejects.toThrow('Renewal timed out'); });
            expect(screen.queryByText('Destination unavailable')).not.toBeInTheDocument();
            expect(screen.getByText('Protected page')).toBeInTheDocument();
            expect(draft).toBeInTheDocument();
            expect(draft).toHaveValue('unsaved edits');
            expect(screen.getByTestId('access-loading')).toBeInTheDocument();

            vi.mocked(client.isLoggedIn).mockResolvedValue(true);
            await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
            expect(screen.queryByText('Destination unavailable')).not.toBeInTheDocument();
            expect(screen.getByText('Protected page')).toBeInTheDocument();
            expect(screen.getByRole('textbox', {name: 'Draft'})).toBe(draft);
            expect(draft).toHaveValue('unsaved edits');
            expect(screen.queryByTestId('access-loading')).not.toBeInTheDocument();
        } finally {
            unsubscribe();
        }
    });
});

function renderRecoveringRoute() {
    const context = initTestAppContext();
    const auth = context.auth();
    const check = vi.spyOn(auth, 'isLoggedIn').mockRejectedValue(new Error('Renewal timed out'));
    const loaded = vi.spyOn(auth, 'isAccessLoaded').mockReturnValue(false);
    const route = render(<MemoryRouter initialEntries={['/protected']}>
        <Routes>
            <Route path="/protected" element={<IdeaAuthenticatedRoute isLoggedIn><p>Protected page</p></IdeaAuthenticatedRoute>}/>
            <Route path="/auth/login" element={<IdeaAuthenticatedRoute isLoggedIn><p>Login page</p></IdeaAuthenticatedRoute>}/>
        </Routes>
    </MemoryRouter>);
    return {check, loaded, ...route};
}

it('backs off after errors and renders recovered claims without a parent update', async () => {
    const {check, loaded} = renderRecoveringRoute();
    await act(async () => {});
    expect(check).toHaveBeenCalledTimes(1);
    for (const delay of [2000, 5000, 10000, 30000, 30000]) {
        const calls = check.mock.calls.length;
        await act(async () => { await vi.advanceTimersByTimeAsync(delay - 1); });
        expect(check).toHaveBeenCalledTimes(calls);
        await act(async () => { await vi.advanceTimersByTimeAsync(1); });
        expect(check).toHaveBeenCalledTimes(calls + 1);
    }
    check.mockImplementation(async () => { loaded.mockReturnValue(true); return true; });
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByText('Protected page')).toBeInTheDocument();
    expect(screen.queryByTestId('access-loading')).not.toBeInTheDocument();
    const calls = check.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(check).toHaveBeenCalledTimes(calls);
});

it('navigates to login when the session check resolves false', async () => {
    const {check} = renderRecoveringRoute();
    await act(async () => {});
    check.mockResolvedValue(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText('Login page')).toBeInTheDocument();
    expect(screen.queryByText('Protected page')).not.toBeInTheDocument();
});

it.each(['visibilitychange', 'online'])('retries immediately on %s and cleans up on unmount', async event => {
    const {check, loaded, unmount} = renderRecoveringRoute();
    await act(async () => {});
    const target = event === 'visibilitychange' ? document : window;
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    if (event === 'visibilitychange') {
        await act(async () => { target.dispatchEvent(new Event(event)); });
        expect(check).toHaveBeenCalledTimes(1);
    }
    visibility.mockReturnValue('visible');
    let finish!: (status: boolean) => void;
    check.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    await act(async () => { target.dispatchEvent(new Event(event)); });
    expect(check).toHaveBeenCalledTimes(2);
    await act(async () => {
        target.dispatchEvent(new Event(event));
        await vi.advanceTimersByTimeAsync(2000);
    });
    expect(check).toHaveBeenCalledTimes(2);
    await act(async () => { loaded.mockReturnValue(true); finish(true); });
    expect(screen.getByText('Protected page')).toBeInTheDocument();
    unmount();
    loaded.mockReturnValue(false);
    await act(async () => {
        target.dispatchEvent(new Event(event));
        await vi.advanceTimersByTimeAsync(30000);
    });
    expect(check).toHaveBeenCalledTimes(2);
});

it('cancels pending retries and ignores checks that settle after unmount', async () => {
    const {check, unmount} = renderRecoveringRoute();
    await act(async () => {});
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(check).toHaveBeenCalledTimes(1);

    const pending = renderRecoveringRoute();
    await act(async () => {});
    let reject!: (error: Error) => void;
    pending.check.mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
    await act(async () => { window.dispatchEvent(new Event('online')); });
    pending.unmount();
    await act(async () => {
        reject(new Error('Renewal timed out'));
        await vi.advanceTimersByTimeAsync(30000);
    });
    expect(pending.check).toHaveBeenCalledTimes(2);
});
