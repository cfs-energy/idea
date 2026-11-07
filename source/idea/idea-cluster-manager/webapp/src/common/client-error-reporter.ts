import {matchPath} from 'react-router-dom';

export type ClientErrorType = 'uncaught_error' | 'unhandled_rejection' | 'render_crash' | 'api_failure' | 'list_load_failed';

export interface ClientErrorReport {
    error_type: ClientErrorType
    route: string
    app_version: string
    api_namespace?: string
    message?: string
}

export type ClientErrorSender = (report: ClientErrorReport) => Promise<unknown>;

// App.tsx routes, as patterns, so a live path with an id or a person's input never leaves the page.
export const ROUTE_PATTERNS = [
    "/",
    "/auth/challenge",
    "/auth/confirm-forgot-password",
    "/auth/forgot-password",
    "/auth/login",
    "/cluster/ai-usage",
    "/cluster/email-templates",
    "/cluster/groups",
    "/cluster/projects",
    "/cluster/reconciliation-runs",
    "/cluster/services",
    "/cluster/settings/*",
    "/cluster/status",
    "/cluster/user-costs",
    "/cluster/users",
    "/dashboard",
    "/home",
    "/home/account-settings",
    "/home/active-jobs",
    "/home/completed-jobs",
    "/home/custom-dashboard",
    "/home/file-browser",
    "/home/file-browser/tail",
    "/home/my-costs",
    "/home/script-workbench",
    "/home/shared-desktops",
    "/home/ssh-access",
    "/home/terminal",
    "/home/virtual-desktops",
    "/reporting/*",
    "/soca/active-jobs",
    "/soca/applications",
    "/soca/applications/create",
    "/soca/applications/update",
    "/soca/completed-jobs",
    "/soca/custom-amis",
    "/soca/jobs/submit-job",
    "/soca/licenses",
    "/soca/licenses/create",
    "/soca/licenses/update",
    "/soca/queues",
    "/soca/queues/create",
    "/soca/queues/update",
    "/soca/scratch-cleanup",
    "/soca/settings/*",
    "/virtual-desktop/dashboard",
    "/virtual-desktop/debug",
    "/virtual-desktop/permission-profiles",
    "/virtual-desktop/permission-profiles/:profile_id",
    "/virtual-desktop/sessions",
    "/virtual-desktop/sessions/:idea_session_id",
    "/virtual-desktop/settings/*",
    "/virtual-desktop/software-stacks",
    "/virtual-desktop/software-stacks/:software_stack_id",
];

export const MAX_REPORTS_PER_PAGE_LOAD = 20;
const REPEAT_WINDOW_MS = 10 * 60 * 1000;
const MAX_MESSAGE_LENGTH = 300;

// Failures only the browser can see. The server already logged every error it answered with.
const REPORTED_API_CODES = new Set(['NETWORK_ERROR', 'REQUEST_TIMEOUT', 'SERVER_ERROR', 'GENERAL_ERROR']);
// Harmless browser notices, not failures.
const IGNORED_MESSAGE = /ResizeObserver loop/;

let sender: ClientErrorSender | null = null;
let appVersion = 'unknown';
let sentThisLoad = 0;
let lastSent = new Map<string, number>();

export function routePattern(hash: string = window.location.hash): string {
    const path = hash.replace(/^#/, '').split(/[?#]/)[0] || '/';
    return ROUTE_PATTERNS.find(pattern => matchPath(pattern, path) != null) ?? '/unknown';
}

/** Quoted text, paths, URLs and anything with digits go first. The server keeps known words only. */
export function scrubMessage(message: unknown): string | undefined {
    if (typeof message !== 'string' || message.length === 0) {
        return undefined;
    }
    return message.slice(0, MAX_MESSAGE_LENGTH * 3)
        .replace(/'[^']*'|"[^"]*"|`[^`]*`/g, '?')
        .split(/\s+/)
        .map(word => /[\/\\=@&%0-9]/.test(word) ? '?' : word)
        .join(' ')
        .slice(0, MAX_MESSAGE_LENGTH);
}

function describe(error: any): string | undefined {
    if (error?.errorCode) {
        // an API failure's own text can quote what the person sent, so only its code is reported
        return String(error.errorCode);
    }
    if (error instanceof Error) {
        return scrubMessage(`${error.name} ${error.message}`);
    }
    return undefined;
}

/** Never throws and never reports its own failure: the page must not notice this at all. */
export function reportClientError(errorType: ClientErrorType, details: {error?: unknown, message?: string, apiNamespace?: string} = {}): void {
    try {
        if (sender == null) {
            return;
        }
        const message = details.message ?? describe(details.error);
        if (message && IGNORED_MESSAGE.test(message)) {
            return;
        }
        const route = routePattern();
        const key = [errorType, route, details.apiNamespace, message].join('|');
        const now = Date.now();
        const last = lastSent.get(key);
        if ((last !== undefined && now - last < REPEAT_WINDOW_MS) || sentThisLoad >= MAX_REPORTS_PER_PAGE_LOAD) {
            return;
        }
        lastSent.set(key, now);
        sentThisLoad += 1;
        const report: ClientErrorReport = {error_type: errorType, route, app_version: appVersion};
        if (details.apiNamespace) {
            report.api_namespace = details.apiNamespace;
        }
        if (message) {
            report.message = message;
        }
        sender(report).catch(() => undefined);
    } catch {
        // nothing to do: reporting must not become another failure
    }
}

export function reportApiFailure(apiNamespace: string, errorCode: string): void {
    if (REPORTED_API_CODES.has(errorCode)) {
        reportClientError('api_failure', {apiNamespace, message: errorCode});
    }
}

const onWindowError = (event: ErrorEvent) => reportClientError('uncaught_error', {error: event.error, message: event.error ? undefined : scrubMessage(event.message)});
const onUnhandledRejection = (event: PromiseRejectionEvent) => reportClientError('unhandled_rejection', {error: event.reason});

export function installClientErrorReporting(send: ClientErrorSender, version: string): void {
    sender = send;
    appVersion = version;
    window.addEventListener('error', onWindowError);
    window.addEventListener('unhandledrejection', onUnhandledRejection);
}

export function resetClientErrorReporting(): void {
    sender = null;
    sentThisLoad = 0;
    lastSent = new Map();
    window.removeEventListener('error', onWindowError);
    window.removeEventListener('unhandledrejection', onUnhandledRejection);
}
