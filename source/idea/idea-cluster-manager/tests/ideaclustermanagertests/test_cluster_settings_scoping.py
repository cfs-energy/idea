"""
Test Cases for ClusterSettingsAPI GetModuleSettings scoping
"""

import json
import unittest
from unittest.mock import Mock

from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI
from ideadatamodel.cluster_settings import GetModuleSettingsRequest

VDC_SETTINGS = {
    'aws_ssm': {'path': '/idea/vdc'},
    'client_id': 'vdc-client-id',
    'client_secret': 'vdc-client-secret',
    'controller': {
        'autoscaling': {'instance_type': 'm5.large'},
        'iam_role_arn': 'arn:aws:iam::123456789012:role/vdc-controller',
    },
    'dcv_broker': {
        'client_communication_port': 8443,
        'session_token_duration': 60,
    },
    'dcv_session': {
        'idle_timeout': 60,
        'idle_autostop_delay': 60,
        'idle_autostop_delay_max': 240,
        'max_root_volume_memory': 1000,
        'network': {
            'private_subnets': ['subnet-11111111', 'subnet-22222222'],
        },
        'quic_support': True,
        'working_hours': {
            'start_up_time': '09:00',
            'shut_down_time': '17:00',
        },
    },
}

DIRECTORYSERVICE_SETTINGS = {
    'provider': 'activedirectory',
    'name': 'corp.example.local',
    'root_username_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:ds-username',
    'root_password_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:ds-password',
    'ldap_connection_uri': 'ldap://10.0.0.10',
}

BASTION_SETTINGS = {
    'public': False,
    'public_ip': '',
    'private_ip': '10.0.1.5',
    'instance_id': 'i-0123456789abcdef0',
    'kms_key_id': 'arn:aws:kms:us-east-1:123456789012:key/abc',
}

CLUSTER_MANAGER_SETTINGS = {
    'client_id': 'cluster-manager-client-id',
    'client_secret': 'cluster-manager-client-secret',
    'ec2': {'autoscaling': {'instance_type': 'm5.large'}},
    'web_portal': {
        'title': 'Integrated Digital Engineering on AWS',
        'session_management': 'in-memory',
        'custom_dashboard': {
            'enabled': True,
            'title': 'Cluster Dashboard',
            'url': 'https://dashboard.example.com/view',
        },
    },
    'maintenance': {
        'enabled': True,
        'message': 'Scheduler closed for the 26.09 upgrade.',
        'ends_at': '2026-09-15T18:00:00Z',
    },
}

GLOBAL_SETTINGS = {
    'module_sets': {
        'default': {
            'cluster-manager': {'module_id': 'cluster-manager'},
            'virtual-desktop-controller': {'module_id': 'vdc'},
        }
    },
    'package_config': {
        'aws_ssm': {'download_url': 'https://example.com/ssm'},
        'dcv': {
            'clients': {'windows': {'url': 'https://example.com/dcv.msi'}},
            'gpg_key': 'https://example.com/NICE-GPG-KEY',
        },
    },
}

CLUSTER_MANAGER_BEDROCK_SETTINGS = {
    'ec2': {'autoscaling': {'instance_type': 'm5.large'}},
    'bedrock': {
        'enabled': True,
        'model_ids': ['vendor-a.model-1', 'vendor-b.model-9'],
    },
}

MODULE_NAMES_BY_ID = {
    'vdc': 'virtual-desktop-controller',
    'directoryservice': 'directoryservice',
    'bastion-host': 'bastion-host',
    'cluster': 'cluster',
    'shared-storage': 'shared-storage',
    'cluster-manager': 'cluster-manager',
    'analytics': 'analytics',
    'metrics': 'metrics',
}


