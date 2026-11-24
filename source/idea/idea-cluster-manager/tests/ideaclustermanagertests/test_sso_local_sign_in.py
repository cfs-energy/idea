from unittest.mock import Mock

import botocore.exceptions
import pytest

from ideaclustermanager.app.accounts.cognito_user_pool import (
    CognitoUserPool,
    CognitoUserPoolOptions,
)
from ideadatamodel import InitiateAuthRequest, exceptions
from ideasdk.config.soca_config import SocaConfig


def test_local_sign_in_under_sso_answers_every_name_as_a_wrong_password():
    context = Mock()
    context.config.return_value = SocaConfig(
        {
            'cluster': {'administrator_username': 'admin-marker'},
            'identity-provider': {'cognito': {'sso_enabled': True}},
        }
    )
    context.aws().cognito_idp().admin_initiate_auth.side_effect = (
        botocore.exceptions.ClientError(
            {'Error': {'Code': 'NotAuthorizedException', 'Message': 'marker'}},
            'AdminInitiateAuth',
        )
    )
    pool = CognitoUserPool(
        context,
        CognitoUserPoolOptions(
            user_pool_id='pool',
            admin_group_name='admins',
            client_id='client',
            client_secret='secret-marker',
        ),
    )
    answers = []
    for username in ('admin-marker', 'someone-else'):
        with pytest.raises(exceptions.SocaException) as refused:
            pool.initiate_username_password_auth(
                InitiateAuthRequest(
                    auth_flow='USER_PASSWORD_AUTH',
                    username=username,
                    password='wrong-marker',
                )
            )
        answers.append((refused.value.error_code, refused.value.message))
    # the administrator's wrong password and a name SSO keeps from signing in locally
    assert answers[0] == answers[1]
    # only the administrator's attempt reached the user pool
    assert context.aws().cognito_idp().admin_initiate_auth.call_count == 1


def test_an_unconfirmed_account_signs_in_as_a_wrong_password():
    context = Mock()
    context.config.return_value = SocaConfig(
        {
            'cluster': {'administrator_username': 'admin-marker'},
            'identity-provider': {'cognito': {'sso_enabled': False}},
        }
    )
    pool = CognitoUserPool(
        context,
        CognitoUserPoolOptions(
            user_pool_id='pool',
            admin_group_name='admins',
            client_id='client',
            client_secret='secret-marker',
        ),
    )
    answers = []
    for code in ('UserNotFoundException', 'UserNotConfirmedException'):
        context.aws().cognito_idp().admin_initiate_auth.side_effect = (
            botocore.exceptions.ClientError(
                {'Error': {'Code': code, 'Message': 'marker'}}, 'AdminInitiateAuth'
            )
        )
        with pytest.raises(exceptions.SocaException) as refused:
            pool.initiate_username_password_auth(
                InitiateAuthRequest(
                    auth_flow='USER_PASSWORD_AUTH', username='user', password='pw'
                )
            )
        answers.append((refused.value.error_code, refused.value.message))
    assert answers[0] == answers[1]


@pytest.mark.parametrize(
    'code, error_code',
    [
        ('NotAuthorizedException', 'UNAUTHORIZED_ACCESS'),
        ('TooManyRequestsException', 'GENERAL_ERROR'),
        ('InternalErrorException', 'GENERAL_ERROR'),
    ],
)
def test_a_renewal_tells_a_refused_token_from_a_busy_user_pool(code, error_code):
    context = Mock()
    context.aws().cognito_idp().admin_initiate_auth.side_effect = (
        botocore.exceptions.ClientError(
            {'Error': {'Code': code, 'Message': 'marker'}}, 'AdminInitiateAuth'
        )
    )
    pool = CognitoUserPool(
        context,
        CognitoUserPoolOptions(
            user_pool_id='pool',
            admin_group_name='admins',
            client_id='client',
            client_secret='secret-marker',
        ),
    )
    with pytest.raises(exceptions.SocaException) as refused:
        pool.initiate_refresh_token_auth('user', 'refresh-marker')
    assert refused.value.error_code == error_code
    assert 'marker' not in refused.value.message
