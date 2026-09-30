from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import ldap
import pytest

from ideadatamodel import errorcodes, exceptions
from ideaclustermanager.app.accounts import ad_automation_agent
from ideaclustermanager.app.accounts.ad_automation_agent import (
    ADAutomationAgent,
    AD_RESET_PASSWORD_LOCK_KEY,
)
from ideaclustermanager.app.accounts.ldapclient import abstract_ldap_client
from ideaclustermanager.app.accounts.ldapclient.ldap_client_factory import (
    build_ldap_client,
)
from ideaclustermanagertests.metrics_fakes import FakeContext


def invalid_credentials(code):
    return ldap.INVALID_CREDENTIALS(
        {
            'result': 49,
            'desc': 'Invalid credentials',
            'info': f'AcceptSecurityContext error, data {code}, v3839',
        }
    )


@pytest.fixture
def directory(monkeypatch):
    secrets = {'username-secret': 'admin', 'password-secret': 'old-password'}
    context = FakeContext(
        {
            'directoryservice.provider': 'aws_managed_activedirectory',
            'directoryservice.name': 'example.invalid',
            'directoryservice.directory_id': 'directory-id',
            'directoryservice.ad_short_name': 'EXAMPLE',
            'directoryservice.ldap_connection_uri': 'ldap://example.invalid',
            'directoryservice.users.ou': 'ou=users,dc=example,dc=invalid',
            'directoryservice.root_username_secret_arn': 'username-secret',
            'directoryservice.root_password_secret_arn': 'password-secret',
            'directoryservice.password_max_age': 42,
            'directoryservice.ad_automation.enable_root_password_reset': True,
            'directoryservice.ad_automation.sqs_queue_url': 'queue-url',
        },
        secrets=secrets,
    )
    context._aws = Mock()
    context._lock = Mock()
    state = SimpleNamespace(context=context, secrets=secrets, code='532')
    state.password = secrets['password-secret']
    state.reset = context.aws().ds().reset_user_password
    state.put_secret = context.aws().secretsmanager().put_secret_value
    state.pending = {}
    state.fail_secret_write = False

    def get_secret(SecretId, VersionStage='AWSCURRENT'):
        if VersionStage == 'AWSPENDING':
            if SecretId not in state.pending:
                raise RuntimeError('no pending version')
            return {'SecretString': state.pending[SecretId]}
        return {'SecretString': secrets[SecretId]}

    context.aws().secretsmanager().get_secret_value.side_effect = get_secret

    def reset_password(**kwargs):
        state.password = kwargs['NewPassword']
        state.code = None

    def put_secret(SecretId, SecretString, VersionStages=None):
        if VersionStages == ['AWSPENDING']:
            state.pending[SecretId] = SecretString
            return
        if state.fail_secret_write:
            raise RuntimeError('secret write failed')
        secrets[SecretId] = SecretString

    state.reset.side_effect = reset_password
    state.put_secret.side_effect = put_secret

    @contextmanager
    def connection(bind, passwd):
        assert bind == 'admin@example.invalid'
        if passwd != state.password:
            raise invalid_credentials('52e')
        if state.code:
            raise invalid_credentials(state.code)
        conn = Mock()
        timestamp = (arrow.utcnow().int_timestamp + 11644473600) * 10_000_000
        conn.search_s.return_value = [
            ('cn=admin', {'pwdLastSet': [str(timestamp).encode()]})
        ]
        yield conn

    manager = Mock()
    manager.connection.side_effect = connection
    monkeypatch.setattr(
        abstract_ldap_client, 'ConnectionManager', Mock(return_value=manager)
    )
    monkeypatch.setattr(
        ad_automation_agent.ADAutomationDAO, 'initialize', lambda self: None
    )
    monkeypatch.setattr(ad_automation_agent.time, 'sleep', Mock())

    def initialize(uri):
        conn = Mock()

        def bind_s(who, cred, method):
            if cred != state.password:
                raise invalid_credentials('52e')

        conn.bind_s.side_effect = bind_s
        return conn

    monkeypatch.setattr(abstract_ldap_client.ldap, 'initialize', initialize)

    def build():
        client = build_ldap_client(context)
        client.refresh_root_username_password = Mock(
            wraps=client.refresh_root_username_password
        )
        return ADAutomationAgent(context, client)

    state.build = build
    return state


@pytest.mark.parametrize('code', ['532', '773'])
def test_expired_password_resets_once_and_refreshes_client(directory, code):
    directory.code = code
    agent = directory.build()
    agent.check_and_reset_admin_password()
    password = directory.secrets['password-secret']
    directory.reset.assert_called_once_with(
        DirectoryId='directory-id', UserName='admin', NewPassword=password
    )
    assert [call.kwargs for call in directory.put_secret.call_args_list] == [
        {
            'SecretId': 'password-secret',
            'SecretString': password,
            'VersionStages': ['AWSPENDING'],
        },
        {'SecretId': 'password-secret', 'SecretString': password},
    ]
    assert agent.ldap_client.ldap_root_password == password
    directory.context.distributed_lock().release.assert_called_once_with(
        key=AD_RESET_PASSWORD_LOCK_KEY
    )
    agent.check_and_reset_admin_password()
    assert directory.reset.call_count == 1 and directory.put_secret.call_count == 2


