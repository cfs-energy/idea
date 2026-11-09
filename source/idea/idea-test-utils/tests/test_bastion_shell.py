"""Offline startup and directory authorization checks."""

import importlib.util
from pathlib import Path
import shlex
import subprocess
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
        # a per-source slot limit refused the health check's own connections and killed the task
        self.assertNotIn('PerSourceMaxStartups', settings)
        # one address per source: users behind one NAT address are not grouped with neighbours
        self.assertEqual(settings['PerSourceNetBlockSize'], '32:128')
        penalties = dict(
            item.split(':', 1) for item in settings['PerSourcePenalties'].split()
        )
        self.assertEqual(penalties, {'authfail': '2s', 'min': '30s', 'max': '10m'})

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
        # the task's own checks, which read the banner and close (9.9), and a key scan
        'Connection closed by 127.0.0.1 port 41022',
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
        'Connection reset by 127.0.0.1 port 41022 [preauth]',
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
            check=True,
        )
        self.assertEqual(result.stdout, b'')
        # bytes, split on LF alone, so a CR sshd sent stays on its line
        return result.stderr.decode().split('\n')[:-1]

    def test_only_health_check_closes_are_dropped(self):
        self.assertEqual(self.run_filter(self.DROPPED + self.KEPT), self.KEPT)

    def test_sshd_stderr_lines_ending_in_cr_lf_are_dropped_too(self):
        # sshd -e ends every line in CR LF; a filter anchored at LF alone dropped nothing on dev
        lines = [line + '\r' for line in self.DROPPED + self.KEPT]
        self.assertEqual(self.run_filter(lines), [line + '\r' for line in self.KEPT])

    def test_an_all_dropped_stream_does_not_fail(self):
        self.assertEqual(self.run_filter(self.DROPPED), [])

    def test_sshd_logs_through_the_filter(self):
        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        self.assertIn('/usr/sbin/sshd -D -e 2> >(sshd_log_filter) &\nsshd_pid=$!', text)


class HealthCheck(unittest.TestCase):
    """bastion_health.sh: one connection, the banner read and nothing sent, every refusal noted"""

    def check(self, answer):
        import socket
        import threading

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(0.1)
        accepted = []
        done = threading.Event()

        def serve():
            while not done.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    connection.sendall(answer)
                    # what the check sent before it closed
                    received = b''
                    try:
                        while chunk := connection.recv(4096):
                            received += chunk
                    except OSError:
                        pass
                    accepted.append(received)

        worker = threading.Thread(target=serve)
        worker.start()
        with tempfile.TemporaryDirectory() as folder:
            drops = Path(folder) / 'drops'
            drops.write_bytes(b'')
            code = subprocess.run(
                [
                    'bash',
                    str(
                        ROOT
                        / 'deployment/ecr/idea-control-plane/roles/bastion_health.sh'
                    ),
                    str(listener.getsockname()[1]),
                ],
                env={**__import__('os').environ, 'SSHD_DROPS_FILE': str(drops)},
                capture_output=True,
                timeout=20,
            ).returncode
            done.set()
            worker.join()
            listener.close()
            return code, accepted, drops.read_bytes()

    def test_a_refused_check_is_one_connection_noted_once(self):
        # a second, unnoted connection would be one more drop in sshd's totals, counted as remote
        self.assertEqual(
            self.check(b'Not allowed at this time\r\n'),
            (0, [b''], b'loopback check refused\n'),
        )

    def test_a_serving_sshd_is_one_connection_left_with_nothing_unread(self):
        # nothing sent: sshd's answer to an identification would be unread at the close, which
        # makes it a reset sshd logs on 10.5
        self.assertEqual(self.check(b'SSH-2.0-OpenSSH_9.9\r\n'), (0, [b''], b''))

    def test_anything_else_is_unhealthy(self):
        self.assertEqual(self.check(b'\r\n')[::2], (1, b''))


