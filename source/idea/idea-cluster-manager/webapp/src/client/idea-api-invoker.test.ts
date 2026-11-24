import { IdeaApiInvoker } from './idea-api-invoker';
import { initTestAppContext, initTestAppData } from '../test-support';
import { LONG_REQUEST_TIMEOUT } from '../common/authentication-context';

function buildInvoker(postMessage: (message: any, transfer: any[]) => void, timeout?: number): IdeaApiInvoker {
    initTestAppData();
    return new IdeaApiInvoker({
        name: 'test-invoker',
        url: 'http://localhost:8080/api/v1',
        timeout: timeout,
        serviceWorkerRegistration: {
            active: {
                postMessage: postMessage
            }
        } as any
    });
}

describe('idea api invoker', () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    // the network timeout lives inside the service worker, so a worker that never replies used
    // to leave the caller pending forever - the request looks like it is still in flight.
    it('fails the request when the service worker never replies', async () => {
        vi.useFakeTimers();
        const invoker = buildInvoker(() => {});

        const request = invoker.invoke_alt('Scheduler.SubmitJob', { dry_run: false });
        const settled = request.then(
            () => 'resolved',
            (error) => error
        );

        await vi.advanceTimersByTimeAsync(600000);
        expect(await Promise.race([settled, Promise.resolve('pending')])).toBe('pending');

        await vi.advanceTimersByTimeAsync(60001);
        const error: any = await settled;
        expect(error.errorCode).toBe('REQUEST_TIMEOUT');
        expect(error.message).toBe('The portal took too long to respond. Try again.');
    });

    it('returns the payload when the service worker replies', async () => {
        const invoker = buildInvoker((message, transfer) => {
            transfer[0].postMessage({
                response: { success: true, payload: { job: { job_id: '101' } } }
            });
        });

        const result: any = await invoker.invoke_alt('Scheduler.SubmitJob', { dry_run: false });
        expect(result.job.job_id).toBe('101');
    });

    it('keeps created API tokens out of trace logs', async () => {
        const token = `idea_${'a'.repeat(43)}`;
        const invoker = buildInvoker((message, transfer) => {
            transfer[0].postMessage({response: {success: true, payload: {token}}});
        });
        vi.spyOn(invoker.logger, 'isTrace').mockReturnValue(true);
        const trace = vi.spyOn(invoker.logger, 'trace').mockImplementation(() => {});
        const result: any = await invoker.invoke_alt('Auth.CreateApiToken', {name: 'automation', expires_in_days: 30});
        expect(result.token).toBe(token);
        expect(JSON.stringify(trace.mock.calls)).not.toContain(token);
    });

    it('keeps terminal tickets and browser bindings out of trace logs', async () => {
        const invoker = buildInvoker((_message, transfer) => transfer[0].postMessage({response: {success: true, payload: {ticket: 'private-ticket'}}}));
        vi.spyOn(invoker.logger, 'isTrace').mockReturnValue(true);
        const trace = vi.spyOn(invoker.logger, 'trace').mockImplementation(() => {});
        await invoker.invoke_alt('Terminal.CreateTicket', {target: 'bastion', client: 'private-binding'});
        expect(JSON.stringify(trace.mock.calls)).not.toMatch(/private-ticket|private-binding/);
    });

    it.each([
        ['Auth.InitiateAuth', {auth_flow: 'USER_PASSWORD_AUTH', username: 'marker-user', password: 'marker-password'}, {auth: {}}],
        ['Auth.GetUserPrivateKey', {key_format: 'pem'}, {key_material: 'marker-private-key'}],
    ])('logs only the operation and its outcome for %s', async (namespace, payload, reply) => {
        const invoker = buildInvoker((_message, transfer) => transfer[0].postMessage({response: {success: true, payload: reply}}));
        vi.spyOn(invoker.logger, 'isTrace').mockReturnValue(true);
        const trace = vi.spyOn(invoker.logger, 'trace').mockImplementation(() => {});
        await invoker.invoke_alt(namespace, payload);
        expect(trace.mock.calls).toEqual([[`(req) ${namespace}`], [`(res) ${namespace} ok`]]);
    });

    it('honors a caller-supplied timeout instead of the hardcoded default', async () => {
        vi.useFakeTimers();
        const invoker = buildInvoker(() => {}, 5000);

        const request = invoker.invoke_alt('Scheduler.SubmitJob', { dry_run: false });
        const settled = request.then(
            () => 'resolved',
            (error) => error
        );

        await vi.advanceTimersByTimeAsync(4999);
        expect(await Promise.race([settled, Promise.resolve('pending')])).toBe('pending');

        await vi.advanceTimersByTimeAsync(2);
        const error: any = await settled;
        expect(error.errorCode).toBe('REQUEST_TIMEOUT');
        expect(error.message).toBe('The portal took too long to respond. Try again.');
    });
});

describe('error reporting call', () => {
    it('does not end the session or throw when the token has expired', async () => {
        const authContext: any = {invoke: vi.fn().mockResolvedValue({success: false, error_code: 'AUTH_TOKEN_EXPIRED'}), logout: vi.fn()};
        const invoker = new IdeaApiInvoker({name: 'test', url: 'https://portal/api', authContext});
        await expect(invoker.invoke_quiet('Portal.ReportClientError', {})).resolves.toBeUndefined();
        expect(authContext.logout).not.toHaveBeenCalled();
    });
});

describe('clients taken from the app context', () => {
    afterEach(() => vi.restoreAllMocks());
    const signedIn = () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(true);
        return context;
    };

    it('keep the long timeout reconcile asks for', async () => {
        const accounts = signedIn().client().accounts();
        const transport = vi.spyOn((accounts as any).apiInvoker.props.authContext, 'invoke').mockResolvedValue({success: true, payload: {}});
        await accounts.reconcileUsers({dry_run: true});
        expect(transport.mock.calls[0][3]).toBe(LONG_REQUEST_TIMEOUT);
    });

    it('keep an error report quiet when the token has expired', async () => {
        const context = signedIn();
        const invoker = (context.client().clientErrors() as any).apiInvoker;
        vi.spyOn(invoker.props.authContext, 'invoke').mockResolvedValue({success: false, error_code: 'AUTH_TOKEN_EXPIRED'});
        const logout = vi.spyOn(invoker, 'logout').mockResolvedValue(true);
        await invoker.invoke_quiet('Portal.ReportClientError', {});
        expect(logout).not.toHaveBeenCalled();
    });
});

describe('a failed call', () => {
    afterEach(() => vi.restoreAllMocks());
    it('logs the operation and error code, not the answer', async () => {
        initTestAppData();
        const authContext: any = {invoke: vi.fn().mockResolvedValue({success: false, error_code: 'INVALID_PARAMS', message: 'marker', payload: {secret: 'marker-payload'}})};
        const invoker = new IdeaApiInvoker({name: 'test', url: 'https://portal/api', authContext});
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        await expect(invoker.invoke_alt('Marker.Call', {password: 'marker-request'})).rejects.toBeTruthy();
        const logged = JSON.stringify(error.mock.calls);
        expect(logged).toContain('Marker.Call failed: INVALID_PARAMS');
        expect(logged).not.toMatch(/marker-payload|marker-request/);
    });
});
