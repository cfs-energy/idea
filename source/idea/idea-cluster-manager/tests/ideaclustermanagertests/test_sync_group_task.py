from decimal import Decimal
from unittest.mock import Mock

import pytest

from ideaclustermanager.app.accounts.account_tasks import (
    SyncGroupInDirectoryServiceTask,
)


def task(existing):
    context = Mock()
    context.ldap_client.is_readonly.return_value = False
    context.ldap_client.is_existing_group.return_value = existing
    # the row as the table returns it
    context.accounts.group_dao.get_group.return_value = {
        'group_name': 'team-marker',
        'gid': Decimal(7401),
        'enabled': True,
    }
    context.accounts.group_members_dao.get_usernames_in_group.return_value = [
        'member-a',
        'member-b',
    ]
    context.accounts.user_dao.get_user.side_effect = lambda username: {
        'username': username,
        'enabled': username != 'member-disabled',
    }
    return SyncGroupInDirectoryServiceTask(context), context.ldap_client


def test_an_enabled_group_recreated_in_the_directory_gets_its_members_back():
    sync, directory = task(existing=False)
    sync.invoke({'group_name': 'team-marker'})
    directory.sync_group.assert_called_once_with(group_name='team-marker', gid=7401)
    assert [c.args for c in directory.add_user_to_group.call_args_list] == [
        (['member-a'], 'team-marker'),
        (['member-b'], 'team-marker'),
    ]


@pytest.mark.parametrize('existing', [True])
def test_a_group_already_in_the_directory_keeps_its_members_as_they_are(existing):
    sync, directory = task(existing)
    sync.invoke({'group_name': 'team-marker'})
    directory.add_user_to_group.assert_not_called()


def test_one_member_the_directory_refuses_does_not_keep_the_rest_out():
    import ldap

    sync, directory = task(existing=False)
    sync.context.accounts.group_members_dao.get_usernames_in_group.return_value = [
        'member-a',
        'member-gone',
        'member-disabled',
        'member-c',
    ]

    def add(usernames, group_name):
        if usernames == ['member-gone']:
            raise ldap.NO_SUCH_OBJECT({'desc': 'marker'})

    directory.add_user_to_group.side_effect = add
    sync.invoke({'group_name': 'team-marker'})
    assert [c.args[0] for c in directory.add_user_to_group.call_args_list] == [
        ['member-a'],
        ['member-gone'],
        ['member-c'],
    ]


def test_a_directory_outage_leaves_the_restore_for_the_retry():
    import ldap

    sync, directory = task(existing=False)
    members = ['member-a', 'member-b', 'member-c']
    sync.context.accounts.group_members_dao.get_usernames_in_group.return_value = (
        members
    )
    row = {'group_name': 'team-marker', 'gid': Decimal(7401), 'enabled': True}
    sync.context.accounts.group_dao.get_group.side_effect = lambda name: dict(row)
    sync.context.accounts.group_dao.update_group.side_effect = row.update
    added = []
    down = [True]

    def add(usernames, group_name):
        if usernames == ['member-b'] and down[0]:
            raise ldap.SERVER_DOWN({'desc': 'marker'})
        added.append(usernames[0])

    directory.add_user_to_group.side_effect = add
    with pytest.raises(ldap.SERVER_DOWN):
        sync.invoke({'group_name': 'team-marker'})
    assert row['members_pending'] is True
    # the retry finds the group present, and the restore still pending
    directory.is_existing_group.return_value = True
    down[0] = False
    sync.invoke({'group_name': 'team-marker'})
    assert set(added) == set(members)
    assert row['members_pending'] is False
