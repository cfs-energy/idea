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
    assert 'systemctl restart idea-terminal-trust.service || log_warning' in rendered
    assert 'apt_update || log_warning' in rendered
    assert (
        'DEBIAN_FRONTEND=noninteractive package_transaction apt-get -o DPkg::Lock::Timeout=300'
        in rendered
    )
    assert '/opt/idea/python' not in rendered
    subprocess.run(['bash', '-n'], input=rendered, text=True, check=True)


def test_bastion_continues_after_failed_trust(tmp_path):
    # Run the actual trust-install line under the bastion's fail-fast shell options.
    line = next(
        line
        for line in BASTION.read_text().splitlines()
        if '/opt/idea/roles/terminal_host.py' in line
    )
    assert line.startswith('timeout 300 ')
    script = (
        'set -euo pipefail\ntimeout() { return 124; }\n'
        + line
        + '\necho sshd-started\n'
    )
    result = subprocess.run(
        ['bash', '-c', script], text=True, capture_output=True, check=True
    )
    assert 'sshd-started' in result.stdout


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
    assert 'sshd -t && (systemctl reload sshd || systemctl reload ssh)' in rendered
    assert 'After=network-online.target' in rendered
    assert (
        'ExecStart=/usr/bin/python3 /usr/local/libexec/idea-terminal-host.py'
        in rendered
    )
    assert 'EnvironmentFile=/etc/environment' in rendered
    assert 'systemctl enable idea-terminal-trust.service' in rendered
    assert 'systemctl restart idea-terminal-trust.service' in rendered
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


def test_configure_creates_privsep_before_sshd_and_reloads_only_changes(
    helper, tmp_path, monkeypatch
):
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    (ssh / 'sshd_config').write_text('UsePAM yes\n')
    (ssh / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 host-key')
    monkeypatch.setattr(
        helper,
        'dynamodb',
        lambda *a, **k: {'Item': {'value': {'S': 'ssh-ed25519 ca-key'}}},
    )
    calls = []

    def run(args, **kwargs):
        if args[0] == 'sshd':
            assert (tmp_path / 'run/sshd').is_dir()
            assert (tmp_path / 'run/sshd').stat().st_mode & 0o777 == 0o755
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='trustedusercakeys none\n')

    monkeypatch.setattr(helper.subprocess, 'run', run)
    helper.configure('example', 'desktop', root=tmp_path)
    helper.configure('example', 'desktop', root=tmp_path)
    assert calls.count(['systemctl', 'try-reload-or-restart', 'sshd', 'ssh']) == 1


def test_principals_are_published_atomically_and_unchanged_script_is_preserved(
    helper, tmp_path, monkeypatch
):
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    (ssh / 'sshd_config').write_text('UsePAM yes\n')
    (ssh / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 host-key')
    principals = ssh / 'idea_terminal_principals'
    principals.write_text('old script')
    principals.chmod(0o755)
    monkeypatch.setattr(
        helper,
        'dynamodb',
        lambda *a, **k: {'Item': {'value': {'S': 'ssh-ed25519 ca-key'}}},
    )
    monkeypatch.setattr(
        helper.subprocess,
        'run',
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout='trustedusercakeys none\n'
        ),
    )
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
    retry = re.search(r'(# Retry terminal trust.*?)(?=\ntrap )', script, re.S)
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
