"""The discovery schema is also the write boundary, including native storage types."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pyhocon import ConfigFactory

from ideadatamodel import exceptions
from ideadatamodel.cluster_settings import UpdateModuleSettingsRequest
from ideaclustermanager.app.settings_catalog import (
    CATALOG,
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
            item[field]
            for field in ('group', 'label', 'section', 'description', 'module', 'path')
        )
        assert isinstance(item['advanced'], bool)
        assert isinstance(item['validation']['required'], bool)
        if item['value_type'] == 'enum':
            assert item['choices']


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


def test_catalog_places_delivery_and_software_in_the_consolidated_groups():
    assert BY_KEY['cluster.ses.enabled']['group'] == 'email'
    assert (
        BY_KEY['global-settings.gpu_settings.amd.linux.rhel_rocky8_installer_url'][
            'group'
        ]
        == 'deployment'
    )


def test_storage_expands_only_reviewed_fields_for_known_attachments():
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
            'server': {'port': '8443'},
            'cloudwatch_logs': {'enabled': 'false'},
        },
    )
    api.update_module_settings(context)
    assert config.db.sync_cluster_settings_in_db.call_args.kwargs == dict(
        overwrite=True,
        config_entries=[
            {'key': 'cluster-manager.web_portal.title', 'value': 'Portal'},
            {'key': 'cluster-manager.server.port', 'value': 8443},
            {'key': 'cluster-manager.cloudwatch_logs.enabled', 'value': False},
        ],
    )
    assert context.success.call_args.args[0].effects == {
        'web_portal.title': 'runtime',
        'server.port': 'restart',
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
