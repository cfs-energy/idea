import {beforeEach, describe, expect, it} from 'vitest';
import {clearSignedOut, markSignedOut, peekReturnPath, rememberReturnPath, takeSessionEnded} from './session-return';

beforeEach(() => sessionStorage.clear());

describe('return path', () => {
    it('keeps a path inside the portal', () => {
        rememberReturnPath('/cluster/users?view=x', true);
        expect(peekReturnPath()).toBe('/cluster/users?view=x');
        expect(takeSessionEnded()).toBe(true);
        expect(takeSessionEnded()).toBe(false);
    });

    it.each(['//evil.example', 'https://evil.example', '/\\evil.example', '/a\\b', '/auth/login', '/', ''])('refuses %s', path => {
        rememberReturnPath(path, false);
        expect(peekReturnPath()).toBeNull();
    });

    it('refuses a tampered stored value', () => {
        sessionStorage.setItem('idea.return-path', '//evil.example');
        expect(peekReturnPath()).toBeNull();
    });

    it('signing out on purpose leaves nothing to return to until the next sign-in', () => {
        rememberReturnPath('/cluster/users', true);
        markSignedOut();
        rememberReturnPath('/cluster/groups', true);
        expect(peekReturnPath()).toBeNull();
        expect(takeSessionEnded()).toBe(false);
        clearSignedOut();
        rememberReturnPath('/cluster/groups', false);
        expect(peekReturnPath()).toBe('/cluster/groups');
    });
});
