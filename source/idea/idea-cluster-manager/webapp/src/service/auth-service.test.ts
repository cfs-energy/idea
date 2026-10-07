import {vi} from 'vitest';
import AuthService, {AuthServiceProps} from './auth-service';
import {JwtTokenClaims} from '../common/token-utils';

const claims: JwtTokenClaims = {
    username: 'user', groups: [], issued_at: 1, expires_at: 2, auth_time: 1,
    scope: [], email: '', cluster_name: 'test', aws_region: 'us-east-1', email_verified: true
};
const client = {
    isLoggedIn: vi.fn(), getClaims: vi.fn(), initiateAuth: vi.fn(), logout: vi.fn()
};
const props = {
    clients: {auth: () => client},
    reporting: {getCapabilities: vi.fn().mockResolvedValue({can_read_reporting: false})}
} as unknown as AuthServiceProps;

beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-07T12:00:00Z'));
    sessionStorage.clear();
    vi.stubGlobal('window', {
        idea: {app: {sso: true, sso_url: '/sso', sso_auth_status: 'SUCCESS', sso_auth_code: null}},
        location: {pathname: '/', href: 'https://portal.example/#/soca/active-jobs'}
    });
    client.isLoggedIn.mockReset().mockResolvedValue(false);
    client.getClaims.mockReset().mockResolvedValue(claims);
    client.initiateAuth.mockReset().mockResolvedValue({});
    client.logout.mockReset().mockResolvedValue(true);
});

afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    sessionStorage.clear();
});

it.each(['/', '/portal', '/portal/'])('recovers a lost worker session through SSO at %s', async path => {
    window.location.pathname = '/different-entry';
    window.idea.app.sso_url = `${path.replace(/\/$/, '')}/sso`;
    const auth = new AuthService(props);
    const settled = vi.fn();
    const check = auth.isLoggedIn();
    check.then(settled);
    expect(auth.isLoggedIn()).toBe(check);
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe(`${path.replace(/\/$/, '')}/sso`);
    // Do not resolve false and trigger callers' logout while navigation is pending.
    expect(settled).not.toHaveBeenCalled();
    expect(client.initiateAuth).not.toHaveBeenCalled();
});

it('latches repeated session loss across reloads until explicit SSO login', async () => {
    new AuthService(props).isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    window.location.href = 'https://portal.example/';
    window.idea.app.sso_auth_code = 'fresh-code';
    const reloaded = new AuthService(props);
    await expect(reloaded.isLoggedIn()).resolves.toBe(true);
    expect(window.idea.app.sso_auth_code).toBeNull();
    expect(client.initiateAuth).toHaveBeenCalledWith({auth_flow: 'SSO_AUTH', authorization_code: 'fresh-code'});
    await expect(reloaded.isLoggedIn()).resolves.toBe(false);
    await vi.advanceTimersByTimeAsync(59999);
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(false);
    expect(window.location.href).toBe('https://portal.example/');
    await vi.advanceTimersByTimeAsync(1);
    const settled = vi.fn();
    new AuthService(props).isLoggedIn().then(settled, settled);
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toHaveBeenCalledWith(false);
    expect(window.location.href).toBe('https://portal.example/');
    reloaded.initiateSso();
    expect(window.location.href).toBe('/sso');
    window.idea.app.sso_auth_code = 'explicit-code';
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(true);
});

it('does not retry a failed SSO callback even after the redirect guard expires', async () => {
    new AuthService(props).isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    window.location.href = 'https://portal.example/';
    window.idea.app.sso_auth_status = 'FAIL';
    await vi.advanceTimersByTimeAsync(60001);
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(false);
    expect(window.location.href).toBe('https://portal.example/');
    expect(client.initiateAuth).not.toHaveBeenCalled();
});

it.each(['SUCCESS', undefined])('keeps explicit logout across reloads with status %s', async status => {
    const auth = new AuthService(props);
    window.idea.app.sso_auth_code = 'unused-code';
    await auth.logout();
    expect(window.idea.app.sso_auth_code).toBeNull();
    window.idea.app.sso_auth_status = status;
    await vi.advanceTimersByTimeAsync(60001);
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(false);
    expect(window.location.href).toBe('https://portal.example/#/soca/active-jobs');
    expect(client.initiateAuth).not.toHaveBeenCalled();
});

it('does not treat automatic session cleanup as explicit logout', async () => {
    const auth = new AuthService(props);
    await auth.logout(false);
    auth.isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe('/sso');
});

