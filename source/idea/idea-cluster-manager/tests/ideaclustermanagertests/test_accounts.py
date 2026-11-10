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
from ideaclustermanagertests.fake_secrets_manager import client_error

import botocore.exceptions
import ldap
import os
import pytest
import re
import time
from typing import Optional
from unittest.mock import Mock


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


@pytest.mark.parametrize('dry_run', [True, False])
def test_repair_home_ownership_api(monkeypatch, tmp_path, dry_run):
    from unittest.mock import Mock
    from ideadatamodel import RepairHomeOwnershipRequest
    from ideaclustermanager.app.api.accounts_api import AccountsAPI
    from ideaclustermanagertests.test_user_home_directory import ownership_tree

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    context = Mock()
    context.accounts.get_user.return_value = helper.user
    api = AccountsAPI(context)
    invocation = Mock()
    invocation.get_request_payload_as.return_value = RepairHomeOwnershipRequest(
        username='user', dry_run=dry_run
    )
    api.acl['Accounts.RepairHomeOwnership']['method'](invocation)
    result = invocation.success.call_args.args[0]
    assert changes == []
    if dry_run:
        assert result.changed == len(owners) - 1
        assert result.skipped == 1
        assert result.failed == 0
        assert len(result.paths) == result.changed
        context.accounts.task_manager.send.assert_not_called()
    else:
        assert result.queued is True
        assert result.task_name == 'accounts.repair-home-ownership'
        context.accounts.task_manager.send.assert_called_once_with(
            task_name='accounts.repair-home-ownership',
            payload={'username': 'user', 'dry_run': False},
            message_group_id='user',
        )


def test_repair_home_ownership_api_requires_administrator():
    from unittest.mock import Mock
    from ideaclustermanager.app.api.accounts_api import AccountsAPI

    context = Mock()
    api = AccountsAPI(context)
    invocation = Mock()
    invocation.is_administrator.return_value = False
    with pytest.raises(exceptions.SocaException) as error:
        api.repair_home_ownership(invocation)
    assert error.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    context.accounts.get_user.assert_not_called()
    context.accounts.task_manager.send.assert_not_called()


def test_repair_home_ownership_request_defaults_and_validation():
    from ideadatamodel import RepairHomeOwnershipRequest

    assert RepairHomeOwnershipRequest(username='user').dry_run is True
    with pytest.raises(ValueError):
        RepairHomeOwnershipRequest(username='user', dry_run='false')
    with pytest.raises(ValueError):
        RepairHomeOwnershipRequest(username='')


def test_a_repeated_membership_change_is_not_dropped_as_a_duplicate(
    context: AppContext, monkeypatch
):
    # the queue drops a repeated dedupe id for five minutes: remove, add, remove
    sent = []
    monkeypatch.setattr(
        context.accounts.task_manager,
        'send',
        lambda **kwargs: sent.append(kwargs),
    )
    for operation in ('remove', 'add', 'remove'):
        context.accounts.send_group_membership_updated('group', 'user', operation)
    dedupe_ids = [message['message_dedupe_id'] for message in sent]
    assert all(dedupe_ids)
    assert len(set(dedupe_ids)) == 3


def deletable_user(context, monkeypatch, username):
    context.accounts.create_user(
        user=User(
            username=username,
            email=f'{username}@example.com',
            password='MockPassword_123',
        ),
        email_verified=True,
    )
    directory = Mock(wraps=context.ldap_client)
    directory.is_readonly.return_value = False
    directory.is_existing_user.return_value = True
    directory.is_existing_group.return_value = True
    directory.is_sudo_user.return_value = False
    monkeypatch.setattr(context, 'ldap_client', directory)
    monkeypatch.setattr(context.accounts, 'ldap_client', directory)
    return directory


def test_deleting_a_user_removes_the_directory_account_without_the_queue(
    context: AppContext, monkeypatch
):
    # the queue drops a deleted user's tasks, so nothing queued can be relied on
    directory = deletable_user(context, monkeypatch, 'accounts_deleted1')
    removed_from_projects = Mock()
    monkeypatch.setattr(
        context.projects.user_projects_dao,
        'group_member_removed',
        removed_from_projects,
    )
    context.accounts.delete_user('accounts_deleted1')
    directory.delete_user.assert_called_once_with('accounts_deleted1')
    directory.remove_user_from_group.assert_called_once_with(
        ['accounts_deleted1'], 'default-project-group'
    )
    removed_from_projects.assert_called_once_with(
        group_name='default-project-group', username='accounts_deleted1'
    )
    assert context.accounts.user_dao.get_user('accounts_deleted1') is None


