import hashlib
import re
import secrets
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ideadatamodel import CreateApiTokenRequest, exceptions, errorcodes
from ideasdk.auth import ApiAuthorizationType
from ideatestutils.api_tokens import api_token_environment, api_token_invocation


@pytest.fixture
def env():
    return api_token_environment()


def test_token_format_hash_and_private_listing(env):
    token = env.created.token
    assert re.fullmatch(r'idea_[A-Za-z0-9_-]{43}', token)
    row = env.rows[env.created.token_id]
    assert row['token_hash'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(row)
    assert row['expires_on'] - row['created_on'] == 30 * 86400
    listing = env.service.list_api_tokens('user-a').model_dump()
    assert 'token_hash' not in str(listing)
    assert token not in str(listing)
    assert listing['listing'][0]['last_used_on'] is None


@pytest.mark.parametrize('days', [0, -1, 366, 1.5, True, '30'])
def test_expiry_bounds(days):
    with pytest.raises(ValidationError):
        CreateApiTokenRequest(name='automation', expires_in_days=days)


@pytest.mark.parametrize('days', [1, 365])
def test_expiry_limits_are_accepted(env, days):
    created = env.service.create_api_token(
        'user-a', CreateApiTokenRequest(name='automation', expires_in_days=days)
    )
    row = env.rows[created.token_id]
    assert row['expires_on'] - row['created_on'] == days * 86400


@pytest.mark.parametrize('name', ['', ' ' * 3, 'x' * 129])
def test_invalid_name(env, name):
    with pytest.raises((ValidationError, exceptions.SocaException)):
        env.service.create_api_token(
            'user-a', CreateApiTokenRequest(name=name, expires_in_days=30)
        )


def test_valid_token_refreshes_groups_on_cache_miss(env):
    invocation = api_token_invocation(env, 'Auth.GetUser')
    assert invocation.is_authorized_user()
    assert invocation.get_username() == 'user-a'
    assert f'auth:TOKEN:{env.created.token_id}' in invocation.get_log_tag()
    env.service._api_token_cache.clear()
    env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
    assert api_token_invocation(env, 'Auth.GetUser').is_administrator()
    env.service._api_token_cache.clear()
    env.groups.return_value = {'Groups': []}
    invocation = api_token_invocation(env, 'Auth.GetUser')
    assert not invocation.is_administrator()
    assert not invocation.is_authorized_user()
    assert invocation.get_authorization().type == ApiAuthorizationType.USER
    assert env.table.query.call_count == 3
    assert env.groups.call_count == 3


def test_groups_are_paginated(env):
    env.groups.side_effect = [
        {'Groups': [], 'NextToken': 'next'},
        {'Groups': [{'GroupName': 'managers'}]},
    ]
    assert api_token_invocation(env, 'Auth.GetUser').is_manager()
    assert env.groups.call_args.kwargs['NextToken'] == 'next'


@pytest.mark.parametrize('user', [None, {'username': 'user-a', 'enabled': False}])
def test_missing_or_disabled_user_is_denied_after_cache_expiry(env, user):
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=100):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    env.users.get_item.return_value = {'Item': user}
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=130):
        with pytest.raises(exceptions.SocaException):
            api_token_invocation(env, 'Auth.GetUser').get_authorization()


def test_cached_token_still_expires(env):
    row = env.rows[env.created.token_id]
    with patch(
        'ideasdk.auth.token_service.time.time', return_value=row['expires_on'] - 1
    ):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    with patch('ideasdk.auth.token_service.time.time', return_value=row['expires_on']):
        with pytest.raises(exceptions.SocaException) as error:
            api_token_invocation(env, 'Auth.GetUser').get_authorization()
    assert error.value.error_code == errorcodes.AUTH_TOKEN_EXPIRED
    assert env.table.query.call_count == 1


def test_revocation_invalidates_local_cache(env):
    api_token_invocation(env, 'Auth.GetUser').get_authorization()
    env.service.delete_api_token(env.created.token_id, 'user-a')
    with pytest.raises(exceptions.SocaException):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    assert not env.rows


def test_remote_revocation_is_observed_at_cache_expiry(env):
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=100):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
        env.rows.clear()
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=160):
        with pytest.raises(exceptions.SocaException):
            api_token_invocation(env, 'Auth.GetUser').get_authorization()
    assert env.table.query.call_count == 2


