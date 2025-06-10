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

from ideadatamodel import exceptions
from ideadatamodel.scheduler import (
    CreateHpcApplicationRequest,
    CreateHpcApplicationResult,
    UpdateHpcApplicationRequest,
    UpdateHpcApplicationResult,
    GetHpcApplicationRequest,
    GetHpcApplicationResult,
    DeleteHpcApplicationRequest,
    DeleteHpcApplicationResult,
    ListHpcApplicationsRequest,
    ListHpcApplicationsResult,
    GetUserApplicationsRequest,
    GetUserApplicationsResult,
    HpcApplication,
)
from ideasdk.utils import Utils

import ideascheduler
from ideascheduler.app.applications.hpc_applications_dao import HpcApplicationsDAO
from ideascheduler.app.app_protocols import HpcApplicationsProtocol

from typing import Dict


class HpcApplicationsService(HpcApplicationsProtocol):
    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self._logger = context.logger('applications')

        self.applications_dao = HpcApplicationsDAO(context)
        self.table_created = self.applications_dao.initialize()

    @staticmethod
    def validate_and_sanitize(application: HpcApplication):
        if Utils.is_empty(application.job_script_type):
            application.job_script_type = 'default'
            return
        job_script_type = application.job_script_type.strip().lower()
        if job_script_type not in ('default', 'jinja2'):
            raise exceptions.invalid_params(
                'job_script_type must be one of [default, jinja2]'
            )

        application.job_script_type = job_script_type

        job_script_interpreter = application.job_script_interpreter
        if Utils.is_empty(job_script_interpreter):
            raise exceptions.invalid_params('job_script_interpreter is required.')
        if job_script_interpreter not in ('pbs', 'bash'):
            raise exceptions.invalid_params(
                'job_script_interpreter must be one of [pbs, bash]'
            )

    def create_application(
        self, request: CreateHpcApplicationRequest
    ) -> CreateHpcApplicationResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        application = request.application
        if Utils.is_empty(application):
            raise exceptions.invalid_params('application is required')
        if Utils.is_empty(application.title):
            raise exceptions.invalid_params('application.title is required')

        self.validate_and_sanitize(application)

        db_application = self.applications_dao.convert_to_db(application)
        db_created = self.applications_dao.create_application(db_application)

        created = self.applications_dao.convert_from_db(db_created)
        return CreateHpcApplicationResult(application=created)

    def get_application(
        self, request: GetHpcApplicationRequest
    ) -> GetHpcApplicationResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.is_empty(request.application_id):
            raise exceptions.invalid_params('application_id is required')
        db_application = self.applications_dao.get_application(request.application_id)
        if db_application is None:
            raise exceptions.invalid_params(
                f'application not found for application id: {request.application_id}'
            )
        return GetHpcApplicationResult(
            application=self.applications_dao.convert_from_db(db_application)
        )

    def update_application(
        self, request: UpdateHpcApplicationRequest
    ) -> UpdateHpcApplicationResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        application = request.application
        if Utils.is_empty(application):
            raise exceptions.invalid_params('application is required')

        self.validate_and_sanitize(application)

        db_application = self.applications_dao.convert_to_db(application)
        db_updated = self.applications_dao.update_application(db_application)

        updated = self.applications_dao.convert_from_db(db_updated)
        return UpdateHpcApplicationResult(application=updated)

    def delete_application(
        self, request: DeleteHpcApplicationRequest
    ) -> DeleteHpcApplicationResult:
        if Utils.is_empty(request):
            raise exceptions.invalid_params('request is required')
        if Utils.is_empty(request.application_id):
            raise exceptions.invalid_params('application_id is required')
        self.applications_dao.delete_application(application_id=request.application_id)
        return DeleteHpcApplicationResult()

    def list_applications(
        self, request: ListHpcApplicationsRequest
    ) -> ListHpcApplicationsResult:
        return self.applications_dao.list_applications(request)

    def get_user_applications(
        self, request: GetUserApplicationsRequest
    ) -> GetUserApplicationsResult:
        username = request.username
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        user_projects = self.context.projects_client.get_user_projects(username)
        user_project_ids = set()
        for project in user_projects:
            user_project_ids.add(project.project_id)

        db_user_applications = []

        def check_and_add(db_app: Dict):
            application_project_ids = set(
                Utils.get_value_as_list('project_ids', db_app, [])
            )
            applicable_project_ids = user_project_ids & application_project_ids
            if len(applicable_project_ids) > 0:
                db_app['project_ids'] = list(applicable_project_ids)
                db_user_applications.append(db_app)

        if Utils.is_not_empty(request.application_ids):
            requested_application_ids = set(request.application_ids)
            for application_id in requested_application_ids:
                db_application = self.applications_dao.get_application(application_id)
                check_and_add(db_application)
        else:
            last_evaluated_key = None
            while True:
                if last_evaluated_key is None:
                    scan_result = self.applications_dao.table.scan()
                else:
                    scan_result = self.applications_dao.table.scan(
                        LastEvaluatedKey=last_evaluated_key
                    )
                db_applications = Utils.get_value_as_list('Items', scan_result, [])
                for db_application in db_applications:
                    check_and_add(db_application)

                last_evaluated_key = Utils.get_any_value(
                    'LastEvaluatedKey', scan_result
                )
                if last_evaluated_key is None:
                    break

        user_applications = []
        for db_application in db_user_applications:
            user_applications.append(
                self.applications_dao.convert_from_db(db_application)
            )

        return GetUserApplicationsResult(applications=user_applications)
