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
from ideaclustermanager.app.accounts.db.user_dao import UserDAO
from ideadatamodel import exceptions, errorcodes, User, ListUsersRequest

import botocore.exceptions
import pytest
from typing import Optional


class AccountsTestContext:
    crud_user: Optional[User]


def test_user_instance_type_exceptions_are_serialized():
    stored = UserDAO.convert_to_db(
        User(
            username='desktop-user',
            instance_type_exceptions=['m6a.large', 'g5.xlarge'],
        )
    )
    assert stored['instance_type_exceptions'] == ['m6a.large', 'g5.xlarge']

    dao = object.__new__(UserDAO)
    assert dao.convert_from_db(stored).instance_type_exceptions == [
        'm6a.large',
        'g5.xlarge',
    ]
    assert (
        UserDAO.convert_to_db(
            User(username='desktop-user', instance_type_exceptions=[])
        )['instance_type_exceptions']
        == []
    )


def test_accounts_create_user_missing_username_should_fail(context: AppContext):
    """
    create user with missing username
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(username=''))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'username is required' in exc_info.value.message


def test_accounts_create_user_invalid_username_should_fail(context: AppContext):
    """
    create user with invalid username
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(username='Invalid Username'))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'user.username must match regex' in exc_info.value.message


def test_accounts_create_user_system_account_username_should_fail(context: AppContext):
    """
    create user with username as system accounts
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(username='root'))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'invalid username:' in exc_info.value.message


def test_accounts_create_user_missing_email_should_fail(context: AppContext):
    """
    create user with missing email
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(user=User(username='mockuser1'))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'email is required' in exc_info.value.message


def test_accounts_create_user_invalid_email_should_fail(context: AppContext):
    """
    create user with invalid email
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(
            user=User(username='mockuser1', email='invalid-email')
        )
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'invalid email:' in exc_info.value.message


def test_accounts_create_user_with_verified_email_missing_password_should_fail(
    context: AppContext,
):
    """
    create valid account with email verified and no password
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(
            user=User(username='mockuser1', email='mockuser1@example.com'),
            email_verified=True,
        )
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'Password is required' in exc_info.value.message


def test_accounts_crud_create_user(context: AppContext):
    """
    create user
    """
    created_user = context.accounts.create_user(
        user=User(
            username='accounts_user1',
            email='accounts_user1@example.com',
            password='MockPassword_123',
        ),
        email_verified=True,
    )

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
    assert (
        len(user.additional_groups) == 2
    )  # default project group and personal user group
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
        login_shell='/bin/csh',
        instance_type_exceptions=['m6a.large'],
    )
    context.accounts.modify_user(user=modify_user, email_verified=True)

    user = context.accounts.get_user(username=crud_user.username)
    assert user.username == modify_user.username
    assert user.email == modify_user.email
    assert user.uid == modify_user.uid
    assert user.gid == modify_user.gid
    assert user.login_shell == modify_user.login_shell
    assert user.instance_type_exceptions == modify_user.instance_type_exceptions

    context.accounts.modify_user(
        user=User(username=crud_user.username, instance_type_exceptions=[])
    )
    assert (
        context.accounts.get_user(username=crud_user.username).instance_type_exceptions
        == []
    )


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


def _invalid_password_error(operation: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {
            'Error': {
                'Code': 'InvalidPasswordException',
                'Message': 'Password did not conform with policy',
            }
        },
        operation,
    )


