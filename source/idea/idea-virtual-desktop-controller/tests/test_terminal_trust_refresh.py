"""The desktop refresh gives a desktop launched before 26.11.1 the bootstrap's terminal trust."""

import base64
import gzip
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    BOOTSTRAP_REFRESH_LINUX_BASE_OS,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    BOOTSTRAP_REFRESH_VERSION,
    render_bootstrap_refresh,
)

BOOTSTRAP = Path(__file__).resolve().parents[2] / 'idea-bootstrap'
HELPER = BOOTSTRAP / 'common/terminal_host.py'
VARIABLES = {'pbs_server_name': None, 'private_zone': None, 'bootstrap_refresh': True}
LINUX = sorted(BOOTSTRAP_REFRESH_LINUX_BASE_OS)
# Directories the script writes under, moved into the test's root.
MOVED = ('/etc/', '/usr/local/', '/usr/lib/', '/var/', '/run/')
TOOLS = (
    'base64 gzip mktemp cat rm install sha256sum mkdir grep sed printf paste awk tr '
    'chmod dirname cp mv ls env sort head tail cut tee touch'
).split()


def render(base_os):
    return render_bootstrap_refresh(str(BOOTSTRAP), base_os, VARIABLES)


def heredoc(text, path, end):
    found = re.search(
        rf"{re.escape(path)}[^\n]*<<'{end}'[^\n]*\n(.*?\n){end}\n", text, re.S
    )
    assert found, path
    return found[1]


def test_the_refresh_carries_terminal_trust_from_this_release():
    assert BOOTSTRAP_REFRESH_VERSION >= 5


@pytest.mark.parametrize('base_os', LINUX)
def test_refresh_installs_the_bootstrap_helper_unit_and_hook(base_os):
    script = render(base_os)
    archive = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d", script)[1]
    assert gzip.decompress(base64.b64decode(archive)) == HELPER.read_bytes()
    bootstrap = (BOOTSTRAP / '_templates/linux/web_terminal.jinja2').read_text()
    assert heredoc(script, 'IDEA_TERMINAL_STAGE}/unit', 'UNIT') == heredoc(
        bootstrap, '/etc/systemd/system/idea-terminal-trust.service', 'UNIT'
    )
    assert heredoc(script, 'IDEA_TERMINAL_STAGE}/sleep', 'SLEEP') == heredoc(
        bootstrap, '/usr/lib/systemd/system-sleep/idea-terminal-trust', 'SLEEP'
    )
    block = script[script.index('# Begin: Terminal Trust') :]
    # The SSM document runs sh; the trust block stays POSIX so dash runs it unchanged.
    for shell in ('bash', 'dash'):
        if shutil.which(shell):
            subprocess.run([shell, '-n'], input=block, text=True, check=True)
    assert 'systemctl reload' not in block and 'restart sshd' not in block
    # The same input renders the same command, so an unchanged release sends an identical refresh.
    assert render(base_os) == script


