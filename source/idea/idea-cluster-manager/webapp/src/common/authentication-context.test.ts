import {afterEach, vi} from 'vitest';
import { IdeaApiInvoker } from '../client/idea-api-invoker';
import { IdeaAuthenticationContext, isSameOrigin, LONG_REQUEST_TIMEOUT, NETWORK_TIMEOUT } from './authentication-context';
import { initTestAppData } from '../test-support';
import AppContext from './app-context';

describe('IdeaAuthenticationContext', () => {
    beforeEach(() => {
        initTestAppData();
    });

    it('initializes in local-storage mode and reports logged out with no tokens', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.isLoggedIn()).resolves.toBe(false);
    });

    it('has no access token before authentication', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.getAccessToken()).rejects.toMatchObject({
            error_code: 'AUTH_TOKEN_EXPIRED'
        });
    });
});

describe('request failures', () => {
    beforeEach(() => initTestAppData());
    afterEach(() => vi.unstubAllGlobals());
    const invoke = () => new IdeaAuthenticationContext({sessionManagement: 'local-storage', authEndpoint: 'http://localhost/api'})
        .invoke('http://localhost/api/X', {header: {namespace: 'X'}}, true);

    it('words a server failure for a person', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({status: 502}));
        await expect(invoke()).resolves.toMatchObject({error_code: 'SERVER_ERROR', message: 'The portal had a problem. Try again in a minute.'});
    });

    it('words a lost connection for a person', async () => {
        vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed')));
        await expect(invoke()).resolves.toMatchObject({error_code: 'NETWORK_ERROR', message: 'The portal could not be reached. Check your connection and try again.'});
    });
});

describe('request timeout', () => {
    beforeEach(() => {
        initTestAppData();
        vi.useFakeTimers();
        vi.stubGlobal('fetch', vi.fn((_: string, options: any) => new Promise((_resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
        })));
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });
    const context = () => new IdeaAuthenticationContext({sessionManagement: 'local-storage', authEndpoint: 'http://localhost/api'});
    const settled = (promise: Promise<any>) => {
        const state: {value?: any} = {};
        promise.then(value => {state.value = value;});
        return state;
    };

    it('gives up on an ordinary call at three minutes, not ten', async () => {
        const call = settled(context().invoke('http://localhost/api/X', {header: {namespace: 'X'}}, true));
        await vi.advanceTimersByTimeAsync(NETWORK_TIMEOUT - 1);
        expect(call.value).toBeUndefined();
        await vi.advanceTimersByTimeAsync(1);
        expect(NETWORK_TIMEOUT).toBe(180000);
        expect(call.value).toMatchObject({error_code: 'REQUEST_TIMEOUT'});
    });

    it('waits longer for a call that asks to', async () => {
        const call = settled(context().invoke('http://localhost/api/X', {header: {namespace: 'X'}}, true, LONG_REQUEST_TIMEOUT));
        await vi.advanceTimersByTimeAsync(NETWORK_TIMEOUT);
        expect(call.value).toBeUndefined();
        await vi.advanceTimersByTimeAsync(LONG_REQUEST_TIMEOUT - NETWORK_TIMEOUT);
        expect(call.value).toMatchObject({error_code: 'REQUEST_TIMEOUT'});
    });
});

describe('token renewal logging', () => {
    beforeEach(() => initTestAppData());
    afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear();});

    it('never writes tokens to the console at the most verbose level', async () => {
        const jwt = (claims: object) => `x.${btoa(JSON.stringify({exp: 1, username: 'marker-user', ...claims}))}.y`;
        localStorage.setItem('idea.auth.refresh-token', 'marker-refresh-token');
        localStorage.setItem('idea.auth.access-token', jwt({}));
        localStorage.setItem('idea.auth.id-token', jwt({}));
        const fresh = jwt({exp: 4102444800, marker: 'marker-new-access-token'});
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({status: 200, json: async () => ({success: true, payload: {auth: {access_token: fresh, id_token: fresh, refresh_token: 'marker-new-refresh-token'}}})}));
        const output = [vi.spyOn(console, 'log'), vi.spyOn(console, 'info'), vi.spyOn(console, 'warn'), vi.spyOn(console, 'error')];
        output.forEach(spy => spy.mockImplementation(() => {}));
        const context = new IdeaAuthenticationContext({sessionManagement: 'local-storage', authEndpoint: 'http://localhost/api'});
        await context.initializeAuthContext({authEndpoint: 'http://localhost/api', defaultLogLevel: 5});
        await expect(context.isLoggedIn()).resolves.toBe(true);
        expect(output[0]).toHaveBeenCalled();
        expect(JSON.stringify(output.flatMap(spy => spy.mock.calls))).not.toMatch(/marker-refresh-token|marker-new|eyJ|x\./);
    });
});

