"""
Test Cases for the scheduler container's user sync

sync_once rebuilds /etc/passwd and /etc/group from the image's own accounts and the
enabled directory users and groups; these run the real pass on temporary files.
"""

import importlib

import pytest

IMAGE_PASSWD = (
    'root:x:0:0:root:/root:/bin/bash\n'
    'postgres:x:26:26:PostgreSQL:/var/lib/pgsql:/bin/bash\n'
    'ideaserviceaccount:x:990:990::/home/ideaserviceaccount:/sbin/nologin\n'
    'nobody:x:65534:65534:Kernel Overflow User:/:/sbin/nologin\n'
)
IMAGE_GROUP = 'root:x:0:\npostgres:x:26:\nnobody:x:65534:\n'


@pytest.fixture()
def sync(monkeypatch, tmp_path):
    monkeypatch.setenv('IDEA_CLUSTER_NAME', 'idea-mock')
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    module = importlib.import_module('ideascheduler.app.sync_users')
    passwd, group = tmp_path / 'passwd', tmp_path / 'group'
    passwd.write_text(IMAGE_PASSWD)
    group.write_text(IMAGE_GROUP)
    monkeypatch.setattr(module, 'PASSWD', str(passwd))
    monkeypatch.setattr(module, 'GROUP', str(group))
    tables = {'accounts.users': [], 'accounts.groups': [], 'accounts.group-members': []}
    monkeypatch.setattr(module, 'scan', lambda table: tables[table])
    module.tables = tables
    module.passwd, module.group = passwd, group
    return module


def user(name, uid, **fields):
    return {'username': name, 'uid': uid, 'gid': uid, 'enabled': True, **fields}


def lines(path, name):
    return [
        line for line in path.read_text().splitlines() if line.split(':')[0] == name
    ]


def test_a_high_id_user_and_group_are_written_once_and_go_when_disabled(sync):
    # CT-13: ids of 65535 and up were kept as "system" lines and appended again each pass
    sync.tables['accounts.users'] = [user('hi', 70000)]
    sync.tables['accounts.groups'] = [
        {'group_name': 'higrp', 'gid': 70001, 'enabled': True}
    ]
    sync.tables['accounts.group-members'] = [{'group_name': 'higrp', 'username': 'hi'}]
    sync.sync_once()
    sync.sync_once()
    assert len(lines(sync.passwd, 'hi')) == 1
    assert lines(sync.group, 'higrp') == ['higrp:x:70001:hi']
    sync.tables['accounts.users'] = [user('hi', 70000, enabled=False)]
    sync.sync_once()
    assert lines(sync.passwd, 'hi') == []
    # a member who is gone no longer holds the group
    assert lines(sync.group, 'higrp') == ['higrp:x:70001:']


@pytest.mark.parametrize(
    'fields',
    [
        {'login_shell': '/bin/bash\nevil:x:0:0::/:/bin/sh'},
        {'login_shell': '/bin/sh:extra'},
        {'home_dir': '/home/a:b'},
        {'login_shell': 'bash'},
    ],
)
def test_a_user_whose_fields_cant_form_one_line_is_left_out(sync, fields):
    # CT-11: fields were written as stored
    sync.tables['accounts.users'] = [
        user('mallory', 5001, **fields),
        user('alice', 5002),
    ]
    sync.sync_once()
    text = sync.passwd.read_text()
    assert 'mallory' not in text and 'evil' not in text
    assert lines(sync.passwd, 'alice') == ['alice:x:5002:5002:alice:/:/bin/bash']
    assert len(text.splitlines()) == len(IMAGE_PASSWD.splitlines()) + 1


def test_a_directory_user_named_like_an_image_account_is_left_out(sync):
    # CT-12: getpwnam returned whichever postgres came first
    sync.tables['accounts.users'] = [user('postgres', 5003)]
    sync.tables['accounts.groups'] = [
        {'group_name': 'postgres', 'gid': 5003, 'enabled': True}
    ]
    sync.sync_once()
    assert lines(sync.passwd, 'postgres') == [
        'postgres:x:26:26:PostgreSQL:/var/lib/pgsql:/bin/bash'
    ]
    assert lines(sync.group, 'postgres') == ['postgres:x:26:']


def test_the_image_accounts_are_kept_from_the_first_pass(sync):
    # CT-04 relies on this: the image's service account survives every pass
    sync.tables['accounts.users'] = [user('alice', 5002)]
    sync.sync_once()
    sync.sync_once()
    assert sync.passwd.read_text().startswith(IMAGE_PASSWD)
    assert (sync.passwd.parent / 'passwd.idea-base').read_text() == IMAGE_PASSWD


def test_a_user_whose_primary_gid_is_below_1000_is_kept(sync):
    # B-S3: the previous release wrote any directory user with uid >= 1000 whatever its gid
    sync.tables['accounts.users'] = [dict(user('legacy', 5010), gid=100)]
    sync.sync_once()
    assert lines(sync.passwd, 'legacy') == ['legacy:x:5010:100:legacy:/:/bin/bash']