def test_deleting_a_user_stops_while_the_directory_refuses(
    context: AppContext, monkeypatch
):
    directory = deletable_user(context, monkeypatch, 'accounts_deleted2')
    directory.delete_user.side_effect = RuntimeError('directory down')
    with pytest.raises(RuntimeError):
        context.accounts.delete_user('accounts_deleted2')
    left = context.accounts.user_dao.get_user('accounts_deleted2')
    assert left['enabled'] is False
    # the memberships did go, and the record says so should the user be enabled again
    assert 'default-project-group' not in left['additional_groups']
    # a member the directory does not have is no reason to stop
    directory.delete_user.side_effect = None
    directory.remove_user_from_group.side_effect = ldap.NO_SUCH_ATTRIBUTE()
    context.accounts.delete_user('accounts_deleted2')
    assert context.accounts.user_dao.get_user('accounts_deleted2') is None


def test_deleting_a_disabled_user_leaves_no_group_membership(
    context: AppContext, monkeypatch
):
    # removal from groups is refused for a disabled user, so delete does it itself
    deletable_user(context, monkeypatch, 'accounts_deleted3')
    context.accounts.disable_user('accounts_deleted3')
    members = context.accounts.group_members_dao.get_usernames_in_group
    assert 'accounts_deleted3' in members('default-project-group')
    removed_from_projects = Mock()
    monkeypatch.setattr(
        context.projects.user_projects_dao,
        'group_member_removed',
        removed_from_projects,
    )
    context.accounts.delete_user('accounts_deleted3')
    assert 'accounts_deleted3' not in members('default-project-group')
    removed_from_projects.assert_called_once_with(
        group_name='default-project-group', username='accounts_deleted3'
    )


def test_deleting_a_user_survives_its_queued_copy_removing_the_account_first(
    context: AppContext, monkeypatch
):
    # disabling queues the same removal; the consumer can win between check and delete
    directory = deletable_user(context, monkeypatch, 'accounts_deleted4')
    directory.is_sudo_user.return_value = True
    for removal in ('delete_group', 'delete_user', 'remove_sudo_user'):
        getattr(directory, removal).side_effect = ldap.NO_SUCH_OBJECT()
    context.accounts.delete_user('accounts_deleted4')
    directory.delete_user.assert_called_once_with('accounts_deleted4')
    assert context.accounts.user_dao.get_user('accounts_deleted4') is None


def test_deleting_a_user_again_resumes_the_group_removal(
    context: AppContext, monkeypatch
):
    directory = deletable_user(context, monkeypatch, 'accounts_deleted5')
    removed_from_projects = Mock()
    monkeypatch.setattr(
        context.projects.user_projects_dao,
        'group_member_removed',
        removed_from_projects,
    )
    directory.remove_user_from_group.side_effect = ldap.SERVER_DOWN()
    with pytest.raises(ldap.SERVER_DOWN):
        context.accounts.delete_user('accounts_deleted5')
    removed_from_projects.assert_not_called()
    left = context.accounts.user_dao.get_user('accounts_deleted5')
    assert 'default-project-group' in left['additional_groups']

    directory.remove_user_from_group.reset_mock(side_effect=True)
    context.accounts.delete_user('accounts_deleted5')
    directory.remove_user_from_group.assert_called_once_with(
        ['accounts_deleted5'], 'default-project-group'
    )
    removed_from_projects.assert_called_once_with(
        group_name='default-project-group', username='accounts_deleted5'
    )
    assert context.accounts.user_dao.get_user('accounts_deleted5') is None


@pytest.mark.parametrize(
    'provider, kept', [('aws_managed_activedirectory', True), ('openldap', False)]
)
def test_deleting_a_user_whose_directory_account_is_kept_leaves_its_memberships(
    context: AppContext, monkeypatch, provider, kept
):
    username = 'accounts_kept_ad' if kept else 'accounts_kept_ldap'
    directory = deletable_user(context, monkeypatch, username)
    config = context.config()
    get_string = config.get_string
    monkeypatch.setattr(
        config,
        'get_string',
        lambda key, *args, **kwargs: (
            provider
            if key == 'directoryservice.provider'
            else get_string(key, *args, **kwargs)
        ),
    )
    # as the reconciler disables a user it found disabled in the directory
    context.accounts.disable_user(username, preserve_directory=True)
    send = Mock(wraps=context.accounts.task_manager.send)
    monkeypatch.setattr(context.accounts.task_manager, 'send', send)
    context.accounts.delete_user(username)
    assert directory.remove_user_from_group.called is not kept
    assert directory.delete_user.called is not kept
    # the account's own directory group stays or goes with the account
    queued_groups = [
        call.kwargs['payload']['group_name']
        for call in send.call_args_list
        if call.kwargs['task_name'] == 'accounts.sync-group'
    ]
    assert (f'{username}-user-group' in queued_groups) is not kept
    # the cluster's own records go either way
    members = context.accounts.group_members_dao.get_usernames_in_group
    assert username not in members('default-project-group')
    assert context.accounts.group_dao.get_group(f'{username}-user-group') is None
    assert context.accounts.user_dao.get_user(username) is None


