import {vi} from 'vitest';
import appSource from '../App.tsx?raw';
import {
    installClientErrorReporting, MAX_REPORTS_PER_PAGE_LOAD, reportApiFailure, reportClientError,
    resetClientErrorReporting, ROUTE_PATTERNS, routePattern, scrubMessage
} from './client-error-reporter';

describe('client error reporting', () => {
    let send: any;

    beforeEach(() => {
        resetClientErrorReporting();
        send = vi.fn().mockResolvedValue(undefined);
        installClientErrorReporting(send, '26.11.2');
        window.location.hash = '#/home/file-browser?cwd=%2Fdata%2Fhome%2Fsecret-project';
    });

    afterEach(() => {
        resetClientErrorReporting();
        window.location.hash = '';
    });

    it('sends only the route pattern and allowed fields, never the query string', () => {
        reportClientError('uncaught_error', {error: new TypeError('Cannot read properties of undefined')});
        expect(send).toHaveBeenCalledWith({
            error_type: 'uncaught_error', route: '/home/file-browser', app_version: '26.11.2',
            message: 'TypeError Cannot read properties of undefined'
        });
    });

    it('turns live ids into the route pattern and unknown paths into /unknown', () => {
        expect(routePattern('#/virtual-desktop/sessions/1234-abcd?x=1')).toBe('/virtual-desktop/sessions/:idea_session_id');
        expect(routePattern('#/somewhere/job-name')).toBe('/unknown');
    });

    it('knows every route the app declares', () => {
        const declared = Array.from(appSource.matchAll(/<Route path="([^"]+)"/g)).map(match => match[1]);
        expect(declared.length).toBeGreaterThan(30);
        // the catch-all reports as /unknown
        expect(declared.filter(path => path !== '*' && !ROUTE_PATTERNS.includes(path))).toEqual([]);
    });

    it('removes quoted text, paths and numbers before anything is sent', () => {
        expect(scrubMessage(`can't open "/data/home/run7/in.dat" for job fusion_42 a=1`)).not.toMatch(/data|run7|42|a=1/);
    });

    it('reports an API failure by its code, not its text', () => {
        reportClientError('unhandled_rejection', {error: {errorCode: 'FILE_NOT_FOUND', message: 'not found: /data/home/x'}});
        expect(send.mock.calls[0][0].message).toBe('FILE_NOT_FOUND');
    });

    it('reports browser-side API failures and leaves answers the server logged itself', () => {
        reportApiFailure('Scheduler.ListActiveJobs', 'INVALID_PARAMS');
        expect(send).not.toHaveBeenCalled();
        reportApiFailure('Scheduler.ListActiveJobs', 'NETWORK_ERROR');
        expect(send).toHaveBeenCalledWith(expect.objectContaining({
            error_type: 'api_failure', api_namespace: 'Scheduler.ListActiveJobs', message: 'NETWORK_ERROR'
        }));
    });

    it('sends a repeat once, and stops at the limit for a page load', () => {
        reportClientError('render_crash', {message: 'same'});
        reportClientError('render_crash', {message: 'same'});
        expect(send).toHaveBeenCalledTimes(1);
        for (let index = 0; index < MAX_REPORTS_PER_PAGE_LOAD * 2; index++) {
            reportClientError('render_crash', {message: `other ${index}`});
        }
        expect(send).toHaveBeenCalledTimes(MAX_REPORTS_PER_PAGE_LOAD);
    });

    it('never throws and never reports its own failure', async () => {
        send.mockRejectedValue(new Error('down'));
        expect(() => reportClientError('uncaught_error', {message: 'x'})).not.toThrow();
        await new Promise(resolve => setTimeout(resolve));
        expect(send).toHaveBeenCalledTimes(1);
        send.mockImplementation(() => { throw new Error('sync'); });
        expect(() => reportClientError('uncaught_error', {message: 'y'})).not.toThrow();
    });

    it('catches uncaught errors and unhandled rejections on the window', () => {
        window.dispatchEvent(new ErrorEvent('error', {error: new RangeError('Maximum call stack size exceeded')}));
        expect(send.mock.calls[0][0]).toMatchObject({error_type: 'uncaught_error', message: 'RangeError Maximum call stack size exceeded'});
        const rejection = new Event('unhandledrejection') as any;
        rejection.reason = {errorCode: 'SERVER_ERROR'};
        window.dispatchEvent(rejection);
        expect(send.mock.calls[1][0]).toMatchObject({error_type: 'unhandled_rejection', message: 'SERVER_ERROR'});
    });

    it('ignores harmless browser notices', () => {
        reportClientError('uncaught_error', {message: 'ResizeObserver loop completed with undelivered notifications.'});
        expect(send).not.toHaveBeenCalled();
    });
});
