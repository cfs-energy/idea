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
from ideasdk.context import SocaContext
from boto3.dynamodb.conditions import Attr
import botocore.exceptions

DEFAULT_START_ID = 5000
KEY_USERS = 'users'
KEY_GROUPS = 'groups'


class SequenceConfigDAO:

    def __init__(self, context: SocaContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('sequence-config-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.accounts.sequence-config'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {
                        'AttributeName': 'key',
                        'AttributeType': 'S'
                    }
                ],
                'KeySchema': [
                    {
                        'AttributeName': 'key',
                        'KeyType': 'HASH'
                    }
                ],
                'BillingMode': 'PAY_PER_REQUEST'
            },
            wait=True
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

        self._init_key(KEY_USERS, self.context.config().get_int('directoryservice.start_uid', default=DEFAULT_START_ID))
        self._init_key(KEY_GROUPS, self.context.config().get_int('directoryservice.start_gid', default=DEFAULT_START_ID))

    def _init_key(self, key: str, value: int):
        try:
            self.table.put_item(
                Item={
                    'key': key,
                    'value': value
                },
                ConditionExpression=Attr('key').not_exists()
            )
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                pass
            else:
                raise e

    def _next(self, key: str) -> int:
        result = self.table.update_item(
            Key={
                'key': key
            },
            UpdateExpression='ADD #value :value',
            ExpressionAttributeNames={
                '#value': 'value'
            },
            ExpressionAttributeValues={
                ':value': 1
            },
            ReturnValues='ALL_OLD'
        )
        attributes = result['Attributes']
        return Utils.get_value_as_int('value', attributes)

    def next_uid(self) -> int:
        return self._next(KEY_USERS)

    def next_gid(self) -> int:
        return self._next(KEY_GROUPS)
