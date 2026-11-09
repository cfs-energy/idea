"""Drift checks: a wrong setting reports drift, a failed read reports unknown and never ok."""

import io
import re
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideaclustermanager.app.metrics import drift_checks as module
from ideaclustermanager.app.metrics.drift_checks import (
    DRIFT,
    MUST_MATCH,
    OK,
    UNKNOWN,
    DriftChecks,
    DriftCheckCollector,
    analytics_value,
    digest,
)
from ideaclustermanager.app.metrics.storage_metrics_service import StorageTarget
from ideasdk.metrics.health import CollectorRunner

SOURCE = Path(__file__).parents[3]
DC = ['10.0.0.10', '10.0.0.11']
ENDPOINT = 'svm-1.fs-0abc.fsx.us-east-2.amazonaws.com'


def test_an_aws_refusal_reads_as_what_it_is():
    denied = ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'no'}}, 'GetObject'
    )
    assert module.read_reason(denied) == 'access_denied'
    assert module.read_reason(RuntimeError('ONTAP returned 403')) == 'forbidden'
    assert module.read_reason(OSError('no route')) == 'unreachable'


def good_cifs(**security):
    return [
        {
            'svm': {'name': 'svm1'},
            'enabled': True,
            'security': {
                'session_security': 'sign',
                'advertised_kdc_encryptions': ['aes-128', 'aes-256'],
                **security,
            },
        }
    ]


GOOD_LDAP = [
    {'session_security': 'sign', 'status': {'state': 'up'}, 'svm': {'name': 'svm1'}}
]


def good_dns(servers=None, **dynamic):
    return [
        {
            'servers': DC if servers is None else servers,
            'dynamic_dns': {'enabled': True, 'use_secure': True, **dynamic},
        }
    ]


GOOD_VOLUMES = [
    {'name': 'root', 'is_svm_root': True},
    {'name': 'profiles', 'is_svm_root': False, 'analytics': {'state': 'on'}},
]


class FakeOntap:
    answers = {}
    calls = []

    def __init__(self, endpoint, username, password, verify_tls=False, timeout=60):
        self.timeout = timeout

    def get(self, path):
        FakeOntap.calls.append(path)
        for prefix, answer in FakeOntap.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f'unexpected read {path}')


class Config:
    def __init__(self, **overrides):
        self.values = {
            'directoryservice.directory_id': 'd-1',
            'cluster.cluster_s3_bucket': 'bucket',
            module.SSH_CLIENT_IP_KEY: ['1.2.3.4/32'],
            **{key: str(value) for key, value in MUST_MATCH.items()},
            **overrides,
        }

    def get_string(self, key, default=None, required=False):
        return self.values.get(key, default)

    def get_bool(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key, default=None):
        value = self.values.get(key)
        return default if value is None else int(value)

    def get_list(self, key, default=None):
        return self.values.get(key, default)

    def get_secret(self, key):
        return self.values.get('secret', 'the-metrics-password')

    def get_config(self, key, default=None):
        if key == 'shared-storage':
            return self.values.get(
                'shared-storage', {'data': {'provider': 'fsx_netapp_ontap'}}
            )
        return self.values.get(key, default)


