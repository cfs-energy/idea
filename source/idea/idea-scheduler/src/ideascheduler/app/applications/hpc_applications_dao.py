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

import ideascheduler
from ideasdk.utils import Utils

from typing import Dict, Optional
from boto3.dynamodb.conditions import Attr
import arrow
from ideadatamodel import (
    exceptions,
    ListHpcApplicationsRequest,
    ListHpcApplicationsResult,
    SocaPaginator,
    Project,
    HpcApplication,
    SocaUserInputModuleMetadata
)


class HpcApplicationsDAO:

    def __init__(self, context: ideascheduler.AppContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('applications-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.applications'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {
                        'AttributeName': 'application_id',
                        'AttributeType': 'S'
                    }
                ],
                'KeySchema': [
                    {
                        'AttributeName': 'application_id',
                        'KeyType': 'HASH'
                    }
                ],
                'BillingMode': 'PAY_PER_REQUEST'
            },
            wait=True
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    def convert_from_db(self, db_application: Dict, lite: bool = False) -> HpcApplication:

        application = HpcApplication()
        application.application_id = Utils.get_value_as_string('application_id', db_application)
        application.title = Utils.get_value_as_string('title', db_application)
        application.description = Utils.get_value_as_string('description', db_application)
        application.thumbnail_data = Utils.get_value_as_string('thumbnail_data', db_application)
        project_ids = Utils.get_value_as_list('project_ids', db_application)

        # get projects
        if project_ids is not None:
            projects = []
            for project_id in project_ids:
                project = self.context.projects_client.get_project_by_id(project_id)
                projects.append(project)
            projects.sort(key=lambda p: p.name)
            application.projects = projects
        application.created_on = arrow.get(Utils.get_value_as_int('created_on', db_application)).datetime
        application.updated_on = arrow.get(Utils.get_value_as_int('updated_on', db_application)).datetime

        if not lite:
            form_template = Utils.get_value_as_string('form_template', db_application)
            if form_template is not None:
                application.form_template = SocaUserInputModuleMetadata(**Utils.from_json(form_template))
            application.job_script_interpreter = Utils.get_value_as_string('job_script_interpreter', db_application)
            application.job_script_template = Utils.get_value_as_string('job_script_template', db_application)
            application.job_script_type = Utils.get_value_as_string('job_script_type', db_application)

        return application

    @staticmethod
    def convert_to_db(application: HpcApplication) -> Dict:
        db_application = {
            'application_id': application.application_id
        }

        if application.title is not None:
            db_application['title'] = application.title
        if application.description is not None:
            db_application['description'] = application.description
        if application.thumbnail_data is not None:
            db_application['thumbnail_data'] = application.thumbnail_data
        if application.form_template is not None:
            db_application['form_template'] = Utils.to_json(application.form_template)
        if application.job_script_interpreter is not None:
            db_application['job_script_interpreter'] = application.job_script_interpreter
        if application.job_script_template is not None:
            db_application['job_script_template'] = application.job_script_template
        if application.job_script_type is not None:
            db_application['job_script_type'] = application.job_script_type
        if application.projects is not None:
            project_ids = set()
            for project in application.projects:
                if Utils.is_empty(project.project_id):
                    continue
                project_ids.add(project.project_id)
            db_application['project_ids'] = list(project_ids)

        return db_application

    def create_application(self, application: Dict) -> Dict:
        created_application = {
            **application,
            'application_id': Utils.uuid(),
            'created_on': Utils.current_time_ms(),
            'updated_on': Utils.current_time_ms()
        }
        self.table.put_item(
            Item=created_application
        )

        return created_application

    def get_application(self, application_id: str) -> Optional[Dict]:

        if Utils.is_empty(application_id):
            raise exceptions.invalid_params('application_id is required')

        result = self.table.get_item(
            Key={
                'application_id': application_id
            }
        )
        return Utils.get_value_as_dict('Item', result)

    def update_application(self, application: Dict):

        application_id = Utils.get_value_as_string('application_id', application)
        if Utils.is_empty(application_id):
            raise exceptions.invalid_params('application_id is required')

        application['updated_on'] = Utils.current_time_ms()

        update_expression_tokens = []
        expression_attr_names = {}
        expression_attr_values = {}

        for key, value in application.items():
            if key in ('application_id', 'created_on'):
                continue
            update_expression_tokens.append(f'#{key} = :{key}')
            expression_attr_names[f'#{key}'] = key
            expression_attr_values[f':{key}'] = value

        result = self.table.update_item(
            Key={
                'application_id': application_id
            },
            ConditionExpression=Attr('application_id').eq(application_id),
            UpdateExpression='SET ' + ', '.join(update_expression_tokens),
            ExpressionAttributeNames=expression_attr_names,
            ExpressionAttributeValues=expression_attr_values,
            ReturnValues='ALL_NEW'
        )

        updated_application = result['Attributes']
        updated_application['application_id'] = application_id

        return updated_application

    def delete_application(self, application_id: str):

        if Utils.is_empty(application_id):
            raise exceptions.invalid_params('application_id is required')

        self.table.delete_item(
            Key={
                'application_id': application_id
            }
        )

    def list_applications(self, request: ListHpcApplicationsRequest) -> ListHpcApplicationsResult:
        scan_request = {}

        cursor = request.cursor
        last_evaluated_key = None
        if Utils.is_not_empty(cursor):
            last_evaluated_key = Utils.from_json(Utils.base64_decode(cursor))
        if last_evaluated_key is not None:
            scan_request['LastEvaluatedKey'] = last_evaluated_key

        scan_filter = None
        if Utils.is_not_empty(request.filters):
            scan_filter = {}
            for filter_ in request.filters:
                if filter_.eq is not None:
                    scan_filter[filter_.key] = {
                        'AttributeValueList': [filter_.eq],
                        'ComparisonOperator': 'EQ'
                    }
                if filter_.like is not None:
                    scan_filter[filter_.key] = {
                        'AttributeValueList': [filter_.like],
                        'ComparisonOperator': 'CONTAINS'
                    }
        if scan_filter is not None:
            scan_request['ScanFilter'] = scan_filter

        scan_result = self.table.scan(**scan_request)

        db_applications = Utils.get_value_as_list('Items', scan_result, [])
        applications = []
        lite = Utils.get_as_bool(request.lite, False)
        for db_application in db_applications:
            application = self.convert_from_db(db_application, lite=lite)
            applications.append(application)

        response_cursor = None
        last_evaluated_key = Utils.get_any_value('LastEvaluatedKey', scan_result)
        if last_evaluated_key is not None:
            response_cursor = Utils.base64_encode(Utils.to_json(last_evaluated_key))

        return ListHpcApplicationsResult(
            listing=applications,
            paginator=SocaPaginator(
                cursor=response_cursor
            )
        )
