"""
Test Cases for ClusterSettingsAPI validation
"""

import unittest
from unittest.mock import Mock
from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI
from ideadatamodel import exceptions


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
