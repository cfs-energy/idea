"""
Test Cases for ClusterSettingsAPI validation
"""

import unittest

import pytest
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

    def get_config(self, key, default=None, required=False):
        return None

    def get_list(self, key, default=None):
        return self.values.get(key, default)

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

    def is_administrator(self):
        return True

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
        app = Mock()
        app.get_cluster_module_info.return_value = None
        self.api = ClusterSettingsAPI(app)

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

    def test_an_uncataloged_scheduler_setting_is_still_rejected(self):
        """Expanding the catalog must not make unknown paths writable."""

        with self.assertRaises(exceptions.SocaException) as context:
            self.api.validate_settings_allowed(
                'scheduler',
                {'compute_node_ami': 'ami-0123', 'unknown_setting': 'unused'},
            )

        self.assertIn(
            'not allowed to be updated via web UI: unknown_setting.',
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


class TestReconcileSettingsValidation(unittest.TestCase):
    def setUp(self):
        self.api = bedrock_settings_api()
        self.api.context.config().values[
            'cluster-manager.accounts.reconcile.okta.approved_origins'
        ] = ['https://id.example.invalid', 'https://new.example.invalid']
        self.api.context.config().db = Mock()
        self.api.context.config().db.cluster_settings_table.get_item.return_value = {}

    def validate(self, values):
        settings = {'accounts': {'reconcile': values}}
        self.api.validate_settings_allowed('cluster-manager', settings)
        self.api.validate_reconcile_settings('cluster-manager', settings)

    def test_all_fields_are_allowed(self):
        self.validate(
            dict(
                enabled=True,
                dry_run=True,
                reenable=True,
                check_cognito=False,
                interval_minutes=60,
                max_disable_fraction=0.25,
                okta=dict(org_url='', api_token_secret_arn=''),
            )
        )

    def test_invalid_values(self):
        for key, values in {
            'interval_minutes': [0, 1441, 1.5, True, '60'],
            'max_disable_fraction': [-0.1, 1.1, float('nan'), True, '0.25'],
            'enabled': ['true'],
            'dry_run': [0],
            'reenable': [1],
            'check_cognito': [None],
        }.items():
            for value in values:
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(exceptions.SocaException),
                ):
                    self.validate({key: value})

    def test_checkpoints_are_not_editable(self):
        for key in ('last_completed', 'last_run', 'last_saved'):
            with self.subTest(key=key), self.assertRaises(exceptions.SocaException):
                self.validate({key: 0})

    def test_boundaries(self):
        for minutes in (1, 1440):
            for fraction in (0, 1):
                self.validate(
                    dict(interval_minutes=minutes, max_disable_fraction=fraction)
                )

    def test_okta_shapes_and_partial_edits(self):
        token_arn = (
            'arn:aws:secretsmanager:us-east-2:123456789012:secret:directory-token'
        )
        self.validate(
            {
                'okta': dict(
                    org_url='https://id.example.invalid', api_token_secret_arn=token_arn
                )
            }
        )
        self.api.context.config().db.cluster_settings_table.get_item.side_effect = (
            lambda Key, ConsistentRead: {
                'Item': {
                    'value': token_arn
                    if Key['key'].endswith('api_token_secret_arn')
                    else 'https://id.example.invalid'
                }
            }
        )
        self.validate({'okta': {'org_url': 'https://new.example.invalid/'}})
        for org in (
            'http://id.example.invalid',
            'https://id.example.invalid/path',
            'https://user@id.example.invalid',
            'https://id.example.invalid:8443',
            'https://id.example.invalid?q=x',
        ):
            with self.subTest(org=org), self.assertRaises(exceptions.SocaException):
                self.validate({'okta': {'org_url': org}})
        for secret in ('token', 'arn:aws:iam::role/token', ''):
            with (
                self.subTest(secret=secret),
                self.assertRaises(exceptions.SocaException),
            ):
                self.validate({'okta': {'api_token_secret_arn': secret}})

    def test_validation_precedes_write(self):
        request = UpdateModuleSettingsRequest(
            module_id='cluster-manager',
            settings={'accounts': {'reconcile': {'interval_minutes': 0}}},
        )
        with self.assertRaises(exceptions.SocaException):
            self.api.update_module_settings(FakeApiInvocationContext(request))
        self.api.context.config().db.sync_cluster_settings_in_db.assert_not_called()

    def test_update_writes_flat_reconcile_keys(self):
        self.api.context.accounts = Mock()
        invocation = Mock()
        invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
            module_id='cluster-manager',
            settings={
                'accounts': {'reconcile': {'reenable': True, 'interval_minutes': 60}}
            },
        )
        self.api.update_module_settings(invocation)
        self.api.context.config().db.sync_cluster_settings_in_db.assert_called_once_with(
            config_entries=[
                {'key': 'cluster-manager.accounts.reconcile.reenable', 'value': True},
                {
                    'key': 'cluster-manager.accounts.reconcile.interval_minutes',
                    'value': 60,
                },
            ],
            overwrite=True,
            source='api',
        )
        invocation.success.assert_called_once()
        self.api.context.accounts.reconciler.settings_changed.assert_called_once()
        self.api.context.config().db.set_config_entry.assert_called_once()


