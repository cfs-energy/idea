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