def in_cluster_group(context, monkeypatch, username, group_name):
    # administrators and managers are user pool groups; sign-in reads them there
    from ideadatamodel import constants

    context.accounts.group_dao.create_group(
        {
            'group_name': group_name,
            'title': group_name,
            'gid': 5990,
            'group_type': constants.GROUP_TYPE_CLUSTER,
            'enabled': True,
        }
    )
    context.accounts.add_users_to_group([username], group_name)
    remove = Mock()
    monkeypatch.setattr(
        context.aws().cognito_idp(),
        'admin_remove_user_from_group',
        remove,
        raising=False,
    )
    return remove


def test_a_stopped_delete_does_not_leave_user_pool_rights_the_record_no_longer_lists(
    context: AppContext, monkeypatch
):
    username, group_name = 'accounts_deleted6', 'accounts-deleted6-cluster-group'
    directory = deletable_user(context, monkeypatch, username)
    removed_from_pool = in_cluster_group(context, monkeypatch, username, group_name)
    directory.delete_user.side_effect = RuntimeError('directory down')
    with pytest.raises(RuntimeError):
        context.accounts.delete_user(username)
    assert (
        group_name
        not in context.accounts.user_dao.get_user(username)['additional_groups']
    )
    removed_from_pool.assert_called_once()
    assert removed_from_pool.call_args.kwargs['Username'] == username
    assert removed_from_pool.call_args.kwargs['GroupName'] == group_name


@pytest.mark.parametrize('gone', ['UserNotFoundException', 'ResourceNotFoundException'])
def test_deleting_a_user_the_user_pool_no_longer_has_still_finishes(
    context: AppContext, monkeypatch, gone
):
    username = f'accounts_deleted7{gone[0].lower()}'
    group_name = f'{username}-cluster-group'
    deletable_user(context, monkeypatch, username)
    removed_from_pool = in_cluster_group(context, monkeypatch, username, group_name)
    removed_from_pool.side_effect = botocore.exceptions.ClientError(
        {'Error': {'Code': gone}}, 'AdminRemoveUserFromGroup'
    )
    context.accounts.delete_user(username)
    assert context.accounts.user_dao.get_user(username) is None


def test_a_user_pool_that_refuses_the_group_removal_stops_the_delete_with_the_group_listed(
    context: AppContext, monkeypatch
):
    username, group_name = 'accounts_deleted8', 'accounts-deleted8-cluster-group'
    deletable_user(context, monkeypatch, username)
    removed_from_pool = in_cluster_group(context, monkeypatch, username, group_name)
    removed_from_pool.side_effect = botocore.exceptions.ClientError(
        {'Error': {'Code': 'TooManyRequestsException'}}, 'AdminRemoveUserFromGroup'
    )
    with pytest.raises(botocore.exceptions.ClientError):
        context.accounts.delete_user(username)
    # record and member list agree on what is left
    left = context.accounts.user_dao.get_user(username)
    assert group_name in left['additional_groups']
    members = context.accounts.group_members_dao.get_usernames_in_group
    assert username in members(group_name)

    removed_from_pool.side_effect = None
    context.accounts.delete_user(username)
    assert username not in members(group_name)
    assert context.accounts.user_dao.get_user(username) is None


def test_a_directory_that_refuses_a_group_removal_leaves_the_member_list_showing_it(
    context: AppContext, monkeypatch
):
    directory = deletable_user(context, monkeypatch, 'accounts_deleted9')
    directory.remove_user_from_group.side_effect = ldap.SERVER_DOWN()
    with pytest.raises(ldap.SERVER_DOWN):
        context.accounts.delete_user('accounts_deleted9')
    members = context.accounts.group_members_dao.get_usernames_in_group
    assert 'accounts_deleted9' in members('default-project-group')