describe('session storage without a service worker', () => {
    afterEach(() => {vi.unstubAllGlobals(); localStorage.clear();});

    it.each([['in-memory', 0], ['local-storage', 4]] as const)('a %s session writes %i keys to browser storage at sign-in', async (mode, keys) => {
        initTestAppData();
        window.idea.app.session_management = mode;
        const jwt = `x.${btoa(JSON.stringify({exp: 4102444800, username: 'marker-user'}))}.y`;
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({status: 200, json: async () => ({success: true, payload: {auth: {access_token: jwt, id_token: jwt, refresh_token: 'marker-refresh-token'}}})}));
        const context: any = new AppContext({httpEndpoint: 'http://localhost', albEndpoint: 'http://localhost', releaseVersion: 'test', app: window.idea.app});
        await context.authContext.initiateAuth({header: {namespace: 'Auth.InitiateAuth'}, payload: {auth_flow: 'USER_PASSWORD_AUTH'}});
        await expect(context.authContext.isLoggedIn()).resolves.toBe(true);
        expect(Object.keys(localStorage).filter(key => key.startsWith('idea.auth.'))).toHaveLength(keys);
    });
});

describe('a renewal the user pool cannot answer', () => {
    beforeEach(() => initTestAppData());
    afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear();});

    const expiredSession = async (renewal: object) => {
        const jwt = `x.${btoa(JSON.stringify({exp: 1, username: 'marker-user'}))}.y`;
        localStorage.setItem('idea.auth.refresh-token', 'marker-refresh-token');
        localStorage.setItem('idea.auth.access-token', jwt);
        localStorage.setItem('idea.auth.id-token', jwt);
        const fetch = vi.fn().mockResolvedValue({status: 200, json: async () => renewal});
        vi.stubGlobal('fetch', fetch);
        const context = new IdeaAuthenticationContext({sessionManagement: 'local-storage', authEndpoint: 'http://localhost/api'});
        await context.initializeAuthContext({authEndpoint: 'http://localhost/api', defaultLogLevel: 1});
        return context.invoke('http://localhost/api/X', {header: {namespace: 'X'}});
    };

    it('keeps the session and fails only this call', async () => {
        const result = await expiredSession({success: false, error_code: 'GENERAL_ERROR', message: 'Rate exceeded'});
        expect(result.error_code).toBe('GENERAL_ERROR');
        expect(localStorage.getItem('idea.auth.refresh-token')).toBe('marker-refresh-token');
    });

    it('ends the session when the refresh token is refused', async () => {
        const result = await expiredSession({success: false, error_code: 'UNAUTHORIZED_ACCESS'});
        expect(result.error_code).toBe('AUTH_TOKEN_EXPIRED');
    });
});

describe('an in-memory portal after an upgrade', () => {
    afterEach(() => localStorage.clear());
    it('removes the tokens an earlier version left in browser storage', () => {
        initTestAppData();
        window.idea.app.session_management = 'in-memory';
        for (const key of ['refresh-token', 'access-token', 'id-token', 'sso-auth']) localStorage.setItem(`idea.auth.${key}`, 'marker');
        new AppContext({httpEndpoint: 'http://localhost', albEndpoint: 'http://localhost', releaseVersion: 'test', app: window.idea.app});
        expect(Object.keys(localStorage).filter(key => key.startsWith('idea.auth.'))).toEqual([]);
    });
});

describe('the endpoint the service worker accepts', () => {
    it.each([
        ['https://portal.example/api/v1', true],
        ['/api/v1', true],
        ['https://other.example/api/v1', false],
        ['https://portal.example:8443/api/v1', false],
        ['//other.example/api', false],
        [undefined, false],
    ])('%s: %s', (endpoint, accepted) => {
        expect(isSameOrigin(endpoint, 'https://portal.example')).toBe(accepted);
    });
});

