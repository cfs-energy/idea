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

import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel.projects import (
    CreateProjectRequest,
    GetProjectRequest,
    UpdateProjectRequest,
    ListProjectsRequest,
    EnableProjectRequest,
    DisableProjectRequest,
    GetUserProjectsRequest,
    Project,
    ProjectBedrockBudget,
    ProjectBedrockUsage,
    BEDROCK_BUDGET_STATUS_UNAVAILABLE,
)
from ideadatamodel import exceptions
from ideasdk.utils import Utils
from ideadatamodel.aws.model import AwsProjectBudget
from ideadatamodel.common.common_model import SocaAmount
from ideadatamodel import errorcodes


class ProjectsAPI(BaseAPI):
    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context

        self.SCOPE_WRITE = f'{self.context.module_id()}/write'
        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            'Projects.CreateProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.create_project,
            },
            'Projects.GetProject': {
                'scope': self.SCOPE_READ,
                'method': self.get_project,
            },
            'Projects.UpdateProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.update_project,
            },
            'Projects.ListProjects': {
                'scope': self.SCOPE_READ,
                'method': self.list_projects,
            },
            'Projects.GetUserProjects': {
                'scope': self.SCOPE_READ,
                'method': self.admin_get_user_projects,
            },
            'Projects.EnableProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.enable_project,
            },
            'Projects.DisableProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.disable_project,
            },
        }

    def create_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateProjectRequest)
        result = self.context.projects.create_project(request)
        context.success(result)

    def apply_budget_actuals(self, project: Project):
        """
        replace the stored budget reference with live AWS Budgets data.
        a missing budget is rendered as exhausted so the UI blocks submission.
        """
        if not project.is_budgets_enabled():
            return
        try:
            budget = self.context.aws_util().budgets_get_budget(
                budget_name=project.budget.budget_name
            )
            project.budget = budget
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.BUDGET_NOT_FOUND:
                # Budget not found - treat as budget exhausted
                project.budget = AwsProjectBudget(
                    budget_name=project.budget.budget_name,
                    budget_limit=SocaAmount(amount=100.0),  # Set a default budget limit
                    actual_spend=SocaAmount(
                        amount=200.0
                    ),  # Set actual spend higher than limit to show as exhausted
                    forecasted_spend=SocaAmount(
                        amount=200.0
                    ),  # Set forecasted spend higher than limit
                    is_missing=True,  # Flag to indicate budget is missing
                )
            else:
                # For other exceptions, re-raise
                raise e

    def apply_bedrock_usage(self, project: Project, username: str = None):
        """
        attach month to date bedrock usage, and the spend cost explorer reports for
        the project tag, which trails the recorded usage by about a day.
        """
        if not project.is_bedrock_enabled():
            return
        try:
            project.bedrock_usage = self.context.projects.get_project_bedrock_usage(
                project_id=project.project_id,
                username=username,
                project_name=project.name,
            )
        except Exception as e:
            # a failed read must not reach the client as an empty usage record: that
            # is indistinguishable from a project nobody used.
            project.bedrock_usage = ProjectBedrockUsage(is_unavailable=True)
            self.context.logger().warning(
                f'failed to read bedrock usage for project {project.project_id}: {e}'
            )

    def apply_bedrock_budget(self, project: Project):
        """
        attach the bedrock budget verdict. call after apply_budget_actuals: the
        evaluation compares against the live budget, not the stored reference.
        """
        if not project.is_bedrock_enabled():
            return
        try:
            project.bedrock_budget = self.context.projects.get_project_bedrock_budget(
                project
            )
        except Exception as e:
            # an evaluation that did not happen must not read as room to spend.
            project.bedrock_budget = ProjectBedrockBudget(
                status=BEDROCK_BUDGET_STATUS_UNAVAILABLE,
                message='the project budget could not be evaluated',
            )
            self.context.logger().warning(
                f'failed to evaluate the bedrock budget for project '
                f'{project.project_id}: {e}'
            )

    @staticmethod
    def strip_bedrock_provisioner_fields(project: Project):
        """
        the iam role and instance profile are server side selection inputs with no
        client use, and policy errors name administrator policies. the inference
        profile arns stay: they are the identifier a user passes to invoke a model.
        """
        if project is None or project.bedrock is None:
            return
        project.bedrock.role_arn = None
        project.bedrock.instance_profile_arn = None
        project.bedrock.policy_errors = None

    def get_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetProjectRequest)
        result = self.context.projects.get_project(request)
        self.apply_budget_actuals(result.project)
        self.apply_bedrock_usage(result.project)
        self.apply_bedrock_budget(result.project)
        context.success(result)

    def get_member_project(self, context: ApiInvocationContext):
        """
        non-elevated variant of get_project: serves the project only when the
        caller is a member, using the same membership resolution as
        Projects.GetUserProjects. non-membership and non-existence are reported
        identically so project names cannot be probed.
        """
        request = context.get_request_payload_as(GetProjectRequest)
        try:
            result = self.context.projects.get_project(request)
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.PROJECT_NOT_FOUND:
                raise exceptions.unauthorized_access()
            raise e

        is_member = self.context.projects.is_project_member(
            username=context.get_username(),
            project_id=result.project.project_id,
        )
        if not is_member:
            raise exceptions.unauthorized_access()

        self.apply_budget_actuals(result.project)
        self.apply_bedrock_usage(result.project, username=context.get_username())
        self.apply_bedrock_budget(result.project)
        self.strip_bedrock_provisioner_fields(result.project)
        context.success(result)

    def update_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateProjectRequest)
        result = self.context.projects.update_project(request)
        context.success(result)

    def list_projects(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListProjectsRequest)
        result = self.context.projects.list_projects(request)
        for project in result.listing:
            # this call could possibly make some performance degradations, if the configured budget is not available.
            # need to optimize this further.
            self.apply_budget_actuals(project)
            self.apply_bedrock_usage(project)
            self.apply_bedrock_budget(project)
        context.success(result)

    def get_user_projects(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserProjectsRequest)
        request.username = context.get_username()
        result = self.context.projects.get_user_projects(request)
        for project in Utils.get_as_list(result.projects, []):
            self.strip_bedrock_provisioner_fields(project)
        context.success(result)

    def admin_get_user_projects(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserProjectsRequest)
        if Utils.is_empty(request.username):
            request.username = context.get_username()
        result = self.context.projects.get_user_projects(request)
        context.success(result)

    def enable_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(EnableProjectRequest)
        result = self.context.projects.enable_project(request)
        context.success(result)

    def disable_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DisableProjectRequest)
        result = self.context.projects.disable_project(request)
        context.success(result)

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace

        acl_entry = Utils.get_value_as_dict(namespace, self.acl)
        if acl_entry is None:
            raise exceptions.unauthorized_access()

        acl_entry_scope = Utils.get_value_as_string('scope', acl_entry)
        is_authorized = context.is_authorized(
            elevated_access=True, scopes=[acl_entry_scope]
        )
        is_authenticated_user = context.is_authenticated_user()

        if is_authorized:
            acl_entry['method'](context)
            return

        if is_authenticated_user and namespace == 'Projects.GetUserProjects':
            # non-elevated callers must never reach admin_get_user_projects, which
            # honors a caller-supplied username. route to the self-scoped handler.
            self.get_user_projects(context)
            return

        if is_authenticated_user and namespace == 'Projects.GetProject':
            # non-elevated callers can only read projects they are a member of
            self.get_member_project(context)
            return

        raise exceptions.unauthorized_access()
