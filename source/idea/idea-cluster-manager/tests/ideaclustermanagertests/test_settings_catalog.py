"""The discovery schema is also the write boundary, including native storage types."""

import hashlib
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pyhocon import ConfigFactory

from ideadatamodel import exceptions
from ideadatamodel.cluster_settings import UpdateModuleSettingsRequest
from ideaclustermanager.app.settings_catalog import (
    ADVANCED_SETTINGS_LABEL,
    CATALOG,
    DESTINATION_SECTIONS,
    MODEL_ID_LABEL,
    NOTIFICATION_LABELS,
    OPERATIONS_LEADS_GROUP_DEFAULT,
    SCHEDULE_LABELS,
    SETTINGS_GROUPS,
    coerce_settings,
    coerce_value,
    settings_catalog,
)
from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI

BY_KEY = {item['key']: item for item in CATALOG}


def test_catalog_metadata_and_uniqueness():
    assert len(BY_KEY) == len(CATALOG)
    for item in CATALOG:
        assert item['value_type'] in {
            'string',
            'integer',
            'number',
            'boolean',
            'list',
            'enum',
            'secret',
        }
        assert item['effect'] in {'runtime', 'restart', 'deployment'}
        assert all(
            item[field] for field in ('group', 'label', 'section', 'module', 'path')
        )
        assert item['group'] in SETTINGS_GROUPS
        assert item['section'] in DESTINATION_SECTIONS
        assert isinstance(item['description'], str)
        assert all(
            isinstance(item[field], bool)
            for field in ('advanced', 'read_only', 'hidden')
        )
        if item['key'] != 'cluster.aws.pricing_region':
            assert not (item['read_only'] and item['hidden'])
        if item['value_type'] == 'boolean':
            assert item['label'] not in ('Enabled', 'Enable')
            assert not item['label'].startswith(('Enable ', 'Enabled '))
        assert isinstance(item['validation']['required'], bool)
        if item['value_type'] == 'enum':
            assert item['choices']
        assert not item['description'].endswith(
            f'in {item["module"].replace("-", " ")}.'
        )
        assert 'conservative classification' not in item['description']


@pytest.mark.parametrize(
    'key,label,section',
    [
        ('directoryservice.ad_edition', 'AD edition', 'Directory connection'),
        (
            'directoryservice.ad_short_name',
            'AD short name (NetBIOS)',
            'Directory connection',
        ),
        ('directoryservice.ldap_base', 'LDAP base DN', 'Directory connection'),
        (
            'directoryservice.root_password_secret_arn',
            'Service account password secret ARN',
            'Directory connection',
        ),
        (
            'virtual-desktop-controller.dcv_session.cpu_utilization_threshold',
            'Idle CPU threshold (%)',
            'Desktop policy',
        ),
        (
            'scheduler.cost_estimation.ec2_boot_penalty_seconds',
            'EC2 boot penalty (seconds)',
            'Cost estimation',
        ),
        (
            'shared-storage.apps.efs.provisioned_throughput_in_mibps',
            'apps EFS Provisioned throughput (MiB/s)',
            'File systems',
        ),
        (
            'cluster-manager.cloudwatch_logs.enabled',
            'Cluster manager CloudWatch Logs',
            'Logs',
        ),
    ],
)
def test_curated_labels_units_and_sections(key, label, section):
    assert BY_KEY[key]['label'] == label
    assert BY_KEY[key]['section'] == section


def test_human_schedule_and_event_labels_are_centralized():
    assert ADVANCED_SETTINGS_LABEL == 'Advanced settings'
    assert MODEL_ID_LABEL == 'Model ID'
    assert SCHEDULE_LABELS == {
        'NO_SCHEDULE': 'No schedule',
        'WORKING_HOURS': 'Working hours',
        'STOP_ON_IDLE': 'Stop when idle',
        'START_ALL_DAY': 'Run all day',
        'CUSTOM_SCHEDULE': 'Custom hours',
    }
    assert len(NOTIFICATION_LABELS) == 16
    assert NOTIFICATION_LABELS['ready'] == 'Desktop ready'
    assert NOTIFICATION_LABELS['job_completed'] == 'Job completed'