@pytest.mark.parametrize('code', ['52e', '775', '5320', 'unknown'])
def test_other_invalid_credentials_do_not_reset(directory, code):
    directory.code = code
    with pytest.raises(ldap.INVALID_CREDENTIALS):
        directory.build().check_and_reset_admin_password()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()
    directory.context.distributed_lock().acquire.assert_not_called()
    assert any(
        'password will not be reset' in line for line in directory.context._logger.lines
    )


def test_contending_callers_refresh_under_lock(directory):
    agents = [directory.build(), directory.build()]
    ready = Barrier(2)
    lock = Lock()

    def acquire(key):
        assert key == AD_RESET_PASSWORD_LOCK_KEY
        ready.wait(timeout=5)
        assert lock.acquire(timeout=5)

    directory.context.distributed_lock().acquire.side_effect = acquire
    directory.context.distributed_lock().release.side_effect = (
        lambda key: lock.release()
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(agent.check_and_reset_admin_password) for agent in agents
        ]
        for future in futures:
            future.result(timeout=10)
    assert directory.reset.call_count == 1 and directory.put_secret.call_count == 2
    assert directory.context.distributed_lock().release.call_count == 2
    for agent in agents:
        assert agent.ldap_client.ldap_root_password == directory.password
        assert agent.is_password_expired() is False


@pytest.mark.parametrize('code', ['532', '773'])
def test_reset_disabled(directory, code):
    directory.code = code
    directory.context.config().values[
        'directoryservice.ad_automation.enable_root_password_reset'
    ] = False
    directory.build().check_and_reset_admin_password()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()
    directory.context.distributed_lock().acquire.assert_not_called()
    assert any(
        'password reset is disabled' in line for line in directory.context._logger.lines
    )


def test_healthy_password_does_not_reset(directory):
    directory.code = None
    directory.build().check_and_reset_admin_password()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()
    directory.context.distributed_lock().acquire.assert_not_called()


def test_stale_client_refreshes_after_another_task_reset(directory):
    agent = directory.build()
    directory.build().check_and_reset_admin_password()
    assert agent.ldap_client.ldap_root_password != directory.password
    agent.check_and_reset_admin_password()
    assert agent.ldap_client.ldap_root_password == directory.password
    assert directory.reset.call_count == 1 and directory.put_secret.call_count == 2


@pytest.mark.parametrize('code', ['532', '773'])
def test_self_managed_ad_still_raises(directory, code):
    directory.context.config().values['directoryservice.provider'] = 'activedirectory'
    directory.code = code
    agent = directory.build()
    with pytest.raises(ldap.INVALID_CREDENTIALS):
        agent.check_and_reset_admin_password()
    agent.ldap_client.refresh_root_username_password.assert_not_called()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()


def test_failed_lock_acquisition_does_not_release_or_reset(directory):
    directory.context.distributed_lock().acquire.side_effect = RuntimeError('lock busy')
    with pytest.raises(RuntimeError, match='lock busy'):
        directory.build().check_and_reset_admin_password()
    directory.context.distributed_lock().release.assert_not_called()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()


def test_wrong_credentials_on_locked_recheck_do_not_reset(directory):
    directory.context.distributed_lock().acquire.side_effect = lambda key: setattr(
        directory, 'code', '52e'
    )
    with pytest.raises(ldap.INVALID_CREDENTIALS):
        directory.build().check_and_reset_admin_password()
    directory.reset.assert_not_called()
    directory.put_secret.assert_not_called()
    directory.context.distributed_lock().release.assert_called_once_with(
        key=AD_RESET_PASSWORD_LOCK_KEY
    )


def test_exhausted_reset_retries_do_not_update_secret(directory):
    class UserDoesNotExist(Exception):
        pass

    directory.context.aws().ds().exceptions.UserDoesNotExistException = UserDoesNotExist
    directory.reset.side_effect = UserDoesNotExist()
    agent = directory.build()
    with pytest.raises(exceptions.SocaException) as error:
        agent.check_and_reset_admin_password()
    assert error.value.error_code == errorcodes.AUTH_USER_NOT_FOUND
    assert directory.reset.call_count == 4
    # Only the pending version was written; the current secret still holds the working password.
    assert all(
        call.kwargs.get('VersionStages') == ['AWSPENDING']
        for call in directory.put_secret.call_args_list
    )
    assert directory.secrets['password-secret'] == directory.password
    assert agent.ldap_client.ldap_root_password == directory.secrets['password-secret']
    directory.context.distributed_lock().release.assert_called_once_with(
        key=AD_RESET_PASSWORD_LOCK_KEY
    )


def test_failed_secret_write_after_reset_recovers_from_pending(directory):
    directory.fail_secret_write = True
    with pytest.raises(RuntimeError, match='secret write failed'):
        directory.build().check_and_reset_admin_password()
    assert directory.reset.call_count == 1
    # The directory has the new password; the current secret still holds the old one.
    assert directory.secrets['password-secret'] != directory.password
    directory.fail_secret_write = False
    agent = directory.build()
    agent.check_and_reset_admin_password()
    assert directory.secrets['password-secret'] == directory.password
    assert agent.ldap_client.ldap_root_password == directory.password
    assert directory.reset.call_count == 1
    assert any(
        'pending secret version' in line for line in directory.context._logger.lines
    )


def test_healthy_password_reads_no_secrets_after_startup(directory):
    directory.code = None
    agent = directory.build()
    reads = directory.context.aws().secretsmanager().get_secret_value.call_count
    for _ in range(3):
        agent.check_and_reset_admin_password()
    assert directory.context.aws().secretsmanager().get_secret_value.call_count == reads
