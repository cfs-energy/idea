"""Exercise the bootstrap helper using only the system runtime and local commands."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[5]
HOST = ROOT / 'source/idea/idea-bootstrap/common/terminal_host.py'
TEMPLATE = ROOT / 'source/idea/idea-bootstrap/_templates/linux/web_terminal.jinja2'
DCV_HOST = (
    ROOT
    / 'source/idea/idea-bootstrap/virtual-desktop-host-linux/configure_dcv_host.sh.jinja2'
)
BASTION = ROOT / 'deployment/ecr/idea-control-plane/roles/bastion.sh'


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location('terminal_host_test', HOST)
    module = importlib.util.module_from_spec(spec)
    exec(compile(HOST.read_text(), str(HOST), 'exec'), module.__dict__)
    return module


def test_host_imports_without_site_packages():
    subprocess.run(
        [sys.executable, '-S', '-c', f'import runpy; runpy.run_path({str(HOST)!r})'],
        check=True,
    )


def test_desktop_runtime_and_package_transaction():
    from jinja2 import Template

    rendered = Template(TEMPLATE.read_text()).render(context=MagicMock())
    assert 'install -y tmux python3' in rendered
    assert (
        'systemctl --no-block restart idea-terminal-trust.service || log_warning'
        in rendered
    )
    assert 'apt_update || log_warning' in rendered
    assert (
        'DEBIAN_FRONTEND=noninteractive package_transaction apt-get -o DPkg::Lock::Timeout=300'
        in rendered
    )
    assert '/opt/idea/python' not in rendered
    subprocess.run(['bash', '-n'], input=rendered, text=True, check=True)


def test_bastion_starts_ssh_before_optional_trust(tmp_path):
    script = BASTION.read_text()
    # Execute the actual startup ordering, replacing only host daemons and the helper.
    start = script.index('# Site customizations')
    first_helper = script.index('timeout 300 python3.13')
    script = script[
        min(start, first_helper) : script.index('# Losing directory authentication')
    ]
    script = script.replace('/usr/sbin/sshd', 'sshd').replace('/usr/sbin/sssd', 'sssd')
    script = script.replace('/run/idea-terminal-trust-ready', str(tmp_path / 'ready'))
    prefix = r"""set -euo pipefail
