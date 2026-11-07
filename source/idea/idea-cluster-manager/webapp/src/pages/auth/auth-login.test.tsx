import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import IdeaAuthLogin from './auth-login';

beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'setInterval').mockImplementation(() => 0 as unknown as ReturnType<typeof setInterval>);
});

afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
});

it.each(['FAIL', 'SUCCESS', undefined])('shows login when automatic SSO stops with status %s and allows an explicit retry', async status => {
    const context = initTestAppContext();
    window.idea.app.sso = true;
    window.idea.app.sso_auth_status = status;
    vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
    const initiate = vi.spyOn(context.auth(), 'initiateSso').mockImplementation(() => new Promise(() => {}));
    render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
    await act(async () => {});
    const button = screen.getByRole('button', {name: 'Login with SSO'});
    expect(initiate).not.toHaveBeenCalled();
    fireEvent.click(button);
    expect(initiate).toHaveBeenCalledTimes(1);
});

describe('sign-in messages', () => {
    it('tells the person when SSO came back without a session', async () => {
        const context = initTestAppContext();
        window.idea.app.sso = true;
        window.idea.app.sso_auth_status = 'FAIL';
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
        render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        await act(async () => {});
        expect(screen.getByText(/Sign-in with your company account did not complete/)).toBeInTheDocument();
    });

    it('says the same once the automatic retry gives up', async () => {
        const context = initTestAppContext();
        window.idea.app.sso = true;
        window.idea.app.sso_auth_status = undefined;
        sessionStorage.setItem('idea.sso-last-redirect', 'blocked');
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
        render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        await act(async () => {});
        expect(screen.getByText(/did not complete/)).toBeInTheDocument();
        sessionStorage.clear();
    });

    it('shows no SSO failure on an ordinary visit', async () => {
        const context = initTestAppContext();
        window.idea.app.sso = true;
        window.idea.app.sso_auth_status = undefined;
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
        render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        await act(async () => {});
        expect(screen.queryByText(/did not complete/)).not.toBeInTheDocument();
    });

    it('explains an ended session once', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
        sessionStorage.setItem('idea.session-ended', 'true');
        const first = render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        await act(async () => {});
        expect(screen.getByText('Your session ended. Sign in to continue.')).toBeInTheDocument();
        first.unmount();
        render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        await act(async () => {});
        expect(screen.queryByText('Your session ended. Sign in to continue.')).not.toBeInTheDocument();
    });

    it.each([
        ['SERVER_ERROR', 'The portal had a problem. Try again in a minute.', 'secret'],
        ['NOT_AUTHORIZED', 'The username or password is not correct.', '']
    ])('after %s says %s', async (errorCode, message, password) => {
        vi.useRealTimers();
        const context = initTestAppContext();
        window.idea.app.sso = false;
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
        vi.spyOn(context.auth(), 'login').mockRejectedValue({errorCode, message: 'The portal had a problem. Try again in a minute.'});
        render(<MemoryRouter><IdeaAuthLogin/></MemoryRouter>);
        const fill = async (label: RegExp, value: string) => fireEvent.change((await screen.findByLabelText(label)).querySelector('input') ?? screen.getByLabelText(label), {target: {value}});
        await fill(/username/i, 'someone');
        await fill(/password/i, 'secret');
        fireEvent.click(screen.getByRole('button', {name: 'Sign In'}));
        expect(await screen.findByText(message)).toBeInTheDocument();
        expect(screen.getByLabelText(/password/i)).toHaveValue(password);
    });
});
