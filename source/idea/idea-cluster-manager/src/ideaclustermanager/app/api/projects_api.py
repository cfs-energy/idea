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
    GetUserProjectsRequest
)
from ideadatamodel import exceptions
from ideasdk.utils import Utils


class ProjectsAPI(BaseAPI):

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context

        self.SCOPE_WRITE = f'{self.context.module_id()}/write'
        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            'Projects.CreateProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.create_project
            },
            'Projects.GetProject': {
                'scope': self.SCOPE_READ,
                'method': self.get_project
            },
            'Projects.UpdateProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.update_project
            },
            'Projects.ListProjects': {
                'scope': self.SCOPE_READ,
                'method': self.list_projects
            },
            'Projects.GetUserProjects': {
                'scope': self.SCOPE_READ,
                'method': self.admin_get_user_projects
            },
            'Projects.EnableProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.enable_project
            },
            'Projects.DisableProject': {
                'scope': self.SCOPE_WRITE,
                'method': self.disable_project
            }
        }

    def create_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateProjectRequest)
        result = self.context.projects.create_project(request)
        context.success(result)

    def get_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetProjectRequest)
        result = self.context.projects.get_project(request)
        project = result.project
        if project.is_budgets_enabled():
            budget = self.context.aws_util().budgets_get_budget(budget_name=project.budget.budget_name)
            project.budget = budget
        context.success(result)

    def update_project(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateProjectRequest)
        result = self.context.projects.update_project(request)
        context.success(result)

    def list_projects(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListProjectsRequest)
        result = self.context.projects.list_projects(request)
        for project in result.listing:
            if project.is_budgets_enabled():
                # this call could possibly make some performance degradations, if the configured budget is not available.
                # need to optimize this further.
                budget = self.context.aws_util().budgets_get_budget(budget_name=project.budget.budget_name)
                project.budget = budget
        context.success(result)

    def get_user_projects(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserProjectsRequest)
        request.username = context.get_username()
        result = self.context.projects.get_user_projects(request)
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
        is_authorized = context.is_authorized(elevated_access=True, scopes=[acl_entry_scope])
        is_authenticated_user = context.is_authenticated_user()

        if is_authorized:
            acl_entry['method'](context)
            return

        if is_authenticated_user and namespace in ('Projects.GetUserProjects', 'Projects.GetProject'):
            acl_entry['method'](context)
            return

        raise exceptions.unauthorized_access()
