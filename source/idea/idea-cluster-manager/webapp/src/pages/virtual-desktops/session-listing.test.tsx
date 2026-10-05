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

    it('keeps a just-created desktop the listing does not have yet', () => {
        const now = Date.parse('2026-10-01T17:00:00Z');
        const created = session('new', {state: 'PROVISIONING', updated_on: '2026-10-01T16:59:50Z'});
        const current = new Map([['a', session('a')], ['new', created]]);
        expect(Array.from(mergeSessions(current, [session('a')], true, now).keys()).sort()).toEqual(['a', 'new']);
    });

    it('drops a desktop the listing does not have once the grace window has passed', () => {
        const now = Date.parse('2026-10-01T17:00:00Z');
        const old = session('old', {updated_on: '2026-10-01T16:50:00Z'});
        expect(Array.from(mergeSessions(new Map([['old', old]]), [], true, now).keys())).toEqual([]);
    });

    it('drops a deleted desktop the listing still returns', () => {
        const now = Date.parse('2026-10-01T17:00:00Z');
        const current = new Map([['a', session('a', {state: 'DELETED', updated_on: '2026-10-01T16:59:59Z'})]]);
        const stale = session('a', {state: 'STOPPED', updated_on: '2026-10-01T16:00:00Z'});
        expect(Array.from(mergeSessions(current, [stale], true, now).keys())).toEqual([]);
    });

    it('keeps a local copy that is newer than the server copy', () => {
        const current = new Map([['a', session('a', {state: 'STOPPING', updated_on: 20})]]);
        expect(mergeSessions(current, [session('a', {state: 'READY', updated_on: 15})], true).get('a')?.state).toBe('STOPPING');
    });
});
