import {PersonalCostsCache, personalCostsCache} from './personal-costs-cache';
import MyCostsClient from './my-costs-client';
import {initTestAppContext} from '../test-support';

const snapshot = {currency: 'USD', state: 'ready', generation: 'generation-a'};
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
it('shares an in-flight read and polls pending refresh without clearing values', async () => {
    vi.useFakeTimers();
    const client = {getCosts: vi.fn().mockResolvedValue(snapshot), refresh: vi.fn().mockResolvedValue({...snapshot, refresh_pending: true, refresh_acknowledged: true})};
    const cache = new PersonalCostsCache(client as unknown as MyCostsClient);
    const first = vi.fn(), second = vi.fn();
    const closeA = cache.subscribe(first), closeB = cache.subscribe(second);
    await Promise.resolve(); await Promise.resolve();
    expect(client.getCosts).toHaveBeenCalledOnce();
    expect(first).toHaveBeenCalledWith(snapshot);
    expect(second).toHaveBeenCalledWith(snapshot);
    await cache.refresh();
    expect(first.mock.lastCall![0].generation).toBe('generation-a');
    expect(first.mock.lastCall![0].refresh_acknowledged).toBe(true);
    await vi.advanceTimersByTimeAsync(15000);
    expect(client.getCosts).toHaveBeenCalledTimes(2);
    closeA(); closeB();
});
it('keeps the old snapshot on failure and pauses polling while hidden', async () => {
    vi.useFakeTimers();
    const client = {getCosts: vi.fn().mockResolvedValueOnce(snapshot).mockRejectedValue(new Error('offline'))};
    const cache = new PersonalCostsCache(client as unknown as MyCostsClient);
    const listener = vi.fn(), errors = vi.fn();
    const close = cache.subscribe(listener, errors);
    await vi.advanceTimersByTimeAsync(300000);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(errors).toHaveBeenCalledOnce();
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    await vi.advanceTimersByTimeAsync(60000);
    expect(client.getCosts).toHaveBeenCalledTimes(2);
    close();
});
it('isolates caches by authenticated user even when the API client is reused', () => {
    const context = initTestAppContext();
    const identity = vi.spyOn(context.auth(), 'getUsername').mockReturnValue('user-a');
    const first = personalCostsCache();
    expect(personalCostsCache()).toBe(first);
    identity.mockReturnValue('user-b');
    expect(personalCostsCache()).not.toBe(first);
});