@pytest.mark.parametrize(
    'key,value',
    [
        ('cluster-manager.accounts.reconcile.interval_minutes', 0),
        ('cluster-manager.accounts.reconcile.interval_minutes', 1441),
        ('virtual-desktop-controller.dcv_session.cpu_utilization_threshold', 101),
        ('cluster-manager.server.port', 65536),
        ('cluster-manager.server.port', True),
        ('cluster-manager.server.port', 1.5),
        ('cluster-manager.server.port', '1.5'),
        ('cluster-manager.maintenance.enabled', 1),
        ('cluster-manager.maintenance.enabled', 'maybe'),
        ('cluster-manager.bedrock.model_ids', [1]),
        ('cluster-manager.web_portal.title', []),
        ('cluster-manager.web_portal.title', ''),
        ('cluster-manager.bedrock.budgets.action', 'unexpected'),
        ('cluster-manager.accounts.reconcile.max_disable_fraction', float('inf')),
        (
            'cluster-manager.accounts.reconcile.okta.api_token_secret_arn',
            'secret contents',
        ),
        ('virtual-desktop-controller.dcv_session.working_hours.start_up_time', '24:01'),
    ],
)
def test_invalid_values(key, value):
    with pytest.raises(exceptions.SocaException):
        coerce_value(BY_KEY[key], value)


@pytest.mark.parametrize(
    'key,value,expected',
    [
        ('cluster-manager.server.port', '8443', 8443),
        ('cluster-manager.maintenance.enabled', 'true', True),
        ('cluster-manager.maintenance.enabled', 'off', False),
        ('cluster-manager.bedrock.model_ids', 'one, two,', ['one', 'two']),
        ('cluster-manager.bedrock.model_ids', ['one', 'two'], ['one', 'two']),
        ('cluster-manager.web_portal.title', 'Portal', 'Portal'),
        ('cluster-manager.accounts.reconcile.max_disable_fraction', '0.25', 0.25),
    ],
)
def test_coercion_matches_native_configuration_storage(key, value, expected):
    actual = coerce_value(BY_KEY[key], value)
    assert actual == expected
    assert type(actual) is type(expected)


def test_unknown_keys_and_generated_outputs_are_rejected():
    for module, settings in [
        ('cluster', {'aws': {'account_id': 'generated'}}),
        ('scheduler', {'unknown': True}),
        ('cluster-manager', {'module_id': 'generated'}),
        ('cluster-manager', {'server.port': 8443}),
        ('cluster-manager', {'unknown': {}}),
    ]:
        with pytest.raises(exceptions.SocaException):
            coerce_settings(module, settings)


def test_known_effects_cover_live_startup_and_deployment_readers():
    assert BY_KEY['cluster-manager.maintenance.enabled']['effect'] == 'runtime'
    assert BY_KEY['scheduler.job_provisioning.max_nodes_per_job']['effect'] == 'runtime'
    assert BY_KEY['cluster-manager.server.max_workers']['effect'] == 'restart'
    assert BY_KEY['cluster-manager.metrics.storage.enabled']['effect'] == 'restart'
    assert BY_KEY['scheduler.compute_node_ami']['effect'] == 'runtime'
    assert (
        BY_KEY['virtual-desktop-controller.dcv_session.additional_security_groups'][
            'effect'
        ]
        == 'runtime'
    )
    assert BY_KEY['cluster.backups.enabled']['effect'] == 'deployment'
    assert BY_KEY['cluster.network.client_ip']['effect'] == 'deployment'
    assert (
        BY_KEY['virtual-desktop-controller.controller.autoscaling.instance_type'][
            'effect'
        ]
        == 'deployment'
    )


