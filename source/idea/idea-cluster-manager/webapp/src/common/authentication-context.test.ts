import {afterEach, vi} from 'vitest';
import { IdeaAuthenticationContext, LONG_REQUEST_TIMEOUT, NETWORK_TIMEOUT } from './authentication-context';
import { initTestAppData } from '../test-support';

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
