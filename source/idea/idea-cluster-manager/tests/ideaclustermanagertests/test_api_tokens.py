import pytest

from ideadatamodel import exceptions
from ideaclustermanager.app.api.auth_api import AuthAPI
from ideatestutils.api_tokens import api_token_environment, api_token_invocation


@pytest.fixture
def env():
    return api_token_environment()


def invoke(env, method, payload=None):
    invocation = api_token_invocation(env, f'Auth.{method}', payload)
    AuthAPI(env.context).invoke(invocation)
    return invocation.response_payload


@pytest.mark.parametrize('administrator', [False, True])
def test_creation_always_belongs_to_the_caller(env, administrator):
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
    result = invoke(
        env,
        'CreateApiToken',
        {'name': 'new', 'expires_in_days': 365, 'username': 'user-b'},
    )
    assert env.rows[result['token_id']]['username'] == 'user-a'
    assert result['token'].startswith('idea_')


@pytest.mark.parametrize('administrator', [False, True])
def test_list_and_revoke_own_tokens(env, administrator):
    if administrator:
        env.groups.return_value = {'Groups': [{'GroupName': 'administrators'}]}
    result = invoke(env, 'ListApiTokens')
    assert result['listing'][0]['token_id'] == env.created.token_id
    assert 'token_hash' not in str(result)
    invoke(env, 'DeleteApiToken', {'token_id': env.created.token_id})
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
        invoke(env, 'DeleteApiToken', {'token_id': other.token_id})
        assert other.token_id not in env.rows
    else:
        with pytest.raises(exceptions.SocaException):
            invoke(env, 'DeleteApiToken', {'token_id': other.token_id})
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
    account.delete_user('user-a')
    assert set(env.rows) == {other.token_id}
    account.user_dao.delete_user.assert_called_once_with(username='user-a')
