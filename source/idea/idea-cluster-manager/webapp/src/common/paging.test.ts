import {expect, it, vi} from 'vitest';
import {listAllSessionPermissions, listAllUsersInGroups} from './paging';

it('reads every page of grants', async () => {
    const list = vi.fn(async (request: any) => {
        const start = request.paginator.start;
        return {listing: Array.from({length: Math.min(2, 5 - start)}, (_, i) => ({actor_name: `marker-${start + i}`})), paginator: {total: 5}};
    });
    const grants = await listAllSessionPermissions(list, {idea_session_id: 'marker'}, 2);
    expect(grants.map(grant => grant.actor_name)).toEqual(['marker-0', 'marker-1', 'marker-2', 'marker-3', 'marker-4']);
    expect(list).toHaveBeenCalledTimes(3);
});

it('follows the member cursor to the end and lists each member once', async () => {
    const pages: any[] = [
        {listing: [{username: 'a'}, {username: 'b'}], paginator: {cursor: 'one'}},
        {listing: [{username: 'a'}, {username: 'c'}], paginator: {cursor: 'two'}},
        {listing: [{username: 'd'}], paginator: {}},
    ];
    const list = vi.fn(async () => pages.shift());
    expect((await listAllUsersInGroups(list, ['g'])).map(user => user.username)).toEqual(['a', 'b', 'c', 'd']);
    expect(list.mock.calls.map((call: any) => call[0].paginator.cursor)).toEqual([undefined, 'one', 'two']);
});

it('passes a failed page on, so no partial list is taken as complete', async () => {
    const list = vi.fn().mockResolvedValueOnce({listing: [{username: 'a'}], paginator: {cursor: 'one'}}).mockRejectedValueOnce(new Error('marker'));
    await expect(listAllUsersInGroups(list, ['g'])).rejects.toThrow('marker');
});
