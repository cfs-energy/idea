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
    localStorage.clear();
    vi.stubGlobal('window', {
        idea: {app: {sso: true, sso_url: '/sso', sso_auth_status: 'SUCCESS', sso_auth_code: null}},
        location: {pathname: '/', href: 'https://portal.example/#/soca/active-jobs', reload: vi.fn()},
        addEventListener: vi.fn()
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
    localStorage.clear();
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


it('keeps logout shared across tabs, including when another tab accepts worker claims', async () => {
    const tabA = new AuthService(props);
    const tabB = new AuthService(props);
    await tabA.logout();
    expect(localStorage.getItem('idea.sso-logged-out')).toBe('true');
    // Tab B has its own empty sessionStorage, but shares localStorage and the client.
    vi.stubGlobal('sessionStorage', {getItem: vi.fn().mockReturnValue(null), setItem: vi.fn()});
    const redirect = vi.spyOn(tabB, 'initiateSso');
    await expect(tabB.isLoggedIn()).resolves.toBe(false);
    client.isLoggedIn.mockResolvedValue(true);
    await expect(tabB.isLoggedIn()).resolves.toBe(true);
    expect(localStorage.getItem('idea.sso-logged-out')).toBe('true');
    client.isLoggedIn.mockResolvedValue(false);
    await vi.advanceTimersByTimeAsync(60001);
    await expect(tabB.isLoggedIn()).resolves.toBe(false);
    expect(redirect).not.toHaveBeenCalled();
    expect(client.initiateAuth).not.toHaveBeenCalled();
});

it('clears the logout marker for an SSO callback with a code', async () => {
    await new AuthService(props).logout();
    window.idea.app.sso_auth_code = 'fresh-code';
    await expect(new AuthService(props).isLoggedIn()).resolves.toBe(true);
    expect(localStorage.getItem('idea.sso-logged-out')).toBeNull();
    expect(client.initiateAuth).toHaveBeenCalledWith({auth_flow: 'SSO_AUTH', authorization_code: 'fresh-code'});
});

it('reloads a persisted page restored while the SSO redirect promise is pending', async () => {
    const auth = new AuthService(props);
    const pending = auth.isLoggedIn();
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.href).toBe('/sso');
    expect(auth.isLoggedIn()).toBe(pending);
    expect(window.addEventListener).toHaveBeenCalledWith('pageshow', expect.any(Function));
    const listener = vi.mocked(window.addEventListener).mock.calls[0][1] as EventListener;
    listener(new PageTransitionEvent('pageshow', {persisted: false}));
    expect(window.location.reload).not.toHaveBeenCalled();
    listener(new PageTransitionEvent('pageshow', {persisted: true}));
    expect(window.location.reload).toHaveBeenCalledOnce();
});

it('clears the recovery latch on successful password login', async () => {
    sessionStorage.setItem('idea.sso-last-redirect', 'blocked');
    const auth = new AuthService(props);
    await auth.login('user', 'password');
    expect(sessionStorage.getItem('idea.sso-last-redirect')).toBeNull();
});

describe.each((['localStorage', 'sessionStorage'] as const).flatMap(storage =>
    (['getter', 'getItem', 'setItem', 'removeItem'] as const).map(access => [storage, access] as const)
))('unavailable %s: %s', (storage, access) => {
    let auth: AuthService;
    let callback: AuthService;
    beforeEach(() => {
        // Storage becomes unavailable after construction; AppLogger also reads it on startup.
        auth = new AuthService(props);
        callback = new AuthService(props);
        const fail = () => { throw new Error('Storage denied'); };
        if (access === 'getter') {
            vi.spyOn(globalThis, storage, 'get').mockImplementation(fail);
        } else {
            vi.spyOn(Object.getPrototypeOf(globalThis[storage]), access).mockImplementation(fail);
        }
    });

    if (access === 'getter' || access === 'setItem') it('always clears tokens on logout', async () => {
        await expect(auth.logout()).resolves.toBe(true);
        expect(client.logout).toHaveBeenCalledOnce();
        expect(auth.isAccessLoaded()).toBe(false);
    });

    if (access === 'getter' || access === 'removeItem') it('allows password login and its hook', async () => {
        window.idea.app.sso = false;
        const login = vi.fn().mockResolvedValue(true);
        auth.setHooks(login, vi.fn());
        await expect(auth.login('user', 'password')).resolves.toBe(true);
        expect(login).toHaveBeenCalledOnce();
        expect(auth.isAccessLoaded()).toBe(true);
    });

    it('disables automatic SSO but permits explicit SSO and its callback', async () => {
        if (access === 'setItem') await auth.logout();
        if (access === 'removeItem') await auth.login('user', 'password');
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
        await expect(callback.isLoggedIn()).resolves.toBe(true);
    });
});