class TestClusterSettingsScoping(unittest.TestCase):
    def setUp(self):
        self.app_context = Mock()
        self.app_context.get_cluster_module_info.side_effect = lambda module_id: (
            {'module_id': module_id, 'name': MODULE_NAMES_BY_ID[module_id]}
            if module_id in MODULE_NAMES_BY_ID
            else None
        )
        self.api = ClusterSettingsAPI(self.app_context)

    def invoke_get_module_settings(
        self, module_id: str, settings: dict, elevated: bool
    ) -> dict:
        self.app_context.config.return_value.get_config.return_value.as_plain_ordered_dict.return_value = settings
        context = Mock()
        context.get_request_payload_as.return_value = GetModuleSettingsRequest(
            module_id=module_id
        )
        context.is_authorized.return_value = elevated
        context.is_administrator.return_value = False
        self.api.get_module_settings(context)
        result = context.success.call_args[0][0]
        return result.settings

    def test_admin_sees_full_settings(self):
        """Elevated callers get the unfiltered settings dict"""
        settings = self.invoke_get_module_settings('vdc', VDC_SETTINGS, elevated=True)
        self.assertEqual(settings, VDC_SETTINGS)

    def test_user_sees_exact_vdc_projection(self):
        """Non-elevated callers get exactly the allowlisted vdc paths"""
        settings = self.invoke_get_module_settings('vdc', VDC_SETTINGS, elevated=False)
        self.assertEqual(
            settings,
            {
                'dcv_session': {
                    'max_root_volume_memory': 1000,
                    # the schedule modal and session card need both autostop keys
                    'idle_autostop_delay': 60,
                    'idle_autostop_delay_max': 240,
                    'working_hours': {
                        'start_up_time': '09:00',
                        'shut_down_time': '17:00',
                    },
                }
            },
        )

    def test_user_sees_exact_global_settings_projection(self):
        """global-settings projects module_sets and dcv client packages only"""
        settings = self.invoke_get_module_settings(
            'global-settings', GLOBAL_SETTINGS, elevated=False
        )
        self.assertEqual(
            settings,
            {
                'module_sets': GLOBAL_SETTINGS['module_sets'],
                'package_config': {
                    'dcv': {
                        'clients': GLOBAL_SETTINGS['package_config']['dcv']['clients']
                    }
                },
            },
        )

    def test_user_sees_exact_bastion_projection(self):
        """bastion-host projects connectivity fields; falsy values are preserved"""
        settings = self.invoke_get_module_settings(
            'bastion-host', BASTION_SETTINGS, elevated=False
        )
        self.assertEqual(
            settings,
            {'public': False, 'public_ip': '', 'private_ip': '10.0.1.5'},
        )

    def test_secrets_shaped_keys_never_serialize_for_users(self):
        """No secret/credential/infra key names survive the non-elevated projection"""
        module_settings = {
            'vdc': VDC_SETTINGS,
            'directoryservice': DIRECTORYSERVICE_SETTINGS,
            'bastion-host': BASTION_SETTINGS,
            'global-settings': GLOBAL_SETTINGS,
            'cluster-manager': CLUSTER_MANAGER_SETTINGS,
        }
        for module_id, full_settings in module_settings.items():
            settings = self.invoke_get_module_settings(
                module_id, full_settings, elevated=False
            )
            serialized = json.dumps(settings).lower()
            for marker in (
                'secret',
                'password',
                'arn',
                'subnet-',
                'i-0',
                'kms',
                'ldap',
            ):
                self.assertNotIn(
                    marker,
                    serialized,
                    f'{module_id}: sensitive marker {marker} leaked to non-admin',
                )

    def test_user_sees_exact_cluster_manager_projection(self):
        """cluster-manager projects the optional dashboard embed and the maintenance banner"""
        settings = self.invoke_get_module_settings(
            'cluster-manager', CLUSTER_MANAGER_SETTINGS, elevated=False
        )
        self.assertEqual(
            settings,
            {
                # bedrock.enabled is allowlisted for this module too, and the projection
                # always emits the parent scaffold of an allowlisted path, so an empty 'bedrock' appears.
                'bedrock': {},
                'web_portal': {
                    'custom_dashboard': {
                        'enabled': True,
                        'title': 'Cluster Dashboard',
                        'url': 'https://dashboard.example.com/view',
                    }
                },
                # every user has to read these three: the banner is shown to all of them
                'maintenance': {
                    'enabled': True,
                    'message': 'Scheduler closed for the 26.09 upgrade.',
                    'ends_at': '2026-09-15T18:00:00Z',
                },
            },
        )
        # the client id and secret sit beside them in the same module and must not ride along
        self.assertNotIn('client_id', settings)
        self.assertNotIn('client_secret', settings)

    def test_unknown_module_projects_to_empty(self):
        """Modules with no allowlist entry serialize as an empty dict for users"""
        settings = self.invoke_get_module_settings(
            'analytics',
            {'opensearch': {'endpoint': 'https://opensearch.example.com'}},
            elevated=False,
        )
        self.assertEqual(settings, {})

    def test_unknown_module_metrics_projects_to_empty(self):
        """Same rule via a second module with no allowlist entry"""
        settings = self.invoke_get_module_settings(
            'metrics',
            {'provider': 'cloudwatch'},
            elevated=False,
        )
        self.assertEqual(settings, {})

    def test_user_sees_only_the_bedrock_feature_flag(self):
        """cluster-manager projects bedrock.enabled; the catalog stays admin-only"""
        settings = self.invoke_get_module_settings(
            'cluster-manager', CLUSTER_MANAGER_BEDROCK_SETTINGS, elevated=False
        )
        # the custom-dashboard and maintenance paths are allowlisted for this module as
        # well, so their empty parent scaffolds ride along.
        self.assertEqual(
            settings,
            {
                'bedrock': {'enabled': True},
                'web_portal': {'custom_dashboard': {}},
                'maintenance': {},
            },
        )

    def test_admin_sees_the_bedrock_catalog(self):
        """the catalog is served unfiltered to elevated callers"""
        settings = self.invoke_get_module_settings(
            'cluster-manager', CLUSTER_MANAGER_BEDROCK_SETTINGS, elevated=True
        )
        self.assertEqual(settings, CLUSTER_MANAGER_BEDROCK_SETTINGS)

    def test_unresolvable_module_id_projects_to_empty(self):
        """A module_id absent from the modules table projects to an empty dict"""
        settings = self.invoke_get_module_settings(
            'no-such-module', {'anything': 'value'}, elevated=False
        )
        self.assertEqual(settings, {})

    def test_missing_leaf_keeps_parent_scaffold(self):
        """Allowlisted parent dicts exist even when config leaves are absent"""
        settings = self.invoke_get_module_settings(
            'vdc', {'dcv_session': {'idle_timeout': 60}}, elevated=False
        )
        self.assertEqual(settings, {'dcv_session': {'working_hours': {}}})


