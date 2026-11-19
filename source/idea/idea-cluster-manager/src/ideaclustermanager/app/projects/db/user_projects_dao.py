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

from typing import List
from boto3.dynamodb.conditions import Attr, Key


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
            if remaining & self._granting_groups(project):
                continue
            self.delete_user_project(project_id=project_id, username=username)

    def _group_grants(self, group_name: str) -> bool:
        # a disabled group grants none of its projects while it is disabled
        group = self.accounts_service.group_dao.get_group(group_name)
        return Utils.get_value_as_bool('enabled', group, False)

    def _granting_groups(self, project: dict) -> set:
        return {
            name
            for name in Utils.get_value_as_list('ldap_groups', project, [])
            if self._group_grants(name)
        }

    def refresh_project(self, project_id: str):
        """
        the project's user rows made to match who its enabled groups grant it to: rows
        for members of an enabled group of an enabled project, and no others
        """
        project = self.projects_dao.get_project_by_id(project_id) or {}
        self._refresh_group_mapping(project_id, project)
        desired = set()
        if Utils.get_value_as_bool('enabled', project, False):
            for name in self._granting_groups(project):
                desired.update(
                    self.accounts_service.group_members_dao.get_usernames_in_group(name)
                )
        rows = Utils.scan_all(
            self.user_projects_table,
            {'FilterExpression': Attr('project_id').eq(project_id)},
        )
        current = {row['username'] for row in rows}
        for username in sorted(desired - current):
            self.create_user_project(project_id=project_id, username=username)
        for username in sorted(current - desired):
            self.delete_user_project(project_id=project_id, username=username)

    def _refresh_group_mapping(self, project_id: str, project: dict):
        """
        the group to project index made to match the project's groups: earlier releases
        left rows for groups removed from a project and none for groups added to one
        """
        wanted = set(Utils.get_value_as_list('ldap_groups', project, []))
        rows = Utils.scan_all(
            self.project_groups_table,
            {'FilterExpression': Attr('project_id').eq(project_id)},
        )
        held = {row['group_name'] for row in rows}
        for group_name in sorted(wanted - held):
            self.project_groups_table.put_item(
                Item={'group_name': group_name, 'project_id': project_id}
            )
        for group_name in sorted(held - wanted):
            self.project_groups_table.delete_item(
                Key={'group_name': group_name, 'project_id': project_id}
            )

    def group_enabled_changed(self, group_name: str):
        """a group disabled or enabled: its projects follow who it grants them to"""
        for project_id in self.get_projects_by_group_name(group_name):
            self.refresh_project(project_id)

    def refresh_all_projects(self):
        """
        every project's rows made to match its groups: rows left by group edits that
        did not reach the worker in releases before 26.11.2
        """
        for project in Utils.scan_all(self.projects_dao.table, {}):
            self.refresh_project(project['project_id'])

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