class BastionMetrics(unittest.TestCase):
    """the separate process that counts sessions and drops, and cannot reach sshd's log path"""

    def module(self):
        return load(
            'bastion_metrics',
            'deployment/ecr/idea-control-plane/roles/bastion_metrics.py',
        )

    def test_sessions_are_logged_in_connections_only(self):
        metrics = self.module()
        with tempfile.TemporaryDirectory() as proc:
            titles = {
                '1': b'sshd: /usr/sbin/sshd -D -e [listener] 0 of 50-200 startups\0',
                '2': b'sshd-session: alice [priv]\0',
                '3': b'sshd-session: alice@pts/0\0',
                '4': b'sshd: bob@notty\0',
                '5': b'python3.13\0',
            }
            for pid, title in titles.items():
                (Path(proc) / pid).mkdir()
                (Path(proc) / pid / 'cmdline').write_bytes(title)
            (Path(proc) / 'self').mkdir()
            self.assertEqual(metrics.active_sessions(proc), 2)

    def test_established_counts_port_22_only(self):
        metrics = self.module()
        with tempfile.NamedTemporaryFile('w') as table:
            table.write('  sl  local_address rem_address   st\n')
            table.write('   0: 0100007F:0016 0100007F:AAAA 01 x\n')
            table.write('   1: 0100007F:0016 0100007F:AAAB 0A x\n')
            table.write('   2: 0100007F:1F90 0100007F:AAAC 01 x\n')
            table.flush()
            self.assertEqual(metrics.established_connections(table.name), 1)

    def test_drops_are_each_counted_once_with_their_kind_and_the_file_stays_bounded(
        self,
    ):
        metrics = self.module()
        with tempfile.NamedTemporaryFile('ab') as drops:
            tally = metrics.DropTally(drops.name)
            drops.write(
                b'from [127.0.0.1]:5 on [127.0.0.1]:22 past Maxstartups\n'
                b'loopback check refused\n'
                b'past Maxstartups\npast Maxstartups\n'
                b'Timeout before authentication\nTimeout, client not responding\n'
            )
            drops.flush()
            self.assertEqual(
                tally.read(),
                {
                    ('maxstartups', 'loopback'): 1,
                    ('maxstartups', 'remote'): 2,
                    ('auth_timeout', 'remote'): 1,
                    ('client_timeout', 'remote'): 1,
                },
            )
            self.assertEqual(tally.read(), {})
            drops.write(b'past Maxstartups\n' * 70000)
            drops.flush()
            self.assertEqual(tally.read(), {('maxstartups', 'remote'): 70000})
            self.assertEqual(Path(drops.name).stat().st_size, 0)
            self.assertEqual(tally.offset, 0)

    def test_the_tally_holds_no_address_or_name_only_the_phrase(self):
        text = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        start = text.index('SSHD_HEALTH_CHECK_LINES=')
        end = text.index('metrics_pid=\n')
        with tempfile.TemporaryDirectory() as folder:
            drops = Path(folder) / 'drops'
            script = (
                'set -euo pipefail\n'
                + text[start:end]
                + f'export SSHD_DROPS_FILE={shlex.quote(str(drops))}\n'
                + 'printf "%s\\n" "$@" | sshd_log_filter 2>/dev/null\nsleep 1\n'
            )
            subprocess.run(
                [
                    'bash',
                    '-c',
                    script,
                    'x',
                    'drop connection #0 from [131.2.3.4]:5 on [10.0.0.1]:22 past Maxstartups',
                    'drop connection #0 from [127.0.0.1]:5 on [127.0.0.1]:22 past Maxstartups',
                    'Timeout, client not responding from user alice 131.2.3.4 port 5',
                    'exited MaxStartups throttling after 00:00:15, 14 connections dropped\r',
                ],
                check=True,
            )
            written = drops.read_text()
        self.assertNotIn('131.2.3.4', written)
        self.assertNotIn('alice', written)
        self.assertEqual(written.count('past Maxstartups'), 2)
        self.assertIn('Timeout, client not responding', written)
        self.assertIn(
            'exited MaxStartups throttling after 00:00:15, 14 connections dropped',
            written,
        )

    def tally_of(self, *lines):
        """the counts sshd's lines leave once through the role's drop pattern and the tally"""
        script = (
            ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh'
        ).read_text()
        patterns = next(
            line.split('=', 1)[1].strip("'")
            for line in script.splitlines()
            if line.startswith('SSHD_DROP_LINES=')
        )
        # what the checks note goes to the tally directly, in order with sshd's lines
        kept = ''.join(
            line + '\n'
            if line == 'loopback check refused'
            else subprocess.run(
                ['grep', '-oE', patterns],
                input=line + '\r\n',
                capture_output=True,
                text=True,
            ).stdout
            for line in lines
        )
        for address in ('131.0.2.9', '10.0.0.1'):
            self.assertNotIn(address, kept)
        with tempfile.NamedTemporaryFile('ab') as drops:
            drops.write(kept.encode())
            drops.flush()
            return self.module().DropTally(drops.name).read()

    def test_every_drop_of_a_9_9_episode_is_counted_not_just_the_one_it_names(self):
        # the dev control run on 9.9: 70 held connections, sshd named one drop and dropped 14
        self.assertEqual(
            self.tally_of(
                'error: beginning MaxStartups throttling',
                'drop connection #50 from [131.0.2.9]:5 on [10.0.0.1]:22 past Maxstartups',
                'exited MaxStartups throttling after 00:00:15, 14 connections dropped',
            ),
            {('maxstartups', 'remote'): 14},
        )

    def test_a_long_9_9_episode_is_counted_as_it_goes_and_once(self):
        metrics = self.module()
        with tempfile.NamedTemporaryFile('ab') as drops:
            tally = metrics.DropTally(drops.name)

            def read(*lines):
                drops.write(b''.join(line + b'\n' for line in lines))
                drops.flush()
                return tally.read()

            self.assertEqual(
                read(b'past Maxstartups', b'loopback check refused'),
                {('maxstartups', 'remote'): 1, ('maxstartups', 'loopback'): 1},
            )
            # five minutes on: the count so far, which takes in the drop named next
            self.assertEqual(
                read(
                    b'in MaxStartups throttling for 00:05:01, 40 connections dropped',
                    b'past Maxstartups',
                ),
                {('maxstartups', 'remote'): 38},
            )
            self.assertEqual(
                read(b'loopback check refused'), {('maxstartups', 'loopback'): 1}
            )
            self.assertEqual(
                read(
                    b'exited MaxStartups throttling after 00:06:00, 45 connections dropped'
                ),
                {('maxstartups', 'remote'): 4},
            )
            # the next episode starts from nothing
            self.assertEqual(
                read(
                    b'past Maxstartups',
                    b'exited MaxStartups throttling after 00:00:02, 3 connections dropped',
                ),
                {('maxstartups', 'remote'): 3},
            )

    def test_10_5_drops_it_does_not_name_are_counted(self):
        # sshd 10.5's own summary line: its first word is not one more named drop
        self.assertEqual(
            self.tally_of(
                'drop connection #50 from [131.0.2.9]:5 on [10.0.0.1]:22 Maxstartups',
                'Maxstartups logging rate-limited: additional 9 connections dropped',
            ),
            {('maxstartups', 'remote'): 10},
        )

    def test_10_5_checks_refusals_it_does_not_name_are_not_remote(self):
        # a local 10.5 run, slots held, five checks, a minute, two checks: sshd named six of the
        # seven refusals and counted the seventh in the summary that the next drop set off
        named = (
            'drop connection #2 from [127.0.0.1]:{} on [127.0.0.1]:52737 Maxstartups'
        )
        refused = 'loopback check refused'
        lines = []
        for port in (52767, 52768, 52769, 52770):
            lines += [named.format(port), refused]
        lines += [refused, named.format(52861)]
        lines += [
            'Maxstartups logging rate-limited: additional 1 connections dropped',
            refused,
            named.format(52862),
            refused,
        ]
        self.assertEqual(self.tally_of(*lines), {('maxstartups', 'loopback'): 7})

    def test_10_5_remote_drops_in_a_summary_are_counted_without_the_checks(self):
        metrics = self.module()
        with tempfile.NamedTemporaryFile('ab') as drops:
            tally = metrics.DropTally(drops.name)

            def read(*lines):
                drops.write(b''.join(line + b'\n' for line in lines))
                drops.flush()
                return tally.read()

            # a check refused unnamed, then the summary: one of its three was the check's
            self.assertEqual(
                read(
                    b'Maxstartups',
                    b'loopback check refused',
                    b'Maxstartups logging rate-limited: additional 3 connections dropped',
                ),
                {('maxstartups', 'remote'): 3, ('maxstartups', 'loopback'): 1},
            )
            # a check refused unnamed, its summary not yet logged: nothing remote yet
            self.assertEqual(
                read(b'loopback check refused'), {('maxstartups', 'loopback'): 1}
            )
            self.assertEqual(
                read(
                    b'Maxstartups logging rate-limited: additional 4 connections dropped'
                ),
                {('maxstartups', 'remote'): 3},
            )

    def test_a_check_named_before_it_notes_its_refusal_is_not_remote(self):
        # sshd names the drop before the check reads the refusal and notes it, so a read can
        # fall in between
        metrics = self.module()
        with tempfile.NamedTemporaryFile('ab') as drops:
            tally = metrics.DropTally(drops.name)
            for line in (
                b'from [127.0.0.1]:5 on [127.0.0.1]:22 past Maxstartups',
                b'loopback check refused',
            ):
                drops.write(line + b'\n')
                drops.flush()
                self.assertNotIn(('maxstartups', 'remote'), tally.read())

    def test_sshd_naming_a_loopback_drop_does_not_count_the_check_twice(self):
        # real lines: from idea-com's bastion on the image's 9.9, and a local run of 10.5; the
        # check that was refused notes it itself
        for line in (
            'drop connection #0 from [127.0.0.1]:38122 on [127.0.0.1]:22 past Maxstartups',
            'drop connection #1 from [127.0.0.1]:49327 on [127.0.0.1]:22222 Maxstartups',
        ):
            self.assertEqual(self.tally_of(line), {}, line)

    def test_sshd_up_is_0_when_nothing_answers_or_sshd_refuses(self):
        import socket
        import threading

        metrics = self.module()
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        port = closed.getsockname()[1]
        closed.close()
        tally = SimpleNamespace(read=lambda: {})
        metrics.active_sessions = lambda proc_root: 0
        metrics.established_connections = lambda: 0
        metrics.sshd_check = lambda real=metrics.sshd_check: real(('127.0.0.1', port))
        found = metrics.lines('idea-com', tally)
        self.assertIn(
            'idea.health.bastion.sshd_up:0|g|#idea_cluster:idea-com|card:none', found
        )
        self.assertFalse([line for line in found if 'sshd_check_seconds' in line])

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()

        def refuse():
            connection, _ = listener.accept()
            connection.sendall(b'Not allowed at this time\r\n')
            connection.close()

        worker = threading.Thread(target=refuse)
        worker.start()
        with tempfile.NamedTemporaryFile('ab') as drops:
            self.assertEqual(
                self.module().sshd_check(listener.getsockname(), drops.name)[0], False
            )
            worker.join()
            listener.close()
            self.assertEqual(
                metrics.DropTally(drops.name).read(), {('maxstartups', 'loopback'): 1}
            )

    def test_sshd_check_sends_nothing_before_it_closes(self):
        # sshd's answer to an identification would be unread at the close, a reset sshd logs
        import socket
        import threading

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        received = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                connection.sendall(b'SSH-2.0-OpenSSH_9.9\r\n')
                received.append(connection.recv(4096))

        worker = threading.Thread(target=serve)
        worker.start()
        with tempfile.NamedTemporaryFile('ab') as drops:
            up, _ = self.module().sshd_check(listener.getsockname(), drops.name)
        worker.join()
        listener.close()
        self.assertEqual((up, received), (True, [b'']))

    def test_no_line_carries_a_hostname_derived_tag(self):
        metrics = self.module()
        metrics.sshd_check = lambda: (True, 0.01)
        metrics.active_sessions = lambda proc_root: 3
        metrics.established_connections = lambda: 4
        tally = SimpleNamespace(read=lambda: {('maxstartups', 'remote'): 2})
        found = metrics.lines('idea-com', tally)
        base = '#idea_cluster:idea-com'
        self.assertIn(f'idea.health.bastion.sshd_up:1|g|{base}|card:none', found)
        self.assertIn(f'idea.health.bastion.sessions:3|g|{base}|card:none', found)
        self.assertIn(
            'idea.health.bastion.connection_drops:2|c|'
            f'{base},reason:maxstartups,source:remote|card:none',
            found,
        )
        self.assertFalse([line for line in found if 'task:' in line])

    def test_a_collector_with_nothing_to_do_is_not_respawned(self):
        script = (
            ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh'
        ).read_text()
        self.assertIn('bastion_metrics.py && break', script)


