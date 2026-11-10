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

__all__ = (
    'SyncUserInDirectoryServiceTask',
    'CreateUserHomeDirectoryTask',
    'RepairHomeOwnershipTask',
    'SyncGroupInDirectoryServiceTask',
    'SyncPasswordInDirectoryServiceTask',
    'GroupMembershipUpdatedTask',
)

from botocore.exceptions import ClientError

from ideasdk.utils import Utils
from ideadatamodel import errorcodes, exceptions

import ideaclustermanager
from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
    PasswordRefused,
)
from ideaclustermanager.app.tasks.base_task import (
    NOTHING_DONE,
    BaseTask,
    TaskCannotSucceed,
)
from ideaclustermanager.app.accounts.user_home_directory import UserHomeDirectory

from typing import Dict
import ldap
import os
import pathlib
import shutil
import time


class SyncGroupInDirectoryServiceTask(BaseTask):
    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger(self.get_name())

    def get_name(self) -> str:
        return 'accounts.sync-group'

    def entity_ref(self, payload: Dict):
        return 'group', payload['group_name']

    def invoke(self, payload: Dict):
        group_name = payload['group_name']
        group = self.context.accounts.group_dao.get_group(group_name)

        if self.context.ldap_client.is_readonly():
            self.logger.info(
                f'Read-only Directory service - NOOP for {self.get_name()}'
            )
            return

        delete_group = False
        if group is not None:
            if group['enabled']:
                self.logger.info(f'sync group: {group_name} directory service ...')
                self.context.ldap_client.sync_group(
                    group_name=group['group_name'], gid=group['gid']
                )
            else:
                delete_group = True
        else:
            delete_group = True

        if delete_group:
            self.logger.info(f'deleting group: {group_name} from directory service ...')
            if self.context.ldap_client.is_existing_group(group_name):
                self.context.ldap_client.delete_group(group_name)


class GroupMembershipUpdatedTask(BaseTask):
    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger(self.get_name())

    def get_name(self) -> str:
        return 'accounts.group-membership-updated'

    def entity_ref(self, payload: Dict):
        return 'user', payload['username']

    def invoke(self, payload: Dict):
        group_name = payload['group_name']
        username = payload['username']
        operation = payload['operation']
        group = self.context.accounts.group_dao.get_group(group_name)
        ds_readonly = self.context.ldap_client.is_readonly()

        if group is None:
            # no retry brings a deleted group back, and each one would hold up every
            # later task of this user
            if operation == 'add':
                raise TaskCannotSucceed(f'group not found: {group_name}')
            # sync-group removes the group, and its members with it, from the directory
            self.context.projects.user_projects_dao.group_member_removed(
                group_name=group_name, username=username
            )
            return
        if group['enabled']:
            if operation == 'add':
                if ds_readonly:
                    self.logger.info(
                        f'add member: {username} to group: {group_name} in READ-ONLY directory service ...'
                    )
                else:
                    self.logger.info(
                        f'add member: {username} to group: {group_name} in directory service ...'
                    )
                    self._add_member(username, group_name)

                # update membership in user projects (DynamoDB)
                self.logger.info(
                    f'add member: {username} to group: {group_name} - DAO ...'
                )
                self.context.projects.user_projects_dao.group_member_added(
                    group_name=group_name, username=username
                )

            else:
                if ds_readonly:
                    self.logger.info(
                        f'NOOP - remove member: {username} from group: {group_name} in readonly directory service ...'
                    )
                else:
                    self.logger.info(
                        f'remove member: {username} from group: {group_name} in directory service ...'
                    )
                    self.context.ldap_client.remove_user_from_group(
                        [username], group_name
                    )

                # update membership in user projects
                self.context.projects.user_projects_dao.group_member_removed(
                    group_name=group_name, username=username
                )

    def _add_member(self, username: str, group_name: str):
        directory = self.context.ldap_client
        user = self.context.accounts.user_dao.get_user(username) or {}
        if user.get(SyncUserInDirectoryServiceTask.IN_DIRECTORY) is not True:
            directory.add_user_to_group([username], group_name)
            return
        # sync-user added this account, perhaps seconds ago on another domain
        # controller. The one asked here is waited on until it shows the account, where
        # the add failed and was retried; one that never shows it fails as before.
        with directory.one_server():
            deadline = (
                time.monotonic() + SyncUserInDirectoryServiceTask.CATCH_UP_SECONDS
            )
            while (
                not directory.is_existing_user(username) and time.monotonic() < deadline
            ):
                time.sleep(SyncUserInDirectoryServiceTask.CATCH_UP_INTERVAL)
            directory.add_user_to_group([username], group_name)


def keeps_directory_account(context, user: Dict) -> bool:
    # Reconciliation observes authoritative AD; mirror work must not mutate it.
    return bool(user.get('preserve_directory')) and context.config().get_string(
        'directoryservice.provider'
    ) in ('activedirectory', 'aws_managed_activedirectory')