def test_stale_index_cannot_restore_revoked_token(env):
    env.table.query.side_effect = None
    env.table.query.return_value = {'Items': [env.rows[env.created.token_id]]}
    env.rows.clear()
    with pytest.raises(exceptions.SocaException):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()


def test_unknown_tokens_are_negatively_cached_for_five_seconds(env):
    token = 'idea_' + secrets.token_urlsafe(32)
    for clock in [100, 104, 105]:
        with patch('ideasdk.auth.token_service.time.monotonic', return_value=clock):
            with pytest.raises(exceptions.SocaException):
                api_token_invocation(
                    env, 'Auth.GetUser', token=token
                ).get_authorization()
    assert env.table.query.call_count == 2
    assert env.groups.call_count == 0
    assert all(len(key) == 64 for key in env.service._api_token_cache)


@pytest.mark.parametrize('token', ['idea_short', 'idea_' + '!' * 43])
def test_malformed_tokens_do_not_query_storage(env, token):
    with pytest.raises(exceptions.SocaException):
        api_token_invocation(env, 'Auth.GetUser', token=token).get_authorization()
    env.table.query.assert_not_called()


def test_last_used_updates_at_most_once_per_minute_across_readers(env):
    for offset in [0, 10, 59, 60]:
        now = env.rows[env.created.token_id]['created_on'] + offset
        env.service._api_token_cache.clear()
        with patch('ideasdk.auth.token_service.time.time', return_value=now):
            api_token_invocation(env, 'Auth.GetUser').get_authorization()
    assert env.table.update_item.call_count == 2


def test_create_response_is_not_logged_with_payload_tracing(env):
    invocation = api_token_invocation(env, 'Auth.CreateApiToken')
    invocation.success(env.created)
    invocation.log_response()
    assert env.created.token not in str(env.context.logger().mock_calls)
    assert not invocation.is_payload_tracing_enabled()


def test_table_is_initialized_using_cluster_table_conventions(env):
    env.context.aws_util().dynamodb_check_table_exists.return_value = False
    env.service.initialize_api_tokens()
    request = env.context.aws_util().dynamodb_create_table.call_args.kwargs
    assert request['wait'] is True
    assert request['create_table_request']['TableName'] == env.table.name
    assert request['create_table_request']['BillingMode'] == 'PAY_PER_REQUEST'
    env.context.aws_util().dynamodb_create_table.reset_mock()
    env.context.aws_util().dynamodb_check_table_exists.return_value = True
    env.service.initialize_api_tokens()
    env.context.aws_util().dynamodb_create_table.assert_not_called()


def test_negative_cache_has_a_fixed_size(env):
    for _ in range(1025):
        with pytest.raises(exceptions.SocaException):
            env.service.decode_token('idea_' + secrets.token_urlsafe(32))
    assert len(env.service._api_token_negative_cache) == 128
    assert not env.service._api_token_cache


def test_deleted_cognito_user_is_denied(env):
    from botocore.exceptions import ClientError

    env.groups.side_effect = ClientError(
        {'Error': {'Code': 'UserNotFoundException'}}, 'AdminListGroupsForUser'
    )
    with pytest.raises(exceptions.SocaException) as error:
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    assert error.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_list_tokens_reads_all_pages(env):
    row = env.rows[env.created.token_id]
    env.table.query.side_effect = [
        {'Items': [row], 'LastEvaluatedKey': {'token_id': row['token_id']}},
        {'Items': []},
    ]
    result = env.service.list_api_tokens('user-a')
    assert len(result.listing) == 1
    assert env.table.query.call_args.kwargs['ExclusiveStartKey'] == {
        'token_id': row['token_id']
    }


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('created_on', [None, 2000])
def test_recreated_owner_is_denied(env, cached, created_on):
    if cached:
        with patch('ideasdk.auth.token_service.time.monotonic', return_value=100):
            api_token_invocation(env, 'Auth.GetUser').get_authorization()
    env.users.get_item.return_value = {
        'Item': {'username': 'user-a', 'enabled': True, 'created_on': created_on}
    }
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=130):
        with pytest.raises(exceptions.SocaException):
            api_token_invocation(env, 'Auth.GetUser').get_authorization()


def test_token_records_owner_creation_time(env):
    assert env.rows[env.created.token_id]['owner_created_on'] == 1000


