import pytest

from ideadatamodel import exceptions
from ideaclustermanager.app.api.auth_api import AuthAPI
from ideatestutils.api_tokens import api_token_environment, api_token_invocation


@pytest.fixture
def env():
    return api_token_environment()


def invoke(env, method, payload=None, signed_in=False):
    if not signed_in:
        invocation = api_token_invocation(env, f'Auth.{method}', payload)
        AuthAPI(env.context).invoke(invocation)
        return invocation.response_payload
    # a portal session: a Cognito access token, not an api token
    decode = env.service.decode_token
    env.service.decode_token = lambda token, verify_exp=True: {
        'username': 'user-a',
        'cognito:groups': [
            group['GroupName'] for group in env.groups.return_value['Groups']
        ],
    }
    try:
        invocation = api_token_invocation(
            env, f'Auth.{method}', payload, token='session'
        )
        AuthAPI(env.context).invoke(invocation)
        return invocation.response_payload
    finally:
        env.service.decode_token = decode


@pytest.mark.parametrize('administrator', [False, True])
def test_creation_always_belongs_to_the_caller(env, administrator):
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
    result = invoke(
        env,
        'CreateApiToken',
        {'name': 'new', 'expires_in_days': 365, 'username': 'user-b'},
        signed_in=True,
    )
    assert env.rows[result['token_id']]['username'] == 'user-a'
    assert result['token'].startswith('idea_')


def test_a_token_cannot_mint_or_revoke_tokens(env):
    for method, payload in (
        ('CreateApiToken', {'name': 'new', 'expires_in_days': 365}),
        ('DeleteApiToken', {'token_id': env.created.token_id}),
    ):
        with pytest.raises(exceptions.SocaException):
            invoke(env, method, payload)
    assert list(env.rows) == [env.created.token_id]
    assert (
        invoke(env, 'ListApiTokens')['listing'][0]['token_id'] == env.created.token_id
    )


@pytest.mark.parametrize('administrator', [False, True])
def test_list_and_revoke_own_tokens(env, administrator):
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
    result = invoke(env, 'ListApiTokens')
    assert result['listing'][0]['token_id'] == env.created.token_id
    assert 'token_hash' not in str(result)
    invoke(env, 'DeleteApiToken', {'token_id': env.created.token_id}, signed_in=True)
    assert not env.rows
    with pytest.raises(exceptions.SocaException):
        invoke(env, 'ListApiTokens')


@pytest.mark.parametrize('administrator', [False, True])
def test_other_user_list_scope(env, administrator):
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
        assert invoke(env, 'ListApiTokens', {'username': 'user-b'})['listing'] == []
    else:
        with pytest.raises(exceptions.SocaException):
            invoke(env, 'ListApiTokens', {'username': 'user-b'})


@pytest.mark.parametrize('administrator', [False, True])
def test_other_user_delete_scope(env, administrator):
    from ideadatamodel import CreateApiTokenRequest

    other = env.service.create_api_token(
        'user-b', CreateApiTokenRequest(name='other', expires_in_days=30)
    )
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
        invoke(env, 'DeleteApiToken', {'token_id': other.token_id}, signed_in=True)
        assert other.token_id not in env.rows
    else:
        with pytest.raises(exceptions.SocaException):
            invoke(env, 'DeleteApiToken', {'token_id': other.token_id}, signed_in=True)
        assert other.token_id in env.rows


@pytest.mark.parametrize(
    'method', ['CreateApiToken', 'ListApiTokens', 'DeleteApiToken']
)
def test_application_credentials_cannot_manage_tokens(env, method):
    invocation = api_token_invocation(env, f'Auth.{method}')
    invocation._decoded_token = {
        'client_id': 'application',
        'scope': 'cluster-manager/write',
    }
    with pytest.raises(exceptions.SocaException):
        AuthAPI(env.context).invoke(invocation)


