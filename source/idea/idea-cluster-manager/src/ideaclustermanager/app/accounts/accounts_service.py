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
from ideasdk.client.evdi_client import EvdiClient
from ideasdk.context import SocaContext
from ideadatamodel.auth import (
    User,
    Group,
    InitiateAuthRequest,
    InitiateAuthResult,
    RespondToAuthChallengeRequest,
    RespondToAuthChallengeResult,
    ListUsersRequest,
    ListUsersResult,
    ListGroupsRequest,
    ListGroupsResult,
    ListUsersInGroupRequest,
    ListUsersInGroupResult,
)
from ideadatamodel import exceptions, errorcodes, constants
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.auth import TokenService

from ideaclustermanager.app.accounts.ldapclient.abstract_ldap_client import (
    AbstractLDAPClient,
)
from ideaclustermanager.app.accounts.cognito_user_pool import CognitoUserPool
from ideaclustermanager.app.accounts import auth_constants
from ideaclustermanager.app.accounts.auth_utils import AuthUtils
from ideaclustermanager.app.accounts.db.group_dao import GroupDAO
from ideaclustermanager.app.accounts.db.user_dao import UserDAO
from ideaclustermanager.app.accounts.db.sequence_config_dao import SequenceConfigDAO
from ideaclustermanager.app.accounts.db.group_members_dao import GroupMembersDAO
from ideaclustermanager.app.accounts.db.single_sign_on_state_dao import (
    SingleSignOnStateDAO,
)

from ideaclustermanager.app.tasks.task_manager import TaskManager

from typing import Dict, List, Optional
from boto3.dynamodb.conditions import Attr
import botocore.exceptions
import ldap
import os
import re
import stat
import threading
import time


def nonce() -> str:
    return Utils.short_uuid()