def test_token_without_owner_creation_time_is_denied(env):
    env.rows[env.created.token_id].pop('owner_created_on', None)
    with pytest.raises(exceptions.SocaException):
        api_token_invocation(env, 'Auth.GetUser').get_authorization()


def test_token_hash_uses_constant_time_comparison(env):
    with patch(
        'ideasdk.auth.token_service.secrets.compare_digest',
        wraps=secrets.compare_digest,
    ) as compare:
        api_token_invocation(env, 'Auth.GetUser').get_authorization()
    digest = env.rows[env.created.token_id]['token_hash']
    compare.assert_called_once_with(digest, digest)


def test_authorization_is_cached_for_thirty_seconds(env):
    env.users.get_item.reset_mock()
    for clock in (100, 129, 130):
        env.groups.return_value = {'Groups': [{'GroupName': str(clock)}]}
        with patch('ideasdk.auth.token_service.time.monotonic', return_value=clock):
            decoded = env.service.decode_token(env.created.token)
            assert decoded['cognito:groups'] == ['130' if clock == 130 else '100']
    assert env.groups.call_count == 2
    assert env.users.get_item.call_count == 2


def test_bad_tokens_do_not_evict_good_tokens(env):
    env.service.decode_token(env.created.token)
    for _ in range(1025):
        with pytest.raises(exceptions.SocaException):
            env.service.decode_token('idea_' + secrets.token_urlsafe(32))
    before = env.table.query.call_count
    env.service.decode_token(env.created.token)
    assert env.table.query.call_count == before


@pytest.mark.parametrize('operation', ['query', 'get_item'])
def test_table_reads_do_not_hold_cache_lock(env, operation):
    from concurrent.futures import ThreadPoolExecutor

    method = getattr(env.table, operation)
    original = method.side_effect

    def read(**kwargs):
        def probe():
            acquired = env.service._api_token_lock.acquire(timeout=0.2)
            if acquired:
                env.service._api_token_lock.release()
            return acquired

        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(probe).result()
        return original(**kwargs)

    method.side_effect = read
    env.service.decode_token(env.created.token)


def test_revocation_during_authorization_cannot_restore_cache(env):
    def groups(**kwargs):
        env.service.delete_api_token(env.created.token_id, 'user-a')
        return {'Groups': []}

    env.groups.side_effect = groups
    with pytest.raises(exceptions.SocaException):
        env.service.decode_token(env.created.token)
    assert not env.service._api_token_cache


def test_query_token_is_ignored_and_header_is_preserved():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ideasdk.server.soca_server import ApiInvocationHandler

    handler = object.__new__(ApiInvocationHandler)
    handler.server = Mock()
    request = SimpleNamespace(args={'token': ['query-secret']})
    for header in (None, {'token_type': 'Bearer', 'token': 'header-secret'}):
        handler.server.get_authorization_header.return_value = header
        assert handler.get_token(request) == header


def test_bulk_revocation_scans_all_pages_and_invalidates_absent_rows(env):
    from copy import deepcopy

    first = deepcopy(env.rows[env.created.token_id])
    env.service.decode_token(env.created.token)
    other = env.service.create_api_token(
        'user-a', CreateApiTokenRequest(name='second', expires_in_days=30)
    )
    second = deepcopy(env.rows[other.token_id])
    env.service.decode_token(other.token)
    env.rows.pop(first['token_id'])
    env.table.scan.side_effect = [
        {'Items': [], 'LastEvaluatedKey': {'token_id': first['token_id']}},
        {'Items': [second]},
    ]
    env.service.revoke_user_api_tokens('user-a')
    assert not env.rows
    assert not env.service._api_token_cache
    assert env.table.scan.call_args.kwargs['ConsistentRead'] is True
    assert env.table.scan.call_args.kwargs['ExclusiveStartKey'] == {
        'token_id': first['token_id']
    }


def test_new_token_recovers_from_index_delay_after_five_seconds(env):
    query = env.table.query.side_effect
    env.table.query.side_effect = None
    env.table.query.return_value = {'Items': []}
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=100):
        with pytest.raises(exceptions.SocaException):
            env.service.decode_token(env.created.token)
    env.table.query.side_effect = query
    with patch('ideasdk.auth.token_service.time.monotonic', return_value=105):
        assert env.service.decode_token(env.created.token)['username'] == 'user-a'
