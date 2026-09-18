"""Offline startup and directory authorization checks."""

import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[5]
sys.modules['boto3'] = SimpleNamespace()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load('bastion_runtime', 'deployment/ecr/idea-control-plane/roles/bastion.py')
identity = load(
    'task_identity',
    'source/idea/idea-cluster-manager/src/ideaclustermanager/app/accounts/helpers/task_directory_identity.py',
)


class Secret:
    def __init__(self):
        self.value = {'schema': 1}
        self.writes = 0

    def get_secret_value(self, **kwargs):
        return {'SecretString': json.dumps(self.value)}

    def put_secret_value(self, **kwargs):
        self.writes += 1
        self.value = json.loads(kwargs['SecretString'])


class HostKeys(unittest.TestCase):
    def test_replacement_restores_same_keys_without_a_write(self):
        secret = Secret()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime.restore_host_keys(secret, 'test-secret', root / 'first')
            runtime.restore_host_keys(secret, 'test-secret', root / 'second')
            self.assertEqual(secret.writes, 1)
            for path in (root / 'first').iterdir():
                self.assertEqual(
                    path.read_bytes(), (root / 'second' / path.name).read_bytes()
                )
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o644 if path.suffix == '.pub' else 0o600,
                )

    def test_concurrent_first_starters_restore_winner(self):
        secret = Secret()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime.restore_host_keys(secret, 'test-secret', root / 'first')
            winner = secret.value
            secret.get_secret_value = Mock(
                side_effect=[
                    {'SecretString': '{"schema":1}'},
                    {'SecretString': json.dumps(winner)},
                ]
            )
            secret.put_secret_value = Mock(
                side_effect=RuntimeError('version already exists')
            )
            runtime.restore_host_keys(secret, 'test-secret', root / 'second')
            self.assertEqual(
                (root / 'first/ssh_host_ed25519_key').read_text(),
                (root / 'second/ssh_host_ed25519_key').read_text(),
            )
            token = secret.put_secret_value.call_args.kwargs['ClientRequestToken']
            self.assertEqual(
                secret.get_secret_value.call_args.kwargs['VersionId'], token
            )

    def test_invalid_secret_never_regenerates_identity(self):
        secret = Secret()
        secret.value = {'schema': 1, 'keys': {'../../escape': 'bad'}}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                runtime.restore_host_keys(secret, 'test-secret', Path(temporary))
        self.assertEqual(secret.writes, 0)

    def test_read_failure_stops_startup(self):
        secret = Mock()
        secret.get_secret_value.side_effect = RuntimeError('access denied')
        with self.assertRaises(RuntimeError):
            runtime.restore_host_keys(secret, 'test-secret')
        secret.put_secret_value.assert_not_called()


