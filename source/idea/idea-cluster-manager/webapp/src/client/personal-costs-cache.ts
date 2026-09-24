import {GetMyCostsResult} from './data-model';
import MyCostsClient from './my-costs-client';
import {AppContext} from '../common';

type Listener = (costs: GetMyCostsResult) => void;
export class PersonalCostsCache {
    private value?: GetMyCostsResult;
    private listeners = new Set<Listener>();
    private errors = new Map<Listener, (error: unknown) => void>();
    private flight?: Promise<GetMyCostsResult>;
    private timer?: ReturnType<typeof setTimeout>;
    private updated = 0;
    private pendingSince = 0;
    constructor(private client: MyCostsClient) {}
    read = (): Promise<GetMyCostsResult> => {
        if (this.flight) return this.flight;
        this.flight = this.client.getCosts({}).then(this.accept).finally(() => { this.flight = undefined; });
        return this.flight;
    };
    private accept = (value: GetMyCostsResult) => {
        this.value = value;
        this.updated = Date.now();
        this.listeners.forEach(listener => listener(value));
        const pending = value.refresh_pending || value.state === 'collecting' || value.state === 'computing';
        this.pendingSince = pending ? this.pendingSince || Date.now() : 0;
        clearTimeout(this.timer);
        const delay = pending ? (Date.now() - this.pendingSince > 120000 ? 60000 : 15000) : 300000;
        if (this.listeners.size) this.timer = setTimeout(() => {
            if (document.visibilityState === 'visible') this.read().catch(this.retry);
        }, delay);
        return value;
    };
    private retry = (error?: unknown) => {
        if (error) this.errors.forEach(listener => listener(error));
        if (this.listeners.size) this.timer = setTimeout(this.visible, 60000);
    };
    private visible = () => { if (document.visibilityState === 'visible') this.read().catch(this.retry); };
    subscribe(listener: Listener, onError?: (error: unknown) => void) {
        if (onError) this.errors.set(listener, onError);
        this.listeners.add(listener);
        if (this.value) listener(this.value);
        if (this.listeners.size === 1) {
            document.addEventListener('visibilitychange', this.visible);
            if (!this.value || Date.now() - this.updated > 300000) this.read().catch(this.retry);
            else this.accept(this.value);
        }
        return () => {
            this.listeners.delete(listener);
            this.errors.delete(listener);
            if (!this.listeners.size) {
                clearTimeout(this.timer);
                document.removeEventListener('visibilitychange', this.visible);
            }
        };
    }
    async refresh() {
        // Serialize against a read so an older response cannot erase acknowledgement.
        if (this.flight) await this.flight.catch(() => undefined);
        return this.accept(await this.client.refresh());
    }
}
const caches = new WeakMap<MyCostsClient, Map<string, PersonalCostsCache>>();
export function personalCostsCache() {
    const context = AppContext.get();
    const client = context.client().myCosts();
    const subject = context.auth().getUsername();
    let users = caches.get(client);
    if (!users) { users = new Map(); caches.set(client, users); }
    let cache = users.get(subject);
    if (!cache) { cache = new PersonalCostsCache(client); users.set(subject, cache); }
    return cache;
}
