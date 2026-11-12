"""
`ldap search-groups --query 'zzz*'` ended in a traceback, `ldap.FILTER_ERROR: Bad search
filter`: what was typed went into the search filter as written (`(cn=*zzz**)`). The same
was true of every name, email and group the client put into a filter, so a value holding
`)(` could add its own conditions to a directory search. Every value is escaped now, and a
search query is matched as written: `*` is a character, not a wildcard.
"""

from unittest.mock import Mock

import ldap
import pytest
from click.testing import CliRunner
from ldap.filter import escape_filter_chars

from ideadatamodel import SocaFilter
from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
    ActiveDirectoryClient,
)
from ideaclustermanager.app.accounts.ldapclient.openldap_client import OpenLDAPClient
from ideaclustermanager.cli import ldap_commands

HOSTILE = [
    'zzz*',
    '*',
    'a)(objectClass=*',
    '*)(|(cn=*',
    '(',
    'back\\slash',
    'nul\x00',
]


def parse(filterstr):
    """
    the library's own filter parser, with no directory: it reads the filter before it
    connects, so a bad one is FILTER_ERROR (what the cluster saw) and a good one gets as
    far as finding nothing listening.
    """
    with pytest.raises(ldap.SERVER_DOWN):
        ldap.initialize('ldap://127.0.0.1:1').search_ext(
            'dc=example,dc=invalid', ldap.SCOPE_SUBTREE, filterstr
        )


class Searches:
    """a client with no directory behind it: it keeps the filters it would have sent"""

    ldap_user_base = 'ou=users,dc=example,dc=invalid'
    ldap_group_base = 'ou=groups,dc=example,dc=invalid'
    ldap_computers_base = 'ou=computers,dc=example,dc=invalid'
    ldap_sudoers_base = 'ou=sudoers,dc=example,dc=invalid'
    ldap_sudoers_group_dn = 'cn=sudoers,ou=groups,dc=example,dc=invalid'

    def __init__(self):
        self.logger = Mock()
        self.filters = []

    def search_s(self, base, filterstr=None, **_):
        self.filters.append(filterstr)
        parse(filterstr)
        return []

    def simple_paginated_search(self, base, filterstr=None, **_):
        self.search_s(base, filterstr)
        return {'result': None, 'total': None, 'cookie': None}

    def sssvlv_paginated_search(self, base, filterstr=None, **_):
        self.search_s(base, filterstr)
        return {'result': [], 'total': 0, 'cookie': None}

    def delete_s(self, dn):
        raise AssertionError('nothing was found, so nothing is deleted')


class ActiveDirectory(Searches, ActiveDirectoryClient):
    def is_activedirectory(self):
        return True


class OpenLDAP(Searches, OpenLDAPClient):
    def is_activedirectory(self):
        return False


def test_the_parser_used_here_refuses_the_filter_the_cluster_sent():
    # positive control for parse(): the filter from the cluster's traceback
    with pytest.raises(ldap.FILTER_ERROR):
        ldap.initialize('ldap://127.0.0.1:1').search_ext(
            'dc=example,dc=invalid',
            ldap.SCOPE_SUBTREE,
            '(&(objectClass=group)(cn=*zzz**))',
        )


@pytest.mark.parametrize('directory', [ActiveDirectory, OpenLDAP])
@pytest.mark.parametrize('query', HOSTILE)
def test_a_search_matches_what_was_typed_as_written(directory, query):
    client = directory()
    typed = escape_filter_chars(query)

    assert client.search_users(username_filter=SocaFilter(like=query))[0] == []
    assert client.search_groups(group_name_filter=SocaFilter(like=query))[0] == []
    assert client.search_groups(username_filter=SocaFilter(eq=query))[0] == []

    assert client.filters == [
        f'(&{client.ldap_user_filterstr}(cn=*{typed}*))',
        f'(&{client.ldap_group_filterstr}(cn=*{typed}*))',
        f'(&{client.ldap_group_filterstr}(memberUid={typed}))',
    ]


@pytest.mark.parametrize(
    'kind, pattern',
    [
        ('eq', 'a\\2ab'),
        ('starts_with', 'a\\2ab*'),
        ('ends_with', '*a\\2ab'),
        ('like', '*a\\2ab*'),
    ],
)
def test_the_only_wildcards_are_the_ones_the_kind_of_match_adds(kind, pattern):
    client = ActiveDirectory()
    client.search_users(username_filter=SocaFilter(**{kind: 'a*b'}))
    assert client.filters == [f'(&(objectClass=user)(cn={pattern}))']


def test_a_search_with_no_query_still_lists_everything():
    client = ActiveDirectory()
    client.search_users(username_filter=SocaFilter(like=None))
    client.search_groups(group_name_filter=SocaFilter(like=None))
    assert client.filters == ['(objectClass=user)', '(objectClass=group)']


BUILDERS = [
    lambda c, v: c.build_user_filterstr(v),
    lambda c, v: c.build_group_filterstr(group_name=v),
    lambda c, v: c.build_group_filterstr(group_name=f'CN={v},ou=groups,dc=example'),
    lambda c, v: c.build_group_filterstr(username=v),
    lambda c, v: c.build_sudoer_filterstr(v),
]
AD_BUILDERS = [
    lambda c, v: c.build_samaccountname_filterstr(v),
    lambda c, v: c.build_email_filterstr(v),
    lambda c, v: c.build_nestedgroup_membership_filterstr(v, 'group'),
    lambda c, v: c.build_nestedgroup_membership_filterstr('user', v),
    lambda c, v: c.build_nestedgroup_email_membership_filterstr(v, 'group'),
    lambda c, v: c.build_nestedgroup_email_membership_filterstr('user', v),
]


@pytest.mark.parametrize(
    'directory, build',
    [(ActiveDirectory, build) for build in BUILDERS + AD_BUILDERS]
    + [(OpenLDAP, build) for build in BUILDERS],
)
@pytest.mark.parametrize('value', HOSTILE)
def test_a_value_cannot_add_to_the_filter_it_is_put_in(directory, build, value):
    client = directory()
    plain = build(client, 'PLAINVALUE')
    assert plain.count('PLAINVALUE') == 1

    built = build(client, value)

    assert built == plain.replace('PLAINVALUE', escape_filter_chars(value))
    parse(built)


def test_a_lookup_of_one_user_cannot_match_another():
    client = ActiveDirectory()
    assert client.get_user('*') is None
    assert client.get_user('*', required_group='g*') is None
    assert client.get_user_by_email('*@example.invalid') is None
    assert client.is_existing_user('*') is False
    assert client.is_sudo_user('*') is False
    assert client.is_existing_group('*') is False
    assert client.get_group('*') is None
    assert client.get_all_groups_with_user('*') == []
    client.delete_computer('*')
    assert client.filters and not [f for f in client.filters if '*' in f]


@pytest.mark.parametrize(
    'command, line',
    [
        ('search-users', 'no matching users'),
        ('search-groups', 'no matching groups'),
    ],
)
def test_the_cli_takes_a_query_ending_in_a_star(monkeypatch, command, line):
    context = Mock()
    client = ActiveDirectory()
    monkeypatch.setattr(ldap_commands, 'build_cli_context', lambda **_: context)
    monkeypatch.setattr(ldap_commands, 'build_ldap_client', lambda _context: client)

    result = CliRunner().invoke(
        ldap_commands.ldap_commands, [command, '--query', 'zzz*']
    )

    assert result.exit_code == 0, result.output
    context.print.assert_called_once_with(line)
    assert client.filters[0].endswith('(cn=*zzz\\2a*))')
