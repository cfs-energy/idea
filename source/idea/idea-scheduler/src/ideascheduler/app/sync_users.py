"""Synchronize cluster users and groups into /etc/passwd and /etc/group.

Each pass writes the image's own accounts (a baseline copied once from the files as the
image shipped them) followed by the enabled directory users and groups. A directory entry
whose name or id is the image's, or whose fields can't form one passwd or group line, is
left out and logged.
"""

import os
import re
import sys
import time

import boto3

CLUSTER = os.environ['IDEA_CLUSTER_NAME']
REGION = os.environ.get('AWS_DEFAULT_REGION') or os.environ['AWS_REGION']
INTERVAL = int(os.environ.get('IDEA_USER_SYNC_INTERVAL', '60'))
SYSTEM_ID_LIMIT = 1000
NOBODY_ID = 65534
BASELINE_SUFFIX = '.idea-base'
# one field of a passwd or group line: no separator, no list comma, no control character
FIELD = re.compile(r'[^:,\x00-\x1f\x7f]+')
PATH = re.compile(r'/[A-Za-z0-9._/@+-]*')

PASSWD = '/etc/passwd'
GROUP = '/etc/group'

ddb = boto3.resource('dynamodb', region_name=REGION)


def scan(table):
    t = ddb.Table(f'{CLUSTER}.{table}')
    items, kwargs = [], {}
    while True:
        page = t.scan(**kwargs)
        items.extend(page.get('Items', []))
        if 'LastEvaluatedKey' not in page:
            return items
        kwargs['ExclusiveStartKey'] = page['LastEvaluatedKey']


def write_atomic(path, lines):
    tmp = f'{path}.idea-tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def is_image_account(line):
    parts = line.split(':')
    if len(parts) < 3:
        return False
    try:
        ident = int(parts[2])
    except ValueError:
        return False
    return ident < SYSTEM_ID_LIMIT or ident == NOBODY_ID


def baseline_lines(path):
    """
    the image's accounts. the baseline is copied from the live file the first time, when it
    still holds what the image shipped; later passes never read their own output back, so
    entries they wrote can't be kept as if they were the image's.
    """
    baseline = path + BASELINE_SUFFIX
    if not os.path.exists(baseline):
        with open(path) as f:
            image = [
                line.rstrip('\n') for line in f if is_image_account(line.rstrip('\n'))
            ]
        write_atomic(baseline, image)
    with open(baseline) as f:
        return [line.rstrip('\n') for line in f if line.strip()]


def names_and_ids(lines):
    names, ids = set(), set()
    for line in lines:
        parts = line.split(':')
        names.add(parts[0])
        if len(parts) > 2 and parts[2].isdigit():
            ids.add(int(parts[2]))
    return names, ids


def skip(kind, name, reason):
    print(
        f'[sync_users] {kind} {name!r} left out: {reason}', file=sys.stderr, flush=True
    )


def valid_id(value):
    try:
        ident = int(value)
    except (TypeError, ValueError):
        return None
    return (
        ident if SYSTEM_ID_LIMIT <= ident < 2**32 - 1 and ident != NOBODY_ID else None
    )


def any_id(value):
    try:
        ident = int(value)
    except (TypeError, ValueError):
        return None
    return ident if 0 <= ident < 2**32 - 1 else None


def user_line(u, image_names, image_ids):
    name = str(u.get('username') or '')
    # the uid follows the directory range; the primary gid is written as stored, as the
    # previous release did: rows with a gid below 1000 exist and must keep working (B-S3)
    uid, gid = valid_id(u.get('uid')), any_id(u.get('gid'))
    home = str(u.get('home_dir') or '/')
    shell = str(u.get('login_shell') or '/bin/bash')
    if not FIELD.fullmatch(name):
        return skip('user', name, 'name is not one passwd field')
    if name in image_names or uid in image_ids:
        return skip('user', name, 'name or uid belongs to an image account')
    if uid is None or gid is None:
        return skip('user', name, 'uid or gid out of range')
    if not PATH.fullmatch(home) or not PATH.fullmatch(shell):
        return skip('user', name, 'home or shell is not an absolute path')
    return f'{name}:x:{uid}:{gid}:{name}:{home}:{shell}'


def group_line(g, image_names, image_ids, members):
    name = str(g.get('group_name') or '')
    gid = valid_id(g.get('gid'))
    if not FIELD.fullmatch(name):
        return skip('group', name, 'name is not one group field')
    if name in image_names or gid in image_ids:
        return skip('group', name, 'name or gid belongs to an image group')
    if gid is None:
        return skip('group', name, 'gid out of range')
    return f'{name}:x:{gid}:{",".join(sorted(members))}'


def sync_once():
    users = [
        u
        for u in scan('accounts.users')
        if u.get('enabled', True) and u.get('uid') is not None
    ]
    groups = [
        g
        for g in scan('accounts.groups')
        if g.get('enabled', True) and g.get('gid') is not None
    ]
    members = {}
    for m in scan('accounts.group-members'):
        members.setdefault(m['group_name'], set()).add(m['username'])
    for u in users:
        for g in u.get('additional_groups') or []:
            members.setdefault(g, set()).add(u['username'])

    base_users = baseline_lines(PASSWD)
    base_groups = baseline_lines(GROUP)
    user_names, uids = names_and_ids(base_users)
    group_names, gids = names_and_ids(base_groups)

    written = {}
    for u in sorted(users, key=lambda u: valid_id(u.get('uid')) or 0):
        line = user_line(u, user_names, uids)
        if line is not None and line.split(':')[0] not in written:
            written[line.split(':')[0]] = line
    group_entries = []
    for g in sorted(groups, key=lambda g: valid_id(g.get('gid')) or 0):
        # only accounts this pass writes can be members; a disabled one drops out
        group_members = members.get(g.get('group_name'), set()) & written.keys()
        line = group_line(g, group_names, gids, group_members)
        if line is not None:
            group_entries.append(line)
    write_atomic(PASSWD, base_users + list(written.values()))
    write_atomic(GROUP, base_groups + group_entries)
    return len(written), len(group_entries)


def tables_not_created_yet(error):
    # On a fresh cluster the account tables appear when the cluster manager first starts, which
    # can be after this task does. That is deferred to the background pass; anything else, a
    # denial above all, stays fatal on the first pass so a misconfigured role fails loudly.
    return (
        getattr(error, 'response', {}).get('Error', {}).get('Code')
        == 'ResourceNotFoundException'
    )


def main():
    once = '--once' in sys.argv
    while True:
        try:
            nu, ng = sync_once()
            print(f'[sync_users] {nu} users, {ng} groups', flush=True)
        except Exception as e:  # Retry on the next sync interval.
            print(f'[sync_users] failed: {e}', file=sys.stderr, flush=True)
            if once:
                if tables_not_created_yet(e):
                    print(
                        '[sync_users] account tables not created yet; the background sync retries',
                        flush=True,
                    )
                    return
                sys.exit(1)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