def test_catalog_count_contract_and_explicit_dispositions():
    post_review_keys = {
        'global-settings.gpu_settings.nvidia_public_driver_versions.g7',
        'global-settings.gpu_settings.nvidia_public_driver_versions.g7e',
        'scheduler.job_provisioning.node_unavailable_timeout_seconds',
        'identity-provider.cognito.operations_leads_group_name',
    }
    baseline = [item for item in CATALOG if item['key'] not in post_review_keys]
    assert len(DESTINATION_SECTIONS) == 36
    assert len(baseline) == 753
    assert post_review_keys <= set(BY_KEY)
    assert (
        hashlib.sha256('\n'.join(sorted(BY_KEY)).encode()).hexdigest()
        == '6fb51e5a2d772cc472566bcf3ec87e0803559d54d991dbc700eba81540142c82'
    )
    dispositions = Counter(
        'hidden' if item['hidden'] else 'read_only' if item['read_only'] else 'editable'
        for item in CATALOG
    )
    assert sum(dispositions.values()) == len(CATALOG)
    assert set(item['section'] for item in CATALOG) | {
        'Email templates',
        'History backfill',
    } == set(DESTINATION_SECTIONS)
    editors = [
        item['key'] for item in CATALOG if not item['hidden'] and not item['read_only']
    ]
    assert len(editors) == len(set(editors))
    print(
        dict(dispositions),
        len(SETTINGS_GROUPS),
        len(DESTINATION_SECTIONS),
        len(editors),
    )


def test_table_owned_fields_have_one_destination():
    schedule = [
        item
        for item in CATALOG
        if item['key'].startswith('virtual-desktop-controller.dcv_session.schedule.')
        or item['key'].startswith(
            'virtual-desktop-controller.dcv_session.working_hours.'
        )
    ]
    assert len(schedule) == 23
    assert len({item['key'].split('.')[-2] for item in schedule[:21]}) == 7
    assert {item['section'] for item in schedule} == {'Desktop schedule'}

    desktop_events = [
        item
        for item in CATALOG
        if item['key'].startswith(
            'virtual-desktop-controller.dcv_session.notifications.'
        )
    ]
    notification_leaves = desktop_events + [
        BY_KEY['cluster-manager.notifications.email.enabled'],
        BY_KEY['scheduler.notifications.enabled'],
        BY_KEY['scheduler.notifications.job_started.email_template'],
        BY_KEY['scheduler.notifications.job_completed.email_template'],
    ]
    assert len(desktop_events) == 28
    assert len(notification_leaves) == 32
    assert {item['section'] for item in notification_leaves} == {'Notifications'}
    assert (
        BY_KEY[
            'virtual-desktop-controller.dcv_session.stopped_session_cleanup.email_template'
        ]['section']
        == 'Stopped desktop cleanup'
    )


def test_read_only_hidden_and_service_write_metadata():
    for key in (
        'cluster.network.vpc_cidr_block',
        'cluster.network.max_azs',
        'cluster.load_balancers.external_alb.public',
        'directoryservice.provider',
        'directoryservice.name',
        'shared-storage.apps.efs.encrypted',
        'shared-storage.apps.efs.kms_key_id',
        'shared-storage.apps.efs.performance_mode',
        'shared-storage.apps.fsx_lustre.deployment_type',
        'shared-storage.apps.fsx_lustre.storage_type',
        'cluster.backups.backup_vault.kms_key_id',
    ):
        assert BY_KEY[key]['read_only'] is True
        assert BY_KEY[key]['hidden'] is False
        assert BY_KEY[key]['advanced'] is True

    dead = [item for item in CATALOG if item['key'].startswith('scheduler.fair_share.')]
    live = [
        item
        for item in CATALOG
        if item['key'].startswith('scheduler.job_provisioning.queue_mode.fair_share.')
    ]
    assert len(dead) == len(live) == 5
    assert all(item['hidden'] for item in dead)
    assert all(not item['hidden'] and not item['read_only'] for item in live)
    assert BY_KEY['cluster-manager.web_portal.copyright_text']['hidden'] is True
    assert BY_KEY['cluster-manager.web_portal.default_log_level']['hidden'] is True

    service_key = 'virtual-desktop-controller.controller.autoscaling.max_capacity'
    assert BY_KEY[service_key]['hidden'] is True
    values, _ = coerce_settings(
        'virtual-desktop-controller',
        {'controller': {'autoscaling': {'max_capacity': '4'}}},
    )
    assert values['controller']['autoscaling']['max_capacity'] == 4


