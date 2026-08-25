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
        """cluster-manager projects only the optional dashboard embed keys"""
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
            },
        )

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
        # the custom-dashboard paths are allowlisted for this module as well, so
        # their empty parent scaffold rides along.
        self.assertEqual(
            settings,
            {'bedrock': {'enabled': True}, 'web_portal': {'custom_dashboard': {}}},
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