it('allows an explicit SSO login after logout and a failed callback', async () => {
    const auth = new AuthService(props);
    await auth.logout();
    window.idea.app.sso_auth_status = 'FAIL';
    auth.initiateSso();
    expect(window.location.href).toBe('/sso');
    window.idea.app.sso_auth_status = 'SUCCESS';
    window.idea.app.sso_auth_code = 'fresh-code';
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(true);
});

it('clears explicit logout after a successful password login', async () => {
    const auth = new AuthService(props);
    await auth.logout();
    await auth.login('user', 'password');
    auth.isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe('/sso');
});

it('preserves the initial SSO redirect when no callback status is present', async () => {
    delete window.idea.app.sso_auth_status;
    new AuthService(props).isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe('/sso');
    window.location.href = 'https://portal.example/';
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(false);
    expect(window.location.href).toBe('https://portal.example/');
});

it.each([false, undefined])('leaves non-SSO clusters at login (sso=%s)', async sso => {
    window.idea.app.sso = sso;
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(false);
    expect(window.location.href).toBe('https://portal.example/#/soca/active-jobs');
});

it('propagates transient errors for the existing route retry without redirecting', async () => {
    client.isLoggedIn.mockRejectedValueOnce(new Error('Renewal timed out'));
    const auth = new AuthService(props);
    await expect(auth.isLoggedIn()).rejects.toThrow('Renewal timed out');
    expect(window.location.href).toBe('https://portal.example/#/soca/active-jobs');
    client.isLoggedIn.mockResolvedValue(true);
    await expect(auth.isLoggedIn()).resolves.toBe(true);
});


it.each(['worker', 'sso'])('clears the logout marker when accepting a %s session', async source => {
    await new AuthService(props).logout();
    client.isLoggedIn.mockResolvedValue(true);
    const auth = new AuthService(props);
    await expect(source === 'worker' ? auth.isLoggedIn() : auth.login_using_sso_auth_code('code')).resolves.toBe(true);
    expect(sessionStorage.getItem('idea.sso-logged-out')).toBeNull();
    client.isLoggedIn.mockResolvedValue(false);
    auth.isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe('/sso');
});

it('clears the recovery latch on successful password login', async () => {
    sessionStorage.setItem('idea.sso-last-redirect', 'blocked');
    const auth = new AuthService(props);
    await auth.login('user', 'password');
    expect(sessionStorage.getItem('idea.sso-last-redirect')).toBeNull();
});

describe.each(['getter', 'getItem', 'setItem', 'removeItem'] as const)('unavailable sessionStorage: %s', access => {
    beforeEach(() => {
        const fail = () => { throw new Error('Storage denied'); };
        if (access === 'getter') {
            vi.spyOn(globalThis, 'sessionStorage', 'get').mockImplementation(fail);
        } else {
            vi.spyOn(Object.getPrototypeOf(sessionStorage), access).mockImplementation(fail);
        }
    });

    if (access === 'getter' || access === 'setItem') it('always clears tokens on logout', async () => {
        const auth = new AuthService(props);
        await expect(auth.logout()).resolves.toBe(true);
        expect(client.logout).toHaveBeenCalledOnce();
        expect(auth.isAccessLoaded()).toBe(false);
    });

    if (access === 'getter' || access === 'removeItem') it('allows password login and its hook', async () => {
        window.idea.app.sso = false;
        const auth = new AuthService(props);
        const login = vi.fn().mockResolvedValue(true);
        auth.setHooks(login, vi.fn());
        await expect(auth.login('user', 'password')).resolves.toBe(true);
        expect(login).toHaveBeenCalledOnce();
        expect(auth.isAccessLoaded()).toBe(true);
    });

    it('disables automatic SSO but permits explicit SSO and its callback', async () => {
        const auth = new AuthService(props);
        // Exercise marker clearing as well as guard reads/writes before losing the session.
        client.isLoggedIn.mockResolvedValue(true);
        await expect(auth.isLoggedIn()).resolves.toBe(true);
        client.isLoggedIn.mockResolvedValue(false);
        const settled = vi.fn();
        auth.isLoggedIn().then(settled, settled);
        await vi.advanceTimersByTimeAsync(0);
        expect(settled).toHaveBeenCalledWith(false);
        expect(window.location.href).toBe('https://portal.example/#/soca/active-jobs');
        auth.initiateSso();
        expect(window.location.href).toBe('/sso');
        window.idea.app.sso_auth_code = 'manual-code';
        await expect(new AuthService(props).isLoggedIn()).resolves.toBe(true);
    });
});