def test_service_autoscaling_blocks_are_hidden_but_remain_valid():
    prefixes = (
        'cluster-manager.ec2.autoscaling.',
        'virtual-desktop-controller.controller.autoscaling.',
        'virtual-desktop-controller.dcv_broker.autoscaling.',
        'virtual-desktop-controller.dcv_connection_gateway.autoscaling.',
    )
    blocks = [
        [item for item in CATALOG if item['key'].startswith(prefix)]
        for prefix in prefixes
    ]
    assert [len(block) for block in blocks] == [18, 18, 18, 18]
    assert sum(len(block) for block in blocks) == 72
    assert all(item['hidden'] for block in blocks for item in block)
    broker_table = BY_KEY[
        'virtual-desktop-controller.dcv_broker.dynamodb_table.autoscaling.enabled'
    ]
    assert broker_table['hidden'] is True
    assert broker_table['read_only'] is False


def test_opensearch_indices_remain_distinct_and_shards_are_read_only():
    jobs = BY_KEY['scheduler.opensearch.jobs_index.suffix']
    nodes = BY_KEY['scheduler.opensearch.jobs.index_suffix']
    assert jobs['label'] == 'Job index suffix'
    assert nodes['label'] == 'Node index suffix'
    assert jobs['section'] == nodes['section'] == 'Analytics'
    assert jobs['hidden'] is nodes['hidden'] is False
    for key in (
        'scheduler.opensearch.jobs.number_of_shards',
        'scheduler.opensearch.nodes.number_of_shards',
        'analytics.opensearch.default_number_of_shards',
    ):
        assert BY_KEY[key]['read_only'] is True


def test_misfiled_policy_and_limit_fields_have_curated_destinations():
    for key in (
        'scheduler.job_provisioning.max_nodes_per_job',
        'scheduler.job_provisioning.max_provisioning_retries',
    ):
        assert BY_KEY[key]['section'] == 'Job limits and placement'
        assert BY_KEY[key]['advanced'] is False
    assert (
        BY_KEY['virtual-desktop-controller.controller.enforce_project_budgets'][
            'section'
        ]
        == 'Desktop policy'
    )
    pricing = BY_KEY['cluster.aws.pricing_region']
    assert pricing['section'] == 'Cost estimation'
    assert pricing['advanced'] is True


def test_reporting_group_default_and_normalized_collisions():
    key = 'identity-provider.cognito.operations_leads_group_name'
    assert BY_KEY[key]['validation']['required'] is True
    assert (
        coerce_value(BY_KEY[key], OPERATIONS_LEADS_GROUP_DEFAULT)
        == 'operations-leads-cluster-group'
    )
    values, effects = coerce_settings(
        'identity-provider',
        {'cognito': {'operations_leads_group_name': 'report-readers'}},
    )
    assert values == {'cognito': {'operations_leads_group_name': 'report-readers'}}
    assert effects == {'cognito.operations_leads_group_name': 'runtime'}

    for settings in (
        {'cognito': {'operations_leads_group_name': 'administrators'}},
        {
            'cognito': {
                'administrators_group_name': 'privileged',
                'managers_group_name': 'privileged-cluster-group',
            }
        },
    ):
        with pytest.raises(exceptions.SocaException):
            coerce_settings('identity-provider', settings)


def test_storage_expands_only_reviewed_fields_for_known_attachments():
    # A list remains compatible with callers that do not yet supply providers.
    catalog = settings_catalog(['archive', 'invalid.path'])
    key = 'shared-storage.archive.fsx_netapp_ontap.metrics.password_secret_arn'
    assert any(
        item['key'] == key and item['value_type'] == 'secret' for item in catalog
    )
    assert not any('invalid.path' in item['key'] for item in catalog)
    assert not any(item['key'].endswith('.file_system_id') for item in catalog)
    values, effects = coerce_settings(
        'shared-storage', {'archive': {'mount_dir': '/archive'}}, catalog
    )
    assert values['archive']['mount_dir'] == '/archive'
    assert effects == {'archive.mount_dir': 'deployment'}