def _remove_if_present(remove, name: str):
    # Deleting a user runs this removal itself while the copy it queued can run too;
    # whichever comes second finds the entry gone, which is the result it wanted.
    try:
        remove(name)
    except ldap.NO_SUCH_OBJECT:
        pass


class SyncUserInDirectoryServiceTask(BaseTask):
    # user attribute. True: set before this task first adds the account and its group
    # to the directory, so an attempt that stops after its add leaves it set. False:
    # the task has removed them. Absent: the task has not handled the account yet.
    IN_DIRECTORY = 'directory_account'
    # Active Directory tells its other domain controllers of a change after 15 s
    CATCH_UP_SECONDS = 30
    CATCH_UP_INTERVAL = 2

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger(self.get_name())

    def get_name(self) -> str:
        return 'accounts.sync-user'

    def _await_directory(self, user: Dict, group_too: bool):
        # A read can be answered by a domain controller that this task's last change
        # has not reached yet. Adding an account it does not show yet leaves two, one
        # of which survives a disable; skipping its removal leaves one; and updating
        # one it still shows after a removal leaves none. So the directory is waited
        # on until it shows what was last done. An account removed or added by hand
        # never does, and after the wait the task goes by what it reads.
        recorded = user.get(self.IN_DIRECTORY)
        if recorded is None:
            return
        directory = self.context.ldap_client
        username, group_name = user['username'], user['group_name']

        def caught_up():
            if not recorded:
                return not directory.is_existing_user(
                    username
                ) and not directory.is_existing_group(group_name)
            # a removal that stopped part way has taken the group and left the account
            return directory.is_existing_user(username) and (
                not group_too or directory.is_existing_group(group_name)
            )

        deadline = time.monotonic() + self.CATCH_UP_SECONDS
        while not caught_up():
            if time.monotonic() >= deadline:
                self.logger.warning(
                    'the directory does not show an account that a task set out to '
                    'create in it'
                    if recorded
                    else 'the directory still shows an account that was removed from it'
                )
                return
            time.sleep(self.CATCH_UP_INTERVAL)
            # deleting a user runs the removal itself beside the copy it queued: the
            # one that comes second must not wait out an account the first removed
            current = self.context.accounts.user_dao.get_user(username)
            if not current or current.get(self.IN_DIRECTORY) is not recorded:
                return

    def _record_in_directory(self, user: Dict, present: bool):
        if user.get(self.IN_DIRECTORY) is present:
            return
        try:
            self.context.accounts.user_dao.table.update_item(
                Key={'username': user['username']},
                UpdateExpression='SET #flag = :present',
                # a record deleted meanwhile is not brought back
                ConditionExpression='attribute_exists(username)',
                ExpressionAttributeNames={'#flag': self.IN_DIRECTORY},
                ExpressionAttributeValues={':present': present},
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise

    def entity_ref(self, payload: Dict):
        return 'user', payload['username']

    def invoke(self, payload: Dict):
        username = payload['username']
        user = self.context.accounts.user_dao.get_user(username)
        readonly = self.context.ldap_client.is_readonly()
        if keeps_directory_account(self.context, user):
            return

        if readonly:
            self.logger.info(
                f'sync user: Read-only Directory service - sync {username} NOOP ... returning'
            )
            return

        # the wait, each read and the write decided on it are answered by one domain
        # controller: one that has the account cannot be followed by one that has not
        with self.context.ldap_client.one_server():
            self._sync(user)

    def _sync(self, user: Dict):
        username = user['username']
        group_name = user['group_name']
        enabled = user['enabled']
        sudo = Utils.get_value_as_bool('sudo', user, False)
        self._await_directory(user, group_too=enabled)
        if enabled:
            # before the add: the retry of an attempt that fails after it must wait too
            self._record_in_directory(user, True)
            self.logger.info(f'sync user: {username} TO directory service ...')
            self.context.ldap_client.sync_group(
                group_name=user['group_name'], gid=user['gid']
            )
            self.context.ldap_client.sync_user(
                uid=user['uid'],
                gid=user['gid'],
                username=user['username'],
                email=user['email'],
                login_shell=user['login_shell'],
                home_dir=user['home_dir'],
            )

            if sudo:
                if not self.context.ldap_client.is_sudo_user(username):
                    self.context.ldap_client.add_sudo_user(username)
            else:
                if self.context.ldap_client.is_sudo_user(username):
                    self.context.ldap_client.remove_sudo_user(username)

        else:
            self.logger.info(f'deleting user: {username} from directory service ...')
            directory = self.context.ldap_client
            if directory.is_existing_group(group_name):
                _remove_if_present(directory.delete_group, group_name)
            if directory.is_existing_user(username):
                _remove_if_present(directory.delete_user, username)
            if directory.is_sudo_user(username):
                _remove_if_present(directory.remove_sudo_user, username)
            self._record_in_directory(user, False)


class SyncPasswordInDirectoryServiceTask(BaseTask):
    # Passwords stay ordered among themselves, and a failing one holds up none of the
    # account's other tasks. One for an account not yet in the directory is sent
    # behind the task that creates it, and joins this lane when that has finished.
    lane = 'password'
    # user attribute: sent_at of the newest password finished with, applied or given up
    APPLIED = 'password_sync_sent_at'

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger(self.get_name())

    def get_name(self) -> str:
        return 'accounts.sync-password'

    def entity_ref(self, payload: Dict):
        return 'user', payload['username']

    def invoke(self, payload: Dict):
        username = payload['username']
        password_file = payload['password_file']

        self.logger.info(f'sync password for user: {username} in directory service')

        # The lane runs one password at a time, so this check cannot race another
        # change. A message can still arrive behind a newer one: moved in from the
        # group an earlier release used, or queued twice. It must not undo the newer.
        sent_at = payload.get('sent_at')
        user = self.context.accounts.user_dao.get_user(username) or {}
        if sent_at is not None and sent_at < user.get(self.APPLIED, 0):
            self.logger.info(f'a newer password superseded this for user: {username}')
            self.on_terminal_failure(payload)
            # the newer one's outcome stands, a recorded failure included
            return NOTHING_DONE

        try:
            with open(password_file, 'r') as f:
                password = f.read()
        except FileNotFoundError:
            if not os.path.isdir(os.path.dirname(os.path.dirname(password_file))):
                # the shared file system is away, not the file
                raise
            # The file goes only once its message is finished with, so this is a
            # redelivery of one that was: nothing is left to do, and no retry finds it.
            self.logger.warning(f'password file is gone for user: {username}; done')
            # it may have ended in a recorded failure, which this must not clear
            return NOTHING_DONE

        try:
            self._change_password(user, username, password)
        except PasswordRefused:
            # the same password is refused every time, and each retry would hold up any
            # later password behind it
            self._remove_password_file(password_file)
            raise TaskCannotSucceed(
                'The directory refused this password, so it was not synced. '
                'Desktop and SSH sign-in keep the old password until a new one is set.'
            ) from None

        if sent_at is not None:
            self._record_applied(username, sent_at)
        self._remove_password_file(password_file)

    def _change_password(self, user: Dict, username: str, password: str):
        # The directory service chooses the domain controller it asks, and an account
        # reaches the others some seconds after sync-user added it. One recorded as
        # added is waited for here; the failure and its retry are left for an account
        # that is really missing.
        added = user.get(SyncUserInDirectoryServiceTask.IN_DIRECTORY) is True
        deadline = time.monotonic() + SyncUserInDirectoryServiceTask.CATCH_UP_SECONDS
        while True:
            try:
                return self.context.ldap_client.change_password(username, password)
            except exceptions.SocaException as error:
                if (
                    not added
                    or error.error_code != errorcodes.AUTH_USER_NOT_FOUND
                    or time.monotonic() >= deadline
                ):
                    raise
            time.sleep(SyncUserInDirectoryServiceTask.CATCH_UP_INTERVAL)

    def _record_applied(self, username: str, sent_at: int):
        try:
            self.context.accounts.user_dao.table.update_item(
                Key={'username': username},
                UpdateExpression='SET #applied = :sent_at',
                ConditionExpression='attribute_exists(username) AND '
                '(attribute_not_exists(#applied) OR #applied < :sent_at)',
                ExpressionAttributeNames={'#applied': self.APPLIED},
                ExpressionAttributeValues={':sent_at': sent_at},
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise

    def on_terminal_failure(self, payload: Dict):
        try:
            self._remove_password_file(payload['password_file'])
        except FileNotFoundError:
            pass
        # An older password still queued behind this one must not become the
        # directory's password once this one is given up on.
        if payload.get('sent_at') is not None:
            self._record_applied(payload['username'], payload['sent_at'])

    def _remove_password_file(self, password_file: str):
        password_file_path = pathlib.Path(password_file)
        ds_automation_dir = self.context.config().get_string(
            'directoryservice.automation_dir', required=True
        )
        if str(password_file_path.parent.parent) == ds_automation_dir:
            shutil.rmtree(password_file_path.parent)
        else:
            os.remove(password_file)


class CreateUserHomeDirectoryTask(BaseTask):
    # Homes use the stored uid and gid, never the directory.
    lane = 'home'

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger(self.get_name())

    def get_name(self) -> str:
        return 'accounts.create-home-directory'

    def entity_ref(self, payload: Dict):
        return 'user', payload['username']

    def invoke(self, payload: Dict):
        username = payload['username']
        user = self.context.accounts.get_user(username)
        UserHomeDirectory(context=self.context, user=user).initialize()


class RepairHomeOwnershipTask(BaseTask):
    lane = 'home'

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context

    def get_name(self) -> str:
        return 'accounts.repair-home-ownership'

    def entity_ref(self, payload: Dict):
        return 'user', payload['username']

    def invoke(self, payload: Dict):
        user = self.context.accounts.get_user(payload['username'])
        result = UserHomeDirectory(self.context, user).repair_ownership(
            dry_run=payload.get('dry_run', False),
            stale_uid=payload.get('stale_uid'),
        )
        if result.failed:
            raise exceptions.general_exception(
                f'Home ownership repair failed for {result.failed} paths'
            )
        return result