class AccountsService:
    """
    Account Management Service

    Integrates with OpenLDAP/AD, Cognito User Pools and exposes functionality around:
    1. User Management
    2. Groups
    3. User Onboarding
    4. Single Sign-On

    The service is primarily invoked via AuthAPI and AccountsAPI
    """

    def __init__(
        self,
        context: SocaContext,
        ldap_client: Optional[AbstractLDAPClient],
        user_pool: Optional[CognitoUserPool],
        evdi_client: Optional[EvdiClient],
        task_manager: Optional[TaskManager],
        token_service: Optional[TokenService],
    ):
        self.context = context
        self.logger = context.logger('accounts-service')
        self.user_pool = user_pool
        self.ldap_client = ldap_client
        self.evdi_client = evdi_client
        self.task_manager = task_manager
        self.token_service = token_service

        self.group_name_helper = GroupNameHelper(context)
        self.user_dao = UserDAO(context, user_pool=user_pool)
        self.group_dao = GroupDAO(context)
        self.sequence_config_dao = SequenceConfigDAO(context)
        self.group_members_dao = GroupMembersDAO(context, self.user_dao)
        self.sso_state_dao = SingleSignOnStateDAO(context)

        self.user_dao.initialize()
        self.group_dao.initialize()
        self.sequence_config_dao.initialize()
        self.group_members_dao.initialize()
        self.sso_state_dao.initialize()

        self.ds_automation_dir = self.context.config().get_string(
            'directoryservice.automation_dir', required=True
        )
        self._next_password_secret_sweep = 0.0
        self._password_secret_sweep = threading.Lock()
        self._password_sets = threading.BoundedSemaphore(self.PASSWORD_SETS_AT_ONCE)
        self._password_refusals = {}
        self._password_refusals_lock = threading.Lock()
        self._password_policy = None
        self._password_sets_paused_until = 0.0
        self._forgot_password_waits = {}
        self._forgot_password_lock = threading.Lock()

    def is_cluster_administrator(self, username: str) -> bool:
        cluster_administrator = self.context.config().get_string(
            'cluster.administrator_username', required=True
        )
        if Utils.is_empty(username):
            return False
        # the record lookup lowercases and strips the name, so the guard does too
        return AuthUtils.sanitize_username(username) == AuthUtils.sanitize_username(
            cluster_administrator
        )

    def is_sso_enabled(self) -> bool:
        return self.context.config().get_bool(
            'identity-provider.cognito.sso_enabled', False
        )

    # user group management methods

    def get_group(self, group_name: str) -> Optional[Group]:
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'Group not found: {group_name}',
            )

        return self.group_dao.convert_from_db(group)

    def create_group(self, group: Group, directory_gid: bool = False) -> Group:
        """
        create a new group
        :param group: Group
        :param directory_gid: the gid was read from a read-only directory's own group at
            start-up, not named in a request: the directory decides it
        """

        ds_readonly = self.ldap_client.is_readonly()

        group_name = group.name
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group.name is required')
        if Utils.is_empty(group.group_type):
            raise exceptions.invalid_params('group.group_type is required')
        if group.group_type not in constants.ALL_GROUP_TYPES:
            raise exceptions.invalid_params(
                f'invalid group type: {group.group_type}. must be one of: {constants.ALL_GROUP_TYPES}'
            )

        ds_name = group.ds_name
        if Utils.is_empty(ds_name) and ds_readonly:
            raise exceptions.invalid_params(
                f'group.ds_name is required when using Read-Only Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}'
            )

        db_existing_group = self.group_dao.get_group(group_name)
        if db_existing_group is not None:
            raise exceptions.invalid_params(f'group: {group_name} already exists')

        gid = group.gid
        if ds_readonly and not directory_gid:
            # the directory's group decides the gid: a named one must be the same
            directory_gid_value = self._get_gid_from_existing_ldap_group(ds_name)
            if gid is not None and gid != directory_gid_value:
                raise exceptions.invalid_params(
                    f'gid {gid} differs from the gid of directory group {ds_name}'
                )
            gid = group.gid = directory_gid_value
        elif not directory_gid:
            self._check_explicit_id('gid', gid, group_name)
        if gid is None:
            gid = self._next_free_id('gid', group_name)
            group.gid = gid

        group.enabled = True

        db_group = self.group_dao.convert_to_db(group)

        self.group_dao.create_group(db_group)

        self.task_manager.send(
            task_name='accounts.sync-group',
            payload={'group_name': group_name},
            message_group_id=group_name,
            message_dedupe_id=f'{group_name}.create-group.{nonce()}',
        )

        return self.group_dao.convert_from_db(db_group)

    def modify_group(self, group: Group) -> Group:
        """
        modify an existing group
        :param group: Group
        """

        group_name = group.name
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        db_group = self.group_dao.get_group(group_name)
        if db_group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group not found for group name: {group_name}',
            )

        if not db_group['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                message='cannot modify a disabled group',
            )

        # do not support modification of group name or GID
        # only title updates are supported
        update_group = {
            'group_name': db_group['group_name'],
            # the title asked for; a group without one keeps none
            'title': group.title if group.title is not None else db_group.get('title'),
        }

        # only update db, sync with DS not required.
        updated_group = self.group_dao.update_group(update_group)

        return self.group_dao.convert_from_db(updated_group)

    def delete_group(
        self, group_name: str, force: bool = False, *, keep_directory: bool = False
    ):
        """
        delete a group
        :param group_name:
        :param force: force delete a group even if group has existing members
        :param keep_directory: leave the directory's group alone
        :return:
        """

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.',
            )

        if self.group_members_dao.has_users_in_group(group_name):
            if force:
                usernames = self.group_members_dao.get_usernames_in_group(
                    group_name=group_name
                )
                self.remove_users_from_group(
                    usernames=usernames, group_name=group_name, force=force
                )
            else:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_INVALID_OPERATION,
                    message='group must be empty before it can be deleted.',
                )

        self.group_dao.delete_group(group_name=group_name)

        if keep_directory:
            return
        self.task_manager.send(
            task_name='accounts.sync-group',
            payload={'group_name': group_name},
            message_group_id=group_name,
            message_dedupe_id=f'{group_name}.update-group.{nonce()}',
        )

    def enable_group(self, group_name: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.',
            )

        self.group_dao.update_group({'group_name': group_name, 'enabled': True})
        # its members get its projects back
        self.context.projects.user_projects_dao.group_enabled_changed(group_name)

        self.task_manager.send(
            task_name='accounts.sync-group',
            payload={'group_name': group_name},
            message_group_id=group_name,
            message_dedupe_id=f'{group_name}.update-group.{nonce()}',
        )

    def disable_group(self, group_name: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.',
            )

        self.group_dao.update_group({'group_name': group_name, 'enabled': False})
        # while disabled it grants none of its projects; running work is not stopped
        self.context.projects.user_projects_dao.group_enabled_changed(group_name)

        self.task_manager.send(
            task_name='accounts.sync-group',
            payload={'group_name': group_name},
            message_group_id=group_name,
            message_dedupe_id=f'{group_name}.update-group.{nonce()}',
        )

    def list_groups(self, request: ListGroupsRequest) -> ListGroupsResult:
        return self.group_dao.list_groups(request)

    def list_users_in_group(
        self, request: ListUsersInGroupRequest
    ) -> ListUsersInGroupResult:
        return self.group_members_dao.list_users_in_group(request)

    def add_users_to_group(
        self, usernames: List[str], group_name: str, notify: bool = True
    ):
        """
        notify=False skips the membership task so a caller that may still roll the
        membership back can queue it once the account is complete.
        """
        if Utils.is_empty(usernames):
            raise exceptions.invalid_params('usernames is required')

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)
        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.',
            )
        if not group['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                message='cannot add users to a disabled user group',
            )

        users = []
        for username in usernames:
            user = self.user_dao.get_user(username)
            if user is None:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_USER_NOT_FOUND,
                    message=f'user not found: {username}',
                )
            if not user['enabled']:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_USER_IS_DISABLED,
                    message=f'user is disabled: {username}',
                )
            users.append(user)

        for user in users:
            username = user['username']
            additional_groups = Utils.get_value_as_list('additional_groups', user, [])

            if group_name not in additional_groups:
                additional_groups.append(group_name)

            self.user_dao.update_user(
                {'username': username, 'additional_groups': additional_groups}
            )
            self.group_members_dao.create_membership(group_name, username)

            if group['group_type'] not in (
                constants.GROUP_TYPE_USER,
                constants.GROUP_TYPE_PROJECT,
            ):
                self.user_pool.admin_add_user_to_group(
                    username=username, group_name=group_name
                )

            if notify:
                self.send_group_membership_updated(group_name, username, 'add')

    def send_group_membership_updated(
        self, group_name: str, username: str, operation: str
    ):
        self.task_manager.send(
            task_name='accounts.group-membership-updated',
            payload={
                'group_name': group_name,
                'username': username,
                'operation': operation,
            },
            message_group_id=username,
            # the queue drops a repeat of the same content for five minutes
            message_dedupe_id=f'{username}.{group_name}.{operation}.{nonce()}',
        )

    def remove_user_from_groups(self, username: str, group_names: List[str]):
        """
        remove a user from multiple groups.
        useful for operations such as delete user.
        :param username:
        :param group_names:
        :return:
        """
        if Utils.is_empty(username):
            raise exceptions.invalid_params('usernames is required')
        if Utils.is_empty(group_names):
            raise exceptions.invalid_params('group_names is required')

        self.logger.info(f'removing user: {username} from groups: {group_names}')

        # dedupe and sanitize
        groups_to_remove = []
        for group_name in group_names:
            if group_name in groups_to_remove:
                continue
            groups_to_remove.append(group_name)

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'user not found: {username}',
            )
        if not user['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_IS_DISABLED,
                message=f'user is disabled: {username}',
            )

        additional_groups = Utils.get_value_as_list('additional_groups', user, [])

        for group_name in groups_to_remove:
            group = self.group_dao.get_group(group_name)
            if group is None:
                self.logger.warning(
                    f'user: {username} cannot be removed from group: {group_name}. group not found'
                )
                continue
            if not group['enabled']:
                self.logger.warning(
                    f'user: {username} cannot be removed from group: {group_name}. group is disabled'
                )
                continue

            # queued first: a send that fails leaves the membership as it was
            self.send_group_membership_updated(group_name, username, 'remove')

            if group_name in additional_groups:
                additional_groups.remove(group_name)

            self.group_members_dao.delete_membership(group_name, username)

            if group['group_type'] not in (
                constants.GROUP_TYPE_USER,
                constants.GROUP_TYPE_PROJECT,
            ):
                self.user_pool.admin_remove_user_from_group(
                    username=username, group_name=group_name
                )

        self.user_dao.update_user(
            {'username': username, 'additional_groups': additional_groups}
        )

    def remove_users_from_group(
        self, usernames: List[str], group_name: str, force: bool = False
    ):
        """
        remove multiple users from a group
        useful for bulk operations from front end
        :param usernames:
        :param group_name:
        :param force: force delete even if group is disabled. if user is not found, skip user.
        :return:
        """
        if Utils.is_empty(usernames):
            raise exceptions.invalid_params('usernames is required')

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)
        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.',
            )
        if not group['enabled'] and not force:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                message='cannot remove users from a disabled user group',
            )

        users = []
        for username in usernames:
            user = self.user_dao.get_user(username)
            if user is None:
                if force:
                    continue
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_USER_NOT_FOUND,
                    message=f'user not found: {username}',
                )
            if not user['enabled'] and not force:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_USER_IS_DISABLED,
                    message=f'user is disabled: {username}',
                )
            users.append(user)

        for user in users:
            username = user['username']
            # queued first: a send that fails leaves the membership as it was
            self.send_group_membership_updated(group_name, username, 'remove')
            additional_groups = Utils.get_value_as_list('additional_groups', user, [])
            if group_name in additional_groups:
                additional_groups.remove(group_name)

            self.user_dao.update_user(
                {'username': username, 'additional_groups': additional_groups}
            )
            self.group_members_dao.delete_membership(group_name, username)

            if group['group_type'] not in (
                constants.GROUP_TYPE_USER,
                constants.GROUP_TYPE_PROJECT,
            ):
                self.user_pool.admin_remove_user_from_group(
                    username=username, group_name=group_name
                )

    # sudo user management methods

    def add_sudo_user(self, username: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )

        # a repeat still puts the account in the user pool's administrators group:
        # sign-in takes administrator rights from there, not from the record
        if not Utils.get_value_as_bool('sudo', user, False):
            self.user_dao.update_user({'username': username, 'sudo': True})

        self.user_pool.admin_add_sudo_user(username)

        self.task_manager.send(
            task_name='accounts.sync-user',
            payload={'username': username},
            message_group_id=username,
            message_dedupe_id=f'{username}.add-sudo-user.{nonce()}',
        )

    def remove_sudo_user(self, username: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation(
                f'Admin rights cannot be revoked from IDEA Cluster Administrator: {username}.'
            )

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )

        if Utils.get_value_as_bool('sudo', user, False):
            self.user_dao.update_user({'username': username, 'sudo': False})

        self.user_pool.admin_remove_sudo_user(username)

        self.task_manager.send(
            task_name='accounts.sync-user',
            payload={'username': username},
            message_group_id=username,
            message_dedupe_id=f'{username}.remove-sudo-user.{nonce()}',
        )

    # user management methods

    def get_user(self, username: str) -> User:
        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required.')

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )

        return self.user_dao.convert_from_db(user)

    # Below this an id belongs to the operating system (UID_MIN in login.defs); 65534 is
    # the overflow id 'nobody'.
    SYSTEM_ID_LIMIT = 1000

    def _check_explicit_id(self, kind: str, value, group_name: Optional[str] = None):
        """
        a uid or gid named in a request, not taken from the sequence: never a system id,
        never another account's uid, and in a directory IDEA writes, never another
        group's gid. Ids below the sequence start stay open: the sequence never skips
        an id in use, so that band is where a named id cannot meet a later one.
        :param group_name: for a gid, the group that may already hold it (its own group)
        """
        if value is None:
            return
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < self.SYSTEM_ID_LIMIT
            or value == 65534
        ):
            raise exceptions.invalid_params(
                f'{kind} {value} is reserved for the system'
            )
        readonly = self.ldap_client.is_readonly()
        if kind == 'uid':
            table, condition = self.user_dao.table, Attr('uid').eq(value)
        elif readonly:
            # the directory's own groups: several IDEA groups may name one of them
            return
        else:
            table, condition = self.group_dao.table, Attr('gid').eq(value)
            if group_name is not None:
                condition = condition & Attr('group_name').ne(group_name)
        scan = {'FilterExpression': condition}
        while True:
            page = table.scan(**scan)
            if page.get('Items'):
                raise exceptions.invalid_params(f'{kind} {value} is already in use')
            if 'LastEvaluatedKey' not in page:
                return
            scan['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def _next_free_id(self, kind: str, group_name: Optional[str] = None) -> int:
        """
        the next sequence id that no account or group holds: the sequence only counts
        up, and an administrator may have named an id ahead of it
        """
        take = (
            self.sequence_config_dao.next_uid
            if kind == 'uid'
            else self.sequence_config_dao.next_gid
        )
        for _ in range(1000):
            value = take()
            try:
                self._check_explicit_id(kind, value, group_name)
                return value
            except exceptions.SocaException as error:
                if error.error_code != errorcodes.INVALID_PARAMS:
                    raise
        raise exceptions.general_exception(f'no free {kind} found in the sequence')

    def _check_directory_user_ids(self, username: str, uid, gid) -> Optional[Dict]:
        directory_user = self.ldap_client.get_user(username)
        if directory_user is None:
            if uid is None and gid is None:
                return None
            raise exceptions.invalid_params(
                f'{username} is not in the directory, so its ids cannot be named'
            )
        for kind, value in (('uid', uid), ('gid', gid)):
            if value is not None and value != Utils.get_value_as_int(
                kind, directory_user
            ):
                raise exceptions.invalid_params(
                    f'{kind} {value} differs from the directory account of {username}'
                )
        return directory_user

    def update_my_preferences(self, username: str, landing_page: str = None) -> User:
        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.unauthorized_access()
        allowed = {
            None,
            '',
            'home',
            'my-jobs',
            'my-desktops',
            'files',
            'my-costs',
            'reports',
        }
        if landing_page not in allowed:
            raise exceptions.invalid_params('landing_page is not a portal destination')
        if self.user_dao.get_user(username) is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )
        updated = self.user_dao.update_user(
            {'username': username, 'landing_page': landing_page or ''}
        )
        return self.user_dao.convert_from_db(updated)

    def create_user(self, user: User, email_verified: bool = False) -> User:
        """
        create a new user
        """

        # username
        username = user.username
        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('user.username is required')
        if not re.match(auth_constants.USERNAME_REGEX, username):
            raise exceptions.invalid_params(
                f'user.username must match regex: {auth_constants.USERNAME_REGEX}'
            )
        AuthUtils.check_allowed_username(username)

        existing_user = self.user_dao.get_user(username)
        if existing_user is not None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_ALREADY_EXISTS,
                message=f'username: {username} already exists.',
            )

        # email
        email = user.email
        email = AuthUtils.sanitize_email(email)

        # password
        password = user.password
        if email_verified:
            if Utils.is_empty(password):
                raise exceptions.invalid_params('Password is required')

            user_pool_password_policy = self.user_pool.describe_password_policy()
            # Validate password compliance versus Cognito user pool password policy
            # Cognito: https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-policies.html
            if len(password) < user_pool_password_policy.minimum_length:
                raise exceptions.invalid_params(
                    f'Password should be greater than {user_pool_password_policy.minimum_length} characters'
                )
            elif len(password) > 256:
                raise exceptions.invalid_params('Password can be up to 256 characters')
            elif (
                user_pool_password_policy.require_numbers
                and re.search('[0-9]', password) is None
            ):
                raise exceptions.invalid_params(
                    'Password should include at least 1 number'
                )
            elif (
                user_pool_password_policy.require_uppercase
                and re.search('[A-Z]', password) is None
            ):
                raise exceptions.invalid_params(
                    'Password should include at least 1 uppercase letter'
                )
            elif (
                user_pool_password_policy.require_lowercase
                and re.search('[a-z]', password) is None
            ):
                raise exceptions.invalid_params(
                    'Password should include at least 1 lowercase letter'
                )
            elif (
                user_pool_password_policy.require_symbols
                and re.search(self.PASSWORD_SYMBOLS, password) is None
            ):
                raise exceptions.invalid_params(self.PASSWORD_SYMBOLS_MESSAGE)
        else:
            self.logger.debug('create_user() - setting password to random value')
            password = Utils.generate_password(8, 2, 2, 2, 2)
            # password = None

        # login_shell
        login_shell = user.login_shell
        if Utils.is_empty(login_shell):
            login_shell = auth_constants.DEFAULT_LOGIN_SHELL

        # home_dir
        home_dir = os.path.join(auth_constants.USER_HOME_DIR_BASE, username)

        # gid, group name
        if self.ldap_client.is_readonly():
            # group_name = self.group_name_helper.get_default_project_group()
            group_name = self.group_name_helper.get_user_group(username)
        else:
            group_name = self.group_name_helper.get_user_group(username)

        uid, gid = user.uid, user.gid
        if self.ldap_client.is_readonly():
            # the directory's own account decides its ids: a named one must match it,
            # and an omitted one is the directory's, which is what hosts resolve
            directory_user = self._check_directory_user_ids(username, uid, gid)
            if directory_user is not None:
                if uid is None:
                    uid = Utils.get_value_as_int('uid', directory_user)
                if gid is None:
                    gid = Utils.get_value_as_int('gid', directory_user)
        # an id given here (only an administrator can) is checked before anything is made
        self._check_explicit_id('uid', uid)
        self._check_explicit_id('gid', gid, group_name)

        # an existing personal group is adopted only when it is this user's own, and
        # then its gid is the user's
        existing_group = self.group_dao.get_group(group_name=group_name)
        if existing_group is not None:
            if (
                Utils.get_value_as_string('group_type', existing_group)
                != constants.GROUP_TYPE_USER
                or Utils.get_value_as_string('ref', existing_group) != username
            ):
                raise exceptions.invalid_params(
                    f'group: {group_name} already exists and is not the personal '
                    f'group of {username}'
                )
            existing_gid = Utils.get_value_as_int('gid', existing_group)
            if gid is not None and gid != existing_gid:
                raise exceptions.invalid_params(
                    f'gid {gid} differs from the gid of group {group_name}'
                )

        # uid
        if uid is None:
            uid = self._next_free_id('uid')

        if gid is None:
            gid = (
                Utils.get_value_as_int('gid', existing_group)
                if existing_group is not None
                else self._next_free_id('gid', group_name)
            )

        # sudo
        sudo = Utils.get_as_bool(user.sudo, False)

        self.logger.info(
            f'creating Cognito user pool entry: {username} , uid: {uid}, gid: {gid}, Group Name: {group_name}, Email: {email} , email_verified: {email_verified}'
        )

        # every step below persists state; track what this call creates so a failure partway
        # through can be unwound instead of leaving an account that exists but can't sign in.
        created_pool_user = False
        created_group = False
        created_db_user = False
        joined_groups: List[str] = []

        try:
            # the pool entry exists before the password is set, so a failure inside
            # this call can leave it behind. admin_delete_user tolerates a missing user.
            # A name the pool already holds is another create's, never this one's.
            try:
                self.user_pool.admin_create_user(
                    username=username,
                    email=email,
                    password=password,
                    email_verified=email_verified,
                )
            except botocore.exceptions.ClientError as error:
                if self._error_code(error) == 'UsernameExistsException':
                    # no account record holds the name (checked above): the entry is
                    # another create's in progress, or one an earlier create left
                    raise exceptions.soca_exception(
                        error_code=errorcodes.AUTH_USER_ALREADY_EXISTS,
                        message=f'username: {username} is held in the user pool with no '
                        'account in IDEA. If no other create of it is running, an '
                        'administrator removes that user pool entry and creates the '
                        'account again.',
                    ) from None
                created_pool_user = True
                raise
            except Exception:
                created_pool_user = True
                raise
            created_pool_user = True

            if self.is_sso_enabled():
                self.logger.debug(f'Performing IDP Link for {username} / {email}')
                self.user_pool.admin_link_idp_for_user(username, email)

            if sudo:
                self.logger.debug(f'Performing SUDO for {username}')
                self.user_pool.admin_add_sudo_user(username)

            # additional groups
            additional_groups = Utils.get_as_list(user.additional_groups, default=[])
            self.logger.debug(f'Additional groups for {username}: {additional_groups}')
            if group_name in additional_groups:
                additional_groups.remove(group_name)

            if existing_group is None:
                self.logger.debug(
                    f'Creating new group for {username}: GroupName: {group_name}'
                )
                self.group_dao.create_group(
                    {
                        'title': f"{username}'s Personal User Group",
                        'group_name': group_name,
                        'gid': gid,
                        'group_type': constants.GROUP_TYPE_USER,
                        'ref': username,
                        'enabled': True,
                    }
                )
                created_group = True
            else:
                self.logger.debug(
                    f'No need to create group for username: {username}:   Group ({group_name}) already exists.'
                )

            # A release before 26.11.2 left a deleted user's group and project rows
            # behind when the user was disabled first. A new user must not inherit them.
            user_projects = self.context.projects.user_projects_dao
            for project_id in user_projects.get_projects_by_username(username):
                user_projects.delete_user_project(
                    project_id=project_id, username=username
                )
            self.group_members_dao.delete_memberships_of(username)

            created_user = self.user_dao.create_user(
                {
                    'username': username,
                    'email': email,
                    'uid': uid,
                    'gid': gid,
                    'group_name': group_name,
                    'additional_groups': additional_groups,
                    'login_shell': login_shell,
                    'home_dir': home_dir,
                    'sudo': sudo,
                    'enabled': True,
                }
            )
            created_db_user = True
            self.logger.debug(f'Adding user {username} to specific group {group_name}')
            self.add_users_to_group([username], group_name, notify=False)
            joined_groups.append(group_name)

            default_project_group = self.group_name_helper.get_default_project_group()
            self.logger.debug(
                f'Adding user {username} to default group: {default_project_group}'
            )
            self.add_users_to_group([username], default_project_group, notify=False)
            joined_groups.append(default_project_group)

            for additional_group in additional_groups:
                self.logger.debug(
                    f'Adding username {username} to additional group: {additional_group}'
                )
                self.add_users_to_group([username], additional_group, notify=False)
                joined_groups.append(additional_group)

            account_sync_queued = False
            if Utils.is_not_empty(password):
                account_sync_queued = self.change_ldap_password(username, password)

            # queued only after the last step that can fail, so directory work never
            # races a rollback of the account it targets. Queued once: a second copy
            # can ask a domain controller the first one's account has not reached,
            # and add the account again.
            if not account_sync_queued:
                self.task_manager.send(
                    task_name='accounts.sync-user',
                    payload={'username': username},
                    message_group_id=username,
                    message_dedupe_id=f'{username}.create-user.{nonce()}',
                )
            for joined_group in joined_groups:
                self.send_group_membership_updated(joined_group, username, 'add')
            self.task_manager.send(
                task_name='accounts.create-home-directory',
                payload={'username': username},
                message_group_id=username,
                message_dedupe_id=f'{username}.create-home.{nonce()}',
            )

            return self.user_dao.convert_from_db(created_user)
        except Exception:
            self._rollback_create_user(
                username=username,
                group_name=group_name,
                joined_groups=joined_groups,
                created_pool_user=created_pool_user,
                created_group=created_group,
                created_db_user=created_db_user,
            )
            raise

    def _rollback_create_user(
        self,
        username: str,
        group_name: str,
        joined_groups: List[str],
        created_pool_user: bool,
        created_group: bool,
        created_db_user: bool,
    ):
        """
        undo the artifacts a failed create_user call already persisted.
        each step is best effort and its failure is logged, never raised: the caller
        must still see the error that caused the create to fail.
        """
        self.logger.warning(
            f'create user failed for: {username}. rolling back partial account.'
        )

        if len(joined_groups) > 0:
            try:
                self.remove_user_from_groups(username, list(reversed(joined_groups)))
            except Exception as e:
                self.logger.warning(
                    f'rollback: could not remove {username} from groups {joined_groups}: {e}'
                )
                for joined_group in reversed(joined_groups):
                    try:
                        self.group_members_dao.delete_membership(joined_group, username)
                    except Exception as e:
                        self.logger.warning(
                            f'rollback: could not remove {username} from group {joined_group}: {e}'
                        )

        if created_db_user:
            try:
                self.user_dao.delete_user(username=username)
            except Exception as e:
                self.logger.warning(f'rollback: could not delete user {username}: {e}')

        if created_group:
            try:
                self.group_dao.delete_group(group_name)
            except Exception as e:
                self.logger.warning(
                    f'rollback: could not delete group {group_name}: {e}'
                )

        if created_pool_user:
            try:
                self.user_pool.admin_delete_user(username=username)
            except Exception as e:
                self.logger.warning(
                    f'rollback: could not delete user pool entry {username}: {e}'
                )

    def modify_user(self, user: User, email_verified: bool = False) -> User:
        """
        Modify User

        Only ``email`` updates are supported at the moment.

        :param user:
        :param email_verified:
        :return:
        """

        username = user.username
        username = AuthUtils.sanitize_username(username)

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )
        if not existing_user['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_IS_DISABLED,
                message='User is disabled and cannot be modified.',
            )

        user_updates = {'username': username}

        if Utils.is_not_empty(user.email):
            new_email = AuthUtils.sanitize_email(user.email)
            existing_email = Utils.get_value_as_string('email', existing_user)
            if existing_email != new_email:
                user_updates['email'] = new_email
                self.user_pool.admin_update_email(
                    username, new_email, email_verified=email_verified
                )
                if self.is_sso_enabled():
                    self.user_pool.admin_link_idp_for_user(username, new_email)

        if Utils.is_not_empty(user.login_shell):
            user_updates['login_shell'] = user.login_shell

        # sudo goes through add/remove, which also change the user pool's group
        sudo = None if user.sudo is None else Utils.get_as_bool(user.sudo, False)

        # a changed id is checked as one named at create, against everyone else
        group_name = Utils.get_value_as_string('group_name', existing_user)
        if user.uid is not None and user.uid != Utils.get_value_as_int(
            'uid', existing_user
        ):
            self._check_explicit_id('uid', user.uid)
            user_updates['uid'] = user.uid

        gid_changed = user.gid is not None and user.gid != Utils.get_value_as_int(
            'gid', existing_user
        )
        if gid_changed:
            self._check_explicit_id('gid', user.gid, group_name)
            user_updates['gid'] = user.gid

        if user.instance_type_exceptions is not None:
            user_updates['instance_type_exceptions'] = sorted(
                set(user.instance_type_exceptions)
            )

        updated_user = self.user_dao.update_user(user_updates)

        if gid_changed and self.group_dao.get_group(group_name) is not None:
            # the personal group carries the gid the file worker and hosts use
            self.group_dao.update_group({'group_name': group_name, 'gid': user.gid})
            self.task_manager.send(
                task_name='accounts.sync-group',
                payload={'group_name': group_name},
                message_group_id=group_name,
                message_dedupe_id=f'{group_name}.modify-user-gid.{nonce()}',
            )

        self.task_manager.send(
            task_name='accounts.sync-user',
            payload={'username': username},
            message_group_id=username,
            message_dedupe_id=f'{username}.modify-user.{nonce()}',
        )

        if sudo is True:
            self.add_sudo_user(username)
        elif sudo is False and not (
            self.is_cluster_administrator(username)
            and not Utils.get_value_as_bool('sudo', existing_user, False)
        ):
            # every time, from both stores: a repeat after the user pool call failed
            # completes the demotion instead of reading the record and stopping
            self.remove_sudo_user(username)
        if sudo is not None:
            updated_user = self.user_dao.get_user(username)

        return self.user_dao.convert_from_db(updated_user)

    def enable_user(self, username: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )

        is_enabled = Utils.get_value_as_bool('enabled', existing_user, False)
        if is_enabled:
            return

        if self.group_dao.get_group(existing_user['group_name']) is None:
            # only a delete removes it, and one that stopped there has already
            # removed the account from the directory and the user pool
            raise AuthUtils.invalid_operation(
                f'User: {username} is part way through being deleted. Delete it again.'
            )

        if existing_user.get('disable_pending'):
            self.disable_user(
                username, reconcile_sources=existing_user.get('reconcile_sources', [])
            )
        self.user_pool.admin_enable_user(username)
        self.user_dao.update_user(
            {'username': username, 'enabled': True, 'reconcile_sources': []}
        )
        self._ssh_keys(existing_user, aside=False)
        self.group_dao.update_group(
            {'group_name': existing_user['group_name'], 'enabled': True}
        )

        self.task_manager.send(
            task_name='accounts.sync-user',
            payload={'username': username},
            message_group_id=username,
            message_dedupe_id=f'{username}.enable-user.{nonce()}',
        )
        from ideaclustermanager.app.accounts.account_tasks import (
            keeps_directory_account,
        )

        if (
            not self.ldap_client.is_readonly()
            and not keeps_directory_account(self.context, existing_user)
            and not self.is_sso_enabled()
        ):
            # disabling deleted the directory account, and its password with it: the
            # next sign-in sets a new one, which reaches the directory
            try:
                self.reset_password(username)
            except botocore.exceptions.ClientError as error:
                raise exceptions.general_exception(
                    f'User: {username} is enabled, but the password reset was not '
                    f'sent ({self._error_code(error)}). Reset the password so the '
                    'user can sign in.'
                ) from None

    def disable_user(
        self, username: str, *, preserve_directory=False, reconcile_sources=None
    ):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation(
                'Cluster Administrator cannot be disabled.'
            )

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}',
            )

        is_enabled = Utils.get_value_as_bool('enabled', existing_user, False)
        if not is_enabled:
            if reconcile_sources is None:
                # An explicit administrator disable takes precedence over automatic restoration.
                self.user_dao.update_user(
                    {'username': username, 'reconcile_sources': []}
                )
            if not existing_user.get('disable_pending'):
                self.revoke_user_api_tokens(username)
                self._ssh_keys(existing_user, aside=True)
                return
        else:
            # Persist the retry obligation with local revocation before any remote effect.
            self.user_dao.update_user(
                {
                    'username': username,
                    'enabled': False,
                    'preserve_directory': preserve_directory,
                    'reconcile_sources': reconcile_sources or [],
                    'disable_pending': True,
                }
            )
        # a session from before the disable mints nothing after a re-enable
        self._end_sessions(username)
        self.revoke_user_api_tokens(username)
        # the bastion takes the directory's word: an account the directory keeps
        # enabled would still sign in with its key
        self._ssh_keys(existing_user, aside=True)
        self.user_pool.admin_disable_user(username)
        self.group_dao.update_group(
            {'group_name': existing_user['group_name'], 'enabled': False}
        )

        self.task_manager.send(
            task_name='accounts.sync-user',
            payload={'username': username},
            message_group_id=username,
            message_dedupe_id=f'{username}.disable-user.{nonce()}',
        )
        self.evdi_client.publish_user_disabled_event(username=username)
        self.user_dao.update_user({'username': username, 'disable_pending': False})

    def delete_user(self, username: str):
        log_tag = f'(DeleteUser: {username})'

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation(
                'Cluster Administrator cannot be deleted.'
            )

        user = self.user_dao.get_user(username=username)
        if user is None:
            self.logger.info(f'{log_tag} user not found. skip.')
            return

        group_name = Utils.get_value_as_string('group_name', user)

        # The record keeps each of these until that membership is removed, so a delete
        # that stopped part way finds the rest when it is run again.
        groups = Utils.get_value_as_list('additional_groups', user, [])
        if group_name in groups:
            groups.remove(group_name)

        # disable user from db, user pool and delete from directory service
        self.logger.info(f'{log_tag} disabling user')
        self.disable_user(username=username)

        self.logger.info(f'{log_tag} removing memberships and directory account')
        keep_account = self._remove_deleted_user_now(username, groups)
        if not keep_account and not self.ldap_client.is_readonly():
            # the directory account is gone too: the home, keys included, is moved aside
            # root-only (ACC-18), so a later account of the same name starts with a new
            # one; a read-only directory keeps the account, and with it the home
            self._archive_home(user)

        # delete user in user pool
        self.logger.info(f'{log_tag} delete user from user pool')
        self.user_pool.admin_delete_user(username=username)

        # delete user's group from db
        self.logger.info(f'{log_tag} deleting group: {group_name}')
        try:
            # the task this queues cannot ask a deleted user whether its directory
            # account is kept, and with the account stays the group it belongs to
            self.delete_group(
                group_name=group_name, force=True, keep_directory=keep_account
            )
        except exceptions.SocaException as e:
            # a delete that stopped after this step finds the group gone when run again
            if e.error_code != errorcodes.AUTH_GROUP_NOT_FOUND:
                raise

        self.revoke_user_api_tokens(username)

        # delete user from db
        self.logger.info(f'{log_tag} delete user in ddb')
        self.user_dao.delete_user(username=username)

    def _ssh_keys(self, user: Dict, aside: bool, timeout: int = 300) -> Optional[str]:
        """
        sets the account's authorized SSH keys aside on disable, back on enable
        :return: what was done, 'failed', or None when its home did not answer in time
        """
        from ideaclustermanager.app.accounts.user_home_directory import (
            UserHomeDirectory,
        )

        if not Utils.get_value_as_string('home_dir', user):
            return 'no home'
        home = UserHomeDirectory(self.context, self.user_dao.convert_from_db(user))
        try:
            done = (
                home.set_aside_authorized_keys(timeout=timeout)
                if aside
                else home.restore_authorized_keys(timeout=timeout)
            )
        except Exception as error:
            # the account's state still changes; the log says the keys did not
            self.logger.error(
                f'ssh keys not {"set aside" if aside else "restored"} for '
                f'{user.get("username")}: {type(error).__name__}'
            )
            timed_out = (
                isinstance(error, exceptions.SocaException)
                and error.error_code == errorcodes.FILE_OPERATION_TIMED_OUT
            )
            return None if timed_out else 'failed'
        # a key file left in place on disable still opens the bastion: say so
        log = self.logger.warning if done == 'left as it is' else self.logger.info
        log(
            f'ssh keys {"set aside" if aside else "restored"} for '
            f'{user.get("username")}: {done}'
        )
        return done

    # the start-time pass: each account briefly, and the whole pass bounded, so a home
    # file system that does not answer cannot hold the service for long
    KEYS_PASS_ACCOUNT_SECONDS = 20
    KEYS_PASS_SECONDS = 600

    def set_aside_keys_of_disabled_users(self):
        """
        at every start, in the background: disabled accounts' keys set aside, for those
        disabled before 26.11.2 and any a failed disable left. Each account is read again
        just before its change and skipped unless still disabled; the pass stops at the
        first file system that does not answer, or when its time is up.
        """
        deadline = time.monotonic() + self.KEYS_PASS_SECONDS
        rows = Utils.scan_all(
            self.user_dao.table, {'FilterExpression': Attr('enabled').eq(False)}
        )
        for row in rows:
            if time.monotonic() > deadline:
                self.logger.warning(
                    "disabled accounts' keys: pass stopped, out of time"
                )
                return
            current = self.user_dao.get_user(row.get('username'))
            if current is None or Utils.get_value_as_bool('enabled', current, True):
                continue
            if (
                self._ssh_keys(
                    current, aside=True, timeout=self.KEYS_PASS_ACCOUNT_SECONDS
                )
                is None
            ):
                self.logger.warning(
                    "disabled accounts' keys: pass stopped at a home that did not answer"
                )
                return

    def _archive_home(self, user: Dict):
        from ideaclustermanager.app.accounts.user_home_directory import (
            UserHomeDirectory,
        )

        try:
            UserHomeDirectory(
                self.context, self.user_dao.convert_from_db(user)
            ).archive()
        except Exception as error:
            # the delete goes on; the home stays where it was, and the log says so
            self.logger.error(
                f'home not archived for deleted user {user.get("username")}: '
                f'{type(error).__name__}'
            )

    def _remove_deleted_user_now(self, username: str, group_names: List[str]) -> bool:
        # The queue drops a user's tasks once the account record is gone, so the
        # work they would do runs here, before the record goes. A failure stops the
        # delete with the user disabled, and deleting again resumes it.
        # imported here: the tasks module needs this package fully loaded
        from ideaclustermanager.app.accounts.account_tasks import (
            SyncUserInDirectoryServiceTask,
            keeps_directory_account,
        )

        user = self.user_dao.get_user(username)
        # the directory keeps its memberships wherever it keeps the account
        keep_account = keeps_directory_account(self.context, user)
        keep_directory = keep_account or self.ldap_client.is_readonly()
        remaining = Utils.get_value_as_list('additional_groups', user, [])
        # the record and the member list drop a group last, together, so a delete
        # that stops inside a group leaves both still showing it
        for group_name in group_names:
            if not keep_directory:
                try:
                    self.ldap_client.remove_user_from_group([username], group_name)
                except (
                    ldap.NO_SUCH_ATTRIBUTE,
                    ldap.NO_SUCH_OBJECT,
                    # Active Directory's answer for a member it does not have; there
                    # the memberships go with the user in any case
                    ldap.UNWILLING_TO_PERFORM,
                ):
                    pass
            group = self.group_dao.get_group(group_name)
            if group is None or group['group_type'] not in (
                constants.GROUP_TYPE_USER,
                constants.GROUP_TYPE_PROJECT,
            ):
                # sign-in takes administrator and manager rights from the user
                # pool's groups, not from the record
                self._remove_from_user_pool_group(username, group_name)
            self.context.projects.user_projects_dao.group_member_removed(
                group_name=group_name, username=username
            )
            self.group_members_dao.delete_membership(group_name, username)
            # the record lists what is still to remove, and no more: a delete that
            # stops here resumes from it, and enabling the user instead shows the truth
            remaining = [name for name in remaining if name != group_name]
            self.user_dao.update_user(
                {'username': username, 'additional_groups': remaining}
            )
        SyncUserInDirectoryServiceTask(self.context).invoke({'username': username})
        return keep_account

    def _remove_from_user_pool_group(self, username: str, group_name: str):
        try:
            self.user_pool.admin_remove_user_from_group(
                username=username, group_name=group_name
            )
        except botocore.exceptions.ClientError as e:
            # no such user or no such group: there is no membership left to remove
            if e.response['Error']['Code'] not in (
                'UserNotFoundException',
                'ResourceNotFoundException',
            ):
                raise

    def revoke_user_api_tokens(self, username):
        if self.token_service is not None:
            self.token_service.revoke_user_api_tokens(username)

    def reset_password(self, username: str):
        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        # the cutoff first: a token minted while the deletion runs is already refused
        self._end_sessions(username)
        self.revoke_user_api_tokens(username)

        # trigger reset password email
        self.user_pool.admin_reset_password(username)
        # the emailed code works at once, whatever earlier attempts were refused
        self._clear_password_refusals(username)

    def list_users(self, request: ListUsersRequest) -> ListUsersResult:
        return self.user_dao.list_users(request)

    def change_ldap_password(self, username: str, password: str) -> bool:
        """
        Change password for given username in ldap or ad. The password waits for its
        task in a Secrets Manager secret that the task deletes, never in a file.
        :param username:
        :param password:
        :return: True when the task that creates the account was queued too
        """
        self._remove_password_leftovers()
        return self._queue_password_sync(username, self._hold_password(password))

    def _remove_password_leftovers(self):
        # Only where an administrator or a signed-in user sets a password: the sign-in
        # page's two paths need no session, and would let anyone run these.
        # ponytail: the file sweep goes with the password_file branch of
        # accounts.sync-password, in the release after 26.11.2
        self.remove_stranded_password_files()
        self.remove_stray_password_secrets()

    # A password set makes a secret and, when the user pool refuses the password,
    # deletes it. The sign-in page's two paths need no session, so requests that only
    # ever fail can come as fast as cluster-manager serves them: this many at a time
    # keeps their Secrets Manager writes well under the account's quotas, which every
    # other workload in it shares.
    PASSWORD_SETS_AT_ONCE = 4
    PASSWORD_SET_WAIT_SECONDS = 10
    # The places above bound how many run at once, not how many come in an hour. A
    # username the user pool keeps refusing is turned away, with no secret made, once
    # it has this many refusals in this long from one client address. Counted in each
    # cluster-manager task, so the bound is this many per task.
    PASSWORD_REFUSALS_ALLOWED = 5
    # and once it has this many from all addresses together: a much higher bound, so
    # refusals sent from elsewhere rarely reach the user's own attempts
    PASSWORD_REFUSALS_PER_USERNAME = 50
    PASSWORD_REFUSALS_WINDOW_SECONDS = 900
    # A signed-in change is counted apart: the sign-in page's refusals never block it.
    SIGNED_IN = 'signed-in'
    # the user pool's answers to a code or session it did not give, or to too many
    # tries; on the sign-in page they get the same answer an unknown account gets
    PASSWORD_REFUSAL_CODES = frozenset(
        {
            'CodeMismatchException',
            'ExpiredCodeException',
            'LimitExceededException',
            'NotAuthorizedException',
            # botocore's own refusal of a session or code too short to send
            'ParamValidationError',
            'TooManyFailedAttemptsException',
            'UserNotFoundException',
        }
    )
    # the sign-in page's one answer for each path: true of every cause it covers
    CODE_REFUSED = (
        'The verification code or the new password was not accepted. Check the '
        'code or ask for a new one, and do not reuse a recent password. After '
        'several tries, wait 15 minutes before trying again.'
    )
    SESSION_REFUSED = (
        'The sign-in session or the new password was not accepted. Sign in again, '
        'and do not reuse a recent password. After several tries, wait 15 minutes '
        'before trying again.'
    )
    # the symbols the user pool counts, the backslash among them, as the message lists
    PASSWORD_SYMBOLS = r"""[\^\$\*\.\[\]{}\(\)\?"!@#%&\/\\,><':;\|_~`=\+\-]"""
    PASSWORD_SYMBOLS_MESSAGE = (
        'Password should include at least 1 of these special characters: '
        '^ $ * . [ ] { } ( ) ? " ! @ # % & / \\ , > < \' : ; | _ ~ ` = + -'
    )
    # the user pool's refusals of a password it alone can judge (a recent one); the
    # history code comes from a pool with a password history set
    PASSWORD_REUSE_CODES = frozenset(
        {'InvalidPasswordException', 'PasswordHistoryPolicyViolationException'}
    )
    # a signed-in change: the account is known, so each refusal is told as it is
    SIGNED_IN_ANSWERS = {
        'PasswordHistoryPolicyViolationException': (
            exceptions.invalid_params,
            'The new password was used recently. Choose another.',
        ),
        'NotAuthorizedException': (
            exceptions.general_exception,
            'The current password was not accepted. After several tries, wait 15 '
            'minutes before trying again.',
        ),
        'InvalidPasswordException': (
            exceptions.invalid_params,
            # the policy was checked first: what is left is what the pool alone knows
            'The new password does not meet the password policy, or was used before.',
        ),
        'LimitExceededException': (
            exceptions.general_exception,
            'Too many attempts. Try again in 15 minutes.',
        ),
        'TooManyFailedAttemptsException': (
            exceptions.general_exception,
            'Too many attempts. Try again in 15 minutes.',
        ),
        'TooManyRequestsException': (
            exceptions.general_exception,
            'The password could not be set right now. Try again.',
        ),
    }
    PASSWORD_POLICY_SECONDS = 300

    def _password_policy_rules(self):
        """
        :return: (the policy's message, its rules as (required, pattern) pairs), in the
            character classes the user pool counts: ASCII letters and digits, and the
            symbols the create-user check takes
        """
        now = time.monotonic()
        if self._password_policy is None or (
            now - self._password_policy[1] >= self.PASSWORD_POLICY_SECONDS
        ):
            self._password_policy = (self.user_pool.describe_password_policy(), now)
        policy = self._password_policy[0]
        needs = [
            (policy.require_uppercase, 'an uppercase letter', '[A-Z]'),
            (policy.require_lowercase, 'a lowercase letter', '[a-z]'),
            (policy.require_numbers, 'a number', '[0-9]'),
            (policy.require_symbols, 'a symbol', self.PASSWORD_SYMBOLS),
        ]
        parts = [name for required, name, _ in needs if required]
        message = (
            f'The new password needs at least {policy.minimum_length} characters'
            + (f', with {", ".join(parts)}.' if parts else '.')
        )
        return policy.minimum_length, message, needs

    def _check_password_policy(self, password: str):
        """
        Checked before the user pool is asked, so the user is told the rule. On the
        sign-in page's two paths it comes before the account is looked up, so a password
        it refuses gets the same answer for every username.
        """
        minimum_length, message, needs = self._password_policy_rules()
        if len(password) < minimum_length or any(
            required and re.search(pattern, password) is None
            for required, _, pattern in needs
        ):
            raise exceptions.invalid_params(message)

    # forgot-password calls in their padded wait at once, in each cluster-manager task,
    # and how many of them one client address may hold
    FORGOT_PASSWORD_WAITS_AT_ONCE = 4
    FORGOT_PASSWORD_WAITS_PER_SOURCE = 2

    def _password_refused(
        self, username: str, source: Optional[str] = None, count: bool = False
    ) -> bool:
        """
        :param source: the client address, SIGNED_IN, or None when it is not known
            (then only the per-username bound applies)
        :param count: record one more refusal
        :return: True once the username has used up its refusals for the window
        """
        # without a known address only the per-username bound applies: the low one
        # would let attempts from anywhere use up the user's own
        limits = (
            []
            if source is None
            else [((username, source), self.PASSWORD_REFUSALS_ALLOWED)]
        )
        if source != self.SIGNED_IN:
            limits.append(((username, '*'), self.PASSWORD_REFUSALS_PER_USERNAME))
        now = time.monotonic()
        window = self.PASSWORD_REFUSALS_WINDOW_SECONDS
        used_up = False
        with self._password_refusals_lock:
            if count and len(self._password_refusals) > 4096:
                for key, (_, since) in list(self._password_refusals.items()):
                    if now - since >= window:
                        del self._password_refusals[key]
            for key, allowed in limits:
                refusals, since = self._password_refusals.get(key, (0, now))
                if now - since >= window:
                    refusals, since = 0, now
                if count:
                    refusals += 1
                    self._password_refusals[key] = (refusals, since)
                used_up = used_up or refusals >= allowed
        return used_up

    def _clear_password_refusals(self, username: str):
        with self._password_refusals_lock:
            for key in [key for key in self._password_refusals if key[0] == username]:
                del self._password_refusals[key]

    def _set_password(
        self,
        username: str,
        password: str,
        change_in_user_pool,
        refused: Optional[str] = None,
        source: Optional[str] = SIGNED_IN,
    ):
        """
        The secret is made before the user pool takes the password: were Secrets
        Manager to fail after it, sign-in to the portal would have a password the
        directory never gets. Made first, a failure leaves nothing changed.
        :param refused: on the sign-in page, the one answer to an unknown account, to
            the user pool refusing the code or session, to used-up refusals and to a
            busy user pool, so the answer does not say which accounts exist
        :param source: who the refusals are counted against (see _password_refused)
        :return: what change_in_user_pool returned
        """
        if self._password_refused(username, source):
            raise exceptions.general_exception(
                refused or 'Too many attempts. Try again in 15 minutes.'
            )
        if time.monotonic() < self._password_sets_paused_until or not (
            # the sign-in page's paths hold no worker waiting for a place
            self._password_sets.acquire(blocking=False)
            if refused is not None
            else self._password_sets.acquire(timeout=self.PASSWORD_SET_WAIT_SECONDS)
        ):
            raise exceptions.general_exception(
                refused or 'The password could not be set right now. Try again.'
            )
        try:
            if refused is not None and self._password_refused(username, source):
                # refusals counted by attempts that held the places meanwhile
                raise exceptions.general_exception(refused)
            try:
                # asked before the user pool is: a directory fault stops the change
                # before anything is committed
                is_user_synced = self.ldap_client.is_existing_user(username)
                password_secret = self._hold_password(password)
            except Exception as error:
                if refused is None:
                    raise
                # on the sign-in page only an existing account gets this far
                self.logger.error(
                    f'password set not started: {self._error_code(error)}'
                )
                raise exceptions.general_exception(refused) from None
            try:
                result = change_in_user_pool()
            except Exception as error:
                self._drop_password_secret(password_secret)
                code = self._error_code(error)
                if code == 'TooManyRequestsException':
                    # The user pool is busy, which says nothing of this username, so it
                    # is no refusal. No one's attempt makes a secret for a while instead.
                    self._password_sets_paused_until = (
                        time.monotonic() + self.PASSWORD_SET_WAIT_SECONDS
                    )
                else:
                    self._password_refused(username, source, count=True)
                if refused is not None and (
                    code == 'TooManyRequestsException'
                    or code in self.PASSWORD_REFUSAL_CODES
                    # the policy was checked first: what the pool alone refuses (a
                    # recent password) only an existing account can reach, so it gets
                    # the one answer, which says so
                    or code in self.PASSWORD_REUSE_CODES
                ):
                    raise exceptions.general_exception(refused) from None
                if refused is None and code in self.SIGNED_IN_ANSWERS:
                    make, message = self.SIGNED_IN_ANSWERS[code]
                    raise make(message) from None
                raise
            if result is False:
                # the user pool has not taken the password
                self._drop_password_secret(password_secret)
                self._password_refused(username, source, count=True)
            else:
                self._clear_password_refusals(username)
                self._queue_password_sync(
                    username, password_secret, is_user_synced, committed=True
                )
            return result
        finally:
            self._password_sets.release()

    def _refuse_unknown_account(self, username: str, refused: str) -> str:
        """
        The sign-in page's two paths: an account with no record, or a disabled one,
        cannot be given a password, so its request makes nothing.
        :return: the account's username, as its record and its tasks know it
        """
        user = self.user_dao.get_user(username)
        if user is None and '@' in username:
            # the user pool signs in by email too, and no username has an '@'
            pool_user = self.user_pool.admin_get_user(username.strip(), use_cache=False)
            if pool_user is not None and pool_user.Username:
                user = self.user_dao.get_user(pool_user.Username)
        if not Utils.get_value_as_bool('enabled', user, False):
            raise exceptions.general_exception(refused)
        return user['username']

    def _hold_password(self, password: str) -> str:
        request = {
            'Name': f'{self.password_secret_prefix}{Utils.uuid()}',
            'SecretString': password,
            'Tags': [
                {'Key': key, 'Value': value}
                for key, value in self.password_secret_tags.items()
            ],
        }
        kms_key_id = self.context.config().get_string(
            'cluster.secretsmanager.kms_key_id'
        )
        if kms_key_id:
            request['KmsKeyId'] = kms_key_id
        try:
            return self.context.aws().secretsmanager().create_secret(**request)['ARN']
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as error:
            # the caller may be anyone at the sign-in page, and the AWS message names
            # the role and the account
            self.logger.error(
                f'a password secret could not be made: {self._error_code(error)}'
            )
            raise exceptions.general_exception(
                'The password could not be set right now. Try again.'
            ) from None

    def _queue_password_sync(
        self,
        username: str,
        password_secret: str,
        is_user_synced: Optional[bool] = None,
        committed: bool = False,
    ) -> bool:
        """
        :param committed: the user pool already holds the new password. A failed send
            then keeps the secret (the send may have been taken) and says the password
            reached sign-in only
        """
        try:
            if is_user_synced is None:
                is_user_synced = self.ldap_client.is_existing_user(username)
            if not is_user_synced:
                self.task_manager.send(
                    task_name='accounts.sync-user',
                    payload={'username': username},
                    message_group_id=username,
                    message_dedupe_id=f'{username}.change-password.{nonce()}',
                )

            self.task_manager.send(
                task_name='accounts.sync-password',
                payload={'username': username, 'password_secret': password_secret},
                message_group_id=username,
                # the directory has no account to give the password to until the
                # task queued above has run
                behind_entity_tasks=not is_user_synced,
            )
            return not is_user_synced
        except Exception as error:
            if committed:
                # the stray-secret sweep removes it if no task reads it
                self.logger.error(
                    f'password sync not queued for {username}: {self._error_code(error)}'
                )
                raise exceptions.general_exception(
                    'The password was changed for signing in to the portal, but could '
                    'not be passed on to the cluster hosts. Set it again to use it on '
                    'the hosts.'
                ) from None
            # only the queued task deletes the secret, and none was queued
            self._drop_password_secret(password_secret)
            raise

    def _drop_password_secret(self, password_secret: str):
        try:
            self.context.aws().secretsmanager().delete_secret(
                SecretId=password_secret, ForceDeleteWithoutRecovery=True
            )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as error:
            # the sweep removes it; the caller is told why the change failed
            self.logger.warning(
                'a password secret that was not queued could not be deleted: '
                f'{self._error_code(error)}'
            )

    @property
    def password_secret_prefix(self) -> str:
        # the task role may create, read and delete secrets of this name only, with
        # the tags below
        return f'{self.context.cluster_name()}-password-sync-'

    @property
    def password_secret_tags(self) -> dict:
        return {
            # No idea:ModuleName: the role may read and write the value of every
            # secret of this cluster that carries it, and must not overwrite these.
            constants.IDEA_TAG_CLUSTER_NAME: self.context.cluster_name(),
            'idea:Purpose': 'password-sync',
        }

    PASSWORD_SECRET_SWEEP_SECONDS = 3600
    # The tasks queue keeps a message 4 days (the stack sets its retention), and a
    # password can wait there that long behind a stopped cluster-manager or other
    # passwords. One moved into the password lane is sent again, which starts the 4
    # days over: two retention periods, plus a day. This must stay above that. A secret
    # swept while its message is still queued is a password that is never applied.
    STRAY_PASSWORD_SECRET_SECONDS = 9 * 24 * 3600

    def remove_stray_password_secrets(self, listed: Optional[List[dict]] = None):
        """
        a password secret outlives its task when the task is lost: a queue purged by
        hand, or a delete Secrets Manager did not take. Removes only secrets with this
        cluster's password name and tags, and never fails the caller.

        The secrets health collector calls this on the leader, every six hours and soon
        after a start, with the secrets it has just listed for its own count (`listed`),
        so a cluster where nobody sets a password still sweeps and no second list is
        made. A password an administrator or a signed-in user sets looks too, with a
        list of its own, at most once an hour
        and not within an hour of any other look: the only look where the health
        collectors do not run (metrics provider not DogStatsD, or switched off).
        """
        # one look at a time: a second would be told its secrets are already gone
        if not self._password_secret_sweep.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if listed is None and now < self._next_password_secret_sweep:
                return
            self._next_password_secret_sweep = now + self.PASSWORD_SECRET_SWEEP_SECONDS
            self._remove_stray_password_secrets(listed)
        finally:
            self._password_secret_sweep.release()

    def _remove_stray_password_secrets(self, listed: Optional[List[dict]]):
        cutoff = time.time() - self.STRAY_PASSWORD_SECRET_SECONDS
        wanted = self.password_secret_tags
        try:
            secrets = self.context.aws().secretsmanager()
            if listed is None:
                pages = secrets.get_paginator('list_secrets').paginate(
                    Filters=[
                        {'Key': 'name', 'Values': [self.password_secret_prefix]},
                        {'Key': 'tag-key', 'Values': ['idea:Purpose']},
                    ]
                )
                listed = [
                    secret for page in pages for secret in page.get('SecretList', [])
                ]
            for secret in listed:
                # the filters match by prefix, and on any tag
                tags = {tag['Key']: tag['Value'] for tag in secret.get('Tags', [])}
                if not secret['Name'].startswith(self.password_secret_prefix):
                    continue
                if any(tags.get(key) != value for key, value in wanted.items()):
                    continue
                if secret['CreatedDate'].timestamp() > cutoff:
                    continue
                try:
                    secrets.delete_secret(
                        SecretId=secret['ARN'], ForceDeleteWithoutRecovery=True
                    )
                    self.logger.info('removed a stray password secret')
                except (
                    botocore.exceptions.ClientError,
                    botocore.exceptions.BotoCoreError,
                ) as error:
                    # the others are still looked at
                    self.logger.warning(
                        'could not delete a stray password secret: '
                        f'{self._error_code(error)}'
                    )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as error:
            self.logger.warning(
                f'could not look for stray password secrets: {self._error_code(error)}'
            )

    @staticmethod
    def _error_code(error: Exception) -> str:
        # the message of an AWS error names the role and the secret
        if isinstance(error, botocore.exceptions.ClientError):
            return error.response['Error']['Code']
        return type(error).__name__

    # No queue keeps a message longer than this, so no task is left to read the file.
    STRANDED_PASSWORD_FILE_SECONDS = 14 * 24 * 3600

    def remove_stranded_password_files(self):
        """
        a password file outlives its task when the task is lost: a release that did
        not clean up after a task it gave up on, or a queue purged by hand. Removes
        only what change_ldap_password made up to 26.11.1, a folder holding password.txt and
        nothing else, and never fails the caller.
        """
        cutoff = time.time() - self.STRANDED_PASSWORD_FILE_SECONDS
        try:
            with os.scandir(self.ds_automation_dir) as entries:
                folders = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for folder in folders:
            password_file = os.path.join(folder, 'password.txt')
            try:
                if os.listdir(folder) != ['password.txt']:
                    continue
                file = os.lstat(password_file)
                if not stat.S_ISREG(file.st_mode):
                    continue
                if max(file.st_mtime, os.lstat(folder).st_mtime) > cutoff:
                    continue
                os.remove(password_file)
                # not rmtree: anything that appeared meanwhile stays
                os.rmdir(folder)
                self.logger.info(f'removed a stranded password file: {folder}')
            except OSError:
                # another task removed it first, or the file system is away
                continue

    def change_password(
        self, access_token: str, username: str, old_password: str, new_password: str
    ):
        """
        change password for given username in user pool and ldap
        this method expects an access token from an already logged-in user, who is trying to change their password.
        :return:
        """

        self._remove_password_leftovers()
        self._check_password_policy(new_password)
        # change password in user pool before changing in ldap
        self._set_password(
            username,
            new_password,
            lambda: self.user_pool.change_password(
                username=username,
                access_token=access_token,
                old_password=old_password,
                new_password=new_password,
            ),
        )

    # public API methods for user onboarding, login, forgot password flows.

    def initiate_auth(self, request: InitiateAuthRequest) -> InitiateAuthResult:
        auth_flow = request.auth_flow
        if Utils.is_empty(auth_flow):
            raise exceptions.invalid_params('auth_flow is required.')

        if auth_flow == 'USER_PASSWORD_AUTH':
            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            password = request.password
            if Utils.is_empty(password):
                raise exceptions.invalid_params('password is required.')

            return self.user_pool.initiate_username_password_auth(request)

        elif auth_flow == 'REFRESH_TOKEN_AUTH':
            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            refresh_token = request.refresh_token
            if Utils.is_empty(refresh_token):
                raise exceptions.invalid_params('refresh_token is required.')

            return self.user_pool.initiate_refresh_token_auth(username, refresh_token)

        elif auth_flow == 'SSO_AUTH':
            if not self.is_sso_enabled():
                raise exceptions.unauthorized_access()

            authorization_code = request.authorization_code
            if Utils.is_empty(authorization_code):
                raise exceptions.invalid_params('authorization_code is required.')

            # the portal page hands over '<state>.<browser key>' only to the browser
            # holding the key's cookie, so the state alone redeems nothing
            state, _, browser_key = authorization_code.partition('.')
            db_sso_state = self.sso_state_dao.redeem_sso_state(
                state, Utils.sha256(browser_key) if browser_key else None
            )
            if db_sso_state is None:
                raise exceptions.unauthorized_access()

            return InitiateAuthResult(
                auth=self.sso_state_dao.convert_from_db(db_sso_state)
            )

        elif auth_flow == 'SSO_REFRESH_TOKEN_AUTH':
            if not self.is_sso_enabled():
                raise exceptions.unauthorized_access()

            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            refresh_token = request.refresh_token
            if Utils.is_empty(refresh_token):
                raise exceptions.invalid_params('refresh_token is required.')

            return self.user_pool.initiate_refresh_token_auth(
                username, refresh_token, sso=True
            )

    def respond_to_auth_challenge(
        self, request: RespondToAuthChallengeRequest, source: Optional[str] = None
    ) -> RespondToAuthChallengeResult:
        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required.')

        challenge_name = request.challenge_name
        if Utils.is_empty(challenge_name):
            raise exceptions.invalid_params('challenge_name is required.')
        if challenge_name != 'NEW_PASSWORD_REQUIRED':
            raise exceptions.invalid_params(
                f'challenge_name: {challenge_name} is not supported.'
            )

        if Utils.is_empty(request.session):
            raise exceptions.invalid_params('session is required.')

        if Utils.is_empty(request.new_password):
            raise exceptions.invalid_params('new_password is required.')

        refused = self.SESSION_REFUSED
        self._check_password_policy(request.new_password)
        username = self._refuse_unknown_account(request.username, refused)
        request = request.model_copy(update={'username': username})

        result = None

        def respond():
            nonlocal result
            result = self.user_pool.respond_to_auth_challenge(request)
            # another challenge follows: no password is set yet
            return result.auth is not None

        self._set_password(username, request.new_password, respond, refused, source)
        return result

    def forgot_password(self, username: str, source: Optional[str] = None):
        """
        invoke user pool's forgot password API
        introduce mandatory timing delays to ensure valid / invalid user invocations are processed in approximately the same time
        :param source: the client address, when known: it may hold only a few places
        """
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        # The wait holds one of the API's shared workers and needs no account, so only
        # this many wait at once, and only a few from one address. A call past them is
        # told to try again, for every username alike, and asks the user pool nothing.
        with self._forgot_password_lock:
            waiting = self._forgot_password_waits
            if sum(waiting.values()) >= self.FORGOT_PASSWORD_WAITS_AT_ONCE or (
                source is not None
                and waiting.get(source, 0) >= self.FORGOT_PASSWORD_WAITS_PER_SOURCE
            ):
                raise exceptions.general_exception(
                    'Password reset is busy. Try again in a minute.'
                )
            waiting[source] = waiting.get(source, 0) + 1
        try:
            wait_time_seconds = 5
            start = Utils.current_time_ms()
            self.user_pool.forgot_password(username)
            end = Utils.current_time_ms()
            total_secs = (end - start) / 1000

            if total_secs <= wait_time_seconds:
                time.sleep(wait_time_seconds - total_secs)
        finally:
            with self._forgot_password_lock:
                self._forgot_password_waits[source] -= 1
                if not self._forgot_password_waits[source]:
                    del self._forgot_password_waits[source]

    def confirm_forgot_password(
        self,
        username: str,
        password: str,
        confirmation_code: str,
        source: Optional[str] = None,
    ):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        if Utils.is_empty(password):
            raise exceptions.invalid_params('password is required')
        if Utils.is_empty(confirmation_code):
            raise exceptions.invalid_params('confirmation_code is required')

        refused = self.CODE_REFUSED
        self._check_password_policy(password)
        username = self._refuse_unknown_account(username, refused)
        # the user pool verifies the confirmation code; then sync with LDAP/AD
        self._set_password(
            username,
            password,
            lambda: self.user_pool.confirm_forgot_password(
                username, password, confirmation_code
            ),
            refused,
            source,
        )

    def sign_out(self, refresh_token: str, sso_auth: bool):
        """
        revokes the refresh token issued by InitiateAuth API.
        """
        self.token_service.revoke_authorization(
            refresh_token=refresh_token, sso_auth=sso_auth
        )

    def global_sign_out(self, username: str):
        """
        Signs out a user from all devices.
        It also invalidates all refresh tokens that Amazon Cognito has issued to a user.
        The user's current access and ID tokens remain valid until they expire.
        """

        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        # the cutoff first: a token minted while the deletion runs is already refused
        self._end_sessions(username)
        self.revoke_user_api_tokens(username)
        self.user_pool.admin_global_sign_out(username=username)

    def session_signed_in_ms(self, username: str, decoded_token: Optional[Dict]) -> int:
        """
        the sign-in time of a session token, refused when the account's sessions ended
        after it or the account was created after it (a name used again): anything a
        session opens that outlives it is bound to this
        :return: the sign-in time in milliseconds
        """
        decoded = decoded_token or {}
        signed_in = decoded.get('auth_time', decoded.get('iat'))
        row = self.user_dao.get_user(AuthUtils.sanitize_username(username))
        if signed_in is None or row is None:
            raise exceptions.unauthorized_access()
        signed_in_ms = int(signed_in) * 1000
        since = max(
            int(row.get('created_on') or 0), int(row.get('sessions_revoked_on') or 0)
        )
        if signed_in_ms < since:
            raise exceptions.unauthorized_access()
        return signed_in_ms

    def _end_sessions(self, username: str):
        # a session token issued before this cannot mint an API token
        username = AuthUtils.sanitize_username(username)
        if self.user_dao.get_user(username) is not None:
            self.user_dao.update_user(
                {'username': username, 'sessions_revoked_on': Utils.current_time_ms()}
            )

    def _get_ds_group_name(self, groupname: str) -> str:
        ds_group_name = self.context.config().get_string(
            f'directoryservice.group_mapping.{groupname}', default=groupname
        )
        return ds_group_name

    def _get_gid_from_existing_ldap_group(self, groupname: str):
        existing_gid = None
        existing_group_from_ds = self.ldap_client.get_group(group_name=groupname)
        self.logger.debug(
            f'READ-ONLY DS lookup results for group  {groupname}: {existing_group_from_ds}'
        )

        if existing_group_from_ds is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Unable to Resolve a required IDEA group from directory services: IDEA group {groupname}',
            )

        existing_gid = Utils.get_value_as_int(
            'gid', existing_group_from_ds, default=None
        )

        if existing_gid is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Found group without POSIX gidNumber attribute - UNABLE to use this group. Update group with gidNumber attribute within Directory Service: {groupname}',
            )

        return existing_gid

    def provision_operations_leads_group(self, group_name):
        if self.group_dao.get_group(group_name) is None:
            ds_name = None
            gid = None
            if self.ldap_client.is_readonly():
                ds_name = self._get_ds_group_name(group_name)
                try:
                    gid = self._get_gid_from_existing_ldap_group(ds_name)
                except exceptions.SocaException:
                    reason = (
                        f'Operations leads group {group_name} is not configured: '
                        f'create directory group {ds_name} with a POSIX gidNumber, '
                        f'set directoryservice.group_mapping.{group_name} to that group, '
                        'then restart cluster-manager to retry provisioning. '
                        'Operations leads reporting access remains unavailable.'
                    )
                    self.operations_leads_configuration_status = dict(
                        status='configuration_required',
                        group_name=group_name,
                        directory_group=ds_name,
                        reason=reason,
                    )
                    self.logger.warning(reason)
                    return
            self.create_group(
                directory_gid=True,
                group=Group(
                    title='Operations leads (read-only reporting)',
                    name=group_name,
                    ds_name=ds_name,
                    gid=gid,
                    group_type=constants.GROUP_TYPE_CLUSTER,
                ),
            )
        self.operations_leads_configuration_status = dict(
            status='ready',
            group_name=group_name,
        )

    def create_defaults(self):
        operations_leads_group_name = (
            self.group_name_helper.get_cluster_operations_leads_group()
        )
        ds_provider = self.context.config().get_string(
            'directoryservice.provider', required=True
        )
        ds_readonly = self.ldap_client.is_readonly()

        self.logger.info(
            f'Creating defaults for Directory Service provider ({ds_provider}). Read-Only status: {ds_readonly}'
        )

        # create default project group
        # if ds_readonly:
        #    # todo - these may be DNs ? Clean them up to just names?
        #    default_project_group_name = self.context.config().get_string('directoryservice.group_mapping.default-project-group', required=True)
        # else:

        default_project_group_name = self.group_name_helper.get_default_project_group()
        default_project_group_name_ds = None
        if ds_readonly:
            default_project_group_name_ds = self._get_ds_group_name(
                groupname=default_project_group_name
            )

        self.logger.info(
            f'Default group name: ({default_project_group_name}) Directory Service group name: ({default_project_group_name_ds})'
        )

        default_group = self.group_dao.get_group(default_project_group_name)
        if default_group is None:
            self.logger.info(f'Default group not found: {default_project_group_name}')

            gid = None
            if ds_readonly:
                gid = self._get_gid_from_existing_ldap_group(
                    groupname=default_project_group_name_ds
                )

            self.logger.info(
                f'creating default group: ({default_project_group_name}) / Directory Service: ({default_project_group_name_ds})'
            )
            if gid is not None:
                self.logger.info(
                    f'Group will use Directory Service Discovered GID: {gid}'
                )
            else:
                self.logger.info('Group will use AUTO GID')

            self.create_group(
                directory_gid=True,
                group=Group(
                    title=f'IDEA Default Project Group (DS: {default_project_group_name_ds})',
                    name=default_project_group_name,
                    ds_name=default_project_group_name_ds,
                    gid=gid,
                    group_type=constants.GROUP_TYPE_PROJECT,
                    ref=constants.DEFAULT_PROJECT,
                ),
            )

        # cluster admin user
        admin_username = self.context.config().get_string(
            'cluster.administrator_username', required=True
        )
        admin_email = self.context.config().get_string(
            'cluster.administrator_email', required=True
        )
        admin_user = self.user_dao.get_user(username=admin_username)
        if admin_user is None:
            self.logger.info(f'creating cluster admin user: {admin_username}')
            self.create_user(
                user=User(username=admin_username, email=admin_email, sudo=True),
                email_verified=False,
            )

        # create managers group
        cluster_managers_group_name = (
            self.group_name_helper.get_cluster_managers_group()
        )
        cluster_managers_group_name_ds = None
        if ds_readonly:
            cluster_managers_group_name_ds = self._get_ds_group_name(
                groupname=cluster_managers_group_name
            )

        cluster_managers_group = self.group_dao.get_group(cluster_managers_group_name)

        if cluster_managers_group is None:
            self.logger.info(
                f'Cluster-managers group not found: {cluster_managers_group_name} / DS Translation: {cluster_managers_group_name_ds}'
            )

            gid = None
            if ds_readonly:
                gid = self._get_gid_from_existing_ldap_group(
                    groupname=cluster_managers_group_name_ds
                )

            self.logger.info(
                f'creating managers group: {cluster_managers_group_name} from DS group name {cluster_managers_group_name_ds}'
            )
            if gid is not None:
                self.logger.info(
                    f'Group will use Directory Service Discovered GID: {gid}'
                )
            else:
                self.logger.info('Group will use AUTO GID')

            self.create_group(
                directory_gid=True,
                group=Group(
                    title='Managers (cluster administrators without sudo access)',
                    name=cluster_managers_group_name,
                    ds_name=cluster_managers_group_name_ds,
                    gid=gid,
                    group_type=constants.GROUP_TYPE_CLUSTER,
                ),
            )

        self.provision_operations_leads_group(operations_leads_group_name)

        # for all "app" modules in the cluster, create the module users and module administrators group to enable fine-grained access
        # if an application module is added at a later point in time, a cluster-manager restart should fix the issue.
        # ideally, an 'ideactl initialize-defaults' command is warranted to address this scenario and will be taken up in a future release.
        modules = self.context.get_cluster_modules()
        for module in modules:
            if module['type'] != constants.MODULE_TYPE_APP:
                continue
            module_id = module['module_id']
            module_name = module['name']

            module_administrators_group_name = (
                self.group_name_helper.get_module_administrators_group(
                    module_id=module_id
                )
            )
            module_administrators_group_name_ds = None

            if ds_readonly:
                module_administrators_group_name_ds = self._get_ds_group_name(
                    groupname=module_administrators_group_name
                )

            self.logger.info(
                f'Looking up module group info for ModuleID: ({module_id}) Name: ({module_name}) / IDEA: {module_administrators_group_name}  DS translation: {module_administrators_group_name_ds}'
            )

            module_administrators_group = self.group_dao.get_group(
                module_administrators_group_name
            )

            if module_administrators_group is None:
                self.logger.info(
                    f'Module administrators group not found: {module_administrators_group_name}'
                )

                gid = None
                if ds_readonly:
                    gid = self._get_gid_from_existing_ldap_group(
                        groupname=module_administrators_group_name_ds
                    )

                if gid is not None:
                    self.logger.info(
                        f'Group will use Directory Service Discovered GID: {gid}'
                    )
                else:
                    self.logger.info('Group will use AUTO GID')

                self.logger.info(
                    f'creating module administrators group: {module_administrators_group_name}'
                )
                self.create_group(
                    directory_gid=True,
                    group=Group(
                        title=f'Administrators for Module: {module_name}, ModuleId: {module_id}, DS: {module_administrators_group_name_ds}',
                        name=module_administrators_group_name,
                        ds_name=module_administrators_group_name_ds,
                        gid=gid,
                        group_type=constants.GROUP_TYPE_MODULE,
                        ref=module_id,
                    ),
                )

            module_users_group_name = self.group_name_helper.get_module_users_group(
                module_id=module_id
            )
            module_users_group_name_ds = None

            if ds_readonly:
                module_users_group_name_ds = self._get_ds_group_name(
                    groupname=module_users_group_name
                )

            module_users_group = self.group_dao.get_group(module_users_group_name)
            if module_users_group is None:
                self.logger.info(
                    f'creating module administrators group: {module_users_group_name} / DS Translation: {module_users_group_name_ds}'
                )

                gid = None
                if ds_readonly:
                    gid = self._get_gid_from_existing_ldap_group(
                        groupname=module_users_group_name_ds
                    )

                if gid is not None:
                    self.logger.info(
                        f'Group will use Directory Service Discovered GID: {gid}'
                    )
                else:
                    self.logger.info('Group will use AUTO GID')

                self.create_group(
                    directory_gid=True,
                    group=Group(
                        title=f'Users for Module: {module_name}, ModuleId: {module_id}',
                        name=module_users_group_name,
                        ds_name=module_users_group_name_ds,
                        gid=gid,
                        group_type=constants.GROUP_TYPE_MODULE,
                        ref=module_id,
                    ),
                )