def test_storage_provider_mapping_filters_each_attachment():
    catalog = settings_catalog(
        {
            'apps': 'efs',
            'data': 'fsx_lustre',
            'archive': 'fsx_netapp_ontap',
            'invalid.path': 'efs',
        }
    )
    keys = {item['key'] for item in catalog}
    assert 'shared-storage.apps.efs.throughput_mode' in keys
    assert not any(key.startswith('shared-storage.apps.fsx_lustre.') for key in keys)
    assert not any(
        key.startswith('shared-storage.apps.fsx_netapp_ontap.') for key in keys
    )
    assert 'shared-storage.data.fsx_lustre.storage_capacity' in keys
    assert not any(key.startswith('shared-storage.data.efs.') for key in keys)
    assert 'shared-storage.archive.fsx_netapp_ontap.metrics.password_secret_arn' in keys
    assert not any(key.startswith('shared-storage.archive.efs.') for key in keys)
    assert not any(key.startswith('shared-storage.archive.fsx_lustre.') for key in keys)
    assert not any('invalid.path' in key for key in keys)
    assert {
        item['section'] for item in catalog if item['module'] == 'shared-storage'
    } == {'File systems', 'Storage measurement'}


def make_api(module_name='cluster-manager'):
    config = Mock()
    config.get_config.return_value = ConfigFactory.from_dict(
        {'archive': {'provider': 'efs'}}
    )
    config.get_module_id.side_effect = lambda module: module
    app = SimpleNamespace(
        config=lambda: config,
        module_id=lambda: 'cluster-manager',
        get_cluster_module_info=lambda module: {'name': module_name},
    )
    api = ClusterSettingsAPI(app)
    return api, config


def invocation(module, settings):
    context = Mock()
    context.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id=module, settings=settings
    )
    return context


def test_update_coerces_all_values_and_returns_effects():
    api, config = make_api()
    context = invocation(
        'cluster-manager',
        {
            'web_portal': {'title': 'Portal'},
            'logging': {'profile': 'console'},
            'cloudwatch_logs': {'enabled': 'false'},
        },
    )
    api.update_module_settings(context)
    assert config.db.sync_cluster_settings_in_db.call_args.kwargs == dict(
        overwrite=True,
        source='api',
        config_entries=[
            {'key': 'cluster-manager.web_portal.title', 'value': 'Portal'},
            {'key': 'cluster-manager.logging.profile', 'value': 'console'},
            {'key': 'cluster-manager.cloudwatch_logs.enabled', 'value': False},
        ],
    )
    assert context.success.call_args.args[0].effects == {
        'web_portal.title': 'runtime',
        'logging.profile': 'restart',
        'cloudwatch_logs.enabled': 'deployment',
    }


def test_update_validates_the_whole_request_before_writing():
    api, config = make_api()
    context = invocation(
        'cluster-manager',
        {'web_portal': {'title': 'Portal'}, 'server': {'port': 'invalid'}},
    )
    with pytest.raises(exceptions.SocaException):
        api.update_module_settings(context)
    config.db.sync_cluster_settings_in_db.assert_not_called()


def test_module_alias_uses_catalog_name_and_stores_deployed_id():
    api, config = make_api('virtual-desktop-controller')
    context = invocation('desktop-service', {'dcv_session': {'idle_timeout': '60'}})
    api.update_module_settings(context)
    assert config.db.sync_cluster_settings_in_db.call_args.kwargs['config_entries'] == [
        {'key': 'desktop-service.dcv_session.idle_timeout', 'value': 60}
    ]


def test_discovery_and_write_require_elevated_authorization():
    api, config = make_api()
    context = invocation('cluster-manager', {'web_portal': {'title': 'Portal'}})
    context.is_authorized.return_value = False
    for operation in (api.describe_settings_catalog, api.update_module_settings):
        with pytest.raises(exceptions.SocaException):
            operation(context)
    config.db.sync_cluster_settings_in_db.assert_not_called()


def test_discovery_dispatch_and_storage_extensions():
    api, _ = make_api()
    context = Mock(namespace='ClusterSettings.DescribeSettingsCatalog')
    api.invoke(context)
    result = context.success.call_args.args[0]
    assert any(
        item.key == 'shared-storage.archive.mount_options' for item in result.settings
    )


def test_existing_named_backup_rules_use_the_reviewed_rule_schema():
    catalog = settings_catalog(
        backup_rules={'cluster.backups.backup_plan.rules': ['weekly', 'invalid.path']}
    )
    key = 'cluster.backups.backup_plan.rules.weekly.delete_after_days'
    assert any(
        item['key'] == key and item['effect'] == 'deployment' for item in catalog
    )
    weekly = [item for item in catalog if '.rules.weekly.' in item['key']]
    assert len(weekly) == 5
    assert {item['section'] for item in weekly} == {'Backup policy'}
    assert not any('invalid.path' in item['key'] for item in catalog)
    values, _ = coerce_settings(
        'cluster',
        {
            'backups': {
                'backup_plan': {'rules': {'weekly': {'delete_after_days': '30'}}}
            }
        },
        catalog,
    )
    assert (
        values['backups']['backup_plan']['rules']['weekly']['delete_after_days'] == 30
    )