def harness(monkeypatch, config=None, values_yml=b'ssh_client_ip:\n- 1.2.3.4/32\n'):
    monkeypatch.setattr(module, 'OntapClient', FakeOntap)
    FakeOntap.answers = {
        '/api/protocols/cifs/services': good_cifs(),
        '/api/name-services/ldap': GOOD_LDAP,
        '/api/name-services/dns': good_dns(),
        '/api/storage/volumes': GOOD_VOLUMES,
    }
    FakeOntap.calls = []
    context = Mock()
    context.module_id.return_value = 'cluster-manager'
    context.config.return_value = config or Config()
    aws = context.aws.return_value
    aws.ds.return_value.describe_directories.return_value = {
        'DirectoryDescriptions': [{'DnsIpAddrs': DC}]
    }
    aws.fsx.return_value.describe_file_systems.return_value = {
        'FileSystems': [
            {
                'StorageType': 'SSD',
                'OntapConfiguration': {
                    'DeploymentType': 'MULTI_AZ_2',
                    'AutomaticBackupRetentionDays': 30,
                    'DiskIopsConfiguration': {'Mode': 'AUTOMATIC'},
                    'HAPairs': 1,
                },
            }
        ]
    }
    aws.s3.return_value.get_object.return_value = {'Body': io.BytesIO(values_yml)}
    context.ldap_client.fetch_root_password.return_value = 'the-ad-secret'
    context.ldap_client.root_password_binds.return_value = True
    storage = Mock()
    storage.targets.return_value = [
        StorageTarget('data', ENDPOINT, 'idea-metrics', 'password-key')
    ]
    checks = DriftChecks(context, storage)
    published = {'state': [], 'value': []}
    checks.metrics = Mock()
    checks.metrics.level.side_effect = lambda name, value, **tags: published[
        name.split('.')[1]
    ].append((value, tags))
    checks.logger = Mock()
    checks.published = published
    return checks


def states(results):
    return {result.check: result.state for result in results}


def test_a_cluster_that_holds_everything_is_ok_everywhere(monkeypatch):
    checks = harness(monkeypatch)
    results = checks.run_once()
    found = states(results)
    assert set(found.values()) == {OK}
    assert set(found) == {
        'ontap_cifs_session_security',
        'ontap_ldap_session_security',
        'ontap_dns_domain_controllers',
        'ontap_cifs_aes',
        'ontap_secure_ddns',
        'ontap_machine_account',
        'ontap_analytics',
        'fsx_parity',
        'ad_agent_bind',
        'ssh_client_ip',
        'settings_must_match',
        'values_owned_settings',
    }


def test_wrong_dns_reports_drift(monkeypatch):
    checks = harness(monkeypatch)
    FakeOntap.answers['/api/name-services/dns'] = good_dns(servers=['10.10.0.2'])
    found = states(checks.run_once())
    assert found['ontap_dns_domain_controllers'] == DRIFT
    assert found['ontap_secure_ddns'] == OK


@pytest.mark.parametrize(
    'path, answer, check',
    [
        (
            '/api/protocols/cifs/services',
            good_cifs(session_security='none'),
            'ontap_cifs_session_security',
        ),
        (
            '/api/name-services/ldap',
            [{'session_security': 'none', 'status': {'state': 'up'}}],
            'ontap_ldap_session_security',
        ),
        (
            '/api/protocols/cifs/services',
            good_cifs(advertised_kdc_encryptions=['rc4']),
            'ontap_cifs_aes',
        ),
        (
            '/api/name-services/dns',
            good_dns(use_secure=False),
            'ontap_secure_ddns',
        ),
        (
            '/api/name-services/ldap',
            [{'session_security': 'sign', 'status': {'state': 'down'}}],
            'ontap_machine_account',
        ),
    ],
)
def test_a_wrong_ontap_setting_reports_drift(monkeypatch, path, answer, check):
    checks = harness(monkeypatch)
    FakeOntap.answers[path] = answer
    found = states(checks.run_once())
    assert found[check] == DRIFT
    # and that is what reaches datadog
    assert any(
        value == 2 and tags['check'] == check and tags['scope'] == 'data'
        for value, tags in checks.published['state']
    )


def test_signing_none_on_cifs_is_what_failed_on_three_clusters(monkeypatch):
    checks = harness(monkeypatch)
    FakeOntap.answers['/api/protocols/cifs/services'] = good_cifs(
        session_security='none'
    )
    assert states(checks.run_once())['ontap_cifs_session_security'] == DRIFT