def stopped_after_the_user_group_went(context, monkeypatch, username):
    deletable_user(context, monkeypatch, username)
    delete_record = context.accounts.user_dao.delete_user
    monkeypatch.setattr(
        context.accounts.user_dao,
        'delete_user',
        Mock(side_effect=RuntimeError('table throttled')),
    )
    with pytest.raises(RuntimeError):
        context.accounts.delete_user(username)
    monkeypatch.setattr(context.accounts.user_dao, 'delete_user', delete_record)
    assert context.accounts.group_dao.get_group(f'{username}-user-group') is None


def test_a_delete_that_stopped_after_the_user_group_went_can_be_run_again(
    context: AppContext, monkeypatch
):
    stopped_after_the_user_group_went(context, monkeypatch, 'accounts_deleted10')
    context.accounts.delete_user('accounts_deleted10')
    assert context.accounts.user_dao.get_user('accounts_deleted10') is None


def test_a_user_whose_delete_stopped_after_its_accounts_went_cannot_be_enabled(
    context: AppContext, monkeypatch
):
    stopped_after_the_user_group_went(context, monkeypatch, 'accounts_deleted11')
    with pytest.raises(exceptions.SocaException) as refused:
        context.accounts.enable_user('accounts_deleted11')
    assert refused.value.error_code == errorcodes.AUTH_INVALID_OPERATION
    assert context.accounts.user_dao.get_user('accounts_deleted11')['enabled'] is False


@pytest.fixture
def secrets(context: AppContext, monkeypatch, tmp_path):
    # no file is ever written for a password set by this release
    monkeypatch.setattr(context.accounts, 'ds_automation_dir', str(tmp_path))
    monkeypatch.setattr(context.accounts, '_next_password_secret_sweep', 0.0)
    fake = context.aws().secretsmanager()
    monkeypatch.setattr(fake, 'secrets', {})
    monkeypatch.setattr(fake, 'created', [])
    monkeypatch.setattr(fake, 'deleted', [])
    monkeypatch.setattr(fake, 'list_calls', [])
    monkeypatch.setattr(fake, 'failing', {})
    monkeypatch.setattr(fake, 'undeletable', set())
    return fake


PASSWORD_TAGS = {
    # not idea:ModuleName: the role may overwrite any secret of the cluster that has it
    'idea:ClusterName': 'idea-mock',
    'idea:Purpose': 'password-sync',
}


@pytest.mark.parametrize('kms_key_id', [None, 'customer-key-id'])
def test_a_password_waits_for_its_task_in_a_secret_and_never_in_a_file(
    context: AppContext, monkeypatch, tmp_path, secrets, kms_key_id
):
    send = Mock()
    monkeypatch.setattr(context.accounts.task_manager, 'send', send)
    if kms_key_id:
        context.config().put('cluster.secretsmanager.kms_key_id', kms_key_id)
    try:
        context.accounts.change_ldap_password('user', 'secret-value')
    finally:
        context.config().put('cluster.secretsmanager.kms_key_id', None)

    (request,) = secrets.created
    assert re.fullmatch(r'idea-mock-password-sync-[0-9a-f-]{36}', request['Name'])
    assert request['SecretString'] == 'secret-value'
    assert {tag['Key']: tag['Value'] for tag in request['Tags']} == PASSWORD_TAGS
    # the cluster's key when it has one, else the account's default key
    assert request.get('KmsKeyId') == kms_key_id
    assert set(request) == {'Name', 'SecretString', 'Tags'} | (
        {'KmsKeyId'} if kms_key_id else set()
    )
    (arn,) = secrets.secrets
    queued = send.call_args_list[-1].kwargs
    assert queued['task_name'] == 'accounts.sync-password'
    assert queued['payload'] == {'username': 'user', 'password_secret': arn}
    assert 'secret-value' not in str(send.call_args_list)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('failing', ['directory', 'queue'])
def test_a_password_that_could_not_be_queued_leaves_no_secret(
    context: AppContext, monkeypatch, tmp_path, secrets, failing
):
    if failing == 'directory':
        monkeypatch.setattr(
            context.accounts.ldap_client,
            'is_existing_user',
            Mock(side_effect=RuntimeError('directory down')),
        )
    else:
        monkeypatch.setattr(
            context.accounts.task_manager,
            'send',
            Mock(side_effect=RuntimeError('queue unavailable')),
        )
    with pytest.raises(RuntimeError):
        context.accounts.change_ldap_password('user', 'secret-value')
    assert len(secrets.created) == 1
    assert secrets.secrets == {}
    assert list(tmp_path.iterdir()) == []


