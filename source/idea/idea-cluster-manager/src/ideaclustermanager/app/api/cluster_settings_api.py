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
from ideadatamodel.cluster_settings import (
    ListClusterModulesResult,
    ListClusterHostsRequest,
    ListClusterHostsResult,
    GetModuleSettingsRequest,
    GetModuleSettingsResult,
    UpdateModuleSettingsRequest,
    UpdateModuleSettingsResult,
    DescribeInstanceTypesResult,
)
from ideadatamodel import exceptions, constants
from ideasdk.utils import Utils

from threading import RLock
from typing import List, Dict


class ClusterSettingsAPI(BaseAPI):
    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.instance_types_lock = RLock()

    def list_cluster_modules(self, context: ApiInvocationContext):
        cluster_modules = self.context.get_cluster_modules()
        context.success(ListClusterModulesResult(listing=cluster_modules))

    def get_module_settings(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetModuleSettingsRequest)

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        module_config = self.context.config().get_config(module_id, module_id=module_id)

        context.success(
            GetModuleSettingsResult(settings=module_config.as_plain_ordered_dict())
        )

    def get_allowed_settings_for_module(self, module_id: str) -> List[str]:
        """
        Define which settings can be updated via the web UI for each module.
        This provides security by preventing arbitrary config modifications.
        """
        allowed_settings = {
            'vdc': [
                'dcv_session.idle_timeout',
                'dcv_session.idle_timeout_warning',
                'dcv_session.cpu_utilization_threshold',
                'dcv_session.idle_autostop_delay',
                'dcv_session.additional_security_groups',
                'dcv_session.max_root_volume_memory',
                'dcv_session.instance_types.allow',
                'dcv_session.instance_types.deny',
                # Network settings
                'dcv_session.network.subnet_autoretry',
                'dcv_session.network.randomize_subnets',
                'dcv_session.network.private_subnets',
                # Working hours
                'dcv_session.working_hours.start_up_time',
                'dcv_session.working_hours.shut_down_time',
                # Default schedules for each day of the week
                'dcv_session.schedule.monday.type',
                'dcv_session.schedule.monday.start_up_time',
                'dcv_session.schedule.monday.shut_down_time',
                'dcv_session.schedule.tuesday.type',
                'dcv_session.schedule.tuesday.start_up_time',
                'dcv_session.schedule.tuesday.shut_down_time',
                'dcv_session.schedule.wednesday.type',
                'dcv_session.schedule.wednesday.start_up_time',
                'dcv_session.schedule.wednesday.shut_down_time',
                'dcv_session.schedule.thursday.type',
                'dcv_session.schedule.thursday.start_up_time',
                'dcv_session.schedule.thursday.shut_down_time',
                'dcv_session.schedule.friday.type',
                'dcv_session.schedule.friday.start_up_time',
                'dcv_session.schedule.friday.shut_down_time',
                'dcv_session.schedule.saturday.type',
                'dcv_session.schedule.saturday.start_up_time',
                'dcv_session.schedule.saturday.shut_down_time',
                'dcv_session.schedule.sunday.type',
                'dcv_session.schedule.sunday.start_up_time',
                'dcv_session.schedule.sunday.shut_down_time'
            ],
            'scheduler': [
                # Add scheduler settings that should be editable here
            ],
            'cluster-manager': [
                # Add cluster manager settings that should be editable here
            ]
        }

        return allowed_settings.get(module_id, [])

    def validate_settings_allowed(self, module_id: str, settings: dict) -> None:
        """
        Validate that all requested setting paths are in the allowed list.
        Raises an exception if any non-whitelisted settings are attempted.
        """
        allowed_paths = self.get_allowed_settings_for_module(module_id)

        def get_setting_paths(obj, prefix=''):
            """Recursively extract all setting paths from the settings object"""
            paths = []
            for key, value in obj.items():
                current_path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    paths.extend(get_setting_paths(value, current_path))
                else:
                    paths.append(current_path)
            return paths

        requested_paths = get_setting_paths(settings)

        # Check if any requested path is not in the allowed list
        invalid_paths = [path for path in requested_paths if path not in allowed_paths]

        if invalid_paths:
            raise exceptions.invalid_params(
                f'The following settings are not allowed to be updated via web UI: {", ".join(invalid_paths)}. '
                f'Allowed settings for {module_id}: {", ".join(allowed_paths)}'
            )

    def traverse_config_to_entries(self, config_entries: List[Dict], prefix: str, config: Dict):
        """
        Convert nested config dictionary to flat key-value pairs for database storage.
        Replicates the functionality of ConfigGenerator.traverse_config.
        """
        for key in config:
            if '.' in key or ':' in key:
                raise exceptions.invalid_params(
                    f'Config key name: {key} under: {prefix} cannot contain a dot(.), colon(:) or comma(,)'
                )

            value = config[key]

            if prefix:
                path_prefix = f'{prefix}.{key}'
            else:
                path_prefix = key

            if isinstance(value, dict):
                self.traverse_config_to_entries(config_entries, path_prefix, value)
            else:
                config_entries.append({'key': path_prefix, 'value': value})

    def update_module_settings(self, context: ApiInvocationContext):
        if not context.is_authorized(elevated_access=True):
            raise exceptions.unauthorized_access()

        request = context.get_request_payload_as(UpdateModuleSettingsRequest)

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        if request.settings is None:
            raise exceptions.invalid_params('settings is required')

        # Validate that only allowed settings are being updated
        self.validate_settings_allowed(module_id, request.settings)

        # Convert nested settings to flat config entries
        config_entries = []
        self.traverse_config_to_entries(config_entries, module_id, request.settings)

        # Update settings in database
        cluster_config = self.context.config()
        cluster_config.db.sync_cluster_settings_in_db(
            config_entries=config_entries, overwrite=True
        )

        # Config automatically updates via DynamoDB stream subscriptions
        context.success(UpdateModuleSettingsResult(success=True))

    def list_cluster_hosts(self, context: ApiInvocationContext):
        # returns all infrastructure instances
        request = context.get_request_payload_as(ListClusterHostsRequest)
        ec2_instances = self.context.aws_util().ec2_describe_instances(
            filters=[
                {
                    'Name': 'instance-state-name',
                    'Values': ['pending', 'stopped', 'running'],
                },
                {
                    'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                    'Values': [self.context.cluster_name()],
                },
                {
                    'Name': f'tag:{constants.IDEA_TAG_NODE_TYPE}',
                    'Values': [
                        constants.NODE_TYPE_INFRA,
                        constants.NODE_TYPE_APP,
                        constants.NODE_TYPE_AMI_BUILDER,
                    ],
                },
            ],
            page_size=request.page_size,
        )
        result = []
        for instance in ec2_instances:
            result.append(instance.instance_data())

        context.success(ListClusterHostsResult(listing=result))

    def describe_instance_types(self, context: ApiInvocationContext):
        instance_types = (
            self.context.cache().long_term().get('aws.ec2.all-instance-types')
        )
        if instance_types is None:
            with self.instance_types_lock:
                instance_types = (
                    self.context.cache().long_term().get('aws.ec2.all-instance-types')
                )
                if instance_types is None:
                    instance_types = []
                    has_more = True
                    next_token = None

                    while has_more:
                        if next_token is None:
                            result = (
                                self.context.aws()
                                .ec2()
                                .describe_instance_types(MaxResults=100)
                            )
                        else:
                            result = (
                                self.context.aws()
                                .ec2()
                                .describe_instance_types(
                                    MaxResults=100, NextToken=next_token
                                )
                            )

                        next_token = Utils.get_value_as_string('NextToken', result)
                        has_more = Utils.is_not_empty(next_token)
                        current_instance_types = Utils.get_value_as_list(
                            'InstanceTypes', result
                        )
                        if len(current_instance_types) > 0:
                            instance_types += current_instance_types

                    self.context.cache().long_term().set(
                        'aws.ec2.all-instance-types', instance_types
                    )

        context.success(DescribeInstanceTypesResult(instance_types=instance_types))

    def invoke(self, context: ApiInvocationContext):
        if not context.is_authenticated():
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace == 'ClusterSettings.ListClusterModules':
            self.list_cluster_modules(context)
        elif namespace == 'ClusterSettings.GetModuleSettings':
            self.get_module_settings(context)
        elif namespace == 'ClusterSettings.UpdateModuleSettings':
            self.update_module_settings(context)
        elif namespace == 'ClusterSettings.ListClusterHosts':
            self.list_cluster_hosts(context)
        elif namespace == 'ClusterSettings.DescribeInstanceTypes':
            self.describe_instance_types(context)
