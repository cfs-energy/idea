"""Offline startup and directory authorization checks."""

import importlib.util
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[4]
sys.modules['boto3'] = SimpleNamespace()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load(
    'task_identity',
    'source/idea/idea-cluster-manager/src/ideaclustermanager/app/accounts/helpers/task_directory_identity.py',
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
        # a key the cluster has no value for reads as None, as it does from the real configuration
        config.get_string.side_effect = lambda key, **kwargs: {
            'bastion-host.task_role_id': 'ROLE',
            'ecs.cluster_name': 'cluster',
            'bastion-host.service_name': 'cluster-bastion',
        }.get(key)
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

    def test_activating_task_is_a_member(self):
        # sshd is the container health check, so at join time the task is still ACTIVATING.
        self.assertEqual(
            identity.verify_bastion_task(
                self.context(lastStatus='ACTIVATING', desiredStatus='RUNNING'),
                'ROLE:session',
                'task/cluster/session',
            ),
            'session',
        )

    def test_spoofed_role_session_service_and_stopped_task(self):
        for sender, arn, changes in [
            ('OTHER:session', 'task/cluster/session', {}),
            ('ROLE:other', 'task/cluster/session', {}),
            ('ROLE:session', 'task/cluster/session', {'group': 'service:another'}),
            ('ROLE:session', 'task/cluster/session', {'lastStatus': 'STOPPED'}),
            ('ROLE:session', 'task/cluster/session', {'lastStatus': 'PENDING'}),
            ('ROLE:session', 'task/cluster/session', {'desiredStatus': 'STOPPED'}),
            ('ROLE:session', 'task/cluster/session', {'taskArn': 'task/other/session'}),
        ]:
            with (
                self.subTest(sender=sender, changes=changes),
                self.assertRaises(ValueError),
            ):
                identity.verify_bastion_task(self.context(**changes), sender, arn)


class SshdConfig(unittest.TestCase):
    def test_strict_modes_stay_on_over_numeric_home_ownership(self):
        # the host pool mounts the homes over NFSv3 (numeric owners), so the ownership check holds
        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        block = text.split("cat > /etc/ssh/sshd_config <<'EOF'")[1].split('\nEOF')[0]
        self.assertIn('StrictModes yes', block)
        self.assertNotIn('StrictModes no', block)
        self.assertIn('PubkeyAuthentication yes', block)
        self.assertIn('AuthorizedKeysFile .ssh/authorized_keys', block)
        settings = dict(
            line.split(None, 1)
            for line in block.splitlines()
            if line and not line.startswith('#')
        )
        for key, value in {
            'PasswordAuthentication': 'no',
            'KbdInteractiveAuthentication': 'no',
            'UsePAM': 'yes',
            'LoginGraceTime': '30',
            'MaxAuthTries': '6',
        }.items():
            self.assertEqual(settings[key], value)


class SshdSourceLimits(unittest.TestCase):
    """brute force is refused per source; health checks and a shared NAT address are not"""

    def render(self, vpc_cidrs, ssh_client_cidrs=None):
        import subprocess

        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        start = text.index("cat > /etc/ssh/sshd_config <<'EOF'")
        end = text.index('# Offer post-quantum key exchange', start)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sshd_config'
            block = 'set -euo pipefail\n' + text[start:end].replace(
                '/etc/ssh/sshd_config', str(path)
            )
            env = {'PATH': '/usr/bin:/bin'}
            if vpc_cidrs is not None:
                env['IDEA_VPC_CIDRS'] = vpc_cidrs
            if ssh_client_cidrs is not None:
                env['IDEA_SSH_CLIENT_CIDRS'] = ssh_client_cidrs
            result = subprocess.run(
                ['bash', '-c', block],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            return path.read_text(), result.stdout

    def settings(self, config):
        return dict(
            line.split(None, 1)
            for line in config.splitlines()
            if line and not line.startswith('#')
        )

    def test_limits_bite_only_sustained_failures(self):
        settings = self.settings(self.render('10.7.0.0/16')[0])
        self.assertEqual(settings['MaxStartups'], '50:30:200')
        self.assertEqual(settings['PerSourceMaxStartups'], '30')
        # one address per source: users behind one NAT address are not grouped with neighbours
        self.assertEqual(settings['PerSourceNetBlockSize'], '32:128')
        penalties = dict(
            item.split(':', 1) for item in settings['PerSourcePenalties'].split()
        )
        self.assertEqual(penalties, {'authfail': '2s', 'min': '30s', 'max': '10m'})
        # a source holds fewer unauthenticated slots than the point where drops start for all
        self.assertLess(
            int(settings['PerSourceMaxStartups']),
            int(settings['MaxStartups'].split(':')[0]),
        )

    def test_loopback_and_the_vpc_are_exempt(self):
        config, _ = self.render('10.7.0.0/16,100.64.0.0/24')
        self.assertEqual(
            self.settings(config)['PerSourcePenaltyExemptList'],
            '127.0.0.0/8,::1,10.7.0.0/16,100.64.0.0/24',
        )

    def test_a_task_without_vpc_cidrs_still_exempts_loopback(self):
        # a task definition from before this setting runs the new image until it is redeployed
        for value in (None, ''):
            config, _ = self.render(value)
            self.assertEqual(
                self.settings(config)['PerSourcePenaltyExemptList'], '127.0.0.0/8,::1'
            )

    def test_sshd_accepts_the_rendered_config(self):
        # the image's OpenSSH (9.9) knows the per-source keywords; validate where a local one does
        import re
        import shutil
        import subprocess

        sshd = shutil.which('sshd') or (
            '/usr/sbin/sshd' if Path('/usr/sbin/sshd').exists() else None
        )
        version = subprocess.run(['ssh', '-V'], capture_output=True, text=True).stderr
        found = re.search(r'OpenSSH_(\d+)\.(\d+)', version)
        if not sshd or not found or tuple(map(int, found.groups())) < (9, 8):
            self.skipTest('no local OpenSSH 9.8 or later')
        config, _ = self.render('10.7.0.0/16', '23.23.20.238/32,0.0.0.0/0')
        with tempfile.TemporaryDirectory() as tmp:
            path, key = Path(tmp) / 'sshd_config', Path(tmp) / 'host_key'
            path.write_text(config)
            subprocess.run(
                ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                check=True,
            )
            result = subprocess.run(
                [sshd, '-t', '-f', str(path), '-h', str(key)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_malformed_cidr_is_dropped_not_fatal(self):
        config, out = self.render('10.7.0.0/16,bogus value')
        self.assertEqual(
            self.settings(config)['PerSourcePenaltyExemptList'],
            '127.0.0.0/8,::1,10.7.0.0/16',
        )
        self.assertIn('ignoring VPC CIDR bogus value', out)

    def test_the_ssh_allowlist_is_exempt_but_not_the_internet(self):
        # an office behind one NAT address must not be refused for a few users' failures
        config, out = self.render('10.7.0.0/16', '23.23.20.238/32,198.51.100.0/24')
        self.assertEqual(
            self.settings(config)['PerSourcePenaltyExemptList'],
            '127.0.0.0/8,::1,10.7.0.0/16,23.23.20.238/32,198.51.100.0/24',
        )
        # a public bastion's 0.0.0.0/0, or anything wider than /8, keeps its penalties
        config, out = self.render('10.7.0.0/16', '0.0.0.0/0,64.0.0.0/2,23.0.0.0/8')
        self.assertEqual(
            self.settings(config)['PerSourcePenaltyExemptList'],
            '127.0.0.0/8,::1,10.7.0.0/16,23.0.0.0/8',
        )
        self.assertIn('not exempting SSH client CIDR 0.0.0.0/0 from penalties', out)
        self.assertIn('not exempting SSH client CIDR 64.0.0.0/2 from penalties', out)

    def test_a_malformed_allowlist_entry_is_dropped_not_fatal(self):
        # sshd -t refuses host bits or a bad address, which would stop the bastion
        config, out = self.render(
            '', '23.23.20.238/32,23.23.20.238/24,256.1.1.1/32,1.2.3.4,x'
        )
        self.assertEqual(
            self.settings(config)['PerSourcePenaltyExemptList'],
            '127.0.0.0/8,::1,23.23.20.238/32',
        )
        for entry, reason in (
            ('23.23.20.238/24', 'host bits set'),
            ('256.1.1.1/32', 'not an IPv4 network'),
            ('1.2.3.4', 'not an IPv4 network'),
            ('x', 'not an IPv4 network'),
        ):
            self.assertIn(f'ignoring SSH client CIDR {entry}: {reason}', out)


class PbsServerSuffix(unittest.TestCase):
    def run_block(self, resolv):
        import subprocess
        import tempfile

        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        start = text.index('  PRIVATE_ZONE=')
        end = text.index('  cat > /etc/pbs.conf <<EOF', start)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'resolv.conf'
            path.write_text(resolv)
            block = text[start:end].replace('/etc/resolv.conf', str(path))
            env = {
                'IDEA_PBS_SERVER': 'scheduler.idea-test.us-east-1.local',
                'PATH': '/usr/bin:/bin',
            }
            for _ in range(2):  # a restart must not add the zone twice
                subprocess.run(['bash', '-c', block], env=env, check=True)
            return path.read_text()

    def test_job_id_suffix_resolves_through_the_private_zone(self):
        # job ids end in the server's short name (1.scheduler); qdel resolves that suffix
        out = self.run_block('nameserver 10.0.0.2\nsearch us-east-1.compute.internal\n')
        self.assertIn(
            'search idea-test.us-east-1.local us-east-1.compute.internal', out
        )
        self.assertEqual(out.count('idea-test.us-east-1.local'), 1)

    def test_a_resolv_conf_without_search_gets_one(self):
        out = self.run_block('nameserver 10.0.0.2\n')
        self.assertIn('search idea-test.us-east-1.local', out)


class SiteHook(unittest.TestCase):
    """the login node runs the site's hook before sshd accepts; a failure never stops the start"""

    def run_block(self, cluster_home, owner_uid=0):
        import subprocess

        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        start = text.index('# Site customizations')
        end = text.index('/usr/sbin/sshd -t', start)
        # stat reports the owner the test asks for; the mode is the file's own
        # Python follows symlinks like GNU stat -L, without depending on GNU stat on macOS.
        stub = (
            'stat() { '
            + shlex.quote(sys.executable)
            + ' -c '
            + shlex.quote(
                'import os, stat, sys; '
                f'print({owner_uid}, format(stat.S_IMODE(os.stat(sys.argv[1]).st_mode), "o"))'
            )
            + ' "${@: -1}"; }\n'
            'timeout() { shift; "$@"; }\n'
        )
        block = 'set -euo pipefail\n' + stub + text[start:end] + 'echo reached-sshd\n'
        env = {'PATH': '/usr/bin:/bin', 'IDEA_MODULE_ID': 'bastion-host'}
        if cluster_home is not None:
            env['IDEA_CLUSTER_HOME'] = str(cluster_home)
        return subprocess.run(
            ['bash', '-c', block], env=env, capture_output=True, text=True
        )

    def write_hook(self, root, body, mode=0o755):
        hook = Path(root) / 'bastion-host' / 'userdata_customizations.sh'
        hook.parent.mkdir(parents=True)
        hook.write_text(body)
        hook.chmod(mode)
        return hook

    def test_the_hook_runs_before_sshd_and_its_output_reaches_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            hook = self.write_hook(tmp, 'echo tool installed\n')
            result = self.run_block(tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f'running site hook {hook}', 'tool installed', 'reached-sshd'],
        )

    def test_a_missing_hook_or_cluster_home_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_block(tmp).stdout.splitlines(), ['reached-sshd'])
        self.assertEqual(self.run_block(None).stdout.splitlines(), ['reached-sshd'])

    def test_a_failing_hook_does_not_stop_the_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_hook(tmp, 'echo partial\nexit 3\n')
            result = self.run_block(tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            ['partial', 'site hook failed with status 3; continuing', 'reached-sshd'],
        )

    def test_a_hook_others_could_write_is_skipped(self):
        for owner, mode in ((1000, 0o755), (0, 0o775), (0, 0o757)):
            with (
                self.subTest(owner=owner, mode=oct(mode)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                self.write_hook(tmp, 'echo ran\n', mode)
                result = self.run_block(tmp, owner_uid=owner)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertTrue(lines[0].startswith('skipping site hook '), lines)
                self.assertNotIn('ran', lines)
                self.assertEqual(lines[-1], 'reached-sshd')

    def test_the_hook_runs_under_a_time_limit(self):
        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        self.assertIn('timeout 120 /bin/bash "${SITE_HOOK}"', text)
        self.assertLess(
            text.index('timeout 120 /bin/bash'), text.index('/usr/sbin/sshd -t')
        )


class SshdHealthCheckLines(unittest.TestCase):
    """health-check connections never leave the container; every other sshd line does"""

    DROPPED = [
        # the task's ssh-keyscan check
        'Connection closed by 127.0.0.1 port 41022 [preauth]',
        # the load balancer's TCP check, from its private addresses
        'Connection closed by 10.7.0.48 port 3151',
        'Connection closed by 172.31.4.9 port 22',
        'Connection closed by 192.168.1.20 port 60000',
    ]
    KEPT = [
        'Accepted publickey for alice from 23.23.20.238 port 61737 ssh2: RSA SHA256:abc',
        'Accepted publickey for alice from 10.7.0.48 port 61737 ssh2: RSA SHA256:abc',
        'Failed publickey for alice from 10.7.0.48 port 5000 ssh2: RSA SHA256:abc',
        'Invalid user admin from 81.218.86.52 port 18694',
        'Connection closed by invalid user admin 10.7.0.48 port 5000 [preauth]',
        'Connection closed by authenticating user alice 127.0.0.1 port 5000 [preauth]',
        'Disconnected from user alice 23.23.20.238 port 63564',
        'Received disconnect from 10.7.0.48 port 5000:11: Bye Bye [preauth]',
        # a client that sent its identification and left before authenticating
        'Connection closed by 10.7.0.48 port 5000 [preauth]',
        # a close from a public address is not the load balancer
        'Connection closed by 23.23.20.238 port 54871',
        'Connection closed by 172.32.0.1 port 22',
        'Connection closed by 127.0.0.1 port 41022',
        'error: kex_exchange_identification: Connection closed by remote host',
        'Server listening on 0.0.0.0 port 22.',
    ]

    def run_filter(self, lines):
        import subprocess

        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        start = text.index('SSHD_HEALTH_CHECK_LINES=')
        end = text.index('/usr/sbin/sshd -D -e', start)
        # sshd's stderr goes through the filter and on to the task's stderr
        script = (
            'set -euo pipefail\n'
            + text[start:end]
            + 'printf "%s\\n" "$@" | sshd_log_filter'
        )
        result = subprocess.run(
            ['bash', '-c', script, 'filter', *lines],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, '')
        return result.stderr.splitlines()

    def test_only_health_check_closes_are_dropped(self):
        self.assertEqual(self.run_filter(self.DROPPED + self.KEPT), self.KEPT)

    def test_an_all_dropped_stream_does_not_fail(self):
        self.assertEqual(self.run_filter(self.DROPPED), [])

    def test_sshd_logs_through_the_filter(self):
        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        self.assertIn('/usr/sbin/sshd -D -e 2> >(sshd_log_filter) &\nsshd_pid=$!', text)


if __name__ == '__main__':
    unittest.main()