class RealSshd(unittest.TestCase):
    """
    the health check against this machine's own sshd with every startup slot taken: sshd refuses
    the check's connections and the task stays healthy. Its stderr goes through the role's filter,
    so the CR LF it ends lines with is real too. Skipped where no sshd can run unprivileged.
    """

    def setUp(self):
        import shutil
        import socket
        import time

        sshd = '/usr/sbin/sshd'
        if not Path(sshd).exists() or not shutil.which('ssh-keyscan'):
            self.skipTest('no sshd and ssh-keyscan here')
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        subprocess.run(
            [
                'ssh-keygen',
                '-q',
                '-t',
                'ed25519',
                '-N',
                '',
                '-f',
                str(self.folder / 'key'),
            ],
            check=True,
        )
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]
        probe.close()
        config = self.folder / 'sshd_config'
        lines = [
            f'Port {self.port}',
            'ListenAddress 127.0.0.1',
            f'HostKey {self.folder / "key"}',
            'UsePAM no',
            'PidFile none',
            # start = full: every connection past two is refused, none at random
            'MaxStartups 2',
            'LoginGraceTime 20',
        ]
        config.write_text('\n'.join(lines) + '\n')
        # the role's loopback exemption, where this sshd has penalties at all
        exempt = 'PerSourcePenaltyExemptList 127.0.0.0/8'
        if (
            subprocess.run(
                [sshd, '-t', '-f', str(config), '-o', exempt.replace(' ', '=')],
                capture_output=True,
            ).returncode
            == 0
        ):
            config.write_text(config.read_text() + exempt + '\n')
        self.drops = self.folder / 'drops'
        self.drops.write_bytes(b'')
        self.stderr = self.folder / 'stderr'
        role = (ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh').read_text()
        script = (
            'set -euo pipefail\n'
            + role[
                role.index('SSHD_HEALTH_CHECK_LINES=') : role.index('metrics_pid=\n')
            ]
            + f'export SSHD_DROPS_FILE={shlex.quote(str(self.drops))}\n'
            + f'exec {sshd} -D -e -f {shlex.quote(str(config))} '
            + f'2> >(sshd_log_filter 2>>{shlex.quote(str(self.stderr))})\n'
        )
        self.sshd = subprocess.Popen(['bash', '-c', script])
        self.addCleanup(self.stop)
        for _ in range(50):
            try:
                socket.create_connection(('127.0.0.1', self.port), 1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.skipTest('sshd does not start unprivileged here')
        # the connection above is a close without an identification string
        time.sleep(0.3)

    def stop(self):
        if self.sshd.poll() is None:
            self.sshd.terminate()
            self.sshd.wait(10)

    def health(self):
        return subprocess.run(
            [
                'bash',
                str(ROOT / 'deployment/ecr/idea-control-plane/roles/bastion_health.sh'),
                str(self.port),
            ],
            env={**__import__('os').environ, 'SSHD_DROPS_FILE': str(self.drops)},
            capture_output=True,
            timeout=10,
        ).returncode

    def keyscan(self):
        # the health check before 26.11.2's fix
        return subprocess.run(
            f'timeout 4 ssh-keyscan -t ed25519 -p {self.port} 127.0.0.1 >/dev/null 2>&1',
            shell=True,
        ).returncode

    def test_a_full_sshd_stays_healthy_and_a_stopped_one_does_not(self):
        import socket
        import time

        metrics = load(
            'bastion_metrics',
            'deployment/ecr/idea-control-plane/roles/bastion_metrics.py',
        )
        self.assertEqual(self.health(), 0)
        up, _ = metrics.sshd_check(('127.0.0.1', self.port), str(self.drops))
        self.assertTrue(up)
        time.sleep(0.5)
        held = [socket.create_connection(('127.0.0.1', self.port), 5) for _ in range(2)]
        try:
            for connection in held:
                connection.settimeout(5)
                self.assertTrue(connection.recv(64).startswith(b'SSH-'))
            # positive control: sshd really refuses the old check now; noted as the checks
            # note theirs, as sshd counts it
            self.assertNotEqual(self.keyscan(), 0)
            with open(self.drops, 'ab') as drops:
                drops.write(b'loopback check refused\n')
            for _ in range(3):
                self.assertEqual(self.health(), 0)
            up, _ = metrics.sshd_check(('127.0.0.1', self.port), str(self.drops))
            self.assertFalse(up)
        finally:
            for connection in held:
                connection.close()
        time.sleep(0.5)
        tally = metrics.DropTally(str(self.drops))
        self.assertEqual(tally.read(), {('maxstartups', 'loopback'): 5})
        self.assertEqual(self.health(), 0)
        self.stop()
        self.assertNotEqual(self.health(), 0)
        up, _ = metrics.sshd_check(('127.0.0.1', self.port), str(self.drops))
        self.assertFalse(up)
        # whatever sshd counted on the way out (9.9's episode summary) is the checks' own
        self.assertNotIn(('maxstartups', 'remote'), tally.read())
        # every check's close was CR LF terminated and none reached the log
        log = self.stderr.read_bytes()
        self.assertNotIn(b'by 127.0.0.1 port', log)
        self.assertIn(b'Maxstartups\r\n', log)


if __name__ == '__main__':
    unittest.main()
