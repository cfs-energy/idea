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


from ideadatamodel import exceptions, errorcodes, constants, SocaAmount
from ideadatamodel.projects import (
    CreateProjectRequest,
    CreateProjectResult,
    GetProjectRequest,
    GetProjectResult,
    UpdateProjectRequest,
    UpdateProjectResult,
    ListProjectsRequest,
    ListProjectsResult,
    EnableProjectRequest,
    EnableProjectResult,
    DisableProjectRequest,
    DisableProjectResult,
    GetUserProjectsRequest,
    GetUserProjectsResult,
    BedrockUserUsage,
    Project,
    ProjectBedrockBudget,
    ProjectBedrockUsage,
)
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.context import SocaContext

from ideaclustermanager.app.projects.bedrock_provisioner import (
    BedrockProvisioner,
    validate_no_global_profiles,
)
from ideaclustermanager.app.projects.bedrock_budget import (
    BedrockBudget,
    is_bedrock_service,
)
from ideaclustermanager.app.projects.bedrock_usage_service import (
    get_project_usage_by_model,
)
from ideaclustermanager.app.projects.db.bedrock_usage_dao import BedrockUsageDAO
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideaclustermanager.app.projects.db.user_projects_dao import UserProjectsDAO
from ideaclustermanager.app.accounts.accounts_service import AccountsService
from ideaclustermanager.app.tasks.task_manager import TaskManager

import arrow
from typing import Dict, List, Optional, Tuple