def test_a_secret_that_cannot_be_deleted_does_not_hide_why_the_queue_failed(
    context: AppContext, monkeypatch, secrets
):
    monkeypatch.setattr(
        context.accounts.task_manager,
        'send',
        Mock(side_effect=RuntimeError('queue unavailable')),
    )
    secrets.failing['DeleteSecret'] = client_error('InternalServiceError', 'Delete')
    with pytest.raises(RuntimeError, match='queue unavailable'):
        context.accounts.change_ldap_password('user', 'secret-value')


def test_only_this_clusters_password_secrets_past_the_queues_reach_are_swept(
    context: AppContext, secrets
):
    prefix = 'idea-mock-password-sync-'
    old = secrets.add(f'{prefix}old', tags=PASSWORD_TAGS, age_days=9.1)
    kept = [
        # a task moved into the password lane can be queued for twice the 4 days
        secrets.add(f'{prefix}waiting', tags=PASSWORD_TAGS, age_days=8.9),
        # each of the name and the two tags is required
        secrets.add('idea-mock-other-old', tags=PASSWORD_TAGS, age_days=9.1),
        secrets.add(f'{prefix}untagged', age_days=9.1),
        *(
            secrets.add(
                f'{prefix}{n}', tags={**PASSWORD_TAGS, key: 'other'}, age_days=9.1
            )
            for n, key in enumerate(PASSWORD_TAGS)
        ),
    ]

    context.accounts.change_ldap_password('user', 'secret-value')

    assert secrets.deleted == [old]
    assert set(kept) < set(secrets.secrets)
    assert secrets.list_calls == [
        {
            'Filters': [
                {'Key': 'name', 'Values': [prefix]},
                {'Key': 'tag-key', 'Values': ['idea:Purpose']},
            ]
        }
    ]
    # not on every password: the next look is an hour away
    context.accounts.change_ldap_password('user', 'secret-value')
    assert len(secrets.list_calls) == 1


def test_a_stray_secret_that_cannot_be_deleted_does_not_keep_the_others(
    context: AppContext, monkeypatch, secrets
):
    prefix = 'idea-mock-password-sync-'
    stuck, old = (
        secrets.add(f'{prefix}{name}', tags=PASSWORD_TAGS, age_days=10)
        for name in ('a-stuck', 'b-old')
    )
    secrets.undeletable.add(stuck)
    warning = Mock()
    monkeypatch.setattr(context.accounts.logger, 'warning', warning)
    context.accounts.change_ldap_password('user', 'secret-value')
    assert secrets.deleted == [old]
    warning.assert_called_once_with(
        'could not delete a stray password secret: AccessDeniedException'
    )


def user_pool_password_changes(context, user_pool):
    from ideadatamodel.auth import RespondToAuthChallengeRequest

    accounts = context.accounts
    return {
        'change': (
            user_pool.change_password,
            lambda: accounts.change_password('token', 'user', 'old', 'secret-value'),
        ),
        'forgot': (
            user_pool.confirm_forgot_password,
            lambda: accounts.confirm_forgot_password('user', 'secret-value', '123456'),
        ),
        'challenge': (
            user_pool.respond_to_auth_challenge,
            lambda: accounts.respond_to_auth_challenge(
                RespondToAuthChallengeRequest(
                    username='user',
                    challenge_name='NEW_PASSWORD_REQUIRED',
                    session='session',
                    new_password='secret-value',
                )
            ),
        ),
    }


@pytest.fixture
def password_change(context: AppContext, monkeypatch, secrets, request):
    user_pool, send = Mock(), Mock()
    monkeypatch.setattr(context.accounts, 'user_pool', user_pool)
    monkeypatch.setattr(context.accounts.task_manager, 'send', send)
    in_user_pool, change = user_pool_password_changes(context, user_pool)[request.param]
    return secrets, in_user_pool, change, send


every_password_change = pytest.mark.parametrize(
    'password_change', ['change', 'forgot', 'challenge'], indirect=True
)


@every_password_change
def test_a_password_the_secret_cannot_hold_is_not_given_to_the_user_pool(
    password_change,
):
    secrets, in_user_pool, change, send = password_change
    secrets.failing['CreateSecret'] = client_error('InternalServiceError', 'Create')
    with pytest.raises(botocore.exceptions.ClientError):
        change()
    # nothing changed: sign-in and the directory still agree on the old password
    in_user_pool.assert_not_called()
    send.assert_not_called()


