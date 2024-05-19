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
    ListHpcLicenseResourcesRequest,
    ListHpcLicenseResourcesResult,
    SocaPaginator,
    HpcLicenseResource
)


class LicenseResourcesDAO:

    def __init__(self, context: ideascheduler.AppContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('license-resources-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.license-resources'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {
                        'AttributeName': 'name',
                        'AttributeType': 'S'
                    }
                ],
                'KeySchema': [
                    {
                        'AttributeName': 'name',
                        'KeyType': 'HASH'
                    }
                ],
                'BillingMode': 'PAY_PER_REQUEST'
            },
            wait=True
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    @staticmethod
    def convert_from_db(db_license_resource: Dict) -> HpcLicenseResource:
        license_resource = HpcLicenseResource()
        license_resource.name = Utils.get_value_as_string('name', db_license_resource)
        license_resource.title = Utils.get_value_as_string('title', db_license_resource)
        license_resource.availability_check_cmd = Utils.get_value_as_string('availability_check_cmd', db_license_resource)
        license_resource.available_count = Utils.get_value_as_int('available_count', db_license_resource)
        license_resource.reserved_count = Utils.get_value_as_int('reserved_count', db_license_resource)
        license_resource.created_on = arrow.get(Utils.get_value_as_int('created_on', db_license_resource)).datetime
        license_resource.updated_on = arrow.get(Utils.get_value_as_int('updated_on', db_license_resource)).datetime
        return license_resource

    @staticmethod
    def convert_to_db(license_resource: HpcLicenseResource) -> Dict:
        db_license_resource = {
            'name': license_resource.name
        }

        if license_resource.title is not None:
            db_license_resource['title'] = license_resource.title
        if license_resource.availability_check_cmd is not None:
            db_license_resource['availability_check_cmd'] = license_resource.availability_check_cmd
        if license_resource.reserved_count is not None:
            db_license_resource['reserved_count'] = license_resource.reserved_count

        return db_license_resource

    def create_license_resource(self, license_resource: Dict) -> Dict:
        name = Utils.get_value_as_string('name', license_resource)
        if Utils.is_empty(name):
            raise exceptions.invalid_params('name is required')
        created_license_resource = {
            **license_resource,
            'created_on': Utils.current_time_ms(),
            'updated_on': Utils.current_time_ms()
        }
        self.table.put_item(
            Item=created_license_resource
        )

        return created_license_resource

    def get_license_resource(self, name: str) -> Optional[Dict]:

        if Utils.is_empty(name):
            raise exceptions.invalid_params('name is required')

        result = self.table.get_item(
            Key={
                'name': name
            }
        )
        return Utils.get_value_as_dict('Item', result)

    def update_license_resource(self, license_resource: Dict):

        name = Utils.get_value_as_string('name', license_resource)
        if Utils.is_empty(name):
            raise exceptions.invalid_params('name is required')

        license_resource['updated_on'] = Utils.current_time_ms()

        update_expression_tokens = []
        expression_attr_names = {}
        expression_attr_values = {}

        for key, value in license_resource.items():
            if key in ('name', 'created_on'):
                continue
            update_expression_tokens.append(f'#{key} = :{key}')
            expression_attr_names[f'#{key}'] = key
            expression_attr_values[f':{key}'] = value

        result = self.table.update_item(
            Key={
                'name': name
            },
            ConditionExpression=Attr('name').eq(name),
            UpdateExpression='SET ' + ', '.join(update_expression_tokens),
            ExpressionAttributeNames=expression_attr_names,
            ExpressionAttributeValues=expression_attr_values,
            ReturnValues='ALL_NEW'
        )

        updated_license_resource = result['Attributes']
        updated_license_resource['name'] = name

        return updated_license_resource

    def delete_license_resource(self, name: str):

        if Utils.is_empty(name):
            raise exceptions.invalid_params('name is required')

        self.table.delete_item(
            Key={
                'name': name
            }
        )

    def list_license_resources(self, request: ListHpcLicenseResourcesRequest) -> ListHpcLicenseResourcesResult:
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

        db_license_resources = Utils.get_value_as_list('Items', scan_result, [])
        license_resources = []
        for db_license_resource in db_license_resources:
            license_resource = self.convert_from_db(db_license_resource)
            license_resources.append(license_resource)

        response_cursor = None
        last_evaluated_key = Utils.get_any_value('LastEvaluatedKey', scan_result)
        if last_evaluated_key is not None:
            response_cursor = Utils.base64_encode(Utils.to_json(last_evaluated_key))

        return ListHpcLicenseResourcesResult(
            listing=license_resources,
            paginator=SocaPaginator(
                cursor=response_cursor
            )
        )
