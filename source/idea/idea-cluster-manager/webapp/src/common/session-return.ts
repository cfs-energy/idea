// Where to go back to after signing in. sessionStorage survives the SSO round trip and stays per tab.
const KEY_PATH = 'idea.return-path';
const KEY_ENDED = 'idea.session-ended';
const KEY_SIGNED_OUT = 'idea.signed-out';

const read = (key: string): string | null => {
    try {
        return sessionStorage.getItem(key);
    } catch {
        return null;
    }
};
const write = (key: string, value: string | null) => {
    try {
        if (value == null) sessionStorage.removeItem(key);
        else sessionStorage.setItem(key, value);
    } catch {
        // without storage the person lands on Home, as before
    }
};

/** Only a path inside the portal's own hash routes, never another site or the sign-in pages. */
const inApp = (path: string | null): string | null =>
    path != null && /^\/[^/\\]/.test(path) && !path.includes('\\') && !path.startsWith('/auth/') ? path : null;

export function rememberReturnPath(path: string, sessionEnded: boolean) {
    if (read(KEY_SIGNED_OUT)) return;
    if (inApp(path)) write(KEY_PATH, path);
    if (sessionEnded) write(KEY_ENDED, 'true');
}

export const peekReturnPath = (): string | null => inApp(read(KEY_PATH));

export function clearSessionReturn() {
    write(KEY_PATH, null);
    write(KEY_ENDED, null);
}

/** One showing only: the sign-in page reads it as it opens. */
export function takeSessionEnded(): boolean {
    const ended = read(KEY_ENDED) != null;
    write(KEY_ENDED, null);
    return ended;
}

/** Signing out on purpose: nothing to return to, and nothing to explain. */
export function markSignedOut() {
    clearSessionReturn();
    write(KEY_SIGNED_OUT, 'true');
}

export const clearSignedOut = () => write(KEY_SIGNED_OUT, null);