@pytest.mark.parametrize(
    'path, affected',
    [
        (
            '/api/protocols/cifs/services',
            {'ontap_cifs_session_security', 'ontap_cifs_aes', 'ontap_machine_account'},
        ),
        (
            '/api/name-services/ldap',
            {'ontap_ldap_session_security', 'ontap_machine_account'},
        ),
        (
            '/api/name-services/dns',
            {'ontap_dns_domain_controllers', 'ontap_secure_ddns'},
        ),
        ('/api/storage/volumes', {'ontap_analytics'}),
    ],
)
def test_a_failing_ontap_read_reports_unknown_not_ok(monkeypatch, path, affected):
    checks = harness(monkeypatch)
    FakeOntap.answers[path] = RuntimeError(f'GET {path}: 403: role lacks the path')
    found = states(checks.run_once())
    for check in affected:
        assert found[check] == UNKNOWN
    assert all(
        result.detail == 'forbidden'
        for result in checks.run_once()
        if result.state == UNKNOWN and result.check != 'ontap_machine_account'
    )
    for check in set(module.ONTAP_CHECKS) - affected:
        assert found[check] == OK
    assert any(
        value == 1 and tags['check'] in affected
        for value, tags in checks.published['state']
    )


def test_a_field_missing_from_the_answer_is_unknown_not_ok(monkeypatch):
    checks = harness(monkeypatch)
    FakeOntap.answers['/api/protocols/cifs/services'] = [
        {'enabled': True, 'security': {}}
    ]
    FakeOntap.answers['/api/name-services/ldap'] = [{'status': {}}]
    FakeOntap.answers['/api/name-services/dns'] = [{'servers': DC}]
    found = states(checks.run_once())
    for check in (
        'ontap_cifs_session_security',
        'ontap_cifs_aes',
        'ontap_ldap_session_security',
        'ontap_machine_account',
        'ontap_secure_ddns',
    ):
        assert found[check] == UNKNOWN


def test_no_cifs_record_is_unknown_not_drift(monkeypatch):
    # a role without /api/svm/svms reads 200 and no records for a server that is joined
    checks = harness(monkeypatch)
    FakeOntap.answers['/api/protocols/cifs/services'] = []
    found = states(checks.run_once())
    for check in ('ontap_cifs_session_security', 'ontap_cifs_aes', 'ontap_machine_account'):
        assert found[check] == UNKNOWN
    assert found['ontap_ldap_session_security'] == OK
    assert not any(value == 2 for value, _ in checks.published['state'])


def test_domain_controllers_that_cannot_be_read_make_dns_unknown(monkeypatch):
    checks = harness(monkeypatch)
    checks.context.aws().ds().describe_directories.side_effect = RuntimeError('denied')
    assert states(checks.run_once())['ontap_dns_domain_controllers'] == UNKNOWN


def test_a_hung_read_is_left_behind_reported_and_not_started_twice(monkeypatch):
    checks = harness(monkeypatch)
    monkeypatch.setattr(module, 'GROUP_LIMIT_SECONDS', 0.05)
    release = threading.Event()
    started = []

    class HangingOntap(FakeOntap):
        def get(self, path):
            started.append(path)
            release.wait(10)
            return []

    monkeypatch.setattr(module, 'OntapClient', HangingOntap)
    began = time.monotonic()
    first = checks.run_once()
    assert time.monotonic() - began < 2
    assert states(first)['ontap_cifs_session_security'] == UNKNOWN
    assert [r.detail for r in first if r.check == 'ontap_cifs_session_security'] == [
        'timeout'
    ]
    second = checks.run_once()
    assert [r.detail for r in second if r.check == 'ontap_cifs_session_security'] == [
        'stuck'
    ]
    assert len(started) == 1
    release.set()


def test_only_metadata_paths_are_read_and_never_a_file(monkeypatch):
    checks = harness(monkeypatch)
    checks.run_once()
    assert FakeOntap.calls
    for path in FakeOntap.calls:
        assert '/files' not in path and 'quota' not in path


def test_ontap_with_no_login_is_unknown_and_efs_has_no_ontap_checks(monkeypatch):
    checks = harness(monkeypatch)
    checks.storage_metrics.targets.return_value = []
    assert states(checks.run_once())['ontap_cifs_aes'] == UNKNOWN

    efs = harness(
        monkeypatch, Config(**{'shared-storage': {'data': {'provider': 'efs'}}})
    )
    efs.storage_metrics.targets.return_value = []
    assert not [c for c in states(efs.run_once()) if c.startswith('ontap_')]