class Directory(unittest.TestCase):
    def test_openldap_uses_tls_and_shared_homes(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(runtime.subprocess, 'run') as run,
        ):
            root = Path(temporary)
            (root / 'etc/sssd').mkdir(parents=True)
            runtime.configure_directory(
                None,
                {
                    'IDEA_DATA_DIR': '/data',
                    'IDEA_DIRECTORY_PROVIDER': 'openldap',
                    'IDEA_LDAP_HOST': 'directory.example.invalid',
                    'IDEA_LDAP_BASE': 'dc=example,dc=invalid',
                    'IDEA_LDAP_CERTIFICATE': 'certificate',
                },
                root,
            )
            config = (root / 'etc/sssd/sssd.conf').read_text()
            self.assertIn('ldap_tls_reqcert = demand', config)
            self.assertIn('fallback_homedir = /data/home/%u', config)
            self.assertIn('sudo_provider = ldap', config)
            self.assertEqual(
                stat.S_IMODE((root / 'etc/sssd/sssd.conf').stat().st_mode), 0o600
            )
            run.assert_called_once_with(
                ['authselect', 'select', 'sssd', 'with-sudo', '--force'], check=True
            )

    def test_ad_uses_one_time_password_on_stdin_and_shared_homes(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(runtime.subprocess, 'run') as run,
            patch.object(runtime, 'request_ad_join') as authorize,
        ):
            root = Path(temporary)
            (root / 'etc/sssd').mkdir(parents=True)
            (root / 'etc/sudoers.d').mkdir(parents=True)
            authorize.return_value = {
                'hostname': 'IDEA-TEST',
                'domain_controller': 'dc.example.invalid',
                'otp': 'one-time-password',
            }
            runtime.configure_directory(
                None,
                {
                    'IDEA_DATA_DIR': '/data',
                    'IDEA_DIRECTORY_PROVIDER': 'activedirectory',
                    'IDEA_AD_DOMAIN': 'example.invalid',
                    'IDEA_AD_ID_MAPPING': 'false',
                    'IDEA_AD_SUDOERS_GROUP': 'Directory Admins',
                },
                root,
            )
            join = run.call_args_list[0]
            self.assertEqual(join.kwargs['input'], 'one-time-password')
            self.assertNotIn('one-time-password', ' '.join(join.args[0]))
            config = (root / 'etc/sssd/sssd.conf').read_text()
            self.assertIn('access_provider = ad', config)
            self.assertIn('ldap_id_mapping = false', config)
            self.assertIn('fallback_homedir = /data/home/%u', config)


class AdminAccount(unittest.TestCase):
    def test_restores_only_the_public_ec2_key(self):
        session = Mock()
        session.client.return_value.describe_key_pairs.return_value = {
            'KeyPairs': [{'PublicKey': 'ssh-ed25519 public-key'}]
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(runtime.pwd, 'getpwnam') as account,
            patch.object(runtime.os, 'chown'),
        ):
            account.return_value = SimpleNamespace(pw_uid=1000, pw_gid=1000)
            root = Path(temporary)
            (root / 'etc/sudoers.d').mkdir(parents=True)
            runtime.configure_admin_account(
                session, {'IDEA_SSH_KEY_PAIR': 'sample-key'}, root
            )
            keys = root / 'home/ec2-user/.ssh/authorized_keys'
            self.assertEqual(keys.read_text(), 'ssh-ed25519 public-key\n')
            self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o600)
            session.client.return_value.describe_key_pairs.assert_called_once_with(
                KeyNames=['sample-key'], IncludePublicKey=True
            )


class TaskIdentity(unittest.TestCase):
    def context(self, **changes):
        task = {
            'taskArn': 'task/cluster/session',
            'lastStatus': 'RUNNING',
            'group': 'service:cluster-bastion',
        }
        task.update(changes)
        config = Mock()
        config.get_string.side_effect = lambda key, **kwargs: {
            'bastion-host.task_role_id': 'ROLE',
            'ecs.cluster_name': 'cluster',
            'bastion-host.service_name': 'cluster-bastion',
        }[key]
        context = Mock()
        context.config.return_value = config
        context.aws.return_value.get_client.return_value.describe_tasks.return_value = {
            'tasks': [task]
        }
        return context

    def test_authorized_task(self):
        self.assertEqual(
            identity.verify_bastion_task(
                self.context(), 'ROLE:session', 'task/cluster/session'
            ),
            'session',
        )

    def test_spoofed_role_session_service_and_stopped_task(self):
        for sender, arn, changes in [
            ('OTHER:session', 'task/cluster/session', {}),
            ('ROLE:other', 'task/cluster/session', {}),
            ('ROLE:session', 'task/cluster/session', {'group': 'service:another'}),
            ('ROLE:session', 'task/cluster/session', {'lastStatus': 'STOPPED'}),
            ('ROLE:session', 'task/cluster/session', {'taskArn': 'task/other/session'}),
        ]:
            with (
                self.subTest(sender=sender, changes=changes),
                self.assertRaises(ValueError),
            ):
                identity.verify_bastion_task(self.context(**changes), sender, arn)


if __name__ == '__main__':
    unittest.main()
