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
from ideadatamodel import exceptions, errorcodes, constants
from ideasdk.utils import Utils

from ideaclustermanager.app.projects.bedrock_provisioner import (
    validate_no_global_profiles,
)

from threading import RLock
from typing import List, Dict, Optional

# module settings served to non-elevated callers, keyed by module name; everything else in a
# module's config is admin-only. derived from the webapp's non-admin call sites.
USER_VISIBLE_MODULE_SETTINGS: Dict[str, List[str]] = {
    constants.MODULE_GLOBAL_SETTINGS: [
        'module_sets',
        'package_config.dcv.clients',
    ],
    constants.MODULE_CLUSTER: [
        'cluster_name',
        'locale',
        'timezone',
    ],
    constants.MODULE_CLUSTER_MANAGER: [
        # feature flag only. the catalog is admin-only; a user's own allowed
        # model ids travel on the project record, not in module settings.
        'bedrock.enabled',
        # optional embedded dashboard; the web portal reads these to decide whether to
        # render the nav entry and page
        'web_portal.custom_dashboard.enabled',
        'web_portal.custom_dashboard.title',
        'web_portal.custom_dashboard.url',
    ],
    constants.MODULE_DIRECTORYSERVICE: [
        'provider',
    ],
    constants.MODULE_SHARED_STORAGE: [
        'apps.mount_dir',
    ],
    constants.MODULE_BASTION_HOST: [
        'public',
        'public_ip',
        'private_ip',
    ],
    constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER: [
        'dcv_session.working_hours.start_up_time',
        'dcv_session.working_hours.shut_down_time',
        'dcv_session.max_root_volume_memory',
        # per-session idle autostop (schedule modal + session card render these)
        'dcv_session.idle_autostop_delay',
        'dcv_session.idle_autostop_delay_max',
    ],
}

_MISSING = object()