def test_account_deletion_removes_only_owner_tokens(env):
    from unittest.mock import Mock
    from ideadatamodel import CreateApiTokenRequest
    from ideaclustermanager.app.accounts.accounts_service import AccountsService

    other = env.service.create_api_token(
        'user-b', CreateApiTokenRequest(name='other', expires_in_days=30)
    )
    account = AccountsService.__new__(AccountsService)
    account.logger = Mock()
    account.is_cluster_administrator = Mock(return_value=False)
    account.user_dao = Mock()
    account.user_dao.get_user.return_value = {'username': 'user-a'}
    account.disable_user = Mock()
    account.user_pool = Mock()
    account.delete_group = Mock()
    account.token_service = env.service
    env.table.scan.side_effect = lambda **kwargs: {
        'Items': [row for row in env.rows.values() if row['username'] == 'user-a']
    }
    account.delete_user('user-a')
    assert set(env.rows) == {other.token_id}
    account.user_dao.delete_user.assert_called_once_with(username='user-a')


@pytest.mark.parametrize('path', ['disable_user', 'reset_password', 'global_sign_out'])
def test_account_security_actions_revoke_all_tokens(env, path):
    from unittest.mock import Mock
    from ideadatamodel import CreateApiTokenRequest
    from ideaclustermanager.app.accounts.accounts_service import AccountsService

    second = env.service.create_api_token(
        'user-a', CreateApiTokenRequest(name='second', expires_in_days=30)
    )
    other = env.service.create_api_token(
        'user-b', CreateApiTokenRequest(name='other', expires_in_days=30)
    )
    env.service.decode_token(env.created.token)
    env.service.decode_token(second.token)
    account = object.__new__(AccountsService)
    account.is_cluster_administrator = Mock(return_value=False)
    account.user_dao = Mock()
    account.user_dao.get_user.return_value = dict(
        username='user-a', enabled=True, group_name='user-a-group'
    )
    account.user_pool = Mock()
    account.group_dao = Mock()
    account.task_manager = Mock()
    account.evdi_client = Mock()
    account.token_service = env.service
    env.table.scan.side_effect = lambda **kwargs: {
        'Items': [row for row in env.rows.values() if row['username'] == 'user-a']
    }
    if path == 'global_sign_out':
        env.context.accounts = account
        invoke(env, 'GlobalSignOut')
    else:
        getattr(account, path)('user-a')
    assert set(env.rows) == {other.token_id}
    for token in (env.created.token, second.token):
        with pytest.raises(exceptions.SocaException):
            env.service.decode_token(token)


def test_accounts_reuses_invocation_decoding(env):
    from unittest.mock import patch
    from ideaclustermanager.app.api.accounts_api import AccountsAPI

    invocation = api_token_invocation(env, 'Accounts.ListUsers')
    with patch.object(
        env.service, 'decode_token', wraps=env.service.decode_token
    ) as decode:
        invocation.get_authorization()
        AccountsAPI(env.context).is_applicable(invocation, 'cluster-manager/read')
    assert decode.call_count == 1


@pytest.mark.parametrize('method', ['AddUserToGroup', 'RemoveUserFromGroup'])
@pytest.mark.parametrize(
    'group',
    [
        'plain-group',
        'admins-cluster-group',
        'managers-cluster-group',
        'leads-cluster-group',
    ],
)
@pytest.mark.parametrize('administrator', [False, True])
def test_privileged_membership_requires_administrator(
    env, method, group, administrator
):
    from ideaclustermanager.app.api.accounts_api import AccountsAPI

    settings = {
        'identity-provider.cognito.administrators_group_name': 'admins',
        'identity-provider.cognito.managers_group_name': 'managers',
        'identity-provider.cognito.operations_leads_group_name': 'leads',
    }
    env.context.config().get_string.side_effect = lambda key, **kwargs: settings.get(
        key, 'pool'
    )
    env.context.get_cluster_modules.return_value = []
    env.groups.return_value = {
        'Groups': [
            {
                'GroupName': 'administrators'
                if administrator
                else 'cluster-manager-administrators-module-group'
            }
        ]
    }
    invocation = api_token_invocation(
        env, f'Accounts.{method}', {'usernames': ['user-a'], 'group_name': group}
    )
    api = AccountsAPI(env.context)
    if administrator or group == 'plain-group':
        api.invoke(invocation)
        assert invocation.response is not None
    else:
        with pytest.raises(exceptions.SocaException):
            api.invoke(invocation)
        env.context.accounts.add_users_to_group.assert_not_called()
        env.context.accounts.remove_users_from_group.assert_not_called()
