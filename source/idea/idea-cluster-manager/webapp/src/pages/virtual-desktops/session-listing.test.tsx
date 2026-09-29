import {describe, expect, it} from 'vitest';
import {mergeSessions} from './my-virtual-desktop-sessions';

const session = (id: string, patch: any = {}) => ({idea_session_id: id, name: id, state: 'READY', updated_on: 10, ...patch}) as any;

describe('session listing merge', () => {
    it('drops a session the server no longer lists (a terminated desktop leaves at the next refresh)', () => {
        const current = new Map([['a', session('a')], ['b', session('b')]]);
        expect(Array.from(mergeSessions(current, [session('b')], true).keys())).toEqual(['b']);
    });

    it('keeps the other sessions when an action result names only one', () => {
        const current = new Map([['a', session('a')], ['b', session('b')]]);
        const next = mergeSessions(current, [session('b', {state: 'DELETING', updated_on: 11})], false);
        expect(Array.from(next.keys()).sort()).toEqual(['a', 'b']);
        expect(next.get('b')?.state).toBe('DELETING');
    });

    it('keeps a local copy that is newer than the server copy', () => {
        const current = new Map([['a', session('a', {state: 'STOPPING', updated_on: 20})]]);
        expect(mergeSessions(current, [session('a', {state: 'READY', updated_on: 15})], true).get('a')?.state).toBe('STOPPING');
    });
});
