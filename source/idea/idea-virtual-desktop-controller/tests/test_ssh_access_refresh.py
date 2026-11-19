"""Running desktops take the SSH access rule from the refresh, once, without touching sshd."""

import shutil
import subprocess
from pathlib import Path

import pytest

from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    BOOTSTRAP_REFRESH_LINUX_BASE_OS,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    BOOTSTRAP_REFRESH_TEMPLATES,
    BOOTSTRAP_REFRESH_VERSION,
    render_bootstrap_refresh,
)

BOOTSTRAP = Path(__file__).resolve().parents[2] / 'idea-bootstrap'
VARIABLES = {'pbs_server_name': None, 'private_zone': None, 'bootstrap_refresh': True}
RULE = 'account    requisite    pam_exec.so quiet '
PAM_SSHD = (
    '#%PAM-1.0\nauth       substack     password-auth\n'
    'account    required     pam_nologin.so\naccount    include      password-auth\n'
    'session    include      password-auth\n'
)


def ssh_block(base_os, owner):
    script = render_bootstrap_refresh(
        str(BOOTSTRAP), base_os, {**VARIABLES, 'ssh_allowed_users': [owner]}
    )
    start = script.index('# Begin: SSH access')
    return script[start : script.index('# End: SSH access', start)]


def run(block, root):
    for path in ('/etc/idea', '/etc/pam.d', '/usr/local/libexec'):
        block = block.replace(path, f'{root}{path}')
    subprocess.run(
        [shutil.which('bash'), '-euc', 'log_info() { :; }\n' + block],
        check=True,
        capture_output=True,
    )


def test_the_refresh_carries_the_ssh_access_rule():
    assert BOOTSTRAP_REFRESH_VERSION >= 9
    assert '_templates/linux/ssh_access.jinja2' in BOOTSTRAP_REFRESH_TEMPLATES


@pytest.mark.parametrize('base_os', sorted(BOOTSTRAP_REFRESH_LINUX_BASE_OS))
def test_a_running_desktop_gets_the_rule_once_for_its_owner(tmp_path, base_os):
    block = ssh_block(base_os, 'alice')
    # PAM reads the stack at each login: no sshd restart, so open sessions stay
    assert 'systemctl' not in block and 'sshd -t' not in block
    (tmp_path / 'etc/pam.d').mkdir(parents=True)
    pam = tmp_path / 'etc/pam.d/sshd'
    pam.write_text(PAM_SSHD)
    run(block, tmp_path)
    once = pam.read_text()
    run(block, tmp_path)
    assert pam.read_text() == once
    assert once.count(RULE) == 1
    assert once.index(RULE) < once.index('account    required     pam_nologin.so')
    assert (tmp_path / 'etc/idea/ssh-allowed-users').read_text() == 'alice\n'
