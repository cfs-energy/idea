"""
`ldap search-users --query <name>` for a user that had been removed ended in
`TypeError: object of type 'NoneType' has no len()`: on Active Directory a search with no
match read as no list at all. It is the command operators use to look for leftover copies
of a user, where no match is the answer they hope for.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from ideadatamodel import SocaFilter
from ideaclustermanager.app.accounts.ldapclient.abstract_ldap_client import (
    AbstractLDAPClient,
)
from ideaclustermanager.cli import ldap_commands


def active_directory(entries):
    """the parts of a client a search reads, answering as Active Directory does"""
    return SimpleNamespace(
        logger=Mock(),
        ldap_user_base='ou=users,dc=example,dc=invalid',
        ldap_group_base='ou=groups,dc=example,dc=invalid',
        ldap_user_filterstr='(objectClass=user)',
        ldap_group_filterstr='(objectClass=group)',
        build_user_filterstr=lambda username: f'(sAMAccountName={username})',
        build_group_filterstr=lambda group_name, username: f'(cn={group_name})',
        is_activedirectory=lambda: True,
        simple_paginated_search=lambda **_: {
            'result': entries,
            'total': None,
            'cookie': None,
        },
        convert_ldap_user=lambda entry: entry,
        convert_ldap_group=lambda entry: entry,
    )


def test_a_user_search_with_no_match_is_an_empty_result():
    users, page = AbstractLDAPClient.search_users(
        active_directory([]), username_filter=SocaFilter(like='gone')
    )
    assert (users, page.total) == ([], 0)


def test_a_user_search_with_a_match_still_returns_it():
    entry = ('cn=kept,ou=users,dc=example,dc=invalid', {'username': 'kept'})
    users, page = AbstractLDAPClient.search_users(
        active_directory([entry]), username_filter=SocaFilter(like='kep')
    )
    assert (users, page.total) == ([{'username': 'kept'}], 1)


def test_a_group_search_with_no_match_is_an_empty_result():
    groups, page = AbstractLDAPClient.search_groups(
        active_directory([]), group_name_filter=SocaFilter(like='gone')
    )
    assert (groups, page.total) == ([], 0)


@pytest.mark.parametrize(
    'command, search, line',
    [
        ('search-users', 'search_users', 'no matching users'),
        ('search-groups', 'search_groups', 'no matching groups'),
    ],
)
def test_the_cli_says_no_match_in_plain_words(monkeypatch, command, search, line):
    context = Mock()
    client = Mock()
    getattr(client, search).return_value = ([], SimpleNamespace(total=0))
    monkeypatch.setattr(ldap_commands, 'build_cli_context', lambda **_: context)
    monkeypatch.setattr(ldap_commands, 'build_ldap_client', lambda _context: client)

    result = CliRunner().invoke(
        ldap_commands.ldap_commands, [command, '--query', 'gone']
    )

    assert result.exit_code == 0, result.output
    context.print.assert_called_once_with(line)
    context.print_json.assert_not_called()
