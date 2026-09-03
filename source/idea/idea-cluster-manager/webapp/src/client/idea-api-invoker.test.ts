import { IdeaApiInvoker } from './idea-api-invoker';
import { initTestAppData } from '../test-support';

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
        expect(error.message).toBe('Request timed-out');
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
        expect(error.message).toBe('Request timed-out');
    });
});
