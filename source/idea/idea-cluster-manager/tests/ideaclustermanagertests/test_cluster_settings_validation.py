"""
Test Cases for ClusterSettingsAPI validation
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI
from ideadatamodel import exceptions, errorcodes
from ideadatamodel.cluster_settings import UpdateModuleSettingsRequest
from ideadatamodel import constants


class FakeConfig:
    def __init__(self, partition='aws', region='us-east-1'):
        self.values = {
            'cluster.aws.partition': partition,
            'cluster.aws.region': region,
        }

    def get_module_id(self, module_name):
        return module_name

    def get_string(self, key, default=None, required=False, module_id=None):
        return self.values.get(key, default)


def bedrock_settings_api(partition='aws', region='us-east-1') -> ClusterSettingsAPI:
    config = FakeConfig(partition=partition, region=region)
    return ClusterSettingsAPI(
        SimpleNamespace(
            config=lambda: config,
            module_id=lambda: constants.MODULE_CLUSTER_MANAGER,
        )
    )


class FakeApiInvocationContext:
    """
    a settings write reaches config().db, which the fake config does not carry,
    so a request that is not rejected fails here rather than passing quietly.
    """

    def __init__(self, request):
        self.request = request

    def is_authorized(self, elevated_access=False, scopes=None):
        return True

    def get_request_payload_as(self, payload_type):
        return self.request

    def success(self, result):
        raise AssertionError('the settings write was reached')


class TestBedrockCatalogValidation(unittest.TestCase):
    def test_global_model_id_is_rejected(self):
        """the geographic id to use instead is named in the error"""
        api = bedrock_settings_api()

        with self.assertRaises(exceptions.SocaException) as context:
            api.validate_bedrock_settings(
                'cluster-manager', {'bedrock': {'model_ids': ['global.vendor.model-1']}}
            )

        message = str(context.exception)
        self.assertIn('global.vendor.model-1', message)
        self.assertIn('us.vendor.model-1', message)

    def test_cross_region_and_bare_model_ids_are_accepted(self):
        api = bedrock_settings_api()

        api.validate_bedrock_settings(
            'cluster-manager',
            {'bedrock': {'model_ids': ['us.vendor.model-1', 'vendor.model-2']}},
        )

    def test_gov_model_id_is_accepted_in_a_gov_partition(self):
        api = bedrock_settings_api(partition='aws-us-gov', region='us-gov-west-1')

        api.validate_bedrock_settings(
            'cluster-manager',
            {'bedrock': {'model_ids': ['us-gov.vendor.model-1', 'vendor.model-2']}},
        )

    def test_the_update_api_rejects_a_global_model_id_before_writing(self):
        api = bedrock_settings_api()
        request = UpdateModuleSettingsRequest(
            module_id='cluster-manager',
            settings={'bedrock': {'model_ids': ['global.vendor.model-1']}},
        )

        with self.assertRaises(exceptions.SocaException) as context:
            api.update_module_settings(FakeApiInvocationContext(request))

        self.assertIn('global.vendor.model-1', str(context.exception))

    def test_another_module_is_not_checked(self):
        api = bedrock_settings_api()

        api.validate_bedrock_settings(
            'vdc', {'bedrock': {'model_ids': ['global.vendor.model-1']}}
        )


class TestClusterSettingsValidation(unittest.TestCase):
    def setUp(self):
        self.api = ClusterSettingsAPI(Mock())

    def test_allowed_settings_valid(self):
        """Test that allowed settings pass validation"""
        module_id = 'vdc'

        # Valid settings that should pass
        valid_settings = {
            'dcv_session': {
                'idle_timeout': 60,
                'instance_types': {'allow': ['t3.xlarge', 'm5.large']},
                'network': {
                    'subnet_autoretry': True,
                    'randomize_subnets': False,
                    'private_subnets': ['subnet-12345', 'subnet-67890'],
                },
                'working_hours': {'start_up_time': '09:00', 'shut_down_time': '17:00'},
            }
        }

        # Should not raise an exception
        try:
            self.api.validate_settings_allowed(module_id, valid_settings)
        except Exception as e:
            self.fail(f'Valid settings failed validation: {e}')

    def test_disallowed_settings_invalid(self):
        """Test that non-whitelisted settings are rejected"""
        module_id = 'vdc'

        # Invalid settings that should fail
        invalid_settings = {
            'dcv_session': {
                'secret_key': 'should-not-be-allowed',  # Not in whitelist
                'idle_timeout': 60,  # This one is allowed
            }
        }

        # Should raise an exception
        with self.assertRaises(exceptions.SocaException) as context:
            self.api.validate_settings_allowed(module_id, invalid_settings)

        # Verify the error message contains the invalid setting
        self.assertIn('secret_key', str(context.exception))
        self.assertIn('not allowed to be updated', str(context.exception))

    def test_unknown_module_rejects_all(self):
        """Test that unknown modules reject all settings"""
        module_id = 'unknown-module'

        settings = {'any_setting': 'any_value'}

        # Should raise an exception since module not in whitelist
        with self.assertRaises(exceptions.SocaException):
            self.api.validate_settings_allowed(module_id, settings)

    def test_nested_path_extraction(self):
        """Test that nested setting paths are correctly extracted"""
        module_id = 'vdc'

        # Deeply nested settings
        settings = {
            'dcv_session': {
                'instance_types': {
                    'allow': ['t3.xlarge'],
                    'deny': ['p3.xlarge'],
                    'invalid_nested': 'not-allowed',  # This should fail
                }
            }
        }

        with self.assertRaises(exceptions.SocaException) as context:
            self.api.validate_settings_allowed(module_id, settings)

        self.assertIn('invalid_nested', str(context.exception))

    def test_the_scheduler_default_image_is_allowed(self):
        """the custom amis page writes this flat key when a build is adopted"""

        self.api.validate_settings_allowed(
            'scheduler', {'compute_node_ami': 'ami-0123'}
        )

    def test_another_scheduler_setting_is_still_rejected(self):
        """the allowance is one key wide, not the whole scheduler module"""

        with self.assertRaises(exceptions.SocaException) as context:
            self.api.validate_settings_allowed(
                'scheduler',
                {'compute_node_ami': 'ami-0123', 'compute_node_os': 'rocky9'},
            )

        self.assertIn(
            'not allowed to be updated via web UI: compute_node_os.',
            str(context.exception),
        )


if __name__ == '__main__':
    print('Testing cluster settings validation...')

    # Quick manual test
    api = ClusterSettingsAPI(Mock())

    # Test 1: Valid setting
    try:
        api.validate_settings_allowed('vdc', {'dcv_session': {'idle_timeout': 60}})
        print('✅ Valid setting passed validation')
    except Exception as e:
        print(f'❌ Valid setting failed: {e}')

    # Test 2: Invalid setting
    try:
        api.validate_settings_allowed(
            'vdc', {'dcv_session': {'secret_password': 'hack'}}
        )
        print('❌ Invalid setting passed validation (should have failed)')
    except Exception as e:
        print(f'✅ Invalid setting correctly rejected: {e}')

    # Run unit tests
    unittest.main(argv=[''], exit=False, verbosity=2)


class FakeConfigDb:
    """dynamodb as it stands immediately after the settings write."""

    def __init__(self, entries):
        self.entries = entries
        self.reads = []

    def get_config_entry(self, key):
        self.reads.append(key)
        if key not in self.entries:
            return None
        return {'key': key, 'value': self.entries[key]}


class StaleConfig(FakeConfig):
    """
    the in-memory tree as the enqueue actually sees it: built before the write, so it
    still answers with the pre-change values. db answers with what was stored.
    """

    def __init__(self, stale_values, db):
        super().__init__()
        self.values.update(stale_values)
        self.db = db

    def get_bool(self, key, default=None, required=False, module_id=None):
        return self.values.get(key, default)

    def get_list(self, key, default=None, required=False, module_id=None):
        return self.values.get(key, default)


class FakeProjectsService:
    def __init__(self):
        self.calls = []

    def send_bedrock_reconcile_all(self, cluster_bedrock=None):
        self.calls.append(cluster_bedrock)


def stale_settings_api(stale_values, stored_values):
    db = FakeConfigDb(stored_values)
    config = StaleConfig(stale_values, db)
    projects = FakeProjectsService()
    api = ClusterSettingsAPI(
        SimpleNamespace(
            config=lambda: config,
            projects=projects,
            logger=lambda *args, **kwargs: Mock(),
            module_id=lambda: constants.MODULE_CLUSTER_MANAGER,
        )
    )
    return api, projects, db


MODULE_ID = constants.MODULE_CLUSTER_MANAGER


class TestBedrockReconcileReadsStoredSettings(unittest.TestCase):
    def test_a_disable_is_carried_even_though_the_config_still_says_enabled(self):
        api, projects, _db = stale_settings_api(
            stale_values={
                f'{MODULE_ID}.bedrock.enabled': True,
                f'{MODULE_ID}.bedrock.model_ids': ['us.vendor-a.model-1'],
            },
            stored_values={
                f'{MODULE_ID}.bedrock.enabled': False,
                f'{MODULE_ID}.bedrock.model_ids': ['us.vendor-a.model-1'],
            },
        )

        api.reconcile_bedrock_projects(MODULE_ID, {'bedrock': {'enabled': False}})

        self.assertEqual(len(projects.calls), 1)
        self.assertEqual(projects.calls[0]['enabled'], False)

    def test_a_removed_model_is_carried_even_though_the_config_still_lists_it(self):
        api, projects, _db = stale_settings_api(
            stale_values={
                f'{MODULE_ID}.bedrock.enabled': True,
                f'{MODULE_ID}.bedrock.model_ids': ['keep.me', 'drop.me'],
            },
            stored_values={
                f'{MODULE_ID}.bedrock.enabled': True,
                f'{MODULE_ID}.bedrock.model_ids': ['keep.me'],
            },
        )

        api.reconcile_bedrock_projects(
            MODULE_ID, {'bedrock': {'model_ids': ['keep.me']}}
        )

        self.assertEqual(projects.calls[0]['model_ids'], ['keep.me'])

    def test_the_stored_values_are_read_from_the_database_not_the_config(self):
        api, _projects, db = stale_settings_api(
            stale_values={f'{MODULE_ID}.bedrock.enabled': True},
            stored_values={f'{MODULE_ID}.bedrock.enabled': False},
        )

        api.reconcile_bedrock_projects(MODULE_ID, {'bedrock': {'enabled': False}})

        self.assertIn(f'{MODULE_ID}.bedrock.enabled', db.reads)
        self.assertIn(f'{MODULE_ID}.bedrock.model_ids', db.reads)

    def test_an_unrelated_settings_change_enqueues_nothing(self):
        api, projects, _db = stale_settings_api(stale_values={}, stored_values={})

        api.reconcile_bedrock_projects(MODULE_ID, {'something_else': {'x': 1}})

        self.assertEqual(projects.calls, [])

    def test_an_enqueue_failure_is_reported_not_swallowed(self):
        # the setting is already written at this point, so a success reply would
        # claim the projects were brought in line when none were.
        api, projects, _db = stale_settings_api(
            stale_values={}, stored_values={f'{MODULE_ID}.bedrock.enabled': False}
        )

        def explode(cluster_bedrock=None):
            raise RuntimeError('sqs is having a day')

        projects.send_bedrock_reconcile_all = explode

        with self.assertRaises(exceptions.SocaException) as raised:
            api.reconcile_bedrock_projects(MODULE_ID, {'bedrock': {'enabled': False}})

        self.assertEqual(raised.exception.error_code, errorcodes.GENERAL_ERROR)
        self.assertIn(f'{MODULE_ID}.bedrock', raised.exception.message)
        self.assertIn('not reconciled', raised.exception.message)