sshd() { if [[ $1 == -t ]]; then return; fi; touch "$SSH_STARTED"; sleep 1; }
sssd() { sleep 1; }
kill() { :; }
timeout() {
  for ((i=0; i<20; i++)); do
    if [[ -f "$SSH_STARTED" ]]; then echo trust-after-ssh; return; fi
    sleep 0.01
  done
  echo trust-blocked-ssh
  return 124
}
"""
    result = subprocess.run(
        ['bash', '-c', prefix + script + '\nwait "$trust_pid"\n'],
        env={'SSH_STARTED': str(tmp_path / 'ssh-started')},
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert 'trust-after-ssh' in result.stdout
    assert 'trust-blocked-ssh' not in result.stdout


def test_retry_budget_and_cause(helper, monkeypatch):
    monkeypatch.setenv('IDEA_MODULE_NAME', 'bastion-host')
    monkeypatch.setenv('IDEA_CLUSTER_NAME', 'test')
    error = ValueError('Terminal CA is not ready')
    configure = MagicMock(side_effect=error)
    sleep = MagicMock()
    monkeypatch.setattr(helper, 'configure', configure)
    monkeypatch.setattr(helper.time, 'sleep', sleep)
    with pytest.raises(RuntimeError) as caught:
        helper.main()
    assert caught.value.__cause__ is error
    assert configure.call_count == 5
    assert sum(call.args[0] for call in sleep.call_args_list) == 20


def test_cli_has_bounded_calls_and_no_sdk(helper, monkeypatch):
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout='{"Item": {}}')
    )
    monkeypatch.setattr(helper.subprocess, 'run', run)
    assert helper.dynamodb('get-item', TableName='test.cluster-settings') == {
        'Item': {}
    }
    args, kwargs = run.call_args
    assert args[0][:3] == ['aws', 'dynamodb', 'get-item']
    assert json.loads(args[0][4]) == {'TableName': 'test.cluster-settings'}
    assert kwargs['timeout'] == 10
    assert kwargs['env']['AWS_MAX_ATTEMPTS'] == '1'


def test_existing_site_ca_survives_install_and_rotation(helper, tmp_path, monkeypatch):
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    config = ssh / 'sshd_config'
    original = 'Include /etc/ssh/sshd_config.d/*.conf\nUsePAM yes\nMatch User guest\n  X11Forwarding no\n'
    config.write_text(original)
    site = ssh / 'site-ca.pub'
    site.write_text('ssh-ed25519 site-key\n')
    (ssh / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 host-key')
    ddb = MagicMock(return_value={'Item': {'value': {'S': 'ssh-ed25519 first-key'}}})
    monkeypatch.setattr(helper, 'dynamodb', ddb)
    monkeypatch.setattr(
        helper.subprocess,
        'run',
        MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout='trustedusercakeys /etc/ssh/site-ca.pub\n'
            )
        ),
    )
    helper.configure('example', 'bastion', root=tmp_path)
    helper.configure('example', 'bastion', root=tmp_path)
    assert config.read_text().endswith(original)
    assert 'TrustedUserCAKeys' not in config.read_text()
    assert site.read_text() == 'ssh-ed25519 site-key\nssh-ed25519 first-key\n'
    ddb.return_value = {'Item': {'value': {'S': 'ssh-ed25519 second-key'}}}
    helper.configure('example', 'bastion', root=tmp_path)
    assert site.read_text() == 'ssh-ed25519 site-key\nssh-ed25519 second-key\n'


@pytest.mark.parametrize(
    'local, directory, ca, principal',
    [
        (0, 0, 'idea-key', ''),
        (2, 0, 'idea-key', 'idea:alice\n'),
        (2, 2, 'idea-key', ''),
        (1, 0, 'idea-key', ''),
        (0, 0, 'site-key', 'alice\n'),
    ],
)
def test_principals_reject_all_local_accounts(
    helper, tmp_path, monkeypatch, capsys, local, directory, ca, principal
):
    key = tmp_path / 'ca.pub'
    key.write_text('ssh-ed25519 idea-key')
    ready = tmp_path / 'ready'
    ready.touch()
    code = helper.PRINCIPALS_COMMAND.replace(
        '/etc/ssh/idea_terminal_ca.pub', str(key)
    ).replace('/run/idea-terminal-trust-ready', str(ready))
    namespace = {'__name__': 'test_principals'}
    exec(compile(code, 'principals', 'exec'), namespace)
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], local, stdout='alice:x:1000:'),
            subprocess.CompletedProcess([], directory, stdout='alice:x:1000:'),
        ]
    )
    monkeypatch.setattr(subprocess, 'run', run)
    namespace['main']('alice', ca)
    assert capsys.readouterr().out == principal
    if ca == 'idea-key':
        assert run.call_args_list[0].args[0] == [
            '/usr/bin/getent',
            '-s',
            'files',
            'passwd',
            'alice',
        ]


@pytest.mark.parametrize('failure', ['read', 'replace', 'tracking'])
def test_failed_rotation_keeps_previous_key_until_bundle_replaced(
    helper, tmp_path, monkeypatch, failure
):
    import os
    import asyncssh

    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    keys = [
        asyncssh.generate_private_key('ssh-ed25519')
        .export_public_key()
        .decode()
        .strip()
        for _ in range(3)
    ]
    site_key, old, new = keys
    site = ssh / 'site-ca.pub'
    site.write_text(site_key + '\n' + old + '\n')
    tracking = ssh / 'idea_terminal_ca.pub'
    tracking.write_text(old + '\n')
    (ssh / 'sshd_config').write_text('UsePAM yes\n')
    (ssh / 'ssh_host_ed25519_key.pub').write_text(site_key)
    monkeypatch.setattr(
        helper, 'dynamodb', MagicMock(return_value={'Item': {'value': {'S': new}}})
    )
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[:3] == ['sshd', '-t', '-f']:
            return subprocess.CompletedProcess(args, 0)
        if args == ['sshd', '-T']:
            return subprocess.CompletedProcess(
                args, 0, stdout='trustedusercakeys /etc/ssh/site-ca.pub\n'
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(helper.subprocess, 'run', run)
    with monkeypatch.context() as failing:
        if failure == 'read':
            read = Path.read_text

            def fail_read(path, *args, **kwargs):
                if path == site:
                    raise OSError('bundle unavailable')
                return read(path, *args, **kwargs)

            failing.setattr(Path, 'read_text', fail_read)
        else:
            replace = os.replace
            write = Path.write_text

            def fail_replace(src, dst):
                if Path(dst) == (tracking if failure == 'tracking' else site):
                    raise OSError('bundle unavailable')
                return replace(src, dst)

            def fail_write(path, *args, **kwargs):
                if path == site:
                    raise OSError('bundle unavailable')
                return write(path, *args, **kwargs)

            failing.setattr(os, 'replace', fail_replace)
            failing.setattr(Path, 'write_text', fail_write)
        with pytest.raises(OSError, match='bundle unavailable'):
            helper.configure('example', 'bastion', root=tmp_path)
    assert tracking.read_text().strip() == old
    helper.configure('example', 'bastion', root=tmp_path)
    assert site.read_text().splitlines() == [site_key, new]
    assert tracking.read_text().strip() == new
    assert not list(ssh.glob('.*.tmp'))


def test_invalid_rotation_key_does_not_replace_effective_trust(
    helper, tmp_path, monkeypatch
):
    import asyncssh

    old = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    site_key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    site = ssh / 'site-ca.pub'
    original = site_key + old
    site.write_text(original)
    (ssh / 'idea_terminal_ca.pub').write_text(old)
    (ssh / 'sshd_config').write_text('UsePAM yes\n')
    monkeypatch.setattr(
        helper,
        'dynamodb',
        MagicMock(return_value={'Item': {'value': {'S': 'ssh-ed25519 invalid'}}}),
    )
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[:3] == ['sshd', '-t', '-f']:
            return subprocess.CompletedProcess(args, 0)
        if args == ['sshd', '-T']:
            return subprocess.CompletedProcess(
                args, 0, stdout='trustedusercakeys /etc/ssh/site-ca.pub\n'
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(helper.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        helper.configure('example', 'bastion', root=tmp_path)
    assert site.read_text() == original
    assert (ssh / 'idea_terminal_ca.pub').read_text() == old


@pytest.mark.parametrize('failure', ['validation', 'replace', None])
def test_sshd_config_replace_is_validated_atomic_and_preserves_metadata(
    helper, tmp_path, monkeypatch, failure
):
    import os

    config = tmp_path / 'sshd_config'
    config.write_text('UsePAM yes\n')
    config.chmod(0o640)
    original = config.stat()
    validations = []

    def validate(args, **kwargs):
        assert args[:3] == ['sshd', '-t', '-f']
        assert Path(args[3]).read_text() == 'UsePAM no\n'
        assert config.read_text() == 'UsePAM yes\n'
        validations.append(args)
        if failure == 'validation':
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(helper.subprocess, 'run', validate)
    replace = os.replace

    def publish(src, dst):
        assert validations
        assert Path(src).stat().st_mode == original.st_mode
        if failure == 'replace':
            raise OSError('disk error')
        replace(src, dst)

    monkeypatch.setattr(os, 'replace', publish)
    if failure:
        with pytest.raises((OSError, subprocess.CalledProcessError)):
            helper.replace_sshd_config(config, 'UsePAM no\n')
        assert config.read_text() == 'UsePAM yes\n'
    else:
        helper.replace_sshd_config(config, 'UsePAM no\n')
        assert config.read_text() == 'UsePAM no\n'
        assert (config.stat().st_uid, config.stat().st_gid) == (
            original.st_uid,
            original.st_gid,
        )
    assert list(tmp_path.iterdir()) == [config]


def test_host_and_principals_use_python36_subprocess_api(
    helper, monkeypatch, tmp_path, capsys
):
    # Enforce the actual 3.6 subprocess keyword surface on the host and generated command.
    real_run = subprocess.run

    def python36_run(args, **kwargs):
        assert 'capture_output' not in kwargs and 'text' not in kwargs
        if args[0] == 'aws':
            args = [sys.executable, '-c', 'print("{}")']
        elif args[0] == '/usr/bin/getent':
            args = (
                [sys.executable, '-c', 'import sys; sys.exit(2)']
                if args[2] == 'files'
                else [sys.executable, '-c', 'print("alice:x:1000:")']
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(helper.subprocess, 'run', python36_run)
    assert helper.dynamodb('get-item') == {}
    key = tmp_path / 'ca.pub'
    key.write_text('ssh-ed25519 idea-key')
    namespace = {'__name__': 'principals_test'}
    ready = tmp_path / 'ready'
    ready.touch()
    exec(
        helper.PRINCIPALS_COMMAND.replace(
            '/etc/ssh/idea_terminal_ca.pub', str(key)
        ).replace('/run/idea-terminal-trust-ready', str(ready)),
        namespace,
    )
    namespace['main']('alice', 'idea-key')
    assert capsys.readouterr().out == 'idea:alice\n'


def test_desktop_installs_trust_refresh_on_every_boot():
    from jinja2 import Template

    rendered = Template(TEMPLATE.read_text()).render(context=MagicMock())
    assert 'Before=sshd.service ssh.service' not in rendered
    assert 'After=idea-terminal-trust.service' not in rendered
    assert 'systemctl reload ' not in rendered
    assert 'After=network-online.target' in rendered
    assert (
        "ExecStart=/bin/sh -c 'until /usr/bin/python3 /usr/local/libexec/idea-terminal-host.py; do [ $$? -ne 78 ] || exit 78; sleep 30; done'"
        in rendered
    )
    assert 'EnvironmentFile=/etc/environment' in rendered
    assert 'systemctl enable idea-terminal-trust.service' in rendered
    assert 'systemctl --no-block restart idea-terminal-trust.service' in rendered
    subprocess.run(['bash', '-n'], input=rendered, text=True, check=True)


def test_previous_boot_ca_is_denied_until_trust_refresh(
    helper, tmp_path, monkeypatch, capsys
):
    key = tmp_path / 'ca.pub'
    key.write_text('ssh-ed25519 old-key')
    ready = tmp_path / 'ready'
    code = helper.PRINCIPALS_COMMAND.replace(
        '/etc/ssh/idea_terminal_ca.pub', str(key)
    ).replace('/run/idea-terminal-trust-ready', str(ready))
    namespace = {'__name__': 'principals_test'}
    exec(code, namespace)
    monkeypatch.setattr(
        subprocess,
        'run',
        MagicMock(
            side_effect=[
                subprocess.CompletedProcess([], 2),
                subprocess.CompletedProcess([], 0, stdout='alice:x:1000:'),
            ]
        ),
    )
    namespace['main']('alice', 'old-key')
    assert capsys.readouterr().out == ''
    # Other site CAs and ordinary SSH keep their existing principal policy.
    namespace['main']('alice', 'site-key')
    assert capsys.readouterr().out == 'alice\n'


def test_hibernate_clears_trust_and_schedules_refresh(tmp_path):
    import os
    import re
    from jinja2 import Template

    rendered = Template(TEMPLATE.read_text()).render(context=MagicMock())
    hook = re.search(
        r"/usr/lib/systemd/system-sleep/idea-terminal-trust <<'SLEEP'\n(.*?)\nSLEEP",
        rendered,
        re.S,
    )
    assert hook, 'hibernation bypasses boot units; clear trust before saving RAM'
    ready = tmp_path / 'ready'
    ready.touch()
    script = tmp_path / 'sleep-hook'
    script.write_text(hook[1].replace('/run/idea-terminal-trust-ready', str(ready)))
    subprocess.run(['bash', str(script), 'pre', 'hibernate'], check=True)
    assert not ready.exists()
    command = tmp_path / 'systemctl'
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    command.chmod(0o755)
    result = subprocess.run(
        ['bash', str(script), 'post', 'hibernate'],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']},
    )
    assert result.stdout.splitlines() == [
        '--no-block',
        'restart',
        'idea-terminal-trust.service',
    ]
    assert not ready.exists(), (
        'only successful trust installation can mark this resume ready'
    )


def fake_host(tmp_path, monkeypatch, helper, reload_failures=0, units=None):
    """A desktop whose sshd unit is sshd.service, with ssh as its Ubuntu-style alias."""
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    (ssh / 'sshd_config').write_text('UsePAM yes\n')
    (ssh / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 host-key')
    monkeypatch.setattr(
        helper,
        'dynamodb',
        lambda *a, **k: {'Item': {'value': {'S': 'ssh-ed25519 ca-key'}}},
    )
    units = {'sshd': 'sshd.service', 'ssh': 'sshd.service'} if units is None else units
    calls = []
    failures = [reload_failures]

    def run(args, **kwargs):
        if args[0] == 'sshd':
            assert (tmp_path / 'run/sshd').is_dir()
            assert (tmp_path / 'run/sshd').stat().st_mode & 0o777 == 0o755
        calls.append(args)
        if args[:2] == ['systemctl', 'show']:
            unit = units.get(args[-1])
            shown = (
                f'Id={unit}\nLoadState=loaded\n'
                if unit
                else (f'Id={args[-1]}.service\nLoadState=not-found\n')
            )
            return subprocess.CompletedProcess(args, 0, stdout=shown)
        if args[:2] == ['systemctl', 'reload-or-try-restart'] and failures[0]:
            failures[0] -= 1
            assert kwargs.get('check'), 'a failed reload must not mark trust ready'
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout='trustedusercakeys none\n')

    monkeypatch.setattr(helper.subprocess, 'run', run)
    return calls


def test_configure_creates_privsep_before_sshd_and_reloads_only_changes(
    helper, tmp_path, monkeypatch
):
    calls = fake_host(tmp_path, monkeypatch, helper)
    helper.configure('example', 'desktop', root=tmp_path)
    helper.configure('example', 'desktop', root=tmp_path)
    reloads = [args for args in calls if args[1] == 'reload-or-try-restart']
    # The alias resolves to one unit, reloaded once, for the one change.
    assert reloads == [['systemctl', 'reload-or-try-restart', 'sshd.service']]


def test_failed_sshd_reload_is_retried_before_trust_is_ready(
    helper, tmp_path, monkeypatch
):
    calls = fake_host(tmp_path, monkeypatch, helper, reload_failures=1)
    ready = tmp_path / 'run/idea-terminal-trust-ready'
    with pytest.raises(subprocess.CalledProcessError):
        helper.configure('example', 'desktop', root=tmp_path)
    assert not ready.exists()
    # sshd_config is already current, but the running daemon has not loaded it yet.
    helper.configure('example', 'desktop', root=tmp_path)
    assert ready.exists()
    reloads = [args for args in calls if args[1] == 'reload-or-try-restart']
    assert len(reloads) == 2


def test_missing_sshd_unit_is_a_failed_attempt(helper, tmp_path, monkeypatch):
    fake_host(tmp_path, monkeypatch, helper, units={})
    with pytest.raises(RuntimeError, match='No sshd unit'):
        helper.configure('example', 'desktop', root=tmp_path)
    assert not (tmp_path / 'run/idea-terminal-trust-ready').exists()


def test_bastion_leaves_sshd_signalling_to_its_entrypoint(
    helper, tmp_path, monkeypatch
):
    calls = fake_host(tmp_path, monkeypatch, helper, units={})
    helper.configure('example', 'bastion', root=tmp_path)
    assert not [args for args in calls if args[0] == 'systemctl']


def test_sshd_config_is_edited_under_the_site_hook_lock(helper, tmp_path, monkeypatch):
    import fcntl

    fake_host(tmp_path, monkeypatch, helper)
    replace = helper.os.replace
    checked = []

    def publish(src, dst):
        if Path(dst).name == 'sshd_config':
            with open(tmp_path / helper.LOCK) as other:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            checked.append(dst)
        replace(src, dst)

    monkeypatch.setattr(helper.os, 'replace', publish)
    helper.configure('example', 'desktop', root=tmp_path)
    assert checked


def test_held_lock_fails_the_attempt_without_editing(helper, tmp_path, monkeypatch):
    import fcntl

    fake_host(tmp_path, monkeypatch, helper)
    monkeypatch.setattr(helper, 'LOCK_WAIT_SECONDS', 2)
    monkeypatch.setattr(helper.time, 'sleep', lambda _: None)
    (tmp_path / 'run').mkdir(exist_ok=True)
    with open(tmp_path / helper.LOCK, 'a') as hook:
        fcntl.flock(hook, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match='locked'):
            helper.configure('example', 'desktop', root=tmp_path)
    assert (tmp_path / 'etc/ssh/sshd_config').read_text() == 'UsePAM yes\n'
    assert not (tmp_path / 'run/idea-terminal-trust-ready').exists()


def test_desktop_site_hook_runs_under_the_trust_lock(helper, tmp_path):
    import re

    script = DCV_HOST.read_text()
    block = re.search(r'\n  \(\n(.*?)\n  \) \|\| log_warning "userdata', script, re.S)
    assert block, (
        'the site hook must hold the lock terminal trust takes for sshd_config'
    )
    body = block[1]
    lock = re.search(r'exec 9>>(\S+) && flock -w \d+ 9', body)
    assert lock and lock[1] == '/' + helper.LOCK
    assert 'userdata_customizations.sh 9>&-' in body
    # An unopenable or stuck lock must delay, never skip, the site hook.
    hook = tmp_path / 'hook.sh'
    hook.write_text('echo ran-hook\n')
    body = body.replace('/' + helper.LOCK, str(tmp_path / 'missing/lock'))
    body = re.sub(r'/bin/bash \S+userdata_customizations.sh', f'/bin/bash {hook}', body)
    body = re.sub(r'>> \S+ 2>&1', '', body)
    result = subprocess.run(
        ['bash', '-c', 'log_warning() { echo "warning: $*"; }\n' + body],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert 'warning: terminal trust lock unavailable' in result.stdout
    assert 'ran-hook' in result.stdout


def test_principals_are_published_atomically_and_unchanged_script_is_preserved(
    helper, tmp_path, monkeypatch
):
    fake_host(tmp_path, monkeypatch, helper)
    principals = tmp_path / 'etc/ssh/idea_terminal_principals'
    principals.write_text('old script')
    principals.chmod(0o755)
    replace = helper.os.replace
    publications = []

    def publish(src, dst):
        if dst == principals:
            assert principals.read_text() == 'old script'
            assert Path(src).read_text() == helper.PRINCIPALS_COMMAND
            assert Path(src).stat().st_mode & 0o777 == 0o755
            publications.append(dst)
        replace(src, dst)

    monkeypatch.setattr(helper.os, 'replace', publish)
    helper.configure('example', 'desktop', root=tmp_path)
    helper.configure('example', 'desktop', root=tmp_path)
    assert publications == [principals]


def test_bastion_retries_failed_trust_and_reloads_running_sshd(tmp_path):
    import re

    script = BASTION.read_text()
    retry = re.search(
        r'(# Initialize and retry terminal trust.*?)(?=\ntrap )', script, re.S
    )
    assert retry, 'a transient trust failure must recover without replacing the task'
    calls = tmp_path / 'calls'
    result = subprocess.run(
        [
            'bash',
            '-c',
            'set -euo pipefail\n'
            + 'sshd_pid=123\n'
            + 'timeout() { echo attempt >> "$CALLS"; [[ $(wc -l < "$CALLS") -gt 1 ]]; }\n'
            + 'sleep() { :; }\n'
            + 'kill() { echo "$*"; }\n'
            + retry[1].replace(
                '/run/idea-terminal-trust-ready', str(tmp_path / 'ready')
            )
            + '\nwait "$trust_pid"\n',
        ],
        env={'CALLS': str(calls)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert len(calls.read_text().splitlines()) == 2
    assert 'Terminal trust not installed; retrying in 30 seconds' in result.stdout
    assert '-HUP 123' in result.stdout


def test_bastion_stops_retrying_on_site_integration_error(tmp_path):
    import re

    retry = re.search(
        r'(# Initialize and retry terminal trust.*?)(?=\ntrap )',
        BASTION.read_text(),
        re.S,
    )
    calls = tmp_path / 'calls'
    result = subprocess.run(
        [
            'bash',
            '-c',
            'set -euo pipefail\n'
            + 'sshd_pid=123\n'
            + 'timeout() { echo attempt >> "$CALLS"; return 78; }\n'
            + 'sleep() { :; }\n'
            + 'kill() { echo "$*"; }\n'
            + retry[1].replace(
                '/run/idea-terminal-trust-ready', str(tmp_path / 'ready')
            )
            + '\nwait "$trust_pid"\n',
        ],
        env={'CALLS': str(calls)},
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 1
    assert 'site principal policy' in result.stdout
    assert '-HUP' not in result.stdout


def unit_section(rendered, section):
    import re

    unit = re.search(
        r"idea-terminal-trust.service <<'UNIT'\n(.*?)\nUNIT", rendered, re.S
    )
    lines, current = {}, None
    for line in unit[1].splitlines():
        if line.startswith('['):
            current = line
        elif '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            lines.setdefault((current, key), []).append(value)
    return {key: values for (name, key), values in lines.items() if name == section}


def run_unit_loop(tmp_path, exits):
    import shlex

    service = unit_section(TEMPLATE.read_text(), '[Service]')
    command = service['ExecStart'][0]
    # systemd expands $NAME itself; only $$ reaches the shell, as a single $.
    assert '$' not in command.replace('$$', '')
    command = command.replace('$$', '$')
    calls = tmp_path / 'calls'
    helper = tmp_path / 'helper'
    helper.write_text(
        '#!/bin/sh\necho attempt >> "{0}"\nset -- {1}\nshift $(($(wc -l < "{0}") - 1))\nexit $1\n'.format(
            calls, ' '.join(map(str, exits))
        )
    )
    helper.chmod(0o755)
    command = command.replace(
        '/usr/bin/python3 /usr/local/libexec/idea-terminal-host.py', str(helper)
    )
    command = command.replace('sleep 30', 'sleep 0.01')
    result = subprocess.run(shlex.split(command), timeout=5)
    return result.returncode, len(calls.read_text().splitlines())


def test_desktop_unit_retries_until_success(tmp_path):
    assert run_unit_loop(tmp_path, [1] * 7 + [0]) == (0, 8)


def test_desktop_unit_stops_on_site_integration_error(tmp_path):
    # Retrying cannot fix a site principal policy; the unit fails visibly instead.
    assert run_unit_loop(tmp_path, [1, 78, 0]) == (78, 2)


def test_desktop_unit_never_holds_boot_targets():
    service = unit_section(TEMPLATE.read_text(), '[Service]')
    install = unit_section(TEMPLATE.read_text(), '[Install]')
    assert install['WantedBy'] == ['multi-user.target']
    # A oneshot looping in ExecStart keeps multi-user.target (and Ubuntu's cloud-final,
    # which the bootstrap reboot waits for) activating until trust succeeds.
    assert service['Type'] == ['simple']
    assert 'until ' in service['ExecStart'][0]
    # A start timeout is meaningless for simple; 'infinity' is unparsed on systemd 219.
    assert 'TimeoutStartSec' not in service
    # The shell loop retries and stops on status 78; there is no Restart= policy.
    assert 'Restart' not in service
    # The proxy profile holds shell exports systemd cannot parse; /etc/environment has IDEA_*.
    assert service['EnvironmentFile'] == ['/etc/environment']


def test_helper_exits_78_on_site_principal_policy(helper, monkeypatch):
    monkeypatch.setenv('IDEA_MODULE_NAME', 'bastion-host')
    monkeypatch.setenv('IDEA_CLUSTER_NAME', 'test')
    configure = MagicMock(
        side_effect=helper.SiteIntegrationRequired('Site certificate principal policy')
    )
    monkeypatch.setattr(helper, 'configure', configure)
    monkeypatch.setattr(helper.time, 'sleep', MagicMock())
    with pytest.raises(SystemExit) as caught:
        helper.main()
    assert caught.value.code == 78 == helper.EXIT_SITE_INTEGRATION
    assert configure.call_count == 1


def test_site_principal_policy_raises_the_permanent_error(
    helper, tmp_path, monkeypatch
):
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    (ssh / 'sshd_config').write_text('AuthorizedPrincipalsFile /etc/ssh/principals\n')
    monkeypatch.setattr(
        helper,
        'dynamodb',
        lambda *a, **k: {'Item': {'value': {'S': 'ssh-ed25519 ca-key'}}},
    )
    monkeypatch.setattr(
        helper.subprocess,
        'run',
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout='authorizedprincipalsfile /etc/ssh/principals\n'
        ),
    )
    with pytest.raises(helper.SiteIntegrationRequired):
        helper.configure('example', 'desktop', root=tmp_path)


def test_cli_receives_cluster_proxy_environment(helper, tmp_path, monkeypatch):
    import os

    # Execute a local CLI stand-in, so subprocess environment propagation is real.
    cli = tmp_path / 'aws'
    cli.write_text(
        '#!'
        + sys.executable
        + '\nimport json, os\nprint(json.dumps({k: os.environ.get(k) for k in ("HTTPS_PROXY", "NO_PROXY")}))\n'
    )
    cli.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    for name in ('HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('IDEA_HTTPS_PROXY', 'http://proxy.example:3128')
    monkeypatch.setenv('IDEA_NO_PROXY', '169.254.169.254,localhost')
    assert helper.dynamodb('get-item') == {
        'HTTPS_PROXY': 'http://proxy.example:3128',
        'NO_PROXY': '169.254.169.254,localhost',
    }
