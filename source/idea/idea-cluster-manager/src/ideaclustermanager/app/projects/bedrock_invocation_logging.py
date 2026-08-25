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

__all__ = ('BedrockInvocationLogging',)

from ideasdk.context import SocaContext
from ideasdk.utils import Utils

import botocore.exceptions
from typing import Dict, Optional

STATE_DISABLED = 'disabled'
STATE_UNAVAILABLE = 'unavailable'
STATE_ADOPTED = 'adopted'
STATE_ADOPTED_ALREADY = 'already-adopted'
STATE_NOT_CONFIGURED = 'not-configured'
STATE_STOOD_DOWN = 'stood-down'

ABSENT_ERROR_CODES = ('ResourceNotFoundException', 'ValidationException')


class BedrockInvocationLogging:
    """
    model invocation logging is one configuration per account and region. idea
    sets it only when nothing else has, never overwrites another owner's
    configuration, and never deletes it.
    """

    def __init__(self, context: SocaContext):
        self.context = context
        self.logger = context.logger('bedrock-invocation-logging')

    @property
    def bedrock(self):
        return self.context.aws().bedrock()

    def _config_key(self, suffix: str) -> str:
        return f'{self.context.module_id()}.bedrock.{suffix}'

    def is_feature_enabled(self) -> bool:
        return self.context.config().get_bool(self._config_key('enabled'), False)

    def is_configuration_managed(self) -> bool:
        return self.context.config().get_bool(
            self._config_key('invocation_logging.manage_configuration'), True
        )

    def is_request_response_data_included(self) -> bool:
        return self.context.config().get_bool(
            self._config_key('invocation_logging.include_request_response_data'), False
        )

    def get_log_group_name(self) -> Optional[str]:
        return self.context.config().get_string(
            self._config_key('invocation_log_group_name')
        )

    def get_delivery_role_arn(self) -> Optional[str]:
        return self.context.config().get_string(
            self._config_key('invocation_log_role_arn')
        )

    def get_configuration(self) -> Optional[Dict]:
        try:
            result = self.bedrock.get_model_invocation_logging_configuration()
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] in ABSENT_ERROR_CODES:
                return None
            raise e
        logging_config = Utils.get_value_as_dict('loggingConfig', result)
        if logging_config is None or len(logging_config) == 0:
            return None
        return logging_config

    @staticmethod
    def get_configured_log_group(logging_config: Dict) -> Optional[str]:
        cloud_watch_config = Utils.get_value_as_dict(
            'cloudWatchConfig', logging_config, {}
        )
        return Utils.get_value_as_string('logGroupName', cloud_watch_config)

    def build_logging_config(self, log_group_name: str, role_arn: str) -> Dict:
        include_data = self.is_request_response_data_included()
        return {
            'cloudWatchConfig': {
                'logGroupName': log_group_name,
                'roleArn': role_arn,
            },
            'textDataDeliveryEnabled': include_data,
            'imageDataDeliveryEnabled': include_data,
            'embeddingDataDeliveryEnabled': include_data,
            'videoDataDeliveryEnabled': include_data,
            'audioDataDeliveryEnabled': include_data,
        }

    def reconcile(self) -> str:
        if not self.is_feature_enabled():
            return STATE_DISABLED

        log_group_name = self.get_log_group_name()
        role_arn = self.get_delivery_role_arn()
        if Utils.is_empty(log_group_name) or Utils.is_empty(role_arn):
            self.logger.warning(
                f'{self._config_key("invocation_log_group_name")} is not set. '
                f'redeploy the cluster-manager module with bedrock enabled to create '
                f'the invocation log group and its delivery role.'
            )
            return STATE_UNAVAILABLE

        logging_config = self.get_configuration()
        if logging_config is not None:
            configured_log_group = self.get_configured_log_group(logging_config)
            if configured_log_group == log_group_name:
                return STATE_ADOPTED_ALREADY
            self.logger.warning(
                f'model invocation logging is already configured for this account and '
                f'region, delivering to {configured_log_group or "a destination idea did not set"}. '
                f'idea will not change it. usage stays empty until it delivers to '
                f'{log_group_name}.'
            )
            return STATE_STOOD_DOWN

        if not self.is_configuration_managed():
            self.logger.warning(
                f'model invocation logging is not configured and '
                f'{self._config_key("invocation_logging.manage_configuration")} is false. '
                f'configure it to deliver to {log_group_name} to record usage.'
            )
            return STATE_NOT_CONFIGURED

        self.bedrock.put_model_invocation_logging_configuration(
            loggingConfig=self.build_logging_config(log_group_name, role_arn)
        )
        self.logger.info(
            f'model invocation logging was not configured for this account and region. '
            f'idea now delivers it to {log_group_name}. this captures every bedrock '
            f'runtime caller in the account, not only idea hosts.'
        )
        return STATE_ADOPTED
