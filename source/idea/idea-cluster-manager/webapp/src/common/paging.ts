import {ListPermissionsRequest, ListPermissionsResponse, ListUsersInGroupRequest, ListUsersInGroupResult, User, VirtualDesktopSessionPermission} from '../client/data-model';

/** Every grant on a desktop, page by page: the listing pages by start and total. */
export async function listAllSessionPermissions(list: (request: ListPermissionsRequest) => Promise<ListPermissionsResponse>, request: ListPermissionsRequest, pageSize = 100): Promise<VirtualDesktopSessionPermission[]> {
    const all: VirtualDesktopSessionPermission[] = [];
    for (let start = 0; ; start += pageSize) {
        const page = await list({...request, paginator: {page_size: pageSize, start}});
        const listing = page.listing ?? [];
        all.push(...listing);
        if (listing.length === 0 || start + pageSize >= (page.paginator?.total ?? 0)) return all;
    }
}

/** Every member of the groups, once each: the listing pages by cursor and can repeat a member across pages. */
export async function listAllUsersInGroups(list: (request: ListUsersInGroupRequest) => Promise<ListUsersInGroupResult>, groupNames: string[], pageSize = 1000): Promise<User[]> {
    const users = new Map<string, User>();
    let cursor: string | undefined;
    do {
        const page = await list({group_names: groupNames, paginator: {page_size: pageSize, cursor}});
        for (const user of page.listing ?? []) {
            if (user.username && !users.has(user.username)) users.set(user.username, user);
        }
        const next = page.paginator?.cursor;
        cursor = next && next !== cursor ? next : undefined;
    } while (cursor);
    return [...users.values()];
}