class ClusterSettingsAPI(BaseAPI):
    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.instance_types_lock = RLock()

    def _scope(self, access: str) -> str:
        # app (client-credentials) tokens are authorized by module scope, users by elevation
        return f'{self.context.module_id()}/{access}'

    def list_cluster_modules(self, context: ApiInvocationContext):
        cluster_modules = self.context.get_cluster_modules()
        context.success(ListClusterModulesResult(listing=cluster_modules))

    def get_module_settings(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetModuleSettingsRequest)

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        module_config = self.context.config().get_config(module_id, module_id=module_id)
        settings = module_config.as_plain_ordered_dict()

        if not context.is_authorized(
            elevated_access=True, scopes=[self._scope('read')]
        ):
            settings = self.build_user_module_settings(
                module_name=self.get_module_name(module_id), settings=settings
            )

        context.success(GetModuleSettingsResult(settings=settings))

    def get_module_name(self, module_id: str) -> Optional[str]:
        """
        Resolve a module_id to its module name. global-settings is registered as a
        config module under its own name, so the branch only saves a table read.
        """
        if module_id == constants.MODULE_GLOBAL_SETTINGS:
            return constants.MODULE_GLOBAL_SETTINGS
        module_info = self.context.get_cluster_module_info(module_id)
        if module_info is None:
            return None
        return Utils.get_value_as_string('name', module_info)

    def build_user_module_settings(
        self, module_name: Optional[str], settings: Dict
    ) -> Dict:
        """
        Project module settings down to the allowlisted paths for non-elevated
        callers. Parent dicts of allowlisted paths are always present so the
        webapp can traverse them; unknown modules project to an empty dict.
        """
        allowed_paths = USER_VISIBLE_MODULE_SETTINGS.get(module_name, [])
        result: Dict = {}
        for path in allowed_paths:
            keys = path.split('.')

            target = result
            for key in keys[:-1]:
                target = target.setdefault(key, {})

            value = settings
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    value = _MISSING
                    break
                value = value[key]

            if value is not _MISSING:
                target[keys[-1]] = value

        return result

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
                'dcv_session.idle_autostop_delay_max',
                'dcv_session.additional_security_groups',
                'dcv_session.max_root_volume_memory',
                'dcv_session.instance_types.allow',
                'dcv_session.instance_types.deny',
                # Stopped desktop cleanup; keep_tags stays on idea-admin.sh
                'dcv_session.stopped_session_cleanup.enabled',
                'dcv_session.stopped_session_cleanup.dry_run',
                'dcv_session.stopped_session_cleanup.stopped_after_days',
                'dcv_session.stopped_session_cleanup.warn_days_before',
                'dcv_session.stopped_session_cleanup.max_per_pass',
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
                'dcv_session.schedule.sunday.shut_down_time',
            ],
            'scheduler': [
                # Add scheduler settings that should be editable here
            ],
            'cluster-manager': [
                # feature flag and the org-approved model catalog. edited from the
                # bedrock tab on the cluster settings page.
                'bedrock.enabled',
                'bedrock.model_ids',
            ],
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
                current_path = f'{prefix}.{key}' if prefix else key
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

    def traverse_config_to_entries(
        self, config_entries: List[Dict], prefix: str, config: Dict
    ):
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
        if not context.is_authorized(
            elevated_access=True, scopes=[self._scope('write')]
        ):
            raise exceptions.unauthorized_access()

        request = context.get_request_payload_as(UpdateModuleSettingsRequest)

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        if request.settings is None:
            raise exceptions.invalid_params('settings is required')

        # Validate that only allowed settings are being updated
        self.validate_settings_allowed(module_id, request.settings)
        self.validate_bedrock_settings(module_id, request.settings)

        # Convert nested settings to flat config entries
        config_entries = []
        self.traverse_config_to_entries(config_entries, module_id, request.settings)

        # Update settings in database
        cluster_config = self.context.config()
        cluster_config.db.sync_cluster_settings_in_db(
            config_entries=config_entries, overwrite=True
        )

        self.reconcile_bedrock_projects(module_id, request.settings)

        context.success(UpdateModuleSettingsResult(success=True))

    def validate_bedrock_settings(self, module_id: str, settings: dict) -> None:
        """
        the catalog is checked here so an unsupported model id is reported to the
        administrator who entered it, instead of being skipped at provision time.
        """
        config = self.context.config()
        if module_id != config.get_module_id(constants.MODULE_CLUSTER_MANAGER):
            return
        bedrock = Utils.get_value_as_dict('bedrock', settings, {})
        if 'model_ids' not in bedrock:
            return
        validate_no_global_profiles(
            Utils.get_value_as_list('model_ids', bedrock, []),
            config.get_string('cluster.aws.partition', ''),
            config.get_string('cluster.aws.region', ''),
        )

    def read_bedrock_settings_from_db(self, module_id: str) -> dict:
        """
        the values as stored, read back after the write. the in-memory config tree is
        built once at construction and only refreshes on the dynamodb stream poller
        (10-30s), so it still holds the pre-change values at this point.
        """
        db = self.context.config().db
        enabled_entry = db.get_config_entry(f'{module_id}.bedrock.enabled')
        model_ids_entry = db.get_config_entry(f'{module_id}.bedrock.model_ids')
        return {
            'enabled': Utils.get_value_as_bool('value', enabled_entry, False),
            'model_ids': Utils.get_value_as_list('value', model_ids_entry, []),
        }

    def reconcile_bedrock_projects(self, module_id: str, settings: dict) -> None:
        """
        the feature flag and the model catalog change what every project resolves
        to, so each one is reconciled. the intended values travel with the task, since
        the reconcile cannot read them back out of the in-memory config yet.
        """
        if module_id != self.context.config().get_module_id(
            constants.MODULE_CLUSTER_MANAGER
        ):
            return
        bedrock = Utils.get_value_as_dict('bedrock', settings, {})
        if 'enabled' not in bedrock and 'model_ids' not in bedrock:
            return
        try:
            self.context.projects.send_bedrock_reconcile_all(
                cluster_bedrock=self.read_bedrock_settings_from_db(module_id)
            )
        except Exception as e:
            self.context.logger().error(
                f'failed to enqueue bedrock reconcile after a settings update: {e}'
            )
            # the setting is already written. reporting success here would hide that
            # no project was brought in line with it.
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=(
                    f'{module_id}.bedrock was saved but projects were not reconciled: '
                    f'{e}. save the setting again, or save each project, to retry.'
                ),
            )

    def list_cluster_hosts(self, context: ApiInvocationContext):
        # returns all infrastructure instances; ip/instance-id/subnet details are admin-only,
        # and the sole consumer is the admin cluster status page.
        if not context.is_authorized(
            elevated_access=True, scopes=[self._scope('read')]
        ):
            raise exceptions.unauthorized_access()

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
