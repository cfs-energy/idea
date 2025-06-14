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

from typing import Dict


class ADAutomationDAO:
    def __init__(self, context: SocaContext):
        self.context = context
        self.ad_automation_entry_ttl_seconds = context.config().get_int(
            'directoryservice.ad_automation.entry_ttl_seconds', default=30 * 60
        )
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.ad-automation'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'instance_id', 'AttributeType': 'S'},
                    {'AttributeName': 'nonce', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'instance_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'nonce', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
            ttl=True,
            ttl_attribute_name='ttl',
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    def create_ad_automation_entry(self, entry: Dict, ttl=None) -> Dict:
        instance_id = Utils.get_value_as_string('instance_id', entry)
        if Utils.is_empty(instance_id):
            raise exceptions.invalid_params('instance_id is required')

        nonce = Utils.get_value_as_string('nonce', entry)
        if Utils.is_empty(nonce):
            raise exceptions.invalid_params('nonce is required')

        status = Utils.get_value_as_string('status', entry)
        if Utils.is_empty(status):
            raise exceptions.invalid_params('status is required.')

        if status not in ('success', 'fail'):
            raise exceptions.invalid_params('status must be one of [success, fail]')

        # TTL is required to be in seconds for DynamoDB auto-expiration functionality
        if ttl is None:
            ttl = Utils.get_as_int(Utils.current_time()) + Utils.get_as_int(
                self.ad_automation_entry_ttl_seconds, default=1800
            )
            self.context.logger().debug(f'Setting AD-Automation entry TTL to {ttl}')

        created_entry = {
            **entry,
            'ttl': ttl,
            'created_on': Utils.current_time_ms(),
            'updated_on': Utils.current_time_ms(),
        }
        self.table.put_item(Item=created_entry)
        return created_entry

    def list_entries_by_instance_id(self, instance_id: str) -> list:
        """
        List all entries for a given instance_id
        :param instance_id: Instance ID to query
        :return: List of entries
        """
        if Utils.is_empty(instance_id):
            raise exceptions.invalid_params('instance_id is required')

        response = self.table.query(
            KeyConditionExpression='instance_id = :instance_id',
            ExpressionAttributeValues={':instance_id': instance_id},
        )

        return response.get('Items', [])

    def delete_entry(self, instance_id: str, nonce: str) -> bool:
        """
        Delete an entry from the table
        :param instance_id: Instance ID
        :param nonce: Nonce value
        :return: True if deleted successfully
        """
        if Utils.is_empty(instance_id):
            raise exceptions.invalid_params('instance_id is required')

        if Utils.is_empty(nonce):
            raise exceptions.invalid_params('nonce is required')

        response = self.table.delete_item(
            Key={'instance_id': instance_id, 'nonce': nonce}, ReturnValues='ALL_OLD'
        )

        return 'Attributes' in response