describe('answers that arrive after the session changed', () => {
    beforeEach(() => initTestAppData());
    afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks();});

    const token = (username: string, exp: number) => `x.${btoa(JSON.stringify({exp, username}))}.y`;
    const answer = (payload: object) => ({status: 200, json: async () => payload});

    it('keep the newer sign-in: a late renewal and a late sign-out write nothing', async () => {
        const pending: Record<string, ((value: any) => void)[]> = {};
        const release = (flow: string, value: any) => {(pending[flow] ?? []).splice(0).forEach(resolve => resolve(value));};
        vi.stubGlobal('fetch', vi.fn((url: string, options: any) => {
            const body = JSON.parse(options.body);
            const flow = body.payload?.auth_flow ?? body.header.namespace;
            if (flow === 'USER_PASSWORD_AUTH' && body.payload.username === 'marker-a') {
                return Promise.resolve(answer({success: true, payload: {auth: {access_token: token('marker-a', 1), id_token: token('marker-a', 1), refresh_token: 'refresh-a'}}}));
            }
            if (flow === 'USER_PASSWORD_AUTH') {
                return Promise.resolve(answer({success: true, payload: {auth: {access_token: token('marker-b', 4102444800), id_token: token('marker-b', 4102444800), refresh_token: 'refresh-b'}}}));
            }
            return new Promise(resolve => {(pending[flow] ??= []).push(resolve);});
        }));
        const context = new IdeaAuthenticationContext({sessionManagement: 'in-memory', authEndpoint: 'http://localhost/api'});
        await context.initializeAuthContext({authEndpoint: 'http://localhost/api', defaultLogLevel: 1});
        const signIn = (username: string) => context.initiateAuth({header: {namespace: 'Auth.InitiateAuth'}, payload: {auth_flow: 'USER_PASSWORD_AUTH', username}});

        await signIn('marker-a');
        const renewal = context.getAccessToken().catch(() => null);
        await vi.waitFor(() => expect(pending.REFRESH_TOKEN_AUTH?.length).toBe(1));
        const signOut = context.logout();
        await signIn('marker-b');

        // the sign-out's own call renews first, so both renewals of the earlier session answer late
        release('REFRESH_TOKEN_AUTH', answer({success: true, payload: {auth: {access_token: token('marker-a', 4102444800), id_token: token('marker-a', 4102444800), refresh_token: 'refresh-a2'}}}));
        await renewal;
        await vi.waitFor(() => expect(pending['Auth.SignOut']?.length ?? 0).toBeGreaterThan(0), {timeout: 500}).catch(() => {});
        release('Auth.SignOut', answer({success: true, payload: {}}));
        await signOut;

        await expect(context.getAccessToken()).resolves.toBe(token('marker-b', 4102444800));
    });

    it('leave the newer sign-in signed in when a call was waiting on the earlier session\'s renewal', async () => {
        const pending: ((value: any) => void)[] = [];
        vi.stubGlobal('fetch', vi.fn((url: string, options: any) => {
            const body = JSON.parse(options.body);
            if (body.payload?.auth_flow === 'USER_PASSWORD_AUTH') {
                const exp = body.payload.username === 'marker-a' ? 1 : 4102444800;
                return Promise.resolve(answer({success: true, payload: {auth: {access_token: token(body.payload.username, exp), id_token: token(body.payload.username, exp), refresh_token: `refresh-${body.payload.username}`}}}));
            }
            if (body.payload?.auth_flow === 'REFRESH_TOKEN_AUTH') return new Promise(resolve => pending.push(resolve));
            return Promise.resolve(answer({success: true, payload: {}}));
        }));
        const context = new IdeaAuthenticationContext({sessionManagement: 'in-memory', authEndpoint: 'http://localhost/api'});
        await context.initializeAuthContext({authEndpoint: 'http://localhost/api', defaultLogLevel: 1});
        const invoker = new IdeaApiInvoker({name: 'test', url: 'http://localhost/api', authContext: context});
        const logout = vi.spyOn(invoker, 'logout');
        const signIn = (username: string) => context.initiateAuth({header: {namespace: 'Auth.InitiateAuth'}, payload: {auth_flow: 'USER_PASSWORD_AUTH', username}});

        await signIn('marker-a');
        const call = invoker.invoke({header: {namespace: 'Marker.Call', request_id: 'r1'}, payload: {}});
        await vi.waitFor(() => expect(pending.length).toBe(1));
        await signIn('marker-b');
        pending.splice(0).forEach(resolve => resolve(answer({success: true, payload: {auth: {access_token: token('marker-a', 4102444800), id_token: token('marker-a', 4102444800), refresh_token: 'refresh-a2'}}})));

        expect((await call).error_code).toBe('SESSION_CHANGED');
        expect(logout).not.toHaveBeenCalled();
        await expect(context.isLoggedIn()).resolves.toBe(true);
        await expect(context.getAccessToken()).resolves.toBe(token('marker-b', 4102444800));
    });
});
