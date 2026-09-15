"""Keep /etc/passwd and /etc/group in step with the cluster's user and group tables.

A module host resolves cluster users through the directory (sssd). A task has no
directory join, but the cluster already assigns every uid and gid itself and records
them in DynamoDB, so the same identities can be written straight into the files the
resolver reads. The scheduler needs this because it submits each job with
"su <owner> -c qsub", which fails as "no such user" until the owner resolves.

System accounts (uid or gid below 1000) are left exactly as the image shipped them;
everything at or above that is rewritten from the tables on each pass.
"""

import os
import sys
import time

import boto3

CLUSTER = os.environ['IDEA_CLUSTER_NAME']
REGION = os.environ.get('AWS_DEFAULT_REGION') or os.environ['AWS_REGION']
INTERVAL = int(os.environ.get('IDEA_USER_SYNC_INTERVAL', '60'))
SYSTEM_ID_LIMIT = 1000

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


def system_lines(path, id_field):
    keep = []
    with open(path) as f:
        for line in f:
            line = line.rstrip('\n')
            parts = line.split(':')
            if len(parts) < 3:
                continue
            try:
                ident = int(parts[id_field])
            except ValueError:
                continue
            if ident < SYSTEM_ID_LIMIT or ident >= 65534:
                keep.append(line)
    return keep


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

    passwd = system_lines('/etc/passwd', 2) + [
        f'{u["username"]}:x:{int(u["uid"])}:{int(u["gid"])}:{u["username"]}:{u.get("home_dir") or "/"}:{u.get("login_shell") or "/bin/bash"}'
        for u in sorted(users, key=lambda u: int(u['uid']))
        if int(u['uid']) >= SYSTEM_ID_LIMIT
    ]
    group = system_lines('/etc/group', 2) + [
        f'{g["group_name"]}:x:{int(g["gid"])}:{",".join(sorted(members.get(g["group_name"], ())))}'
        for g in sorted(groups, key=lambda g: int(g['gid']))
        if int(g['gid']) >= SYSTEM_ID_LIMIT
    ]
    write_atomic('/etc/passwd', passwd)
    write_atomic('/etc/group', group)
    return len(users), len(groups)


def main():
    once = '--once' in sys.argv
    while True:
        try:
            nu, ng = sync_once()
            print(f'[sync_users] {nu} users, {ng} groups', flush=True)
        except Exception as e:  # keep the loop alive; the next pass may succeed
            print(f'[sync_users] failed: {e}', file=sys.stderr, flush=True)
            if once:
                sys.exit(1)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