@every_password_change
def test_a_password_the_user_pool_refuses_leaves_no_secret(password_change):
    secrets, in_user_pool, change, send = password_change
    in_user_pool.side_effect = RuntimeError('wrong old password')
    with pytest.raises(RuntimeError):
        change()
    assert len(secrets.created) == 1
    assert secrets.secrets == {}
    send.assert_not_called()


@every_password_change
def test_a_password_the_user_pool_took_is_queued_with_the_secret_made_before(
    password_change,
):
    secrets, in_user_pool, change, send = password_change
    in_user_pool.side_effect = lambda *_, **__: Mock(held=list(secrets.secrets))
    result = change()
    (arn,) = secrets.secrets
    if result is not None:
        # the secret was there when the user pool was asked
        assert result.held == [arn]
    assert send.call_args_list[-1].kwargs['payload'] == {
        'username': 'user',
        'password_secret': arn,
    }


@pytest.mark.parametrize('password_change', ['challenge'], indirect=True)
def test_a_challenge_that_sets_no_password_leaves_no_secret(password_change):
    secrets, in_user_pool, change, send = password_change
    in_user_pool.return_value = Mock(auth=None)
    assert change().auth is None
    assert len(secrets.created) == 1
    assert secrets.secrets == {}
    send.assert_not_called()


def test_a_sweep_that_cannot_list_secrets_does_not_stop_a_password_change(
    context: AppContext, secrets
):
    secrets.failing['ListSecrets'] = client_error('AccessDeniedException', 'List')
    context.accounts.change_ldap_password('user', 'secret-value')
    assert len(secrets.secrets) == 1


def stranded(folder, age_days, names=('password.txt',)):
    folder.mkdir()
    for name in names:
        (folder / name).write_text('secret-value')
    then = time.time() - age_days * 24 * 3600
    for path in (*folder.iterdir(), folder):
        os.utime(path, (then, then))
    return folder


def test_a_password_file_no_task_is_left_to_read_is_removed(
    context: AppContext, tmp_path, secrets
):
    # a release that gave up on a task without cleaning up after it left the file
    old = stranded(tmp_path / 'tmpold', 15)
    # the queue can still hold the task of anything younger than its longest retention
    waiting = stranded(tmp_path / 'tmpwaiting', 13)
    # only what a password change makes is removed
    other = stranded(tmp_path / 'tmpother', 15, names=('password.txt', 'notes'))
    not_a_password = stranded(tmp_path / 'tmpnotes', 15, names=('notes',))
    (tmp_path / 'elsewhere').mkdir()
    target = stranded(tmp_path / 'elsewhere' / 'kept', 15)
    (tmp_path / 'tmplink').symlink_to(target, target_is_directory=True)
    linked = tmp_path / 'tmplinked'
    linked.mkdir()
    (linked / 'password.txt').symlink_to(target / 'password.txt')

    context.accounts.change_ldap_password('user', 'secret-value')

    assert not old.exists()
    for kept in (waiting, other, not_a_password, target, linked):
        assert kept.exists(), kept
    assert (target / 'password.txt').read_text() == 'secret-value'
    assert len(list(tmp_path.glob('*/password.txt'))) == 4


def test_a_sweep_that_cannot_read_the_folder_does_not_stop_a_password_change(
    context: AppContext, monkeypatch, secrets
):
    monkeypatch.setattr(os, 'scandir', Mock(side_effect=OSError('stale file handle')))
    context.accounts.change_ldap_password('user', 'secret-value')
    assert len(secrets.secrets) == 1


def test_a_new_user_does_not_inherit_rows_an_earlier_user_of_the_name_left(
    context: AppContext,
):
    # before 26.11.2 deleting a disabled user left its group and project rows
    username = 'accounts_reused1'
    members = context.accounts.group_members_dao
    projects = context.projects.user_projects_dao
    members.create_membership('leftover-group', username)
    members.create_membership('leftover-group', 'accounts_someone_else')
    projects.create_user_project(project_id='leftover-project', username=username)
    context.accounts.create_user(
        user=User(
            username=username,
            email=f'{username}@example.com',
            password='MockPassword_123',
        ),
        email_verified=True,
    )
    assert members.get_usernames_in_group('leftover-group') == ['accounts_someone_else']
    assert not projects.is_user_in_project(username, 'leftover-project')
    assert username in members.get_usernames_in_group('default-project-group')
