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

from ideasdk.utils import Utils
from ideadatamodel import exceptions
from ideasdk.context import SocaContext

from ideaclustermanager.app.accounts.accounts_service import AccountsService
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO

from typing import List, Optional
from boto3.dynamodb.conditions import Attr, Key


def is_enabled(group: dict) -> bool:
    # only an explicit false disables a group: records from earlier releases may carry
    # no value. A project is different: it is written without one until first enabled
    return Utils.get_value_as_bool('enabled', group, True)


class UserProjectsDAO:
    def __init__(
        self,
        context: SocaContext,
        projects_dao: ProjectsDAO,
        accounts_service: AccountsService,
        logger=None,
    ):
        self.context = context
        self.projects_dao = projects_dao
        self.accounts_service = accounts_service
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('user-projects-dao')

        self.user_projects_table = None
        self.project_groups_table = None

    def get_user_projects_table_name(self) -> str:
        return f'{self.context.cluster_name()}.projects.user-projects'

    def get_project_groups_table_name(self) -> str:
        return f'{self.context.cluster_name()}.projects.project-groups'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_user_projects_table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'username', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'username', 'KeyType': 'HASH'},
                    {'AttributeName': 'project_id', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
        )
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_project_groups_table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'group_name', 'AttributeType': 'S'},
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'group_name', 'KeyType': 'HASH'},
                    {'AttributeName': 'project_id', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
        )
        self.user_projects_table = (
            self.context.aws()
            .dynamodb_table()
            .Table(self.get_user_projects_table_name())
        )
        self.project_groups_table = (
            self.context.aws()
            .dynamodb_table()
            .Table(self.get_project_groups_table_name())
        )

    def create_user_project(self, project_id: str, username: str):
        if Utils.is_empty(project_id):
            raise exceptions.invalid_params('project_id is required')
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        self.logger.info(f'added user project: {project_id}, username: {username}')
        self.user_projects_table.put_item(
            Item={'project_id': project_id, 'username': username}
        )

    def delete_user_project(self, project_id: str, username: str):
        if Utils.is_empty(project_id):
            raise exceptions.invalid_params('project_id is required')
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        self.logger.info(f'deleted user project: {project_id}, username: {username}')
        self.user_projects_table.delete_item(
            Key={'project_id': project_id, 'username': username}
        )

    def ldap_group_added(self, project_id: str, group_name: str):
        if Utils.is_empty(project_id):
            raise exceptions.invalid_params('project_id is required')
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('username is required')

        self.project_groups_table.put_item(
            Item={'group_name': group_name, 'project_id': project_id}
        )
        self.refresh_project(project_id)

    def ldap_group_removed(self, project_id: str, group_name: str):
        if Utils.is_empty(project_id):
            raise exceptions.invalid_params('project_id is required')
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('username is required')

        self.project_groups_table.delete_item(
            Key={'group_name': group_name, 'project_id': project_id}
        )
        self.refresh_project(project_id)

    def get_projects_by_username(self, username: str) -> List[str]:
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        result = self.user_projects_table.query(
            KeyConditionExpression=Key('username').eq(username)
        )
        user_projects = Utils.get_value_as_list('Items', result, [])

        project_ids = []
        for user_project in user_projects:
            project_ids.append(user_project['project_id'])
        return project_ids

    def get_projects_by_group_name(self, group_name: str) -> List[str]:
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')
        self.logger.debug(
            f'get_projects_by_group_name() - Looking for {group_name} in UserProjectsDAO/DDB'
        )
        result = self.project_groups_table.query(
            KeyConditionExpression=Key('group_name').eq(group_name)
        )
        self.logger.debug(
            f'get_projects_by_group_name() - Group: {group_name} Result: {result}'
        )
        group_projects = Utils.get_value_as_list('Items', result, default=[])

        if Utils.is_empty(group_projects):
            self.logger.info(
                f'get_projects_by_group_name() - Group: {group_name} - No projects found (DAO)'
            )
            return []

        self.logger.debug(
            f'get_projects_by_group_name() - Group: {group_name} group_projects: {group_projects}'
        )
        project_ids = []
        for group_project in group_projects:
            project_ids.append(group_project['project_id'])

        if Utils.is_empty(project_ids):
            self.logger.info(
                f'get_projects_by_group_name() - Group: {group_name} - No projectIDs found (DAO)'
            )
            return []

        self.logger.debug(
            f'get_projects_by_group_name() - Group: {group_name} group_projects: {group_projects} - Returning IDs: {project_ids}'
        )
        return project_ids

    def group_member_added(self, group_name: str, username: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        self.logger.info(
            f'group_member_added() - Adding {username} to {group_name} in DDB. Fetch project_id for {group_name}'
        )

        if not self._group_grants(group_name):
            return
        for project_id in self.get_projects_by_group_name(group_name):
            project = self.projects_dao.get_project_by_id(project_id) or {}
            # the mapping is an index: the project's own groups decide
            if Utils.get_value_as_bool(
                'enabled', project, False
            ) and group_name in Utils.get_value_as_list('ldap_groups', project, []):
                self.create_user_project(project_id=project_id, username=username)

    def group_member_removed(self, group_name: str, username: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        project_ids = self.get_projects_by_group_name(group_name)
        user = self.accounts_service.user_dao.get_user(username) or {}
        # the groups that still grant the user's projects once this one is gone
        remaining = set(Utils.get_value_as_list('additional_groups', user, []))
        remaining.discard(group_name)
        for project_id in project_ids:
            project = self.projects_dao.get_project_by_id(project_id) or {}
            granting, unknown = self._group_states(project)
            # a group with no record may still be what grants it: left as it is
            if remaining & (granting | unknown):
                continue
            self.delete_user_project(project_id=project_id, username=username)

    def _group_grants(self, group_name: str) -> bool:
        # a disabled group grants none of its projects while it is disabled
        group = self.accounts_service.group_dao.get_group(group_name)
        return group is not None and is_enabled(group)

    def _group_states(self, project: dict):
        """the project's groups that grant it, and those that have no group record"""
        granting, unknown = set(), set()
        for name in Utils.get_value_as_list('ldap_groups', project, []):
            group = self.accounts_service.group_dao.get_group(name)
            if group is None:
                unknown.add(name)
            elif is_enabled(group):
                granting.add(name)
        return granting, unknown

    def _plan_project(self, project_id: str, project: Optional[dict]):
        """
        the writes that make the project's user rows match who its enabled groups grant
        it to, worked out from reads alone. None: no project record, nothing to change
        """
        if project is None:
            return None
        wanted = set(Utils.get_value_as_list('ldap_groups', project, []))
        rows = Utils.scan_all(
            self.project_groups_table,
            {'FilterExpression': Attr('project_id').eq(project_id)},
        )
        held = {row['group_name'] for row in rows}
        state = Utils.get_value_as_bool('enabled', project)
        desired, unknown = set(), set()
        if state:
            granting, unknown = self._group_states(project)
            for name in granting:
                desired.update(
                    self.accounts_service.group_members_dao.get_usernames_in_group(name)
                )
        rows = Utils.scan_all(
            self.user_projects_table,
            {'FilterExpression': Attr('project_id').eq(project_id)},
        )
        current = {row['username'] for row in rows}
        for name in sorted(unknown):
            self.logger.warning(
                f'project {project_id} names group {name}, which has no group record: '
                'its members keep the project until an administrator edits it'
            )
        if state is None:
            # never enabled, or a record whose value was lost: what is there is kept,
            # and nothing is granted until it is enabled or disabled
            if current:
                self.logger.warning(
                    f'project {project_id} has user rows but no enabled value: they are '
                    'kept until an administrator enables or disables it'
                )
            desired, current = set(), set()
        return {
            'project_id': project_id,
            'groups_added': sorted(wanted - held),
            'groups_removed': sorted(held - wanted),
            'users_added': sorted(desired - current),
            # who has it through a group with no record cannot be told apart
            'users_removed': [] if unknown else sorted(current - desired),
        }

    def _apply_plan(self, plan: Optional[dict]):
        if plan is None:
            return
        project_id = plan['project_id']
        for group_name in plan['groups_added']:
            self.project_groups_table.put_item(
                Item={'group_name': group_name, 'project_id': project_id}
            )
        for group_name in plan['groups_removed']:
            self.project_groups_table.delete_item(
                Key={'group_name': group_name, 'project_id': project_id}
            )
        for username in plan['users_added']:
            self.create_user_project(project_id=project_id, username=username)
        for username in plan['users_removed']:
            self.delete_user_project(project_id=project_id, username=username)

    def refresh_project(self, project_id: str):
        """
        the project's user rows made to match who its enabled groups grant it to: rows
        for members of an enabled group of an enabled project, and no others. The group
        to project index is made to match the project's groups too: earlier releases
        left rows for groups removed from a project and none for groups added to one
        """
        project = self.projects_dao.get_project_by_id(project_id)
        self._apply_plan(self._plan_project(project_id, project))

    def group_enabled_changed(self, group_name: str):
        """a group disabled or enabled: its projects follow who it grants them to"""
        for project_id in self.get_projects_by_group_name(group_name):
            self.refresh_project(project_id)

    def refresh_all_projects(self):
        """
        every project's rows made to match its groups: rows left by group edits that
        did not reach the worker in releases before 26.11.2. Every table is read before
        anything is written, so a read that fails changes nothing
        """
        plans = [
            self._plan_project(project['project_id'], project)
            for project in Utils.scan_all(self.projects_dao.table, {})
        ]
        for plan in plans:
            self._apply_plan(plan)

    def project_disabled(self, project_id: str):
        if Utils.are_empty(project_id):
            raise exceptions.invalid_params('project_id is required')

        project = self.projects_dao.get_project_by_id(project_id)
        ldap_groups = project['ldap_groups']

        for ldap_group in ldap_groups:
            self.ldap_group_removed(project_id=project_id, group_name=ldap_group)

    def project_enabled(self, project_id: str):
        if Utils.are_empty(project_id):
            raise exceptions.invalid_params('project_id is required')

        project = self.projects_dao.get_project_by_id(project_id)
        for ldap_group in project['ldap_groups']:
            self.project_groups_table.put_item(
                Item={'group_name': ldap_group, 'project_id': project_id}
            )
        self.refresh_project(project_id)

    def is_user_in_project(self, username: str, project_id: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        if Utils.is_empty(project_id):
            raise exceptions.invalid_params('project_id is required')

        result = self.user_projects_table.get_item(
            Key={'username': username, 'project_id': project_id}
        )
        return Utils.get_value_as_dict('Item', result) is not None