def test_storage_discovery_does_not_expand_generated_module_outputs():
    api, config = make_api()
    config.get_config.side_effect = (
        lambda key, **kwargs: ConfigFactory.from_dict(
            {
                'archive': {'provider': 'efs'},
                'security_group_id': 'generated',
                'deployment_id': 'generated',
            }
        )
        if key == 'shared-storage'
        else None
    )
    catalog = api.settings_catalog()
    assert any(item['key'] == 'shared-storage.archive.mount_dir' for item in catalog)
    assert not any(
        item['key'].startswith('shared-storage.security_group_id.') for item in catalog
    )
    assert not any(
        item['key'].startswith('shared-storage.deployment_id.') for item in catalog
    )


def test_ldap_options_cannot_contain_executable_expressions():
    definition = BY_KEY['directoryservice.ldap_options']
    assert coerce_value(definition, ["{'code': 1, 'value': 0}"]) == [
        "{'code': 1, 'value': 0}"
    ]
    with pytest.raises(exceptions.SocaException):
        coerce_value(definition, ['dict(code=1, value=0)'])


def test_update_rejects_non_object_settings_before_writing():
    api, config = make_api()
    for settings in (None, [], 'invalid', {'accounts': None}):
        with pytest.raises(exceptions.SocaException):
            api.update_module_settings(invocation('cluster-manager', settings))
    config.db.sync_cluster_settings_in_db.assert_not_called()


@pytest.mark.parametrize(
    'key',
    [
        'virtual-desktop-controller.dcv_session.provisioning_timeout_seconds',
        'scheduler.job_provisioning.stack_provisioning_timeout_seconds',
    ],
)
def test_provisioning_timeouts_keep_advanced_editors_and_consequences(key):
    entry = BY_KEY[key]
    assert entry['advanced'] is True
    assert entry['hidden'] is False
    assert entry['read_only'] is False
    assert entry['effect'] == 'runtime'
    assert 'seconds' in entry['description']
    assert 'terminat' in entry['description'].lower()
    assert 'fail' in entry['description'].lower()
    assert coerce_value(entry, '2400') == 2400


def test_general_cost_cards_and_estimation_editors():
    for section, prefix in (
        ('Cost header', 'cluster-manager.web_portal.cost_ticker.'),
        ('Dashboard link', 'cluster-manager.web_portal.custom_dashboard.'),
    ):
        entries = [item for item in CATALOG if item['key'].startswith(prefix)]
        assert entries
        assert all(
            item['section'] == section and item['group'] == 'general'
            for item in entries
        )
    assert (
        BY_KEY['cluster-manager.web_portal.cost_ticker.enabled']['description']
        == 'Show a cached cost total for the signed-in user in the portal header.'
    )
    assert (
        'frame'
        in BY_KEY['cluster-manager.web_portal.custom_dashboard.enabled']['description']
    )
    pricing = BY_KEY['cluster.aws.pricing_region']
    assert pricing['hidden'] and pricing['read_only'] and pricing['advanced']
    assert (
        pricing['description'] == "unused; job estimates price the cluster's own region"
    )
    estimates = [
        item for item in CATALOG if item['key'].startswith('scheduler.cost_estimation.')
    ]
    assert len(estimates) == 6
    assert all(
        not item['hidden'] and not item['read_only'] and not item['advanced']
        for item in estimates
    )
    assert (
        BY_KEY['scheduler.cost_estimation.provisioned_iops']['value_type'] == 'number'
    )
    assert BY_KEY['cluster.administrator_email']['section'] == 'Regional defaults'
    assert (
        'initial administrator' in BY_KEY['cluster.administrator_email']['description']
    )