def test_the_ad_secret_is_bound_and_never_logged(monkeypatch):
    checks = harness(monkeypatch)
    checks.context.ldap_client.root_password_binds.return_value = False
    results = checks.run_once()
    assert states(results)['ad_agent_bind'] == DRIFT
    checks.context.ldap_client.root_password_binds.assert_called_with('the-ad-secret')
    logged = str(checks.logger.mock_calls) + str(checks.metrics.mock_calls)
    assert 'the-ad-secret' not in logged and 'the-metrics-password' not in logged


def test_a_directory_that_cannot_be_reached_is_unknown_not_a_rejected_secret(
    monkeypatch,
):
    checks = harness(monkeypatch)
    checks.context.ldap_client.root_password_binds.side_effect = OSError('no route')
    assert states(checks.run_once())['ad_agent_bind'] == UNKNOWN


def test_empty_ssh_client_ip_is_drift_and_the_value_is_a_digest(monkeypatch):
    checks = harness(monkeypatch, Config(**{module.SSH_CLIENT_IP_KEY: []}))
    results = checks.run_once()
    assert states(results)['ssh_client_ip'] == DRIFT
    assert states(results)['values_owned_settings'] == DRIFT

    ok = harness(monkeypatch)
    ok.run_once()
    tags = [t for _, t in ok.published['value'] if t['check'] == 'ssh_client_ip']
    assert {t['setting']: t['current'] for t in tags}['digest'] == digest(
        ['1.2.3.4/32']
    )
    assert '1.2.3.4' not in str(tags)


def test_digest_ignores_order_and_duplicates():
    assert digest(['b', 'a', 'a']) == digest(['a', 'b'])
    assert digest(['a']) != digest(['b'])


def test_a_setting_values_yml_would_overwrite_is_stale(monkeypatch):
    # the 26.11.1 closeout: an old values.yml without the VPN egress
    checks = harness(monkeypatch, values_yml=b'ssh_client_ip: []\n')
    assert states(checks.run_once())['values_owned_settings'] == DRIFT


def test_a_values_file_that_cannot_be_read_is_unknown(monkeypatch):
    checks = harness(monkeypatch)
    checks.context.aws().s3().get_object.side_effect = RuntimeError('NoSuchKey')
    assert states(checks.run_once())['values_owned_settings'] == UNKNOWN


def test_a_must_match_key_off_the_release_value_is_drift(monkeypatch):
    key = next(iter(MUST_MATCH))
    checks = harness(monkeypatch, Config(**{key: '60'}))
    results = checks.run_once()
    assert states(results)['settings_must_match'] == DRIFT
    assert key in [r.detail for r in results if r.check == 'settings_must_match'][0]


def test_a_must_match_key_with_no_row_reads_as_the_default(monkeypatch):
    # a cluster that never stored it runs on the code's default, the same value
    key = next(iter(MUST_MATCH))
    config = Config()
    del config.values[key]
    assert states(harness(monkeypatch, config).run_once())['settings_must_match'] == OK


def test_fsx_and_bastion_are_values_for_datadog_to_compare(monkeypatch):
    checks = harness(
        monkeypatch,
        Config(
            **{
                'bastion-host': {'public': True},
                'bastion-host.public': 'True',
                'bastion-host.cloudwatch_logs.enabled': 'True',
                'ecs.tasks.bastion-host.desired': '2',
            }
        ),
    )
    results = checks.run_once()
    assert states(results)['fsx_parity'] == OK
    assert states(results)['bastion_settings'] == OK
    found = {
        (t['check'], t['setting']): t['current'] for _, t in checks.published['value']
    }
    assert found['fsx_parity', 'deployment_type'] == 'multi_az_2'
    assert found['fsx_parity', 'backup_retention_days'] == '30'
    assert found['bastion_settings', 'desired_count'] == '2'
    assert found['ontap_analytics', 'state'] == 'on'