def test_manager_cannot_write_reconciliation_settings():
    from ideasdk.api.api_invocation_context import ApiInvocationContext

    api = bedrock_settings_api()
    api.context.config().db = Mock()
    invocation = Mock(spec=ApiInvocationContext)
    invocation.is_authorized = lambda **kwargs: ApiInvocationContext.is_authorized(
        invocation, **kwargs
    )
    invocation.is_administrator.return_value = False
    invocation.is_manager.return_value = True
    invocation.is_authorized_app.return_value = False
    invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id='cluster-manager',
        settings={
            'accounts': {
                'reconcile': {
                    'enabled': True,
                    'dry_run': False,
                    'max_disable_fraction': 1,
                }
            }
        },
    )
    assert invocation.is_authorized(elevated_access=True)
    with pytest.raises(exceptions.SocaException):
        api.update_module_settings(invocation)
    api.context.config().db.sync_cluster_settings_in_db.assert_not_called()


def test_portal_cannot_approve_token_destinations():
    api = bedrock_settings_api()
    with pytest.raises(exceptions.SocaException):
        api.validate_settings_allowed(
            'cluster-manager',
            {
                'accounts': {
                    'reconcile': {
                        'okta': {
                            'approved_origins': ['https://unapproved.example.invalid']
                        }
                    }
                }
            },
        )


def test_partial_okta_edit_reads_committed_partner():
    api = bedrock_settings_api()
    config = api.context.config()
    config.values['cluster-manager.accounts.reconcile.okta.approved_origins'] = [
        'https://id.example.invalid'
    ]
    config.db = Mock()
    config.db.get_config_entry.return_value = {
        'value': 'arn:aws:secretsmanager:us-east-2:123456789012:secret:directory-token'
    }

    def read(Key, ConsistentRead=False):
        # The stale replica predates the secret removal; the committed value is empty.
        if Key['key'].endswith('api_token_secret_arn') and not ConsistentRead:
            return {'Item': {'value': 'secret-ref'}}
        return {'Item': {'value': ''}}

    config.db.cluster_settings_table.get_item.side_effect = read
    with pytest.raises(exceptions.SocaException, match='Both Okta settings'):
        api.validate_reconcile_settings(
            'cluster-manager',
            {
                'accounts': {
                    'reconcile': {'okta': {'org_url': 'https://id.example.invalid'}}
                }
            },
        )
    assert all(
        call.kwargs['ConsistentRead']
        for call in config.db.cluster_settings_table.get_item.call_args_list
    )


def test_operations_group_template_matches_upgrade_default():
    from pathlib import Path

    from jinja2 import Template
    import yaml
    from ideasdk.utils.group_name_helper import DEFAULT_OPERATIONS_LEADS_GROUP_NAME

    template = (
        Path(__file__).resolve().parents[3]
        / 'ideactl/resources/config/templates/identity-provider/settings.yml'
    )
    settings = yaml.safe_load(
        Template(template.read_text()).render(identity_provider='cognito-idp')
    )
    assert (
        settings['cognito']['operations_leads_group_name']
        == DEFAULT_OPERATIONS_LEADS_GROUP_NAME
    )