def test_collection_readers_and_advanced_controls():
    prefix = 'cluster-manager.metrics.cost.'
    entries = [item for item in CATALOG if item['key'].startswith(prefix)]
    assert {item['section'] for item in entries} == {'Collection'}
    assert BY_KEY[prefix + 'enabled']['effect'] == 'restart'
    assert all(
        item['effect'] == 'runtime'
        for item in entries
        if not item['key'].endswith('.enabled')
    )
    assert BY_KEY[prefix + 'by_account']['advanced']
    assert not BY_KEY[prefix + 'interval_hours']['advanced']
    assert (
        BY_KEY[prefix + 'interval_hours']['description']
        == 'Hours between collection runs.'
    )
    assert 'dogstatsd' in BY_KEY[prefix + 'enabled']['description']
    assert 'commercial' in BY_KEY[prefix + 'enabled']['description']
    assert 'replaces' in BY_KEY[prefix + 'lookback_days']['description']
    assert BY_KEY['cluster-manager.metrics.storage.enabled']['effect'] == 'restart'
    assert BY_KEY['cluster-manager.metrics.storage.interval_minutes']['advanced']
    assert BY_KEY['cluster-manager.metrics.storage.verify_tls']['advanced']


def test_installation_debris_hidden_and_consumer_policies_preserved():
    for prefix in (
        'global-settings.package_config.',
        'global-settings.gpu_settings.nvidia',
        'global-settings.gpu_settings.amd',
        'cluster-manager.cache.',
        'cluster-manager.task_manager.',
        'scheduler.endpoints.',
    ):
        entries = [item for item in CATALOG if item['key'].startswith(prefix)]
        assert entries and all(
            item['hidden'] and not item['read_only'] for item in entries
        )
        assert all(item['description'] for item in entries)
    for key, section in (
        ('global-settings.gpu_settings.instance_families', 'GPU policy'),
        ('global-settings.gpu_settings.fail_on_missing_driver', 'GPU policy'),
        ('virtual-desktop-controller.server.usb_remotization', 'Desktop policy'),
    ):
        item = BY_KEY[key]
        assert not item['hidden'] and not item['read_only']
        assert item['section'] == section


def test_named_storage_credentials_and_rule_labels_are_distinct():
    entries = settings_catalog(
        {'archive': 'fsx_netapp_ontap', 'apps': 'efs', 'data': 'fsx_lustre'},
        {'cluster.backups.backup_plan.rules': ['weekly']},
    )
    credentials = [
        item
        for item in entries
        if '.metrics.' in item['key'] and item['module'] == 'shared-storage'
    ]
    assert credentials
    assert all(
        item['key'].startswith('shared-storage.archive.fsx_netapp_ontap.')
        for item in credentials
    )
    assert all(
        item['section'] == 'Storage measurement' and 'archive' in item['label']
        for item in credentials
    )
    weekly = [item for item in entries if '.rules.weekly.' in item['key']]
    assert all('weekly' in item['label'] for item in weekly)
    assert len({item['label'] for item in weekly}) == len(weekly)


@pytest.mark.parametrize(
    'key,label',
    [
        ('cluster-manager.accounts.reconcile.enabled', 'Account synchronization'),
        ('cluster-manager.bedrock.enabled', 'Amazon Bedrock'),
        ('scheduler.bedrock.enabled', 'Bedrock for jobs'),
        ('virtual-desktop-controller.bedrock.enabled', 'Bedrock for desktops'),
        ('cluster-manager.bedrock.budgets.enabled', 'Budget enforcement'),
        ('cluster-manager.bedrock.usage.enabled', 'Bedrock usage collection'),
        ('cluster-manager.metrics.cost.enabled', 'Cost collection'),
        ('cluster-manager.metrics.storage.enabled', 'Storage measurement'),
        ('cluster-manager.notifications.email.enabled', 'Portal email notifications'),
        ('scheduler.notifications.enabled', 'Job notifications'),
        ('cluster.ses.enabled', 'Amazon SES delivery'),
        ('cluster.backups.enabled', 'Cluster backups'),
        ('virtual-desktop-controller.vdi_host_backup.enabled', 'Desktop backups'),
        ('cluster.load_balancers.external_alb.waf.enabled', 'Web application firewall'),
        (
            'cluster.load_balancers.external_alb.waf.bot_control.enabled',
            'Bot protection',
        ),
        (
            'cluster.network.vpc_interface_endpoints.logs.enabled',
            'CloudWatch Logs endpoint',
        ),
        (
            'cluster.network.vpc_interface_endpoints.monitoring.enabled',
            'CloudWatch monitoring endpoint',
        ),
        ('scheduler.provisioning_lifecycle_events.enabled', 'Job provisioning events'),
        (
            'virtual-desktop-controller.dcv_session.stopped_session_cleanup.enabled',
            'Stopped desktop cleanup',
        ),
        (
            'virtual-desktop-controller.dcv_broker.dynamodb_table.autoscaling.enabled',
            'Broker table autoscaling',
        ),
        ('analytics.opensearch.logging.app_log_enabled', 'OpenSearch application logs'),
        ('analytics.opensearch.logging.slow_index_log_enabled', 'Slow indexing logs'),
        ('analytics.opensearch.logging.slow_search_log_enabled', 'Slow search logs'),
        ('scheduler.efa.multi_rail_enabled', 'EFA multi-rail'),
    ],
)
def test_feature_switch_labels(key, label):
    assert BY_KEY[key]['label'] == label


