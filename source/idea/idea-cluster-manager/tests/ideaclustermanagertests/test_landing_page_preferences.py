from types import SimpleNamespace

import pytest

from ideadatamodel import exceptions
from ideaclustermanager.app.accounts.accounts_service import AccountsService


class UserDAO:
    def __init__(self):
        self.row = {'username': 'user-a', 'enabled': True}

    def get_user(self, username):
        return self.row if username == 'user-a' else None

    def update_user(self, values):
        assert values['username'] == 'user-a'
        self.row.update(values)
        return self.row

    def convert_from_db(self, row):
        return SimpleNamespace(**row)


@pytest.fixture
def accounts():
    service = AccountsService.__new__(AccountsService)
    service.user_dao = UserDAO()
    return service


def test_user_can_store_own_landing_page(accounts):
    user = accounts.update_my_preferences('user-a', 'my-costs')
    assert user.username == 'user-a' and user.landing_page == 'my-costs'


def test_invalid_landing_page_is_refused(accounts):
    with pytest.raises(exceptions.SocaException):
        accounts.update_my_preferences('user-a', 'administration')


def test_unset_landing_page_keeps_cluster_default_available(accounts):
    user = accounts.update_my_preferences('user-a', None)
    assert user.landing_page == ''
    cluster_default = 'files'
    assert (user.landing_page or cluster_default) == 'files'


def test_preference_update_cannot_target_another_record(accounts):
    # The service accepts identity separately from the preference payload; no target
    # username is part of UpdateMyPreferencesRequest.
    with pytest.raises(exceptions.SocaException):
        accounts.update_my_preferences('user-b', 'home')


def test_a_regular_user_reaches_the_preference_api_through_auth():
    from unittest.mock import Mock
    from ideadatamodel import User
    from ideaclustermanager.app.api.auth_api import AuthAPI

    context = Mock()
    context.namespace = 'Auth.UpdateMyPreferences'
    context.is_authorized_user.return_value = True
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = Mock(landing_page='my-jobs')
    accounts = Mock()
    accounts.update_my_preferences.return_value = User(
        username='user-a', landing_page='my-jobs'
    )
    api = AuthAPI.__new__(AuthAPI)
    api.context = Mock(accounts=accounts)
    api.invoke(context)
    # The record comes from the token, never from the payload.
    accounts.update_my_preferences.assert_called_once_with('user-a', 'my-jobs')
    context.success.assert_called_once()