def test_analytics_value():
    on = {'analytics': {'state': 'on'}}
    off = {'analytics': {'state': 'off'}}
    assert analytics_value([on, on]) == 'on'
    assert analytics_value([on, off]) == 'mixed'
    assert (
        analytics_value([{'is_svm_root': True, 'analytics': {'state': 'off'}}])
        == 'none'
    )


def test_each_must_match_key_holds_the_templates_value():
    templates = SOURCE / 'ideactl/resources/config/templates'
    vdc = (templates / 'virtual-desktop-controller/settings.yml').read_text()
    scheduler = (templates / 'scheduler/settings.yml').read_text()
    assert (
        f'provisioning_timeout_seconds: '
        f'{MUST_MATCH["virtual-desktop-controller.dcv_session.provisioning_timeout_seconds"]}'
        in vdc
    )
    assert (
        f'stack_provisioning_timeout_seconds: '
        f'{MUST_MATCH["scheduler.job_provisioning.stack_provisioning_timeout_seconds"]}'
        in scheduler
    )


def test_the_bastion_keys_are_settings_the_cluster_has():
    keys = (SOURCE / 'ideactl/test/stacks/runtime-keys.json').read_text()
    assert '"bastion-host.public"' in keys
    assert '"bastion-host.cloudwatch_logs.enabled"' in keys
    ecs = (SOURCE / 'ideactl/resources/config/templates/ecs/settings.yml').read_text()
    assert re.search(r'bastion-host:\n(\s+\w+: \d+\n)*\s+desired: \d+', ecs)
    assert module.BASTION_VALUES['desired_count'] == 'ecs.tasks.bastion-host.desired'


def test_values_owned_settings_and_the_file_match_ideactl():
    drift = (SOURCE / 'ideactl/src/config/upgrade-drift.ts').read_text()
    owned = re.search(r'VALUES_OWNED_KEYS.*?new Set\(\[(.*?)\]\)', drift).group(1)
    assert re.findall(r'"([^"]+)"', owned) == list(module.VALUES_OWNED.values())
    invoker = (SOURCE / 'ideactl/src/cli/cdk-invoker.ts').read_text()
    assert f"VALUES_FILE_S3_KEY = '{module.VALUES_FILE_S3_KEY}'" in invoker


def collector(monkeypatch, config=None):
    checks = harness(monkeypatch, config)
    result = DriftCheckCollector(checks.context, checks.storage_metrics)
    result.checks = Mock()
    return result


def test_it_runs_once_a_day_however_often_it_wakes(monkeypatch):
    item = collector(monkeypatch)
    item.collect(None, now=1000.0)
    item.collect(None, now=1000.0 + module.RUN_EVERY_SECONDS - 1)
    assert item.checks.run_once.call_count == 1
    item.collect(None, now=1000.0 + module.RUN_EVERY_SECONDS)
    assert item.checks.run_once.call_count == 2


def test_the_emitter_keeps_it_to_the_leader_and_gives_it_its_switch(monkeypatch):
    item = collector(monkeypatch)
    assert item.leader_only is True
    assert item.interval_seconds < module.RUN_EVERY_SECONDS

    def runner(**values):
        return CollectorRunner(harness(monkeypatch, Config(**values)).context, [item])

    assert runner().is_enabled(item) is True
    off = {'metrics.health.drift_checks.enabled': False}
    assert runner(**off).is_enabled(item) is False


@pytest.mark.parametrize(
    'value, expected', [('seal', OK), ('sign', OK), ('none', DRIFT)]
)
def test_session_security_accepts_sign_or_the_stronger_seal(
    monkeypatch, value, expected
):
    checks = harness(monkeypatch)
    FakeOntap.answers['/api/name-services/ldap'] = [
        {'session_security': value, 'status': {'state': 'up'}}
    ]
    cifs = FakeOntap.answers['/api/protocols/cifs/services']
    cifs[0]['security']['session_security'] = value
    found = states(checks.run_once())
    assert found['ontap_ldap_session_security'] == expected
    assert found['ontap_cifs_session_security'] == expected
