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

"""
Durable record of scratch cleanup: the audit log of every removal, run history, folders in
their notice period, and the lease that keeps one run going at a time.
"""

from typing import Dict, List, Optional, Tuple
from decimal import Decimal
import time

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

LEASE_SECONDS = 300
RUN_RETENTION_SECONDS = 400 * 24 * 3600


class LeaseLost(Exception):
    pass


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _storable(item: Dict) -> Dict:
    stored = {}
    for key, value in item.items():
        if value is None:
            continue
        stored[key] = Decimal(str(value)) if isinstance(value, float) else value
    return stored


class ScratchCleanupStore:
    def __init__(self, context):
        self.context = context
        self.table = None

    def table_name(self) -> str:
        return (
            f'{self.context.cluster_name()}.{self.context.module_id()}.scratch-cleanup'
        )

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.table_name(),
                'AttributeDefinitions': [
                    {'AttributeName': 'pk', 'AttributeType': 'S'},
                    {'AttributeName': 'sk', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'pk', 'KeyType': 'HASH'},
                    {'AttributeName': 'sk', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
            ttl=True,
            ttl_attribute_name='expires_at',
        )
        self.table = self.context.aws().dynamodb_table().Table(self.table_name())

    # lease
    def acquire_lease(self, holder: str) -> bool:
        now = int(time.time())
        try:
            self.table.put_item(
                Item={
                    'pk': 'lease',
                    'sk': 'run',
                    'holder': holder,
                    'lease_until': now + LEASE_SECONDS,
                },
                ConditionExpression=(
                    Attr('pk').not_exists()
                    | Attr('lease_until').lt(now)
                    | Attr('holder').eq(holder)
                ),
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return False
            raise

    def renew_lease(self, holder: str):
        try:
            self.table.update_item(
                Key={'pk': 'lease', 'sk': 'run'},
                UpdateExpression='SET lease_until = :until',
                ConditionExpression=Attr('holder').eq(holder),
                ExpressionAttributeValues={':until': int(time.time()) + LEASE_SECONDS},
            )
        except ClientError as e:
            raise LeaseLost(str(e)) from e

    def release_lease(self, holder: str):
        try:
            self.table.delete_item(
                Key={'pk': 'lease', 'sk': 'run'},
                ConditionExpression=Attr('holder').eq(holder),
            )
        except ClientError:
            # it expires by itself
            pass

    # runs
    def put_run(self, run: Dict):
        self.table.put_item(
            Item=_storable(
                {
                    **run,
                    'pk': 'run',
                    'sk': f'{run["started_at"]}#{run["run_id"]}',
                    'expires_at': int(time.time()) + RUN_RETENTION_SECONDS,
                }
            )
        )

    def list_runs(self, limit: int = 20) -> List[Dict]:
        result = self.table.query(
            KeyConditionExpression=Key('pk').eq('run'),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [
            {
                k: v
                for k, v in _plain(item).items()
                if k not in ('pk', 'sk', 'expires_at')
            }
            for item in result.get('Items', [])
        ]

    # audit
    def put_audit(self, row: Dict):
        self.table.put_item(
            Item=_storable(
                {**row, 'pk': 'audit', 'sk': f'{row["started_at"]}#{row["path"]}'}
            )
        )

    def list_audit(
        self, limit: int = 50, start_key: Optional[Dict] = None
    ) -> Tuple[List[Dict], Optional[Dict]]:
        arguments = {
            'KeyConditionExpression': Key('pk').eq('audit'),
            'ScanIndexForward': False,
            'Limit': limit,
        }
        if start_key:
            arguments['ExclusiveStartKey'] = start_key
        result = self.table.query(**arguments)
        return [_plain(item) for item in result.get('Items', [])], result.get(
            'LastEvaluatedKey'
        )

    def unfinished_removals(self) -> Dict[Tuple[int, int], Dict]:
        """removals that never ended and whose folder may be there, by folder identity"""
        rows = {}
        arguments = {
            'KeyConditionExpression': Key('pk').eq('audit'),
            'FilterExpression': Attr('status').is_in(['started', 'failed']),
        }
        while True:
            result = self.table.query(**arguments)
            for item in result.get('Items', []):
                row = _plain(item)
                rows[(row['device'], row['inode'])] = row
            if 'LastEvaluatedKey' not in result:
                return rows
            arguments['ExclusiveStartKey'] = result['LastEvaluatedKey']

    # what a finished job left behind: removals waiting for their date
    def _list(self, partition: str) -> List[Dict]:
        rows = []
        arguments = {'KeyConditionExpression': Key('pk').eq(partition)}
        while True:
            result = self.table.query(**arguments)
            rows.extend(_plain(item) for item in result.get('Items', []))
            if 'LastEvaluatedKey' not in result:
                return rows
            arguments['ExclusiveStartKey'] = result['LastEvaluatedKey']

    def _put(self, partition: str, key: str, row: Dict):
        self.table.put_item(Item=_storable({**row, 'pk': partition, 'sk': key}))

    def _get(self, partition: str, key: str) -> Optional[Dict]:
        item = self.table.get_item(
            Key={'pk': partition, 'sk': key}, ConsistentRead=True
        ).get('Item')
        return _plain(item) if item else None

    # keyed by date so the hourly check and the page read only what they need, with a small row
    # beside each that finds it again by job
    @staticmethod
    def _removal_sk(key: str, row: Dict) -> str:
        return f'{row["remove_at"]}#{key}'

    def has_removal(self, key: str) -> bool:
        return self._get('removal-key', key) is not None

    def get_removal(self, key: str) -> Optional[Dict]:
        pointer = self._get('removal-key', key)
        return self._get('removal', pointer['removal_sk']) if pointer else None

    def put_removal(self, key: str, row: Dict):
        pointer = self._get('removal-key', key)
        sk = self._removal_sk(key, row)
        self._put('removal', sk, {**row, 'key': key})
        self._put('removal-key', key, {'removal_sk': sk})
        if pointer and pointer['removal_sk'] != sk:
            self.table.delete_item(Key={'pk': 'removal', 'sk': pointer['removal_sk']})

    def delete_removal(self, key: str):
        pointer = self._get('removal-key', key)
        if pointer:
            self.table.delete_item(Key={'pk': 'removal', 'sk': pointer['removal_sk']})
            self.table.delete_item(Key={'pk': 'removal-key', 'sk': key})

    def list_due(self, before: str, limit: int) -> List[Dict]:
        """the soonest removals up to a date, oldest first, never more than the limit"""
        rows: List[Dict] = []
        arguments = {
            'KeyConditionExpression': Key('pk').eq('removal')
            & Key('sk').lt(f'{before}~'),
            'Limit': limit,
        }
        while len(rows) < limit:
            result = self.table.query(**arguments)
            rows.extend(_plain(item) for item in result.get('Items', []))
            if 'LastEvaluatedKey' not in result:
                break
            arguments['ExclusiveStartKey'] = result['LastEvaluatedKey']
        return rows[:limit]

    def page_removals(
        self, limit: int, start_key: Optional[Dict] = None
    ) -> Tuple[List[Dict], Optional[Dict]]:
        arguments = {'KeyConditionExpression': Key('pk').eq('removal'), 'Limit': limit}
        if start_key:
            arguments['ExclusiveStartKey'] = start_key
        result = self.table.query(**arguments)
        return [_plain(item) for item in result.get('Items', [])], result.get(
            'LastEvaluatedKey'
        )

    def count_removals(self, outcome: Optional[str] = None) -> int:
        """a count, which keeps no rows"""
        total = 0
        arguments = {
            'KeyConditionExpression': Key('pk').eq('removal'),
            'Select': 'COUNT',
        }
        if outcome:
            arguments['FilterExpression'] = Attr('outcome').eq(outcome)
        while True:
            result = self.table.query(**arguments)
            total += result.get('Count', 0)
            if 'LastEvaluatedKey' not in result:
                return total
            arguments['ExclusiveStartKey'] = result['LastEvaluatedKey']

    # what an owner's next digest says went
    def list_removed(self) -> List[Dict]:
        return self._list('removed')

    def put_removed(self, key: str, row: Dict):
        self._put('removed', key, row)

    def delete_removed(self, key: str):
        self.table.delete_item(Key={'pk': 'removed', 'sk': key})

    # what the administrators' next summary says went wrong
    def list_failures(self) -> List[Dict]:
        return self._list('failure')

    def put_failure(self, key: str, row: Dict):
        self._put('failure', key[:300], row)

    def delete_failure(self, key: str):
        self.table.delete_item(Key={'pk': 'failure', 'sk': key})

    def release_alert(self, reason: str):
        self.table.delete_item(Key={'pk': 'alert', 'sk': reason[:200]})

    # once in a window, whichever scheduler gets there first
    def should_alert(self, reason: str, every_seconds: int = 86400) -> bool:
        now = int(time.time())
        try:
            self.table.put_item(
                Item={
                    'pk': 'alert',
                    'sk': reason[:200],
                    'sent_at': now,
                    'expires_at': now + every_seconds * 2,
                },
                ConditionExpression=(
                    Attr('pk').not_exists() | Attr('sent_at').lt(now - every_seconds)
                ),
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return False
            raise