@pytest.fixture
def operations_group_catalog_entry():
    from ideaclustermanager.app.settings_catalog import CATALOG

    entries = [
        entry
        for entry in CATALOG
        if entry['key'] == 'identity-provider.cognito.operations_leads_group_name'
    ]
    assert len(entries) == 1, 'operations leads catalog integration is required'
    return entries[0]


def test_operations_group_catalog_matches_default(operations_group_catalog_entry):
    from ideasdk.utils.group_name_helper import DEFAULT_OPERATIONS_LEADS_GROUP_NAME

    assert (
        operations_group_catalog_entry['default'] == DEFAULT_OPERATIONS_LEADS_GROUP_NAME
    )
    assert operations_group_catalog_entry['value_type'] == 'string'


@pytest.mark.parametrize(
    'group_name', ['report-readers', 'report-readers-cluster-group']
)
def test_operations_group_catalog_accepts_custom_names(
    operations_group_catalog_entry, group_name
):
    from ideaclustermanager.app.settings_catalog import coerce_settings

    settings, _ = coerce_settings(
        'identity-provider',
        {
            'cognito': {
                'operations_leads_group_name': group_name,
            }
        },
    )
    assert settings['cognito']['operations_leads_group_name'] in (
        group_name,
        'report-readers-cluster-group',
    )


@pytest.mark.parametrize(
    'privileged_name,operations_name',
    [
        ('administrators_group_name', 'privileged-readers'),
        ('administrators_group_name', 'privileged-readers-cluster-group'),
        ('managers_group_name', 'privileged-readers'),
        ('managers_group_name', 'privileged-readers-cluster-group'),
    ],
)
def test_operations_group_catalog_rejects_normalized_collisions(
    operations_group_catalog_entry,
    privileged_name,
    operations_name,
):
    from ideaclustermanager.app.settings_catalog import coerce_settings

    with pytest.raises(exceptions.SocaException, match='privileged group'):
        coerce_settings(
            'identity-provider',
            {
                'cognito': {
                    privileged_name: 'privileged-readers',
                    'operations_leads_group_name': operations_name,
                }
            },
        )


@pytest.mark.parametrize(
    'group_name',
    [
        'cluster-manager-administrators-module-group',
        'cluster-manager-administrators-module-group-cluster-group',
    ],
)
def test_operations_group_catalog_rejects_module_admin_collisions(
    operations_group_catalog_entry,
    group_name,
):
    from ideaclustermanager.app.settings_catalog import coerce_settings

    with pytest.raises(exceptions.SocaException, match='privileged group'):
        coerce_settings(
            'identity-provider',
            {
                'cognito': {
                    'operations_leads_group_name': group_name,
                }
            },
        )


@pytest.mark.parametrize(
    'saved_key,updated_key',
    [
        ('administrators_group_name', 'operations_leads_group_name'),
        ('managers_group_name', 'operations_leads_group_name'),
        ('operations_leads_group_name', 'administrators_group_name'),
        ('operations_leads_group_name', 'managers_group_name'),
    ],
)
def test_partial_group_edit_rejects_saved_privileged_collision(saved_key, updated_key):
    api = bedrock_settings_api()
    config = api.context.config()
    config.db = Mock()
    config.db.cluster_settings_table.get_item.side_effect = (
        lambda Key, ConsistentRead: (
            {'Item': {'value': 'custom-readers'}}
            if Key['key'] == f'identity-provider.cognito.{saved_key}'
            else {}
        )
    )
    api.context.get_cluster_modules = lambda: [{'module_id': 'compute'}]
    with pytest.raises(exceptions.SocaException, match='privileged group'):
        api.update_module_settings(
            FakeApiInvocationContext(
                UpdateModuleSettingsRequest(
                    module_id='identity-provider',
                    settings={'cognito': {updated_key: 'custom-readers-cluster-group'}},
                )
            )
        )


