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

import math
import re
import time

from ideaclustermanager.app.costs.pricing_rates import fetch_pricing_rates

from ideaclustermanager.app.settings_catalog import (
    MODULES,
    coerce_settings,
    settings_catalog,
)

from ideaclustermanager.app.accounts.reconcile_settings import (
    approved_okta_origin,
    read_reconcile_settings,
)

import ideaclustermanager
from ideaclustermanager.app.metrics.cost_metrics_backfill import CostMetricsBackfill

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideasdk.auth import ApiAuthorizationType
from ideadatamodel.cluster_settings import (
    DescribeSettingsCatalogResult,
    FetchPricingRatesRequest,
    ListClusterModulesResult,
    ListClusterServicesResult,
    ClusterService,
    ClusterServiceTask,
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
from ideasdk.aws.aws_client_provider import AWS_CLIENT_ECS

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
        'web_portal.default_landing_page',
        # maintenance banner. every user sees it, so every user has to be able to read it.
        'maintenance.enabled',
        'maintenance.message',
        'maintenance.ends_at',
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
        'private_dns_name',
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
        self.cost_metrics_backfill = CostMetricsBackfill(context)

    def _scope(self, access: str) -> str:
        # app (client-credentials) tokens are authorized by module scope, users by elevation
        return f'{self.context.module_id()}/{access}'

    def module_administrator(self, context, module_name):
        # Only human module administrators inherit this capability. App tokens
        # continue through the explicit cluster-manager read/write scope checks.
        authorization = context.get_authorization()
        if authorization.type != ApiAuthorizationType.USER:
            return False
        config = self.context.config()
        if not config.is_module_enabled(module_name):
            return False
        module_id = config.get_module_id(module_name)
        group = self.context.accounts.group_name_helper.get_module_administrators_group(
            module_id=module_id
        )
        return group in (authorization.groups or [])

    def service_capabilities(self, context):
        global_access = context.is_administrator() or (
            context.get_authorization().type == ApiAuthorizationType.MANAGER
        )
        scoped_app = (
            context.get_authorization().type == ApiAuthorizationType.APP
            and context.is_authorized(
                elevated_access=True, scopes=[self._scope('read')]
            )
        )
        deployed = {
            module['name']
            for module in self.context.get_cluster_modules()
            if module.get('status') == 'deployed'
        }
        return {
            name
            for name in (
                constants.MODULE_CLUSTER_MANAGER,
                constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
                constants.MODULE_SCHEDULER,
            )
            if (name == constants.MODULE_CLUSTER_MANAGER or name in deployed)
            and (
                global_access or scoped_app or self.module_administrator(context, name)
            )
        }

    def service_module(self, name):
        prefix = f'{self.context.cluster_name()}-'
        identity = name[len(prefix) :] if name.startswith(prefix) else name
        config = self.context.config()
        # Keep registered identities even when a module is no longer deployed,
        # so its remaining services cannot fall through to the control plane.
        modules = {
            module['module_id']: module['name']
            for module in self.context.get_cluster_modules()
        }
        for module in (
            constants.MODULE_CLUSTER_MANAGER,
            constants.MODULE_SCHEDULER,
            constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
        ):
            if config.is_module_enabled(module):
                modules[config.get_module_id(module)] = module
        for module_id, module in modules.items():
            if identity == module_id:
                return (
                    module
                    if module
                    in (
                        constants.MODULE_SCHEDULER,
                        constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
                    )
                    else constants.MODULE_CLUSTER_MANAGER
                )
            if module == constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER and identity in {
                f'{module_id}-{component}'
                for component in ('controller', 'broker', 'gateway')
            }:
                return module
        if identity in (
            'virtual-desktop-controller',
            'dcv-broker',
            'dcv-connection-gateway',
        ):
            return constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
        if identity == 'scheduler':
            return constants.MODULE_SCHEDULER
        return constants.MODULE_CLUSTER_MANAGER

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

        if (
            context.is_administrator()
            and self.get_module_name(module_id) == constants.MODULE_CLUSTER_MANAGER
        ):
            fresh = read_reconcile_settings(
                self.context.config(), module_id, status=True
            )
            reconcile = settings.setdefault('accounts', {}).setdefault('reconcile', {})
            reconcile.setdefault('okta', {}).update(fresh.pop('okta'))
            reconcile.update(fresh)

        elevated = context.is_authorized(
            elevated_access=True, scopes=[self._scope('read')]
        )
        if self.get_module_name(module_id) == 'ecs' and self.service_capabilities(
            context
        ):
            capability = {'container_enabled': bool(settings.get('cluster_name'))}
            settings = {**settings, **capability} if elevated else capability
        elif not elevated and not (
            self.get_module_name(module_id) == constants.MODULE_SCHEDULER
            and self.module_administrator(context, constants.MODULE_SCHEDULER)
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

    def catalog_module_name(self, module_id):
        if module_id == 'vdc':
            return constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
        if module_id in MODULES:
            return module_id
        return self.get_module_name(module_id)

    def settings_catalog(self):
        storage = self.context.config().get_config('shared-storage', required=False)
        values = storage.as_plain_ordered_dict() if storage is not None else {}
        names = (
            {
                name: value['provider']
                for name, value in values.items()
                if isinstance(value, dict) and value.get('provider')
            }
            if isinstance(values, dict)
            else {}
        )
        rules = {}
        for prefix in (
            'cluster.backups.backup_plan.rules',
            'virtual-desktop-controller.vdi_host_backup.backup_plan.rules',
        ):
            config = self.context.config().get_config(prefix, required=False)
            values = config.as_plain_ordered_dict() if config is not None else {}
            rules[prefix] = list(values) if isinstance(values, dict) else []
        return settings_catalog(names, rules)

    def describe_settings_catalog(self, context):
        elevated = context.is_authorized(
            elevated_access=True, scopes=[self._scope('read')]
        )
        if not elevated and not self.module_administrator(
            context, constants.MODULE_SCHEDULER
        ):
            raise exceptions.unauthorized_access()
        catalog = self.settings_catalog()
        if not elevated:
            catalog = [
                item for item in catalog if item['module'] == constants.MODULE_SCHEDULER
            ]
        context.success(DescribeSettingsCatalogResult(settings=catalog))

    def fetch_pricing_rates(self, context):
        if not context.is_authorized(
            elevated_access=True, scopes=[self._scope('read')]
        ) and not self.module_administrator(context, constants.MODULE_SCHEDULER):
            raise exceptions.unauthorized_access()
        request = context.get_request_payload_as(FetchPricingRatesRequest)
        context.success(fetch_pricing_rates(self.context, request.region))

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
                # Stopped desktop cleanup; additional fields are defined by the catalog
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
                # the default image compute nodes launch from, written from the
                # custom AMIs page. a flat key, so no dotted path here.
                'compute_node_ami',
            ],
            'cluster-manager': [
                'accounts.reconcile.enabled',
                'accounts.reconcile.interval_minutes',
                'accounts.reconcile.dry_run',
                'accounts.reconcile.reenable',
                'accounts.reconcile.max_disable_fraction',
                'accounts.reconcile.check_cognito',
                'accounts.reconcile.okta.org_url',
                'accounts.reconcile.okta.api_token_secret_arn',
                # feature flag and the org-approved model catalog. edited from the
                # bedrock tab on the cluster settings page.
                'bedrock.enabled',
                'bedrock.model_ids',
                # maintenance banner, edited from the maintenance tab so a window
                # can be opened and closed without a redeploy.
                'maintenance.enabled',
                'maintenance.message',
                'maintenance.ends_at',
            ],
        }

        module_name = self.catalog_module_name(module_id)
        catalogued = [
            item['path']
            for item in self.settings_catalog()
            if item['module'] == module_name
        ]
        return list(dict.fromkeys(allowed_settings.get(module_id, []) + catalogued))

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
        request = context.get_request_payload_as(UpdateModuleSettingsRequest)
        if not context.is_authorized(
            elevated_access=True, scopes=[self._scope('write')]
        ) and not (
            self.get_module_name(request.module_id) == constants.MODULE_SCHEDULER
            and self.module_administrator(context, constants.MODULE_SCHEDULER)
        ):
            raise exceptions.unauthorized_access()

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        if not isinstance(request.settings, dict):
            raise exceptions.invalid_params('settings must be an object')

        # Who is an administrator, and what the nodes may do, is decided by administrators only.
        admin_only = {'identity-provider': 'cognito', 'cluster': 'iam'}
        module_name = self.catalog_module_name(module_id)
        if (
            module_name in admin_only
            and admin_only[module_name] in request.settings
            and not context.is_administrator()
        ):
            raise exceptions.unauthorized_access()

        # Reject unknown paths, then normalize every value before validating related fields.
        self.validate_settings_allowed(module_id, request.settings)
        request.settings, effects = coerce_settings(
            self.catalog_module_name(module_id),
            request.settings,
            self.settings_catalog(),
            config=self.context.config(),
            module_ids=(
                module['module_id'] for module in self.context.get_cluster_modules()
            )
            if self.catalog_module_name(module_id) == 'identity-provider'
            else (),
        )
        if (
            'reconcile' in request.settings.get('accounts', {})
            and not context.is_administrator()
        ):
            raise exceptions.unauthorized_access()

        self.validate_schedule_and_templates(module_id, request.settings)
        self.validate_bedrock_settings(module_id, request.settings)
        self.validate_reconcile_settings(module_id, request.settings)

        # Convert nested settings to flat config entries
        config_entries = []
        self.traverse_config_to_entries(config_entries, module_id, request.settings)

        # Update settings in database
        cluster_config = self.context.config()
        cluster_config.db.sync_cluster_settings_in_db(
            config_entries=config_entries, overwrite=True, source='api'
        )

        self.reconcile_bedrock_projects(module_id, request.settings)

        if 'reconcile' in request.settings.get('accounts', {}):
            cluster_config.db.set_config_entry(
                f'{module_id}.accounts.reconcile.last_saved',
                int(time.time()),
                source='api',
            )
            self.context.accounts.reconciler.settings_changed()

        context.success(UpdateModuleSettingsResult(success=True, effects=effects))

    def validate_schedule_and_templates(self, module_id: str, settings: dict) -> None:
        module = self.catalog_module_name(module_id)
        config = self.context.config()

        def effective(path, source_module_id=module_id):
            node = settings if source_module_id == module_id else {}
            for part in path.split('.'):
                if not isinstance(node, dict) or part not in node:
                    entry = config.db.cluster_settings_table.get_item(
                        Key={'key': f'{source_module_id}.{path}'}, ConsistentRead=True
                    ).get('Item', {})
                    return entry.get('value')
                node = node[part]
            return node

        def validate_range(prefix):
            start = effective(f'{prefix}.start_up_time')
            stop = effective(f'{prefix}.shut_down_time')
            if (
                not all(
                    isinstance(value, str)
                    and re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', value)
                    for value in (start, stop)
                )
                or start >= stop
            ):
                raise exceptions.invalid_params(
                    f'{prefix}: both HH:mm times are required, with start before stop on the same day'
                )

        session = settings.get('dcv_session', {})
        if module == constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER and (
            'working_hours' in session or 'schedule' in session
        ):
            validate_range('dcv_session.working_hours')
            for day in (
                'monday',
                'tuesday',
                'wednesday',
                'thursday',
                'friday',
                'saturday',
                'sunday',
            ):
                prefix = f'dcv_session.schedule.{day}'
                if effective(f'{prefix}.type') == 'CUSTOM_SCHEDULE':
                    validate_range(prefix)

        catalog = self.settings_catalog()
        email_enabled = (
            module == constants.MODULE_CLUSTER_MANAGER
            and settings.get('notifications', {}).get('email', {}).get('enabled')
            is True
        )
        for item in catalog:
            path = item['path']
            if (item['module'] != module and not email_enabled) or not path.endswith(
                '.email_template'
            ):
                continue
            source_module_id = (
                module_id
                if item['module'] == module
                else config.get_module_id(item['module'])
            )
            if not source_module_id:
                continue
            prefix = path.rsplit('.', 1)[0]
            parent = settings
            for part in prefix.split('.'):
                parent = parent.get(part, {}) if isinstance(parent, dict) else {}
            master_changed = (
                module == constants.MODULE_SCHEDULER
                and 'enabled' in settings.get('notifications', {})
            )
            if not parent and not master_changed and not email_enabled:
                continue
            enabled_path = (
                'notifications.enabled'
                if item['module'] == constants.MODULE_SCHEDULER
                else f'{prefix}.enabled'
            )
            if not effective(enabled_path, source_module_id):
                continue
            name = effective(path, source_module_id)
            if (
                not isinstance(name, str)
                or not name
                or not self.context.email_templates.email_templates_dao.get_email_template(
                    name
                )
            ):
                raise exceptions.invalid_params(
                    f'{path}: enabled notifications require an existing email template'
                )

    def validate_reconcile_settings(self, module_id: str, settings: dict) -> None:
        if module_id != self.context.config().get_module_id(
            constants.MODULE_CLUSTER_MANAGER
        ):
            return
        reconcile = settings.get('accounts', {}).get('reconcile', {})
        for key, value in reconcile.items():
            if key in ('enabled', 'dry_run', 'reenable', 'check_cognito'):
                if type(value) is not bool:
                    raise exceptions.invalid_params(f'{key} must be a boolean')
            elif key == 'interval_minutes':
                if type(value) is not int or not 1 <= value <= 1440:
                    raise exceptions.invalid_params(
                        'interval_minutes must be an integer from 1 to 1440'
                    )
            elif key == 'max_disable_fraction':
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                ):
                    raise exceptions.invalid_params(
                        'max_disable_fraction must be a number from 0 to 1'
                    )
        if 'okta' not in reconcile:
            return
        # Merge partial edits with persisted values, since the config cache can lag a save.
        okta = self.read_reconcile_okta_settings_from_db(module_id)
        okta.update(reconcile['okta'])
        org, secret = okta.get('org_url'), okta.get('api_token_secret_arn')
        if (org is not None and not isinstance(org, str)) or (
            secret is not None and not isinstance(secret, str)
        ):
            raise exceptions.invalid_params('Okta settings must be strings')
        if bool(org) != bool(secret):
            raise exceptions.invalid_params('Both Okta settings are required')
        if org:
            try:
                approved_okta_origin(self.context.config(), module_id, org)
            except ValueError as error:
                raise exceptions.invalid_params(str(error)) from error
        if secret and not re.fullmatch(
            r'arn:aws(?:-us-gov|-cn)?:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+',
            secret,
        ):
            raise exceptions.invalid_params(
                'Okta token must be a Secrets Manager secret ARN'
            )

    def read_reconcile_okta_settings_from_db(self, module_id: str) -> dict:
        db = self.context.config().db
        return {
            key: (
                db.cluster_settings_table.get_item(
                    Key={'key': f'{module_id}.accounts.reconcile.okta.{key}'},
                    ConsistentRead=True,
                ).get('Item', {})
            ).get('value')
            for key in ('org_url', 'api_token_secret_arn')
        }

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

    def list_cluster_services(self, context: ApiInvocationContext):
        capabilities = self.service_capabilities(context)
        if not capabilities:
            raise exceptions.unauthorized_access()
        result = ListClusterServicesResult()
        cluster = self.context.config().get_string('ecs.cluster_name', required=False)
        if not cluster:
            context.success(result)
            return

        def timestamp(value):
            return value.isoformat() if value else None

        def pages(method, key, **kwargs):
            values = []
            while True:
                response = method(**kwargs)
                values.extend(response.get(key, []))
                token = response.get('nextToken')
                if not token:
                    return values
                kwargs['nextToken'] = token

        try:
            ecs = self.context.aws().get_client(AWS_CLIENT_ECS)
            arns = pages(ecs.list_services, 'serviceArns', cluster=cluster)
            definitions = {}
            for offset in range(0, len(arns), 10):
                try:
                    response = ecs.describe_services(
                        cluster=cluster, services=arns[offset : offset + 10]
                    )
                except Exception:
                    result.errors.append(
                        'Could not read some services. Refresh to try again.'
                    )
                    continue
                if response.get('failures'):
                    result.errors.append(
                        'Some services could not be read. Refresh to try again.'
                    )
                for service in response.get('services', []):
                    if self.service_module(service['serviceName']) not in capabilities:
                        continue
                    primary = next(
                        (
                            d
                            for d in service.get('deployments', [])
                            if d.get('status') == 'PRIMARY'
                        ),
                        {},
                    )
                    row = ClusterService(
                        name=service['serviceName'],
                        desired=service.get('desiredCount', 0),
                        running=service.get('runningCount', 0),
                        pending=service.get('pendingCount', 0),
                        rollout_state=primary.get('rolloutState'),
                        updated_at=timestamp(primary.get('updatedAt')),
                    )
                    result.listing.append(row)
                    try:
                        definition = (
                            primary.get('taskDefinition') or service['taskDefinition']
                        )
                        if definition not in definitions:
                            definitions[definition] = ecs.describe_task_definition(
                                taskDefinition=definition
                            )['taskDefinition']
                        row.images = [
                            c['image']
                            for c in definitions[definition].get(
                                'containerDefinitions', []
                            )
                            if c.get('image')
                        ]
                    except Exception:
                        result.errors.append(
                            f'Could not read images for {row.name}. Refresh to try again.'
                        )
                    try:
                        tasks = pages(
                            ecs.list_tasks,
                            'taskArns',
                            cluster=cluster,
                            serviceName=row.name,
                            desiredStatus='RUNNING',
                        )
                        for start in range(0, len(tasks), 100):
                            described = ecs.describe_tasks(
                                cluster=cluster, tasks=tasks[start : start + 100]
                            )
                            if described.get('failures'):
                                result.errors.append(
                                    f'Some tasks for {row.name} could not be read. Refresh to try again.'
                                )
                            row.tasks.extend(
                                ClusterServiceTask(
                                    task_id=t['taskArn'].rsplit('/', 1)[-1],
                                    started_at=timestamp(t.get('startedAt')),
                                    health=t.get('healthStatus', 'UNKNOWN'),
                                )
                                for t in described.get('tasks', [])
                                if t.get('lastStatus') == 'RUNNING'
                            )
                    except Exception:
                        result.errors.append(
                            f'Could not read tasks for {row.name}. Refresh to try again.'
                        )
        except Exception:
            result.errors.append('Could not load services. Refresh to try again.')
        context.success(result)

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
        if namespace in (
            'ClusterSettings.BackfillCostMetrics',
            'ClusterSettings.GetCostMetricsBackfill',
        ):
            if not context.is_authorized(elevated_access=True, scopes=None):
                raise exceptions.unauthorized_access()
            if namespace == 'ClusterSettings.BackfillCostMetrics':
                context.success(
                    self.cost_metrics_backfill.start_request(context.request_payload)
                )
            else:
                context.success(self.cost_metrics_backfill.status())
        elif namespace == 'ClusterSettings.ListClusterModules':
            self.list_cluster_modules(context)
        elif namespace == 'ClusterSettings.DescribeSettingsCatalog':
            self.describe_settings_catalog(context)
        elif namespace == 'ClusterSettings.FetchPricingRates':
            self.fetch_pricing_rates(context)
        elif namespace == 'ClusterSettings.GetModuleSettings':
            self.get_module_settings(context)
        elif namespace == 'ClusterSettings.UpdateModuleSettings':
            self.update_module_settings(context)
        elif namespace == 'ClusterSettings.ListClusterServices':
            self.list_cluster_services(context)
        elif namespace == 'ClusterSettings.ListClusterHosts':
            self.list_cluster_hosts(context)
        elif namespace == 'ClusterSettings.DescribeInstanceTypes':
            self.describe_instance_types(context)