def test_all_desktop_event_and_module_log_labels_survive():
    for event, label in NOTIFICATION_LABELS.items():
        if event.startswith('job_'):
            continue
        assert (
            BY_KEY[
                f'virtual-desktop-controller.dcv_session.notifications.{event}.enabled'
            ]['label']
            == label
        )
    for module, label in (
        ('cluster', 'Cluster'),
        ('cluster-manager', 'Cluster manager'),
        ('directoryservice', 'Directory'),
        ('scheduler', 'Scheduler'),
        ('virtual-desktop-controller', 'Desktop'),
    ):
        assert (
            BY_KEY[f'{module}.cloudwatch_logs.enabled']['label']
            == f'{label} CloudWatch Logs'
        )


def test_certificate_controls_keep_read_only_context_in_network():
    for module in ('cluster-manager', 'scheduler', 'virtual-desktop-controller'):
        for leaf in ('enable_tls', 'tls_certificate_file', 'tls_key_file'):
            item = BY_KEY[f'{module}.server.{leaf}']
            assert item['read_only'] and not item['hidden']
            assert item['group'] == 'network'
            assert item['section'] == 'Load balancers and certificates'


def test_gpu_policy_has_one_destination_without_desktops():
    entries = [
        item
        for item in CATALOG
        if item['key']
        in (
            'global-settings.gpu_settings.instance_families',
            'global-settings.gpu_settings.fail_on_missing_driver',
        )
    ]
    assert len(entries) == 2
    assert all(
        item['group'] == 'general' and item['section'] == 'GPU policy'
        for item in entries
    )


def test_read_only_and_hidden_rows_are_refused_by_the_server():
    for module, settings in (
        ('identity-provider', {'cognito': {'removal_policy': 'DESTROY'}}),
        (
            'global-settings',
            {'package_config': {'aws_ssm': {'x86_64': 'https://example.invalid/ssm'}}},
        ),
        ('cluster-manager', {'server': {'port': '8443'}}),
    ):
        with pytest.raises(exceptions.SocaException, match='not editable'):
            coerce_settings(module, settings)
    values, _ = coerce_settings(
        'cluster-manager', {'ec2': {'autoscaling': {'max_capacity': '3'}}}
    )
    assert values['ec2']['autoscaling']['max_capacity'] == 3


def test_group_names_and_iam_rows_need_an_administrator():
    from ideadatamodel import errorcodes

    for module, settings in (
        ('identity-provider', {'cognito': {'administrators_group_name': 'mine'}}),
        ('cluster', {'iam': {'compute_node_iam_policy_arns': []}}),
    ):
        api, config = make_api(module)
        context = invocation(module, settings)
        context.is_administrator.return_value = False
        with pytest.raises(exceptions.SocaException) as denied:
            api.update_module_settings(context)
        assert denied.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
        config.db.sync_cluster_settings_in_db.assert_not_called()

    api, config = make_api('cluster')
    context = invocation('cluster', {'iam': {'compute_node_iam_policy_arns': []}})
    context.is_administrator.return_value = True
    api.update_module_settings(context)
    config.db.sync_cluster_settings_in_db.assert_called_once()