def test_sequential_group_saves_validate_against_committed_settings():
    api = bedrock_settings_api()
    config = api.context.config()
    committed = {}
    config.db = Mock()

    def read(Key, ConsistentRead):
        assert ConsistentRead is True
        value = committed.get(Key['key'])
        return {'Item': {'value': value}} if value is not None else {}

    def write(config_entries, overwrite, source):
        assert overwrite is True
        assert source == 'api'
        committed.update({entry['key']: entry['value'] for entry in config_entries})

    config.db.cluster_settings_table.get_item.side_effect = read
    config.db.sync_cluster_settings_in_db.side_effect = write
    api.context.get_cluster_modules = lambda: [{'module_id': 'compute'}]

    def save(key):
        invocation = Mock()
        invocation.is_authorized.return_value = True
        invocation.is_administrator.return_value = True
        invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
            module_id='identity-provider',
            settings={'cognito': {key: 'custom-readers'}},
        )
        api.update_module_settings(invocation)

    save('administrators_group_name')
    assert 'identity-provider.cognito.administrators_group_name' not in config.values
    with pytest.raises(exceptions.SocaException, match='privileged group'):
        save('operations_leads_group_name')
    config.db.sync_cluster_settings_in_db.assert_called_once()


def test_partial_group_edit_rejects_actual_module_id_collision():
    api = bedrock_settings_api()
    api.context.config().db = Mock()
    api.context.config().db.cluster_settings_table.get_item.return_value = {}
    api.context.get_cluster_modules = lambda: [{'module_id': 'compute'}]
    with pytest.raises(exceptions.SocaException, match='privileged group'):
        api.update_module_settings(
            FakeApiInvocationContext(
                UpdateModuleSettingsRequest(
                    module_id='identity-provider',
                    settings={
                        'cognito': {
                            'operations_leads_group_name': 'compute-administrators-module-group'
                        }
                    },
                )
            )
        )


def test_settings_api_stamps_source_with_the_value_and_version():
    from ideasdk.config.cluster_config_db import ClusterConfigDB

    api = bedrock_settings_api()
    db = ClusterConfigDB.__new__(ClusterConfigDB)
    db.log_info = Mock()
    db.cluster_settings_table = Mock()
    db.cluster_settings_table.get_item.return_value = {
        'Item': {'value': 30, 'source': 'template', 'version': 1}
    }
    api.context.config().db = db
    api.context.accounts = Mock()
    invocation = Mock()
    invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id='cluster-manager',
        settings={'accounts': {'reconcile': {'interval_minutes': 60}}},
    )
    api.update_module_settings(invocation)
    writes = db.cluster_settings_table.update_item.call_args_list
    settings_write = next(
        call.kwargs
        for call in writes
        if call.kwargs['Key']['key'].endswith('.interval_minutes')
    )
    assert settings_write['UpdateExpression'] == (
        'SET #value=:value, #source=:source ADD #version :version'
    )
    assert settings_write['ExpressionAttributeNames']['#source'] == 'source'
    assert settings_write['ExpressionAttributeValues'] == {
        ':value': 60,
        ':version': 1,
        ':source': 'api',
    }
    invocation.success.assert_called_once()


def test_fractional_io1_rate_survives_api_save():
    api = bedrock_settings_api()
    config = api.context.config()
    config.db = Mock()
    invocation = Mock()
    invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id='scheduler',
        settings={'cost_estimation': {'provisioned_iops': '0.065'}},
    )
    api.update_module_settings(invocation)
    entries = config.db.sync_cluster_settings_in_db.call_args.kwargs['config_entries']
    assert entries == [
        {'key': 'scheduler.cost_estimation.provisioned_iops', 'value': 0.065}
    ]
    assert type(entries[0]['value']) is float
    assert invocation.success.call_args.args[0].effects == {
        'cost_estimation.provisioned_iops': 'runtime'
    }


@pytest.mark.parametrize('value', [-0.065, float('nan'), float('inf'), True])
def test_invalid_io1_rate_does_not_write(value):
    api = bedrock_settings_api()
    api.context.config().db = Mock()
    invocation = Mock()
    invocation.get_request_payload_as.return_value = UpdateModuleSettingsRequest(
        module_id='scheduler', settings={'cost_estimation': {'provisioned_iops': value}}
    )
    with pytest.raises(exceptions.SocaException):
        api.update_module_settings(invocation)
    api.context.config().db.sync_cluster_settings_in_db.assert_not_called()