def test_administrator_reads_reconcile_rows_instead_of_stale_config():
    from ideaclustermanagertests.metrics_fakes import FakeSettingsDB

    context = Mock()
    context.module_id.return_value = 'cluster-manager'
    context.get_cluster_module_info.return_value = {'name': 'cluster-manager'}
    config = context.config.return_value
    config.get_config.return_value.as_plain_ordered_dict.return_value = {
        'accounts': {'reconcile': {'enabled': False, 'interval_minutes': 60}}
    }
    config.db = FakeSettingsDB()
    config.db.values = {
        'cluster-manager.accounts.reconcile.enabled': True,
        'cluster-manager.accounts.reconcile.interval_minutes': 5,
        'cluster-manager.accounts.reconcile.max_disable_fraction': 0,
        'cluster-manager.accounts.reconcile.last_completed': 100,
        'cluster-manager.accounts.reconcile.last_saved': 200,
    }
    invocation = Mock()
    invocation.get_request_payload_as.return_value = GetModuleSettingsRequest(
        module_id='cluster-manager'
    )
    invocation.is_administrator.return_value = True
    ClusterSettingsAPI(context).get_module_settings(invocation)
    settings = invocation.success.call_args.args[0].settings['accounts']['reconcile']
    assert settings['enabled'] is True and settings['interval_minutes'] == 5
    assert settings['max_disable_fraction'] == 0
    assert settings['last_completed'] == 100 and settings['last_saved'] == 200
    assert all(consistent for _, consistent in config.db.reads)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestEffectiveSettingsValidation(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.api = ClusterSettingsAPI(self.context)
        self.stored = {
            'vdc.dcv_session.working_hours.start_up_time': '09:00',
            'vdc.dcv_session.working_hours.shut_down_time': '17:00',
            'vdc.dcv_session.schedule.monday.type': 'CUSTOM_SCHEDULE',
            'vdc.dcv_session.schedule.monday.start_up_time': '10:00',
            'vdc.dcv_session.schedule.monday.shut_down_time': '16:00',
        }
        self.context.config.return_value.cluster_settings_table = Mock()
        self.context.config.return_value.db.cluster_settings_table.get_item.side_effect = (
            lambda Key, **kwargs: {'Item': {'value': self.stored.get(Key['key'])}}
        )
        self.context.config.return_value.get_config.return_value = None

    def test_partial_schedule_validates_persisted_endpoint(self):
        with self.assertRaisesRegex(Exception, 'start before stop'):
            self.api.validate_schedule_and_templates(
                'vdc',
                {'dcv_session': {'schedule': {'monday': {'start_up_time': '18:00'}}}},
            )
        self.api.validate_schedule_and_templates(
            'vdc', {'dcv_session': {'schedule': {'monday': {'start_up_time': '11:00'}}}}
        )

    def test_partial_working_hours_reject_equal_and_overnight_ranges(self):
        for start in ('17:00', '20:00', '9:00', ''):
            with self.subTest(start=start), self.assertRaisesRegex(Exception, 'HH:mm'):
                self.api.validate_schedule_and_templates(
                    'vdc', {'dcv_session': {'working_hours': {'start_up_time': start}}}
                )

    def test_idle_schedule_ignores_internal_sentinel(self):
        self.stored['vdc.dcv_session.schedule.monday.type'] = 'STOP_ON_IDLE'
        self.stored['vdc.dcv_session.schedule.monday.start_up_time'] = '0'
        self.stored['vdc.dcv_session.schedule.monday.shut_down_time'] = '0'
        self.api.validate_schedule_and_templates(
            'vdc', {'dcv_session': {'working_hours': {'start_up_time': '08:00'}}}
        )

    def test_enabling_event_validates_the_existing_template(self):
        prefix = 'vdc.dcv_session.notifications.ready'
        self.stored[f'{prefix}.email_template'] = 'missing'
        self.context.email_templates.email_templates_dao.get_email_template.return_value = None
        with self.assertRaisesRegex(Exception, 'existing email template'):
            self.api.validate_schedule_and_templates(
                'vdc', {'dcv_session': {'notifications': {'ready': {'enabled': True}}}}
            )
        self.context.email_templates.email_templates_dao.get_email_template.return_value = {
            'name': 'available'
        }
        self.api.validate_schedule_and_templates(
            'vdc',
            {
                'dcv_session': {
                    'notifications': {
                        'ready': {'enabled': True, 'email_template': 'available'}
                    }
                }
            },
        )

    def test_template_patch_uses_existing_enabled_state(self):
        self.stored['vdc.dcv_session.notifications.ready.enabled'] = True
        self.context.email_templates.email_templates_dao.get_email_template.return_value = None
        with self.assertRaisesRegex(Exception, 'existing email template'):
            self.api.validate_schedule_and_templates(
                'vdc',
                {
                    'dcv_session': {
                        'notifications': {'ready': {'email_template': 'missing'}}
                    }
                },
            )

    def test_unrelated_module_does_not_read_desktop_schedule(self):
        self.api.validate_schedule_and_templates('cluster', {'timezone': 'UTC'})
        self.context.config.return_value.db.cluster_settings_table.get_item.assert_not_called()

    def test_catalog_receives_attachment_providers(self):
        from unittest.mock import patch

        storage = Mock()
        storage.as_plain_ordered_dict.return_value = {
            'apps': {'provider': 'efs'},
            'data': {'provider': 'fsx_netapp_ontap'},
        }
        self.context.config.return_value.get_config.side_effect = (
            lambda key, **kwargs: storage if key == 'shared-storage' else None
        )
        with patch(
            'ideaclustermanager.app.api.cluster_settings_api.settings_catalog',
            return_value=[],
        ) as catalog:
            self.api.settings_catalog()
        self.assertEqual(
            catalog.call_args.args[0], {'apps': 'efs', 'data': 'fsx_netapp_ontap'}
        )

    def test_email_master_validates_enabled_events_without_cross_module_writes(self):
        self.context.config.return_value.get_module_id.side_effect = (
            lambda module: 'vdc' if module == 'virtual-desktop-controller' else module
        )
        self.stored['vdc.dcv_session.notifications.ready.enabled'] = True
        self.stored['vdc.dcv_session.notifications.ready.email_template'] = 'missing'
        self.context.email_templates.email_templates_dao.get_email_template.return_value = None
        with self.assertRaisesRegex(Exception, 'existing email template'):
            self.api.validate_schedule_and_templates(
                'cluster-manager', {'notifications': {'email': {'enabled': True}}}
            )
        self.context.config.return_value.db.sync_cluster_settings_in_db.assert_not_called()


def test_fetch_denies_before_payload_or_aws_access():
    from ideadatamodel import exceptions
    from ideasdk.api.api_invocation_context import ApiInvocationContext
    import pytest

    app = Mock()
    app.module_id.return_value = 'cluster-manager'
    api = ClusterSettingsAPI(app)
    for authenticated, authorized in ((False, True), (True, False)):
        invocation = Mock(spec=ApiInvocationContext)
        invocation.namespace = 'ClusterSettings.FetchPricingRates'
        invocation.is_authenticated.return_value = authenticated
        invocation.is_authorized.return_value = authorized
        with pytest.raises(exceptions.SocaException):
            api.invoke(invocation)
        invocation.get_request_payload_as.assert_not_called()
        invocation.success.assert_not_called()
    app.aws.assert_not_called()
    app.config.assert_not_called()


def test_fetch_application_scope_is_module_read():
    from ideasdk.api.api_invocation_context import ApiInvocationContext
    from ideadatamodel.cluster_settings import FetchPricingRatesRequest

    app = Mock()
    app.module_id.return_value = 'cluster-manager'
    app.aws.return_value.aws_partition.return_value = 'aws-us-gov'
    invocation = Mock(spec=ApiInvocationContext)
    invocation.namespace = 'ClusterSettings.FetchPricingRates'
    invocation.is_administrator.return_value = False
    invocation.is_manager.return_value = False
    invocation.is_authorized_app.return_value = True
    invocation.is_scope_authorized.side_effect = (
        lambda scope: scope == 'cluster-manager/read'
    )
    invocation.is_authorized = lambda **kwargs: ApiInvocationContext.is_authorized(
        invocation, **kwargs
    )
    invocation.get_request_payload_as.return_value = FetchPricingRatesRequest(
        region='us-east-2'
    )
    ClusterSettingsAPI(app).invoke(invocation)
    invocation.success.assert_called_once()
    app.aws().get_client.assert_not_called()
    app.config.assert_not_called()


def scoped_invocation(role='scheduler', app_scope=None):
    from types import SimpleNamespace
    from ideasdk.auth import ApiAuthorizationType
    from ideasdk.api.api_invocation_context import ApiInvocationContext

    app = Mock()
    app.module_id.return_value = 'cluster-manager'
    modules = {
        'scheduler': 'batch',
        'virtual-desktop-controller': 'desk',
        'cluster-manager': 'portal',
        'ecs': 'ecs',
    }
    app.get_cluster_modules.return_value = [
        {'name': name, 'module_id': mid, 'status': 'deployed'}
        for name, mid in modules.items()
    ]
    app.config().is_module_enabled.side_effect = lambda name: name in modules
    app.config().get_module_id.side_effect = modules.get
    app.get_cluster_module_info.side_effect = lambda mid: next(
        (
            {'name': name, 'module_id': mid}
            for name, value in modules.items()
            if value == mid
        ),
        None,
    )
    app.accounts.group_name_helper.get_module_administrators_group.side_effect = (
        lambda module_id: f'{module_id}-admins'
    )
    app.config().get_config.return_value = None
    invocation = Mock(spec=ApiInvocationContext)
    invocation.is_authenticated.return_value = True
    invocation.is_administrator.return_value = role == 'administrator'
    invocation.is_manager.return_value = role in ('manager', 'cluster-manager')
    invocation.is_authorized_app.return_value = app_scope is not None
    invocation.is_scope_authorized.side_effect = lambda scope: scope == app_scope
    invocation.is_authorized = lambda **kwargs: ApiInvocationContext.is_authorized(
        invocation, **kwargs
    )
    invocation.get_authorization.return_value = SimpleNamespace(
        type=ApiAuthorizationType.APP
        if app_scope
        else ApiAuthorizationType.ADMINISTRATOR
        if role == 'administrator'
        else ApiAuthorizationType.MANAGER
        if role == 'manager'
        else ApiAuthorizationType.USER,
        groups=[f'{modules.get(role)}-admins'],
    )
    return app, invocation


def test_scheduler_administrator_catalog_read_write_and_fetch():
    from unittest.mock import patch
    from ideadatamodel.cluster_settings import (
        UpdateModuleSettingsRequest,
        FetchPricingRatesRequest,
    )

    app, invocation = scoped_invocation()
    api = ClusterSettingsAPI(app)
    api.describe_settings_catalog(invocation)
    assert {item.module for item in invocation.success.call_args.args[0].settings} == {
        'scheduler'
    }
    settings = Mock()
    settings.as_plain_ordered_dict.return_value = {
        'cost_estimation': {'provisioned_iops': 0.1}
    }
    app.config().get_config.return_value = settings
    invocation.get_request_payload_as.return_value = GetModuleSettingsRequest(
        module_id='batch'
    )
    api.get_module_settings(invocation)
    assert (
        invocation.success.call_args.args[0].settings
        == settings.as_plain_ordered_dict()
    )
    app.config().get_config.return_value = None
    invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id='batch', settings={'cost_estimation': {'provisioned_iops': 0.2}}
    )
    api.update_module_settings(invocation)
    assert (
        app.config().db.sync_cluster_settings_in_db.call_args.kwargs['source'] == 'api'
    )
    invocation.get_request_payload_as.return_value = FetchPricingRatesRequest(
        region='us-east-2'
    )
    with patch(
        'ideaclustermanager.app.api.cluster_settings_api.fetch_pricing_rates'
    ) as fetch:
        api.fetch_pricing_rates(invocation)
        fetch.assert_called_once_with(app, 'us-east-2')


