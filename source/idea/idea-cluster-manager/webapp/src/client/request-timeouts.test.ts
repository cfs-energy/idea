import {beforeEach, describe, expect, it, vi} from 'vitest';
import AccountsClient from './accounts-client';
import FileBrowserClient from './file-browser-client';
import {IdeaAuthenticationContext, LONG_REQUEST_TIMEOUT} from '../common/authentication-context';
import IdeaClients from './clients';
import {initTestAppData} from '../test-support';

const authContext = () => {
    const context = {invoke: vi.fn().mockResolvedValue({success: true, payload: {}})};
    return context as unknown as IdeaAuthenticationContext & {invoke: ReturnType<typeof vi.fn>};
};

describe('calls that may outlast the default wait', () => {
    beforeEach(() => initTestAppData());

    it('reconciliation asks for the long wait, other account calls do not', async () => {
        const context = authContext();
        const client = new AccountsClient({name: 'accounts', baseUrl: 'http://x', apiContextPath: '/api', authContext: context});
        await client.reconcileUsers({dry_run: true});
        expect(context.invoke.mock.calls[0][3]).toBe(LONG_REQUEST_TIMEOUT);
        await client.listUsers({});
        expect(context.invoke.mock.calls[1][3]).toBeUndefined();
    });

    it('every file browser call keeps the long wait', async () => {
        const context = authContext();
        const clients = new IdeaClients({appId: 'test', baseUrl: 'http://x', authContext: context} as any);
        const client: FileBrowserClient = clients.fileBrowser();
        await client.downloadFiles({files: []});
        expect(context.invoke.mock.calls[0][3]).toBe(LONG_REQUEST_TIMEOUT);
    });

    it('the worker is told the wait', async () => {
        const postMessage = vi.fn((_: any, [port]: MessagePort[]) => port.postMessage({response: {success: true, payload: {}}}));
        const client = new AccountsClient({name: 'accounts', baseUrl: 'http://x', apiContextPath: '/api', serviceWorkerRegistration: {active: {postMessage}} as any});
        await client.reconcileUsers({dry_run: true});
        expect(postMessage.mock.calls[0][0].options.timeout).toBe(LONG_REQUEST_TIMEOUT);
    });
});
