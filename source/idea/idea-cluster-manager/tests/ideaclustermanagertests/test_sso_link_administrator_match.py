from unittest.mock import Mock

import pytest

from ideaclustermanager.app.accounts.cognito_user_pool import CognitoUserPool


def a_pool():
    pool = object.__new__(CognitoUserPool)
    pool._logger = Mock()
    pool._context = Mock()
    pool._context.config.return_value.get_string.side_effect = lambda key, **_: {
        'cluster.administrator_username': 'clusteradmin',
        'identity-provider.cognito.sso_idp_provider_type': 'OIDC',
    }.get(key, 'provider')
    pool.options = Mock(user_pool_id='pool')
    return pool


@pytest.mark.parametrize(
    'username, linked',
    [('clusteradmin', False), ('admin', True), ('clusteradmin2', True)],
)
def test_only_the_administrator_itself_is_left_unlinked(username, linked):
    pool = a_pool()
    pool.admin_link_idp_for_user(username, f'{username}@example.com')
    link = pool._context.aws.return_value.cognito_idp.return_value
    assert link.admin_link_provider_for_user.called is linked