def test_scheduler_administrator_cannot_write_other_modules_or_read_their_settings():
    import pytest
    from ideadatamodel import exceptions
    from ideadatamodel.cluster_settings import UpdateModuleSettingsRequest

    app, invocation = scoped_invocation()
    api = ClusterSettingsAPI(app)
    for module_id in ('portal', 'desk', 'global-settings', 'cluster'):
        invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
            module_id=module_id, settings={}
        )
        with pytest.raises(exceptions.SocaException):
            api.update_module_settings(invocation)
    app.config().db.sync_cluster_settings_in_db.assert_not_called()
    settings = Mock()
    settings.as_plain_ordered_dict.return_value = {'client_secret': 'private'}
    app.config().get_config.return_value = settings
    invocation.get_request_payload_as.return_value = GetModuleSettingsRequest(
        module_id='portal'
    )
    api.get_module_settings(invocation)
    assert 'client_secret' not in invocation.success.call_args.args[0].settings


def test_application_catalog_scopes_remain_required():
    import pytest
    from ideadatamodel import exceptions
    from ideadatamodel.cluster_settings import UpdateModuleSettingsRequest

    for scope in ('cluster-manager/read', 'cluster-manager/write', 'batch/read'):
        app, invocation = scoped_invocation(app_scope=scope)
        api = ClusterSettingsAPI(app)
        invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
            module_id='batch', settings={'cost_estimation': {'provisioned_iops': 0.2}}
        )
        if scope == 'cluster-manager/read':
            api.describe_settings_catalog(invocation)
        else:
            with pytest.raises(exceptions.SocaException):
                api.describe_settings_catalog(invocation)
        if scope == 'cluster-manager/write':
            api.update_module_settings(invocation)
        else:
            with pytest.raises(exceptions.SocaException):
                api.update_module_settings(invocation)


