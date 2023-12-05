#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
Test Cases for AccountsService
"""

from ideaclustermanager import AppContext
from ideadatamodel import (
    exceptions,
    errorcodes,
    User,
    ListUsersRequest
)

import pytest
from typing import Optional


class AccountsTestContext:
    crud_user: Optional[User]


def test_accounts_create_user_missing_username_should_fail(context: AppContext):
    """
    create user with missing username
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username=''
        ))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'username is required' in exc_info.value.message


def test_accounts_create_user_invalid_username_should_fail(context: AppContext):
    """
    create user with invalid username
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username='Invalid Username'
        ))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'user.username must match regex' in exc_info.value.message


def test_accounts_create_user_system_account_username_should_fail(context: AppContext):
    """
    create user with username as system accounts
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username='root'
        ))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'invalid username:' in exc_info.value.message


def test_accounts_create_user_missing_email_should_fail(context: AppContext):
    """
    create user with missing email
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username='mockuser1'
        ))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'email is required' in exc_info.value.message


def test_accounts_create_user_invalid_email_should_fail(context: AppContext):
    """
    create user with invalid email
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username='mockuser1',
            email='invalid-email'
        ))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'invalid email:' in exc_info.value.message


def test_accounts_create_user_with_verified_email_missing_password_should_fail(context: AppContext):
    """
    create valid account with email verified and no password
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(
            username='mockuser1',
            email='mockuser1@example.com'
        ), email_verified=True)
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'user.password is required' in exc_info.value.message


def test_accounts_crud_create_user(context: AppContext):
    """
    create user
    """
    created_user = context.accounts.create_user(user=User(
        username='accounts_user1',
        email='accounts_user1@example.com',
        password='MockPassword_123'
    ), email_verified=True)

    expected_group_name = f'{created_user.username}-user-group'

    assert created_user.username is not None
    assert created_user.email is not None

    user = context.accounts.get_user(username=created_user.username)

    assert user is not None
    assert user.username is not None
    assert user.email is not None
    assert user.group_name is not None
    assert user.group_name == expected_group_name
    assert user.additional_groups is not None
    assert len(user.additional_groups) == 2  # default project group and personal user group
    assert 'default-project-group' in user.additional_groups
    assert expected_group_name in user.additional_groups
    assert user.enabled is not None
    assert user.enabled is True
    assert user.uid is not None
    assert user.gid is not None
    assert user.sudo is not None or user.sudo is False
    assert user.home_dir is not None
    assert user.login_shell is not None
    assert user.password is None
    assert user.created_on is not None
    assert user.updated_on is not None

    AccountsTestContext.crud_user = user


def test_accounts_crud_get_user(context: AppContext):
    """
    get user
    """
    assert AccountsTestContext.crud_user is not None
    crud_user = AccountsTestContext.crud_user

    user = context.accounts.get_user(username=crud_user.username)

    assert user is not None
    assert user.username == crud_user.username
    assert user.uid == crud_user.uid
    assert user.gid == crud_user.gid


def test_accounts_crud_modify_user(context: AppContext):
    """
    modify user
    """
    assert AccountsTestContext.crud_user is not None
    crud_user = AccountsTestContext.crud_user

    modify_user = User(
        username=crud_user.username,
        email='accounts_user1_modified@example.com',
        uid=6000,
        gid=6000,
        login_shell='/bin/csh'
    )
    context.accounts.modify_user(user=modify_user, email_verified=True)

    user = context.accounts.get_user(username=crud_user.username)
    assert user.username == modify_user.username
    assert user.email == modify_user.email
    assert user.uid == modify_user.uid
    assert user.gid == modify_user.gid
    assert user.login_shell == modify_user.login_shell


def test_accounts_crud_disable_user(context: AppContext):
    """
    disable user
    """
    assert AccountsTestContext.crud_user is not None
    crud_user = AccountsTestContext.crud_user

    context.accounts.disable_user(crud_user.username)
    user = context.accounts.get_user(username=crud_user.username)
    assert user.enabled is False


def test_accounts_crud_enable_user(context: AppContext):
    """
    enable user
    """
    assert AccountsTestContext.crud_user is not None
    crud_user = AccountsTestContext.crud_user

    context.accounts.enable_user(crud_user.username)
    user = context.accounts.get_user(username=crud_user.username)
    assert user.enabled is True


def test_accounts_crud_list_users(context: AppContext):
    """
    list users
    """
    assert AccountsTestContext.crud_user is not None
    crud_user = AccountsTestContext.crud_user

    result = context.accounts.list_users(ListUsersRequest())
    assert result.listing is not None

    found = None
    for user in result.listing:
        if user.username == crud_user.username:
            found = user
            break
    assert found is not None