class Desktop:
    """A desktop filesystem under tmp_path with stub systemd, sshd and package manager."""

    def __init__(self, tmp_path, base_os):
        self.root = tmp_path / 'root'
        self.bin = tmp_path / 'bin'
        # sh runs the SSM document: dash on Ubuntu, bash on the Red Hat family and AL2023.
        dash = shutil.which('dash')
        self.shell = (
            dash if base_os.startswith('ubuntu') and dash else shutil.which('bash')
        )
        self.calls = tmp_path / 'calls'
        self.state = tmp_path / 'state'
        for path in (self.root / 'etc/ssh', self.bin, self.state):
            path.mkdir(parents=True)
        (self.root / 'etc/ssh/sshd_config').write_text('UsePAM yes\n')
        self.calls.write_text('')
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                (self.bin / name).symlink_to(found)
        manager = 'apt-get' if base_os.startswith('ubuntu') else 'yum'
        self.stub(
            manager,
            f'echo "{manager} $*" >> {self.calls}\n'
            f'case "$*" in *install*) for t in tmux python3; do printf "#!/bin/sh\\n" > {self.bin}/$t; '
            f'chmod 755 {self.bin}/$t; done ;; esac',
        )
        self.stub(
            'systemctl',
            f'echo "systemctl $*" >> {self.calls}\n'
            '[ "$1" = --no-block ] && shift\n'
            'case "$1" in\n'
            f'  is-enabled) [ -f {self.state}/enabled ] ;;\n'
            f'  enable) touch {self.state}/enabled ;;\n'
            f'  is-active) [ -f {self.state}/active ] ;;\n'
            f'  restart) [ "$2" = idea-terminal-trust.service ] && touch {self.state}/active; exit 0 ;;\n'
            'esac',
        )
        kex = self.root / 'etc/ssh/sshd_config.d/10-idea-kex.conf'
        self.stub(
            'sshd',
            f'echo "sshd $*" >> {self.calls}\n'
            f'if [ "$1" = -T ]; then if [ -f {kex} ]; then sed "s/^KexAlgorithms/kexalgorithms/" {kex}; '
            'else echo "kexalgorithms curve25519-sha256,ecdh-sha2-nistp256"; fi; fi',
        )
        self.stub(
            'ssh',
            'printf "mlkem768x25519-sha256\\ncurve25519-sha256\\nsntrup761x25519-sha512@openssh.com\\n"',
        )
        self.stub('ssh-keygen', f'echo "ssh-keygen $*" >> {self.calls}')
        self.stub('dconf', 'exit 0')
        script = render(base_os)
        archive = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", script)[1]
        self.script = self.moved(script.replace(archive, 'ARCHIVE')).replace(
            'ARCHIVE', archive
        )

    def moved(self, text):
        # Tokens first, so a root that itself contains a moved directory is not rewritten.
        for index, moved in enumerate(MOVED):
            text = text.replace(moved, f'@MOVED{index}@')
        for index, moved in enumerate(MOVED):
            text = text.replace(f'@MOVED{index}@', f'{self.root}{moved}')
        return text

    def stub(self, name, body):
        (self.bin / name).write_text('#!/bin/sh\n' + body + '\n')
        (self.bin / name).chmod(0o755)

    def run(self):
        before = len(self.calls.read_text().splitlines())
        result = subprocess.run(
            [self.shell],
            input=self.script,
            text=True,
            capture_output=True,
            env={'PATH': str(self.bin), 'TMPDIR': str(self.state)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return self.calls.read_text().splitlines()[before:]

    def files(self):
        return {
            str(path.relative_to(self.root)): (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mode & 0o777,
            )
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }


SERVICE_CHANGES = ('daemon-reload', 'enable', 'restart', 'reload')


def service_changes(calls):
    return [
        call for call in calls if any(word in call.split() for word in SERVICE_CHANGES)
    ]


@pytest.mark.parametrize('base_os', LINUX)
def test_an_old_desktop_gains_trust_once_and_a_second_refresh_changes_nothing(
    tmp_path, base_os
):
    desktop = Desktop(tmp_path, base_os)
    first = desktop.run()
    installed = desktop.files()
    helper = desktop.root / 'usr/local/libexec/idea-terminal-host.py'
    assert helper.read_bytes() == HELPER.read_bytes()
    assert installed['usr/local/libexec/idea-terminal-host.py'][1] == 0o755
    assert installed['etc/systemd/system/idea-terminal-trust.service'][1] == 0o644
    assert installed['usr/lib/systemd/system-sleep/idea-terminal-trust'][1] == 0o755
    assert any(
        call.startswith(('apt-get', 'yum')) and 'install' in call for call in first
    )
    assert 'systemctl daemon-reload' in first
    assert 'systemctl enable idea-terminal-trust.service' in first
    assert 'systemctl --no-block restart idea-terminal-trust.service' in first
    # The refresh never touches sshd_config itself; the helper does, under the site-hook lock.
    assert (desktop.root / 'etc/ssh/sshd_config').read_text() == 'UsePAM yes\n'

    second = desktop.run()
    assert desktop.files() == installed
    assert service_changes(second) == [], second
    assert not any(call.startswith(('apt-get', 'yum')) for call in second)


@pytest.mark.parametrize('base_os', LINUX)
def test_a_desktop_from_this_release_is_left_exactly_as_it_is(tmp_path, base_os):
    # Every desktop gets the refresh once, including one whose bootstrap installed trust.
    desktop = Desktop(tmp_path, base_os)
    bootstrap = (BOOTSTRAP / '_templates/linux/web_terminal.jinja2').read_text()
    for path, end, mode in (
        ('/etc/systemd/system/idea-terminal-trust.service', 'UNIT', 0o644),
        ('/usr/lib/systemd/system-sleep/idea-terminal-trust', 'SLEEP', 0o755),
    ):
        target = desktop.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        # As the refresh sees them: paths in the unit and hook point into the test's root.
        target.write_text(desktop.moved(heredoc(bootstrap, path, end)))
        target.chmod(mode)
    helper = desktop.root / 'usr/local/libexec/idea-terminal-host.py'
    helper.parent.mkdir(parents=True)
    helper.write_bytes(HELPER.read_bytes())
    kex = desktop.root / 'etc/ssh/sshd_config.d/10-idea-kex.conf'
    kex.parent.mkdir()
    kex.write_text(
        'KexAlgorithms mlkem768x25519-sha256,sntrup761x25519-sha512@openssh.com,'
        'curve25519-sha256,ecdh-sha2-nistp256\n'
    )
    for tool in ('tmux', 'python3'):
        desktop.stub(tool, 'exit 0')
    (desktop.state / 'enabled').touch()
    (desktop.state / 'active').touch()
    installed = desktop.files()
    calls = desktop.run()
    assert service_changes(calls) == [], calls
    assert desktop.files() == installed


def test_a_release_candidate_desktop_moves_to_the_current_unit(tmp_path):
    desktop = Desktop(tmp_path, 'rocky9')
    unit = desktop.root / 'etc/systemd/system/idea-terminal-trust.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('[Service]\nType=oneshot\n')
    drop_in = (
        desktop.root / 'etc/systemd/system/sshd.service.d/idea-terminal-trust.conf'
    )
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text('[Unit]\nAfter=idea-terminal-trust.service\n')
    (desktop.state / 'enabled').touch()
    (desktop.state / 'active').touch()
    calls = desktop.run()
    assert 'Type=simple' in unit.read_text()
    assert not drop_in.exists()
    assert 'systemctl daemon-reload' in calls
    assert 'systemctl --no-block restart idea-terminal-trust.service' in calls
    assert service_changes(desktop.run()) == []


def test_a_stopped_trust_unit_is_started_without_rewriting_anything(tmp_path):
    desktop = Desktop(tmp_path, 'ubuntu2404')
    desktop.run()
    installed = desktop.files()
    (desktop.state / 'active').unlink()
    calls = desktop.run()
    assert service_changes(calls) == [
        'systemctl --no-block restart idea-terminal-trust.service'
    ]
    assert desktop.files() == installed


def test_a_failed_install_fails_the_refresh_so_the_sweep_retries(tmp_path):
    desktop = Desktop(tmp_path, 'rhel8')
    desktop.stub('yum', f'echo "yum $*" >> {desktop.calls}; exit 1')
    desktop.stub('sleep', f'echo "sleep $*" >> {desktop.calls}')
    result = subprocess.run(
        [desktop.shell],
        input=desktop.script,
        text=True,
        capture_output=True,
        env={'PATH': str(desktop.bin), 'TMPDIR': str(desktop.state)},
    )
    assert result.returncode == 1
    # three attempts with backoff, then the refresh fails
    assert [
        call
        for call in desktop.calls.read_text().splitlines()
        if call.startswith(('yum', 'sleep'))
    ] == [
        'yum --setopt=timeout=30 install -y tmux python3',
        'sleep 15',
        'yum --setopt=timeout=30 install -y tmux python3',
        'sleep 30',
        'yum --setopt=timeout=30 install -y tmux python3',
    ]
    assert not (
        desktop.root / 'etc/systemd/system/idea-terminal-trust.service'
    ).exists()
    assert os.listdir(desktop.state) == []