def test_service_capability_and_inventory_follow_deployed_module_access():
    import pytest
    from ideadatamodel import exceptions

    for role, expected in [
        ('scheduler', ['batch']),
        (
            'virtual-desktop-controller',
            ['desk-controller', 'desk-broker', 'desk-gateway'],
        ),
        ('cluster-manager', ['portal', 'extra']),
        (
            'manager',
            [
                'portal',
                'batch',
                'desk-controller',
                'desk-broker',
                'desk-gateway',
                'extra',
            ],
        ),
    ]:
        app, invocation = scoped_invocation(role)
        # Role words in the prefix must never affect service ownership.
        prefix = 'broker-lab'
        app.cluster_name.return_value = prefix
        settings = Mock()
        settings.as_plain_ordered_dict.return_value = {
            'cluster_name': 'configured',
            'private': 'hidden',
        }
        app.config().get_config.return_value = settings
        invocation.get_request_payload_as.return_value = GetModuleSettingsRequest(
            module_id='ecs'
        )
        api = ClusterSettingsAPI(app)
        api.get_module_settings(invocation)
        capability = invocation.success.call_args.args[0].settings
        assert capability['container_enabled'] is True
        if role in ('scheduler', 'virtual-desktop-controller'):
            assert capability == {'container_enabled': True}
        ecs = app.aws().get_client.return_value
        names = [
            'portal',
            'batch',
            'desk-controller',
            'desk-broker',
            'desk-gateway',
            'extra',
        ]
        ecs.list_services.return_value = {'serviceArns': names}
        ecs.describe_services.return_value = {
            'services': [
                {'serviceName': f'{prefix}-{name}', 'taskDefinition': 'definition'}
                for name in names
            ]
        }
        ecs.describe_task_definition.return_value = {'taskDefinition': {}}
        ecs.list_tasks.return_value = {'taskArns': []}
        api.list_cluster_services(invocation)
        assert [row.name for row in invocation.success.call_args.args[0].listing] == [
            f'{prefix}-{name}' for name in expected
        ]
        assert ecs.list_tasks.call_count == len(expected)
        for module in app.get_cluster_modules.return_value:
            if module['name'] in ('scheduler', 'virtual-desktop-controller'):
                module['status'] = 'not-deployed'
        if role in ('scheduler', 'virtual-desktop-controller'):
            with pytest.raises(exceptions.SocaException):
                api.list_cluster_services(invocation)
        else:
            api.list_cluster_services(invocation)
            assert [
                row.name for row in invocation.success.call_args.args[0].listing
            ] == [f'{prefix}-portal', f'{prefix}-extra']


def test_service_reads_deny_regular_users_and_require_app_read_scope():
    import pytest
    from ideadatamodel import exceptions

    for scope in (None, 'batch/read', 'cluster-manager/write', 'cluster-manager/read'):
        app, invocation = scoped_invocation(role='user', app_scope=scope)
        settings = Mock()
        settings.as_plain_ordered_dict.return_value = {'cluster_name': 'configured'}
        app.config().get_config.return_value = settings
        invocation.get_request_payload_as.return_value = GetModuleSettingsRequest(
            module_id='ecs'
        )
        api = ClusterSettingsAPI(app)
        api.get_module_settings(invocation)
        if scope == 'cluster-manager/read':
            assert (
                invocation.success.call_args.args[0].settings['container_enabled']
                is True
            )
            app.config().get_string.return_value = None
            api.list_cluster_services(invocation)
        else:
            assert invocation.success.call_args.args[0].settings == {}
            with pytest.raises(exceptions.SocaException):
                api.list_cluster_services(invocation)
            app.aws.assert_not_called()
