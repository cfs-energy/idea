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

from ideadatamodel import exceptions, constants, errorcodes
from ideasdk.utils import Utils, ModuleMetadataHelper
from ideasdk.config.soca_config import SocaConfig
from ideasdk.dynamodb.dynamodb_stream_subscriber import DynamoDBStreamSubscriber
from ideasdk.dynamodb.dynamodb_stream_subscription import DynamoDBStreamSubscription
from ideasdk.aws import AwsClientProvider, AWSClientProviderOptions

import time
from typing import Optional, Dict, List, Any
import botocore.exceptions
import re
from decimal import Decimal
import traceback

SUPPORTED_MODULE_TYPES = ('app', 'stack', 'config')

SHARD_ITERATOR_INITIALIZER_INTERVAL = (10, 30)
SHARD_PROCESSOR_INTERVAL = (10, 30)


class ClusterConfigDB(DynamoDBStreamSubscriber):
    """
    Cluster Configuration DB

    Note:
        * Chicken and Egg problem with SocaContext and AwsUtil when it comes to creating tables.
        * implementation from AwsUtil.dynamodb_create_table() cannot be used in this class and tables must be created manually
    """

    def __init__(
        self,
        cluster_name: str,
        aws_region: str,
        aws_profile: Optional[str],
        dynamodb_kms_key_id: Optional[str] = None,
        create_database: bool = False,
        create_subscription: bool = False,
        cluster_config_subscriber: Optional[DynamoDBStreamSubscriber] = None,
        logger=None,
    ):
        self.logger = logger

        if Utils.is_empty(cluster_name):
            raise exceptions.cluster_config_error('cluster_name is required')
        if len(cluster_name) > 11:
            raise exceptions.cluster_config_error(
                f'cluster_name: {cluster_name} cannot be more than 11 characters. current: {len(cluster_name)}'
            )

        if Utils.is_empty(aws_region):
            raise exceptions.cluster_config_error('aws_region is required')

        self.cluster_name = cluster_name
        self.aws_region = aws_region
        self.aws_profile = aws_profile
        self.create_database = create_database
        self.create_subscription = create_subscription
        self.stream_subscriber = cluster_config_subscriber
        self.dynamodb_kms_key_id = dynamodb_kms_key_id
        self.module_metadata_helper = ModuleMetadataHelper()

        self.aws = AwsClientProvider(
            options=AWSClientProviderOptions(
                region=self.aws_region, profile=self.aws_profile
            )
        )

        if not self.create_database:
            self.check_table_created()

        self.modules_table = self.get_or_create_modules_table()
        self.cluster_settings_table = self.get_or_create_cluster_settings_table()

        self.stream_subscription: Optional[DynamoDBStreamSubscription] = None
        if self.create_subscription and self.stream_subscriber is not None:
            self.stream_subscription = DynamoDBStreamSubscription(
                stream_subscriber=self,
                table_name=self.get_cluster_settings_table_name(),
                aws_region=aws_region,
                aws_profile=aws_profile,
            )

    def set_logger(self, logger):
        self.logger = logger
        if self.stream_subscription is not None:
            self.stream_subscription.set_logger(logger)

    def log_info(self, message: str):
        if self.logger is not None:
            self.logger.info(message)
        else:
            print(message)

    def log_exception(self, message: str):
        if self.logger is not None:
            self.logger.exception(message)
        else:
            print(message)
            traceback.print_exc()

    def log_debug(self, message: str):
        if self.logger is not None:
            self.logger.debug(message)

    def get_modules_table_name(self) -> str:
        return f'{self.cluster_name}.modules'

    def get_cluster_settings_table_name(self) -> str:
        return f'{self.cluster_name}.cluster-settings'

    def check_table_created(self) -> bool:
        try:
            modules_table_name = self.get_modules_table_name()
            self.aws.dynamodb().describe_table(TableName=modules_table_name)
            cluster_settings_table_name = self.get_cluster_settings_table_name()
            self.aws.dynamodb().describe_table(TableName=cluster_settings_table_name)
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                raise exceptions.SocaException(
                    error_code=errorcodes.CLUSTER_CONFIG_NOT_INITIALIZED,
                    message=f'Configuration tables not found for cluster: {self.cluster_name}, region: {self.aws_region}',
                )
            else:
                raise e

    def get_or_create_modules_table(self):
        table_name = self.get_modules_table_name()
        try:
            created = False
            while not created:
                describe_table_result = self.aws.dynamodb().describe_table(
                    TableName=table_name
                )
                table = describe_table_result['Table']
                created = table['TableStatus'] == 'ACTIVE'
                if not created:
                    time.sleep(2)

            return self.aws.dynamodb_table().Table(table_name)

        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                sse_specification = {}
                if self.dynamodb_kms_key_id is not None:
                    self.log_info(
                        f'detected cluster.dynamodb.kms_key_id is set to: {self.dynamodb_kms_key_id}'
                    )
                    sse_specification = {
                        'Enabled': True,
                        'SSEType': 'KMS',
                        'KMSMasterKeyId': self.dynamodb_kms_key_id,
                    }
                if self.create_database:
                    self.log_info(
                        f'creating cluster config dynamodb table: {table_name}, region: {self.aws_region}'
                    )
                    self.aws.dynamodb().create_table(
                        TableName=table_name,
                        AttributeDefinitions=[
                            {'AttributeName': 'module_id', 'AttributeType': 'S'}
                        ],
                        KeySchema=[{'AttributeName': 'module_id', 'KeyType': 'HASH'}],
                        BillingMode='PAY_PER_REQUEST',
                        SSESpecification=sse_specification,
                        Tags=[
                            {
                                'Key': constants.IDEA_TAG_CLUSTER_NAME,
                                'Value': self.cluster_name,
                            },
                            {
                                'Key': constants.IDEA_TAG_BACKUP_PLAN,
                                'Value': f'{self.cluster_name}-{constants.MODULE_CLUSTER}',
                            },
                        ],
                    )
                    return self.get_or_create_modules_table()
                else:
                    raise e
            else:
                raise e

    def get_or_create_cluster_settings_table(self):
        table_name = self.get_cluster_settings_table_name()

        try:
            created = False
            while not created:
                describe_table_result = self.aws.dynamodb().describe_table(
                    TableName=table_name
                )
                table = describe_table_result['Table']
                created = table['TableStatus'] == 'ACTIVE'
                if not created:
                    time.sleep(2)

            return self.aws.dynamodb_table().Table(table_name)

        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                sse_specification = {}
                if self.dynamodb_kms_key_id is not None:
                    sse_specification = {
                        'Enabled': True,
                        'SSEType': 'KMS',
                        'KMSMasterKeyId': self.dynamodb_kms_key_id,
                    }
                if self.create_database:
                    self.log_info(
                        f'creating cluster config dynamodb table: {table_name}, region: {self.aws_region}'
                    )
                    self.aws.dynamodb().create_table(
                        TableName=table_name,
                        AttributeDefinitions=[
                            {'AttributeName': 'key', 'AttributeType': 'S'}
                        ],
                        KeySchema=[{'AttributeName': 'key', 'KeyType': 'HASH'}],
                        BillingMode='PAY_PER_REQUEST',
                        StreamSpecification={
                            'StreamEnabled': True,
                            'StreamViewType': 'NEW_AND_OLD_IMAGES',
                        },
                        SSESpecification=sse_specification,
                        Tags=[
                            {
                                'Key': constants.IDEA_TAG_CLUSTER_NAME,
                                'Value': self.cluster_name,
                            }
                        ],
                    )
                    return self.get_or_create_cluster_settings_table()
                else:
                    raise e
            else:
                raise e

    def sync_cluster_settings_in_db(
        self, config_entries: List[Dict], overwrite: bool = False
    ):
        self.log_info(f'sync config entries to db. overwrite: {overwrite}')

        for entry in config_entries:
            key = entry['key']
            value = entry['value']
            db_result = self.cluster_settings_table.get_item(Key={'key': key})
            db_item = Utils.get_value_as_dict('Item', db_result)

            if not overwrite:
                if db_item is not None:
                    self.log_info(f'entry already exists for key: {key}, skip.')
                    continue

            self.set_config_entry(key, value)

    def sync_modules_in_db(self, modules: List[Dict]):
        """
        sync modules in db. does not support force updates.
        :param modules:
        :return:
        """
        self.log_info('sync modules in db ...')

        modules_to_create = []
        for module in modules:
            module_id = module['id']
            module_name = module['name']
            module_type = module['type']
            db_result = self.modules_table.get_item(Key={'module_id': module_id})
            db_item = Utils.get_value_as_dict('Item', db_result)
            if db_item is not None:
                self.log_info(
                    f'module: {module_id}, name: {module_name} already exists. skip.'
                )
                continue

            if module_type not in SUPPORTED_MODULE_TYPES:
                raise exceptions.invalid_params(
                    f'invalid type: {module_type} for module_id: {module_id}. '
                    f'supported module types: {SUPPORTED_MODULE_TYPES}'
                )
            modules_to_create.append(module)

        for module in modules_to_create:
            module_id = module['id']
            module_name = module['name']
            module_type = module['type']

            if module_type == 'config':
                status = 'deployed'
            else:
                status = 'not-deployed'

            self.log_info(
                f'creating module entry for module: {module_name}, module_id: {module_id}'
            )
            self.modules_table.update_item(
                Key={'module_id': module_id},
                UpdateExpression='SET #name=:name, #status=:status, #stack_name=:stack_name, #version=:version, #type=:type',
                ExpressionAttributeNames={
                    '#name': 'name',
                    '#status': 'status',
                    '#stack_name': 'stack_name',
                    '#version': 'version',
                    '#type': 'type',
                },
                ExpressionAttributeValues={
                    ':name': module_name,
                    ':type': module_type,
                    ':status': status,
                    ':stack_name': None,
                    ':version': None,
                },
            )

    def build_config_from_db(self, query: str = None) -> SocaConfig:
        """
        build configuration by reading all entries from database
        if query is provided, all keys matching the query regex will be returned
        :param query:
        :return:
        """

        entries = self.get_config_entries(query=query)
        config = SocaConfig(config={})

        for entry in entries:
            key = entry['key']
            value = entry['value']
            config.put(key, value)

        return config

    def get_config_entry(self, key: str) -> Optional[Dict]:
        result = self.cluster_settings_table.get_item(Key={'key': key})
        return Utils.get_value_as_dict('Item', result)

    def get_config_entries(self, query: str = None) -> List[Dict]:
        """
        read configuration entries as a list of dict items
        if query is provided, all keys matching the query regex will be returned
        :return:
        """

        pattern = None
        if Utils.is_not_empty(query):
            try:
                pattern = re.compile(query)
            except Exception as e:
                raise exceptions.invalid_params(f'invalid search regex: {query} - {e}')

        self.log_debug(
            f'loading config entries from db for cluster: {self.cluster_name}, region: {self.aws_region}'
        )

        config_entries = []

        total = 0
        read_capacity_units = 0
        last_evaluated_key = None
        while True:
            if last_evaluated_key is not None:
                result = self.cluster_settings_table.scan(
                    LastEvaluatedKey=last_evaluated_key, ReturnConsumedCapacity='TOTAL'
                )
            else:
                result = self.cluster_settings_table.scan(
                    ReturnConsumedCapacity='TOTAL'
                )

            consumed_capacity = Utils.get_value_as_dict('ConsumedCapacity', result)
            read_capacity_units += Utils.get_value_as_int(
                'CapacityUnits', consumed_capacity, 0
            )

            items = Utils.get_value_as_list('Items', result)
            if items is not None:
                for item in items:
                    key = Utils.get_value_as_string('key', item)

                    item = self.post_process_ddb_config_entry(item)

                    if pattern is not None:
                        if pattern.match(key):
                            config_entries.append(item)
                            total += 1
                    else:
                        config_entries.append(item)
                        total += 1

            last_evaluated_key = Utils.get_any_value('LastEvaluatedKey', result)
            if last_evaluated_key is None:
                break

        self.log_debug(
            f'loaded {total} entries, consumed read capacity units: {read_capacity_units}'
        )

        config_entries.sort(key=lambda entry: entry['key'])
        return config_entries

    def get_module_info(self, module_id: str) -> Optional[Dict]:
        result = self.modules_table.get_item(Key={'module_id': module_id})
        return Utils.get_value_as_dict('Item', result)

    def get_cluster_modules(self) -> List[Dict]:
        modules = []
        result = self.modules_table.scan()
        items = Utils.get_value_as_list('Items', result)
        if items is not None:
            for item in items:
                module_name = item['name']
                module_metadata = self.module_metadata_helper.get_module_metadata(
                    module_name=module_name
                )
                item = {
                    **item,
                    'title': module_metadata.title,
                    'deployment_priority': module_metadata.deployment_priority,
                }
                modules.append(item)
        return modules

    def delete_config_entries(self, config_key_prefix: str):
        if Utils.is_empty(config_key_prefix):
            raise exceptions.invalid_params('config_key_prefix is required')

        total = 0

        self.log_info(f'searching for config entries with prefix: {config_key_prefix}')
        config_entries_to_delete = []
        last_evaluated_key = None
        while True:
            if last_evaluated_key is not None:
                result = self.cluster_settings_table.scan(
                    LastEvaluatedKey=last_evaluated_key
                )
            else:
                result = self.cluster_settings_table.scan()

            items = Utils.get_value_as_list('Items', result)
            if items is not None:
                for item in items:
                    key = Utils.get_value_as_string('key', item)
                    if key.startswith(config_key_prefix):
                        config_entries_to_delete.append(item)
                        total += 1

            last_evaluated_key = Utils.get_any_value('LastEvaluatedKey', result)
            if last_evaluated_key is None:
                break

        if total > 0:
            self.log_info(f'found {total} config entries matching: {config_key_prefix}')
            for config_entry in config_entries_to_delete:
                key = config_entry['key']
                value = Utils.get_any_value('value', config_entry)
                self.log_info(f'deleting config entry - {key} = {value}')
                self.cluster_settings_table.delete_item(Key={'key': key})
            self.log_info(f'deleted {total} config entries')
        else:
            self.log_info(
                f'no config entries found matching config prefix: {config_key_prefix}'
            )

    def set_config_entry(self, key: str, value: Any):
        self.log_info(f'updating config: {key} = {value}')

        # ddb does not support float. convert Decimal before updating ...
        if value is not None:
            if isinstance(value, float):
                value = Decimal(str(value))
            elif isinstance(value, list) and len(value) > 0:
                if isinstance(value[0], float):
                    value = [Decimal(str(x)) for x in value]

        self.cluster_settings_table.update_item(
            Key={'key': key},
            UpdateExpression='SET #value=:value ADD #version :version',
            ExpressionAttributeNames={'#value': 'value', '#version': 'version'},
            ExpressionAttributeValues={':value': value, ':version': 1},
        )

    def get_cluster_s3_bucket(self) -> str:
        cluster_modules = self.get_cluster_modules()
        for cluster_module in cluster_modules:
            if cluster_module['name'] == constants.MODULE_CLUSTER:
                module_id = cluster_module['module_id']
                config_key = f'{module_id}.cluster_s3_bucket'
                entry = self.get_config_entry(config_key)
                cluster_s3_bucket = Utils.get_value_as_string('value', entry)
                if Utils.is_empty(cluster_s3_bucket):
                    raise exceptions.cluster_config_error(
                        f'cluster s3 bucket not found for key: {config_key}'
                    )
                return cluster_s3_bucket
        raise exceptions.cluster_config_error('cluster s3 bucket not configured')

    @staticmethod
    def post_process_ddb_config_entry(item: Dict) -> Dict:
        """
        perform Decimal types to int or float conversions if applicable
        :param item:
        :return: updated item
        """
        version = item.get('version')
        item['version'] = Utils.check_and_convert_decimal_value(version)
        value = item.get('value')
        item['value'] = Utils.check_and_convert_decimal_value(value)
        return item

    def on_create(self, entry: Dict):
        updated_entry = self.post_process_ddb_config_entry(entry)
        self.stream_subscriber.on_create(updated_entry)

    def on_update(self, old_entry: Dict, new_entry: Dict):
        updated_old_entry = self.post_process_ddb_config_entry(old_entry)
        updated_new_entry = self.post_process_ddb_config_entry(new_entry)
        self.stream_subscriber.on_update(updated_old_entry, updated_new_entry)

    def on_delete(self, entry: Dict):
        updated_entry = self.post_process_ddb_config_entry(entry)
        self.stream_subscriber.on_delete(updated_entry)

    def stop(self):
        if self.stream_subscription is None:
            return
        self.stream_subscription.stop()