class ProjectsService:
    def __init__(
        self,
        context: SocaContext,
        accounts_service: AccountsService,
        task_manager: TaskManager,
    ):
        self.context = context
        self.accounts_service = accounts_service
        self.task_manager = task_manager
        self.logger = context.logger('projects')

        self.projects_dao = ProjectsDAO(context)
        self.projects_dao.initialize()

        self.user_projects_dao = UserProjectsDAO(
            context=context,
            projects_dao=self.projects_dao,
            accounts_service=self.accounts_service,
        )
        self.user_projects_dao.initialize()

        self.bedrock_provisioner = BedrockProvisioner(
            context=context, projects_dao=self.projects_dao
        )

        self.bedrock_usage_dao = BedrockUsageDAO(context)
        self.bedrock_budget = BedrockBudget(context=context)
        if self.is_bedrock_usage_enabled():
            # the table grants arrive via a cluster-manager redeploy, which can trail the setting;
            # a failure here must not stop the module starting, since the read path handles an uninitialized table.
            try:
                self.bedrock_usage_dao.initialize()
            except Exception as e:
                self.logger.warning(
                    f'bedrock usage table is unavailable: {e}. redeploy the '
                    f'cluster-manager module with bedrock enabled.'
                )

    def is_bedrock_usage_enabled(self) -> bool:
        if not self.bedrock_provisioner.is_enabled():
            return False
        return self.context.config().get_bool(
            f'{self.context.module_id()}.bedrock.usage.enabled', True
        )

    def get_project_bedrock_usage(
        self,
        project_id: str,
        period: str = None,
        username: str = None,
        project_name: str = None,
    ) -> Optional[ProjectBedrockUsage]:
        """
        month to date usage for a project, or for one caller within it. read from
        the rollups the usage service writes; returns None when nothing is recorded.
        project_name is the cost allocation tag value the spend figure is read for.
        """
        if not self.bedrock_provisioner.is_enabled():
            return None
        if self.bedrock_usage_dao.table is None:
            return None
        if Utils.is_empty(period):
            period = arrow.utcnow().format('YYYY-MM')

        if Utils.is_not_empty(username):
            item = self.bedrock_usage_dao.get_user_rollup(
                project_id=project_id, period=period, username=username
            )
            if item is None:
                return None
            return self.build_bedrock_usage(item, period, username=username)

        item = self.bedrock_usage_dao.get_project_rollup(
            project_id=project_id, period=period
        )
        if item is None:
            return None

        usage = self.build_bedrock_usage(item, period)
        max_users = self.context.config().get_int(
            f'{self.context.module_id()}.bedrock.usage.max_users_per_project', 50
        )
        user_rows = self.bedrock_usage_dao.query_user_rollups(
            project_id=project_id, period=period
        )
        user_rows.sort(
            key=lambda row: Utils.get_value_as_int('total_tokens', row, 0), reverse=True
        )
        usage.by_user = [
            BedrockUserUsage(
                username=Utils.get_value_as_string('username', row),
                invocations=Utils.get_value_as_int('invocations', row, 0),
                input_tokens=Utils.get_value_as_int('input_tokens', row, 0),
                output_tokens=Utils.get_value_as_int('output_tokens', row, 0),
                total_tokens=Utils.get_value_as_int('total_tokens', row, 0),
            )
            for row in user_rows[:max_users]
        ]
        usage.by_model = get_project_usage_by_model(
            self.bedrock_usage_dao, project_id, period
        )
        self.apply_bedrock_spend(usage, project_name)
        return usage

    def apply_bedrock_spend(self, usage: ProjectBedrockUsage, project_name: str):
        """
        month to date bedrock cost for the project cost allocation tag. no answer is
        recorded as unavailable, never as zero: an empty result is priced nothing yet.
        """
        spend = None
        if Utils.is_not_empty(project_name):
            try:
                spend = self.context.aws_util().cost_explorer_get_tagged_service_spend(
                    constants.IDEA_TAG_PROJECT, project_name
                )
            except Exception as e:
                self.logger.warning(
                    f'failed to read bedrock spend for project {project_name}: {e}'
                )
        if spend is None:
            usage.spend_is_unavailable = True
            return
        usage.spend = SocaAmount(
            amount=round(
                sum(
                    amount
                    for service, amount in spend.items()
                    if is_bedrock_service(service)
                ),
                2,
            )
        )

    def get_project_bedrock_budget(
        self, project: Project
    ) -> Optional[ProjectBedrockBudget]:
        """
        the verdict on the project budget. the caller passes a project whose budget
        carries live actuals.
        """
        return self.bedrock_budget.evaluate(project)

    @staticmethod
    def build_bedrock_usage(
        item: Dict, period: str, username: str = None
    ) -> ProjectBedrockUsage:
        updated_on = Utils.get_value_as_int('updated_on', item, 0)
        return ProjectBedrockUsage(
            period=period,
            username=username,
            invocations=Utils.get_value_as_int('invocations', item, 0),
            input_tokens=Utils.get_value_as_int('input_tokens', item, 0),
            output_tokens=Utils.get_value_as_int('output_tokens', item, 0),
            total_tokens=Utils.get_value_as_int('total_tokens', item, 0),
            updated_on=arrow.get(updated_on / 1000).datetime if updated_on else None,
        )

    @staticmethod
    def has_bedrock_provisioner_fields(stored: Optional[Dict]) -> bool:
        bedrock = Utils.get_value_as_dict('bedrock', stored, {}) if stored else {}
        for key in ('role_arn', 'instance_profile_arn'):
            if Utils.is_not_empty(Utils.get_value_as_string(key, bedrock)):
                return True
        return len(Utils.get_value_as_dict('inference_profile_arns', bedrock, {})) > 0

    def send_bedrock_reconcile(
        self,
        project_id: str,
        stored: Optional[Dict] = None,
        cluster_bedrock: Optional[Dict] = None,
    ):
        """
        also enqueued while the feature is off when the project still carries
        provisioner fields, so turning the feature off removes what it provisioned.
        a project that has never carried a bedrock block is skipped.

        cluster_bedrock is the caller's intended cluster settings, used when they were
        just written and the in-memory config has not caught up.
        """
        enabled = (
            self.bedrock_provisioner.is_enabled()
            if cluster_bedrock is None
            else Utils.get_value_as_bool('enabled', cluster_bedrock, False)
        )
        if enabled:
            if stored is not None and Utils.is_empty(
                Utils.get_value_as_dict('bedrock', stored, {})
            ):
                return
        elif not self.has_bedrock_provisioner_fields(stored):
            return

        payload = {'project_id': project_id}
        if cluster_bedrock is not None:
            payload['cluster_bedrock'] = cluster_bedrock
        self.task_manager.send(
            task_name='projects.bedrock-reconcile',
            payload=payload,
            message_group_id=project_id,
            message_dedupe_id=Utils.short_uuid(),
        )

    def send_bedrock_reconcile_all(self, cluster_bedrock: Optional[Dict] = None):
        """
        every project that carries a bedrock block. used when a cluster wide
        setting changes, since that changes what each project resolves to.
        """
        cursor = None
        while True:
            result = self.projects_dao.list_projects(ListProjectsRequest(cursor=cursor))
            for project in Utils.get_as_list(result.listing, []):
                if project.bedrock is None:
                    continue
                self.send_bedrock_reconcile(
                    project.project_id,
                    stored=self.projects_dao.convert_to_db(project),
                    cluster_bedrock=cluster_bedrock,
                )
            cursor = result.paginator.cursor if result.paginator is not None else None
            if Utils.is_empty(cursor):
                return

    def get_bedrock_routing(self) -> Tuple[str, str]:
        """
        the partition and region a model id has to be routable from, so a global
        id can be rejected naming the geographic id to use instead.
        """
        config = self.context.config()
        return (
            config.get_string('cluster.aws.partition', ''),
            config.get_string('cluster.aws.region', ''),
        )

    @staticmethod
    def validate_bedrock_config(
        project: Project, catalog: List[str], partition: str = '', region: str = ''
    ):
        """
        a project may only reference model ids present in the cluster catalog, and
        never a global inference profile.
        """
        if project.bedrock is None:
            return

        validate_no_global_profiles(project.bedrock.get_model_ids(), partition, region)

        not_in_catalog = [
            model_id
            for model_id in project.bedrock.get_model_ids()
            if model_id not in catalog
        ]
        if len(not_in_catalog) > 0:
            approved = ', '.join(catalog) if len(catalog) > 0 else '(none)'
            raise exceptions.invalid_params(
                f'bedrock.model_ids contains model ids that are not approved for this '
                f'cluster: {", ".join(not_in_catalog)}. approved model ids: {approved}'
            )

    @staticmethod
    def apply_bedrock_provisioner_fields(
        project: Project, existing: Optional[Dict] = None
    ):
        """
        the provisioner owns role_arn, instance_profile_arn and
        inference_profile_arns. caller supplied values are discarded and the
        stored values are carried forward, since the block is written whole.
        """
        if project.bedrock is None:
            return

        existing_bedrock = {}
        if existing is not None:
            existing_bedrock = Utils.get_value_as_dict('bedrock', existing, {})

        project.bedrock.role_arn = Utils.get_value_as_string(
            'role_arn', existing_bedrock
        )
        project.bedrock.instance_profile_arn = Utils.get_value_as_string(
            'instance_profile_arn', existing_bedrock
        )
        project.bedrock.inference_profile_arns = Utils.get_value_as_dict(
            'inference_profile_arns', existing_bedrock
        )

    def create_project(self, request: CreateProjectRequest) -> CreateProjectResult:
        """
        Create a new Project
        validate required fields, add the project to DynamoDB and Cache.
        :param request:
        :return: the created project (with project_id)
        """

        ds_provider = self.context.config().get_string(
            'directoryservice.provider', required=True
        )

        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')

        project = request.project
        if Utils.is_empty(project):
            raise exceptions.invalid_params('project is required')

        if Utils.is_empty(project.name):
            raise exceptions.invalid_params('project.name is required')

        existing = self.projects_dao.get_project_by_name(project.name)
        if existing is not None:
            raise exceptions.invalid_params(
                f'project with name: {project.name} already exists'
            )

        # Initialize empty ldap_groups list if not provided
        if project.ldap_groups is None:
            project.ldap_groups = []

        # Validate any provided ldap_groups
        for ldap_group_name in project.ldap_groups:
            # check if group exists
            # Active Directory mode checks the back-end LDAP
            if ds_provider in {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}:
                self.logger.debug(f'Performing DS lookup for group: {ldap_group_name}')
                self.accounts_service.ldap_client.get_group(ldap_group_name)
            else:
                self.accounts_service.get_group(ldap_group_name)

        enable_budgets = Utils.get_as_bool(project.enable_budgets, False)
        if enable_budgets:
            if project.budget is None or Utils.is_empty(project.budget.budget_name):
                raise exceptions.invalid_params(
                    'budget.budget_name is required when budgets are enabled'
                )
            budget_name = project.budget.budget_name
            self.context.aws_util().budgets_get_budget(budget_name)

        partition, region = self.get_bedrock_routing()
        self.validate_bedrock_config(
            project, self.bedrock_provisioner.get_model_catalog(), partition, region
        )
        self.apply_bedrock_provisioner_fields(project)

        # ensure project is always disabled during creation
        project.enabled = False

        db_project = self.projects_dao.convert_to_db(project)
        db_created_project = self.projects_dao.create_project(db_project)

        created_project = self.projects_dao.convert_from_db(db_created_project)

        return CreateProjectResult(project=created_project)

    def get_project(self, request: GetProjectRequest) -> GetProjectResult:
        """
        Retrieve the Project from the cache
        :param request.project_name name of the project you are getting
        :param request.project_id UUID of the project being searched
        :return: Project from cache
        """
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.are_empty(request.project_id, request.project_name):
            raise exceptions.invalid_params(
                'Either project_id or project_name is required'
            )

        self.logger.debug(f'get_project(): running with request: {request}')

        project = None
        if Utils.is_not_empty(request.project_id):
            project = self.projects_dao.get_project_by_id(request.project_id)
        elif Utils.is_not_empty(request.project_name):
            project = self.projects_dao.get_project_by_name(request.project_name)

        if project is None:
            if Utils.is_not_empty(request.project_id):
                raise exceptions.soca_exception(
                    error_code=errorcodes.PROJECT_NOT_FOUND,
                    message=f'project not found for project id: {request.project_id}',
                )
            if Utils.is_not_empty(request.project_name):
                raise exceptions.soca_exception(
                    error_code=errorcodes.PROJECT_NOT_FOUND,
                    message=f'project not found for project name: {request.project_name}',
                )

        return GetProjectResult(project=self.projects_dao.convert_from_db(project))

    def update_project(self, request: UpdateProjectRequest) -> UpdateProjectResult:
        """
        Update a Project
        :param request:
        :return:
        """
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')

        project = request.project
        if Utils.is_empty(project):
            raise exceptions.invalid_params('project is required')
        if Utils.is_empty(project.project_id):
            raise exceptions.invalid_params('project.project_id is required')

        existing = self.projects_dao.get_project_by_id(project_id=project.project_id)

        if existing is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.PROJECT_NOT_FOUND,
                message=f'project not found for id: {project.project_id}',
            )

        if Utils.is_not_empty(project.name) and existing['name'] != project.name:
            same_name_project = self.projects_dao.get_project_by_name(project.name)
            if (
                same_name_project is not None
                and same_name_project['project_id'] != project.project_id
            ):
                raise exceptions.invalid_params(
                    f'project with name: {project.name} already exists'
                )

        enable_budgets = Utils.get_as_bool(project.enable_budgets, False)
        if enable_budgets:
            if project.budget is None or Utils.is_empty(project.budget.budget_name):
                raise exceptions.invalid_params(
                    'budget.budget_name is required when budgets are enabled'
                )
            budget_name = project.budget.budget_name
            try:
                self.context.aws_util().budgets_get_budget(budget_name)
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.BUDGET_NOT_FOUND:
                    # We'll allow the update but log a warning about the missing budget
                    self.logger.warning(
                        f'Budget {budget_name} not found but still updating project configuration.'
                    )
                else:
                    # For other exceptions, re-raise
                    raise e

        partition, region = self.get_bedrock_routing()
        self.validate_bedrock_config(
            project, self.bedrock_provisioner.get_model_catalog(), partition, region
        )
        self.apply_bedrock_provisioner_fields(project, existing)

        groups_added = None
        groups_removed = None
        if Utils.is_not_empty(project.ldap_groups):
            existing_ldap_groups = set(
                Utils.get_value_as_list('ldap_groups', existing, [])
            )
            updated_ldap_groups = set(project.ldap_groups)

            groups_added = updated_ldap_groups - existing_ldap_groups
            groups_removed = existing_ldap_groups - updated_ldap_groups

            if len(groups_added) > 0:
                for ldap_group_name in groups_added:
                    # check if group exists
                    self.accounts_service.get_group(ldap_group_name)

        # none values will be skipped by db update. ensure enabled/disabled cannot be called via update project.
        project.enabled = None

        db_updated = self.projects_dao.update_project(
            self.projects_dao.convert_to_db(project)
        )
        updated_project = self.projects_dao.convert_from_db(db_updated)

        if updated_project.enabled:
            if groups_added is not None or groups_removed is not None:
                self.task_manager.send(
                    task_name='projects.project-groups-updated',
                    payload={
                        'project_id': updated_project.project_id,
                        'groups_added': groups_added,
                        'groups_removed': groups_removed,
                    },
                    message_group_id=updated_project.project_id,
                )

        self.send_bedrock_reconcile(updated_project.project_id, stored=db_updated)

        return UpdateProjectResult(project=updated_project)

    def enable_project(self, request: EnableProjectRequest) -> EnableProjectResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.are_empty(request.project_id, request.project_name):
            raise exceptions.invalid_params(
                'Either project_id or project_name is required'
            )

        project = None
        if Utils.is_not_empty(request.project_id):
            project = self.projects_dao.get_project_by_id(request.project_id)
        elif Utils.is_not_empty(request.project_name):
            project = self.projects_dao.get_project_by_name(request.project_name)

        if project is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.PROJECT_NOT_FOUND, message='project not found'
            )

        self.projects_dao.update_project(
            {'project_id': project['project_id'], 'enabled': True}
        )

        self.task_manager.send(
            task_name='projects.project-enabled',
            payload={'project_id': project['project_id']},
            message_group_id=project['project_id'],
            message_dedupe_id=Utils.short_uuid(),
        )

        self.send_bedrock_reconcile(project['project_id'], stored=project)

        return EnableProjectResult()

    def disable_project(self, request: DisableProjectRequest) -> DisableProjectResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.are_empty(request.project_id, request.project_name):
            raise exceptions.invalid_params(
                'Either project_id or project_name is required'
            )

        project = None
        if Utils.is_not_empty(request.project_id):
            project = self.projects_dao.get_project_by_id(request.project_id)
        elif Utils.is_not_empty(request.project_name):
            project = self.projects_dao.get_project_by_name(request.project_name)

        if project is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.PROJECT_NOT_FOUND, message='project not found'
            )
        self.projects_dao.update_project(
            {'project_id': project['project_id'], 'enabled': False}
        )

        self.task_manager.send(
            task_name='projects.project-disabled',
            payload={'project_id': project['project_id']},
            message_group_id=project['project_id'],
            message_dedupe_id=Utils.short_uuid(),
        )

        self.send_bedrock_reconcile(project['project_id'], stored=project)

        return DisableProjectResult()

    def list_projects(self, request: ListProjectsRequest) -> ListProjectsResult:
        return self.projects_dao.list_projects(request)

    def get_user_projects(
        self, request: GetUserProjectsRequest
    ) -> GetUserProjectsResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required')

        self.logger.debug(f'get_user_projects() - request: {request}')

        # Probe directory service
        ds_provider = self.context.config().get_string(
            'directoryservice.provider', required=True
        )
        self.logger.debug(
            f'ProjectsService.get_user_projects() - DS Provider is {ds_provider} ...'
        )
        if ds_provider in {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}:
            self.logger.debug(
                f'get_user_projects() - Running in AD mode - performing AD query for {request.username} group memberships...'
            )
            user_result = self.accounts_service.ldap_client.get_user(
                username=request.username
            )
            self.logger.debug(f'get_user_projects() - User Result: {user_result}')

        user_projects = self.user_projects_dao.get_projects_by_username(
            request.username
        )

        result = []
        # todo - batch get
        for project_id in user_projects:
            db_project = self.projects_dao.get_project_by_id(project_id)
            if db_project is None:
                continue
            if not db_project['enabled']:
                continue
            result.append(self.projects_dao.convert_from_db(db_project))
        result.sort(key=lambda p: p.name)

        return GetUserProjectsResult(projects=result)

    def is_project_member(self, username: str, project_id: str) -> bool:
        """
        check if the user is a member of the project, using the same membership
        resolution as get_user_projects (user-projects table maintained from the
        project's ldap groups).
        """
        if Utils.is_empty(username) or Utils.is_empty(project_id):
            return False
        return project_id in self.user_projects_dao.get_projects_by_username(username)

    def create_defaults(self):
        ds_provider = self.context.config().get_string(
            'directoryservice.provider', required=True
        )

        self.logger.debug(
            f'ProjectsService.create_defaults() - DS Provider is {ds_provider} ...'
        )

        default_project_group_name = GroupNameHelper(
            self.context
        ).get_default_project_group()

        if ds_provider in {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}:
            default_project_group_name_ds = self.context.config().get_string(
                'directoryservice.group_mapping.default-project-group', required=True
            )
        else:
            default_project_group_name_ds = default_project_group_name

        default_project = self.projects_dao.get_project_by_name(
            constants.DEFAULT_PROJECT
        )
        self.logger.debug(
            f'Default project group name: {default_project_group_name} Project: {default_project}'
        )

        if default_project is None:
            self.logger.info('creating and enabling default project ...')
            result = self.create_project(
                CreateProjectRequest(
                    project=Project(
                        name=constants.DEFAULT_PROJECT,
                        title='Default Project',
                        description='Default Project',
                        ldap_groups=[default_project_group_name_ds],
                    )
                )
            )
            self.enable_project(
                EnableProjectRequest(project_id=result.project.project_id)
            )
