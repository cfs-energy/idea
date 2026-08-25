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

__all__ = (
    'BedrockUsageDAO',
    'BedrockInstanceOwnerDAO',
    'build_day_key',
    'build_user_key',
    'build_day_job_key',
    'build_job_key',
    'build_project_key',
    'UNATTRIBUTED_USER',
)

from ideasdk.utils import Utils
from ideasdk.context import SocaContext

from boto3.dynamodb.conditions import Key
from typing import Dict, Iterable, List, Optional

DAY_PREFIX = 'day#'
USER_PREFIX = 'user#'
# a job is a separate dimension, not a variant of the user rows: two jobs by one user on one
# model in one day would otherwise collide.
DAY_JOB_PREFIX = 'dayjob#'
JOB_PREFIX = 'job#'
PROJECT_PREFIX = 'project#'

# parentheses are not valid in a directory service account name, so this bucket
# can never collide with a real user.
UNATTRIBUTED_USER = '(unattributed)'

# highest code point, so a between() upper bound sorts after every real suffix.
_MAX_SUFFIX = '\uffff'


def build_day_key(usage_date: str, username: str, model_id: str) -> str:
    return f'{DAY_PREFIX}{usage_date}#{username}#{model_id}'


def build_user_key(period: str, username: str) -> str:
    return f'{USER_PREFIX}{period}#{username}'


def build_day_job_key(usage_date: str, job_id: str, model_id: str) -> str:
    return f'{DAY_JOB_PREFIX}{usage_date}#{job_id}#{model_id}'


def build_job_key(period: str, job_id: str) -> str:
    return f'{JOB_PREFIX}{period}#{job_id}'


def build_project_key(period: str) -> str:
    return f'{PROJECT_PREFIX}{period}'


class BedrockUsageDAO:
    """
    per project bedrock usage rows. one row per day, user and model, plus month
    rollups per user and per project so the read path is a single query.
    """

    def __init__(self, context: SocaContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('bedrock-usage-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.bedrock.usage'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'project_id', 'AttributeType': 'S'},
                    {'AttributeName': 'usage_id', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'project_id', 'KeyType': 'HASH'},
                    {'AttributeName': 'usage_id', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
            ttl=True,
            ttl_attribute_name='ttl',
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    def put_rows(self, rows: List[Dict]):
        if len(rows) == 0:
            return
        with self.table.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=row)

    def delete_rows(self, project_id: str, usage_ids: Iterable[str]):
        usage_ids = list(usage_ids)
        if len(usage_ids) == 0:
            return
        with self.table.batch_writer() as batch:
            for usage_id in usage_ids:
                batch.delete_item(Key={'project_id': project_id, 'usage_id': usage_id})

    def _query(self, project_id: str, sort_condition) -> List[Dict]:
        # the partition key is applied here so no caller can read across projects
        items = []
        kwargs = {
            'KeyConditionExpression': Key('project_id').eq(project_id) & sort_condition
        }
        while True:
            result = self.table.query(**kwargs)
            items += Utils.get_value_as_list('Items', result, [])
            last_evaluated_key = Utils.get_any_value('LastEvaluatedKey', result)
            if last_evaluated_key is None:
                return items
            kwargs['ExclusiveStartKey'] = last_evaluated_key

    def query_day_rows(
        self, project_id: str, start_date: str, end_date: str
    ) -> List[Dict]:
        return self._query(
            project_id,
            Key('usage_id').between(
                f'{DAY_PREFIX}{start_date}#',
                f'{DAY_PREFIX}{end_date}#{_MAX_SUFFIX}',
            ),
        )

    def query_day_job_rows(
        self, project_id: str, start_date: str, end_date: str
    ) -> List[Dict]:
        return self._query(
            project_id,
            Key('usage_id').between(
                f'{DAY_JOB_PREFIX}{start_date}#',
                f'{DAY_JOB_PREFIX}{end_date}#{_MAX_SUFFIX}',
            ),
        )

    def query_user_rollups(self, project_id: str, period: str) -> List[Dict]:
        return self._query(
            project_id, Key('usage_id').begins_with(build_user_key(period, ''))
        )

    def query_job_rollups(self, project_id: str, period: str) -> List[Dict]:
        return self._query(
            project_id, Key('usage_id').begins_with(build_job_key(period, ''))
        )

    def get_project_rollup(self, project_id: str, period: str) -> Optional[Dict]:
        result = self.table.get_item(
            Key={'project_id': project_id, 'usage_id': build_project_key(period)}
        )
        return Utils.get_value_as_dict('Item', result)

    def get_user_rollup(
        self, project_id: str, period: str, username: str
    ) -> Optional[Dict]:
        result = self.table.get_item(
            Key={
                'project_id': project_id,
                'usage_id': build_user_key(period, username),
            }
        )
        return Utils.get_value_as_dict('Item', result)


class BedrockInstanceOwnerDAO:
    """
    instance id to owner cache. an ec2 instance stops being describable shortly
    after termination, so the mapping is kept for as long as usage is recomputed.
    """

    def __init__(self, context: SocaContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('bedrock-instance-owner-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.bedrock.instances'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'instance_id', 'AttributeType': 'S'}
                ],
                'KeySchema': [{'AttributeName': 'instance_id', 'KeyType': 'HASH'}],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
            ttl=True,
            ttl_attribute_name='ttl',
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    def get_owners(self, instance_ids: List[str]) -> Dict[str, str]:
        return {
            instance_id: entry[0]
            for instance_id, entry in self.get_attribution(instance_ids).items()
            if Utils.is_not_empty(entry[0])
        }

    def get_attribution(self, instance_ids: List[str]) -> Dict[str, tuple]:
        """(username, job_id) per instance. job_id is empty for anything that is not a job host."""
        found = {}
        for instance_id in instance_ids:
            result = self.table.get_item(Key={'instance_id': instance_id})
            item = Utils.get_value_as_dict('Item', result)
            if item is None:
                continue
            username = Utils.get_value_as_string('username', item)
            job_id = Utils.get_value_as_string('job_id', item, '')
            if Utils.is_not_empty(username) or Utils.is_not_empty(job_id):
                found[instance_id] = (username, job_id)
        return found

    def put_owner(
        self, instance_id: str, username: str, ttl_seconds: int, job_id: str = None
    ):
        item = {
            'instance_id': instance_id,
            'username': username,
            'updated_on': Utils.current_time_ms(),
            'ttl': Utils.current_time() + ttl_seconds,
        }
        if Utils.is_not_empty(job_id):
            item['job_id'] = job_id
        self.table.put_item(Item=item)