def test_accounts_create_user_rejected_password_should_fail(
    context: AppContext, monkeypatch
):
    """
    the user pool rejects the password. the call must fail rather than report a
    user that cannot sign in.
    """
    cognito = context.aws().cognito_idp()
    deleted = []

    def raise_invalid_password(**_):
        raise _invalid_password_error('AdminSetUserPassword')

    monkeypatch.setattr(
        cognito, 'admin_set_user_password', raise_invalid_password, raising=False
    )
    monkeypatch.setattr(
        cognito,
        'admin_delete_user',
        lambda **kwargs: deleted.append(kwargs.get('Username')) or {},
        raising=False,
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(
            user=User(
                username='rejectedpwuser',
                email='rejectedpwuser@example.com',
                password='MockPassword_123',
            ),
            email_verified=True,
        )

    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert 'rejectedpwuser' in deleted
    assert context.accounts.user_dao.get_user('rejectedpwuser') is None


def test_accounts_create_user_rejected_temporary_password_should_fail(
    context: AppContext, monkeypatch
):
    """
    an unverified-email create sends the generated password as a temporary password.
    a rejection there must surface as an invalid params error, not a raw client error.
    """
    cognito = context.aws().cognito_idp()

    def raise_invalid_password(**_):
        raise _invalid_password_error('AdminCreateUser')

    monkeypatch.setattr(
        cognito, 'admin_create_user', raise_invalid_password, raising=False
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(
            user=User(
                username='temppwuser',
                email='temppwuser@example.com',
            ),
            email_verified=False,
        )

    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert context.accounts.user_dao.get_user('temppwuser') is None


def test_accounts_create_user_failure_after_db_write_rolls_back(
    context: AppContext, monkeypatch
):
    """
    a step that fails after the user record is written must not leave the account,
    its personal group or its group memberships behind.
    """
    cognito = context.aws().cognito_idp()
    deleted = []
    monkeypatch.setattr(
        cognito,
        'admin_delete_user',
        lambda **kwargs: deleted.append(kwargs.get('Username')) or {},
        raising=False,
    )

    def raise_password_sync(*_, **__):
        raise exceptions.soca_exception(
            error_code=errorcodes.GENERAL_ERROR,
            message='directory service unavailable',
        )

    monkeypatch.setattr(context.accounts, 'change_ldap_password', raise_password_sync)

    sent = []
    monkeypatch.setattr(
        context.task_manager,
        'send',
        lambda *args, **kwargs: sent.append(kwargs),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        context.accounts.create_user(
            user=User(
                username='rollbackuser',
                email='rollbackuser@example.com',
                password='MockPassword_123',
            ),
            email_verified=True,
        )

    assert exc_info.value.error_code == errorcodes.GENERAL_ERROR
    assert context.accounts.user_dao.get_user('rollbackuser') is None
    assert context.accounts.group_dao.get_group('rollbackuser-user-group') is None
    assert 'rollbackuser' in deleted
    # nothing that builds the account was queued: a queued task would run against
    # a record the rollback has already deleted.
    queued = [
        task
        for task in sent
        if not (
            task['task_name'] == 'accounts.group-membership-updated'
            and task['payload']['operation'] == 'remove'
        )
    ]
    assert queued == []
    default_group = context.accounts.group_name_helper.get_default_project_group()
    assert (
        'rollbackuser'
        not in context.accounts.group_members_dao.get_usernames_in_group(default_group)
    )


def test_accounts_create_user_pool_failure_after_create_deletes_pool_user(
    context: AppContext, monkeypatch
):
    """
    the pool entry exists once admin_create_user returns, and the password is set
    in a second call. a failure there must delete the entry, not leave a user
    that exists but cannot sign in.
    """
    cognito = context.aws().cognito_idp()
    deleted = []

    def raise_internal_error(**_):
        raise botocore.exceptions.ClientError(
            {'Error': {'Code': 'InternalErrorException', 'Message': 'try later'}},
            'AdminSetUserPassword',
        )

    monkeypatch.setattr(
        cognito, 'admin_set_user_password', raise_internal_error, raising=False
    )
    monkeypatch.setattr(
        cognito,
        'admin_delete_user',
        lambda **kwargs: deleted.append(kwargs.get('Username')) or {},
        raising=False,
    )

    with pytest.raises(botocore.exceptions.ClientError):
        context.accounts.create_user(
            user=User(
                username='poolfailuser',
                email='poolfailuser@example.com',
                password='MockPassword_123',
            ),
            email_verified=True,
        )

    assert deleted == ['poolfailuser']
    assert context.accounts.user_dao.get_user('poolfailuser') is None


@pytest.mark.parametrize('administrator', [False, True])
def test_group_listing_redacts_other_user_instance_type_exceptions(administrator):
    from unittest.mock import Mock
    from ideadatamodel import ListUsersInGroupRequest, ListUsersInGroupResult
    from ideaclustermanager.app.api.auth_api import AuthAPI

    users = [
        User(username=username, instance_type_exceptions=['m6a.large'])
        for username in ['user-a', 'user-b']
    ]
    invocation = Mock()
    invocation.namespace = 'Auth.ListUsersInGroup'
    invocation.is_authenticated.return_value = True
    invocation.is_administrator.return_value = administrator
    invocation.get_username.return_value = 'user-a'
    invocation.get_request_payload_as.return_value = ListUsersInGroupRequest(
        group_names=['group-a']
    )
    api = AuthAPI.__new__(AuthAPI)
    api.context = Mock()
    api.context.accounts.list_users_in_group.return_value = ListUsersInGroupResult(
        listing=users
    )
    api.invoke(invocation)
    listing = invocation.success.call_args.args[0].listing
    assert listing[0].instance_type_exceptions == ['m6a.large']
    assert listing[1].instance_type_exceptions == (
        ['m6a.large'] if administrator else None
    )
    assert users[1].instance_type_exceptions == ['m6a.large']


@pytest.fixture
def reporting_accounts():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ideaclustermanager.app.accounts.accounts_service import AccountsService
    from ideaclustermanager.app.accounts.db.group_dao import GroupDAO
    from ideadatamodel import constants
    from ideasdk.config.soca_config import SocaConfig
    from ideasdk.utils import GroupNameHelper

    config = SocaConfig(
        {
            'identity-provider': {
                'cognito': {
                    'administrators_group_name': 'administrators',
                    'managers_group_name': 'managers',
                }
            },
            'directoryservice': {'provider': 'openldap'},
            'cluster': {
                'administrator_username': 'admin-user',
                'administrator_email': 'admin@example.invalid',
            },
        }
    )
    context = SimpleNamespace(
        config=lambda: config,
        get_cluster_modules=lambda: [
            {
                'module_id': constants.MODULE_CLUSTER_MANAGER,
                'name': constants.MODULE_CLUSTER_MANAGER,
                'type': constants.MODULE_TYPE_APP,
            }
        ],
    )
    accounts = object.__new__(AccountsService)
    accounts.context = context
    accounts.group_name_helper = GroupNameHelper(context)
    accounts.logger = Mock()
    accounts.ldap_client = Mock()
    accounts.ldap_client.is_readonly.return_value = False
    accounts.ldap_client.get_group.return_value = {'gid': 1400}
    accounts.group_dao = Mock()
    groups = {}
    accounts.group_dao.get_group.side_effect = groups.get
    accounts.group_dao.create_group.side_effect = lambda group: groups.update(
        {group['group_name']: group}
    )
    accounts.group_dao.convert_to_db.side_effect = GroupDAO.convert_to_db
    accounts.group_dao.convert_from_db.side_effect = GroupDAO.convert_from_db
    accounts.user_dao = Mock()
    accounts.user_dao.get_user.return_value = {'username': 'admin-user'}
    accounts.sequence_config_dao = Mock()
    accounts.sequence_config_dao.next_gid.side_effect = iter(range(2000, 2100))
    accounts.task_manager = Mock()
    accounts.user_pool = Mock()
    accounts.group_members_dao = Mock()
    return accounts, groups


@pytest.mark.parametrize('readonly', [False, True])
@pytest.mark.parametrize(
    'configured', [None, 'report-readers', 'report-readers-cluster-group']
)
@pytest.mark.parametrize('upgrade', [False, True])
def test_operations_group_boot_is_idempotent(
    reporting_accounts, readonly, configured, upgrade
):
    from ideadatamodel import constants

    accounts, groups = reporting_accounts
    config = accounts.context.config()
    if configured is not None:
        config.put('identity-provider.cognito.operations_leads_group_name', configured)
    name = accounts.group_name_helper.get_cluster_operations_leads_group()
    assert name == (
        'report-readers-cluster-group'
        if configured
        else 'operations-leads-cluster-group'
    )
    config.put(f'directoryservice.group_mapping.{name}', 'directory-report-readers')
    accounts.ldap_client.is_readonly.return_value = readonly
    if upgrade:
        accounts.create_defaults()
        del groups[name]
        accounts.group_dao.create_group.reset_mock()
        accounts.task_manager.send.reset_mock()
    accounts.create_defaults()
    group = dict(groups[name])
    assert group['group_type'] == constants.GROUP_TYPE_CLUSTER
    assert group['enabled']
    if readonly:
        assert group['ds_name'] == 'directory-report-readers'
        assert group['gid'] == 1400
        accounts.ldap_client.get_group.assert_any_call(
            group_name='directory-report-readers'
        )
        accounts.sequence_config_dao.next_gid.assert_not_called()
    else:
        assert 'ds_name' not in group
        assert group['gid'] >= 2000
        accounts.ldap_client.get_group.assert_not_called()
    if upgrade:
        accounts.group_dao.create_group.assert_called_once()
    syncs = [
        call
        for call in accounts.task_manager.send.call_args_list
        if call.kwargs['payload'] == {'group_name': name}
    ]
    assert len(syncs) == 1
    assert syncs[0].kwargs['task_name'] == 'accounts.sync-group'
    accounts.group_dao.create_group.reset_mock()
    accounts.task_manager.send.reset_mock()
    accounts.create_defaults()
    assert groups[name] == group
    accounts.group_dao.create_group.assert_not_called()
    accounts.task_manager.send.assert_not_called()


@pytest.mark.parametrize('directory_group', [None, {}])
def test_readonly_operations_group_requires_directory_gid(
    reporting_accounts, directory_group
):
    accounts, groups = reporting_accounts
    accounts.create_defaults()
    name = accounts.group_name_helper.get_cluster_operations_leads_group()
    del groups[name]
    accounts.ldap_client.is_readonly.return_value = True
    accounts.ldap_client.get_group.return_value = directory_group
    accounts.create_defaults()
    assert name not in groups
    status = accounts.operations_leads_configuration_status
    assert status['status'] == 'configuration_required'
    assert status['group_name'] == name
    assert name in status['reason']
    assert 'gidNumber' in status['reason']
    accounts.logger.warning.assert_called_with(status['reason'])

    from ideasdk.api import ApiInvocationContext
    from ideasdk.auth import ApiAuthorization, ApiAuthorizationType
    from unittest.mock import Mock

    invocation = object.__new__(ApiInvocationContext)
    invocation._group_name_helper = accounts.group_name_helper
    invocation._token_service = Mock()
    invocation.has_access_token = lambda: True
    invocation.is_unix_domain_socket_invocation = lambda: False
    invocation.get_authorization = lambda: ApiAuthorization(
        type=ApiAuthorizationType.USER, groups=[]
    )
    assert invocation.can_read_reporting() is False

    accounts.context.config().put(
        f'directoryservice.group_mapping.{name}', 'directory-report-readers'
    )
    accounts.ldap_client.get_group.return_value = {'gid': 1400}
    accounts.create_defaults()
    assert groups[name]['gid'] == 1400
    assert groups[name]['ds_name'] == 'directory-report-readers'
    assert accounts.operations_leads_configuration_status['status'] == 'ready'
    accounts.group_dao.create_group.reset_mock()
    accounts.create_defaults()
    accounts.group_dao.create_group.assert_not_called()


@pytest.mark.parametrize(
    'group_name',
    [
        'administrators',
        'administrators-cluster-group',
        'managers',
        'managers-cluster-group',
        'cluster-manager-administrators-module-group',
        'cluster-manager-administrators-module-group-cluster-group',
    ],
)
def test_operations_group_rejects_privileged_collisions_before_provisioning(
    reporting_accounts, group_name
):
    accounts, _ = reporting_accounts
    accounts.context.config().put(
        'identity-provider.cognito.operations_leads_group_name', group_name
    )
    with pytest.raises(exceptions.SocaException, match='privileged group'):
        accounts.create_defaults()
    accounts.group_dao.create_group.assert_not_called()
    accounts.task_manager.send.assert_not_called()


@pytest.mark.parametrize(
    'privileged_key', ['administrators_group_name', 'managers_group_name']
)
def test_operations_group_rejects_custom_privileged_names(
    reporting_accounts, privileged_key
):
    accounts, _ = reporting_accounts
    config = accounts.context.config()
    config.put(f'identity-provider.cognito.{privileged_key}', 'shared-readers')
    config.put(
        'identity-provider.cognito.operations_leads_group_name',
        'shared-readers-cluster-group',
    )
    with pytest.raises(exceptions.SocaException, match='privileged group'):
        accounts.create_defaults()


def test_operations_membership_synchronizes_without_removing_other_groups(
    reporting_accounts,
):
    accounts, _ = reporting_accounts
    accounts.create_defaults()
    name = accounts.group_name_helper.get_cluster_operations_leads_group()
    existing_groups = ['managers-cluster-group', 'cluster-manager-users-module-group']
    user = {
        'username': 'reporting-user',
        'enabled': True,
        'sudo': False,
        'additional_groups': list(existing_groups),
    }
    accounts.user_dao.get_user.return_value = user
    accounts.user_dao.update_user.side_effect = user.update
    accounts.add_users_to_group([user['username']], name)
    accounts.add_users_to_group([user['username']], name)
    assert user['additional_groups'] == existing_groups + [name]
    assert user['sudo'] is False
    accounts.user_pool.admin_add_user_to_group.assert_called_with(
        username=user['username'], group_name=name
    )
    accounts.group_members_dao.create_membership.assert_called_with(
        name, user['username']
    )
    accounts.remove_users_from_group([user['username']], name)
    assert user['additional_groups'] == existing_groups
    assert user['sudo'] is False
    accounts.user_pool.admin_remove_user_from_group.assert_called_once_with(
        username=user['username'], group_name=name
    )
    accounts.group_members_dao.delete_membership.assert_called_once_with(
        name, user['username']
    )
    assert [
        call.kwargs['payload']['operation']
        for call in accounts.task_manager.send.call_args_list
        if call.kwargs['task_name'] == 'accounts.group-membership-updated'
    ] == ['add', 'add', 'remove']
