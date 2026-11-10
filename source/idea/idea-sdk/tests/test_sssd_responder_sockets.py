"""
An Ubuntu 24.04 desktop came up `degraded`: sssd-nss.socket, sssd-pam.socket and
sssd-pam-priv.socket failed. Ubuntu enables a socket unit per sssd responder, and each one
refuses to start when sssd.conf names its responder in `services`, as both directory joins do.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR

COMMON = Path(IDEA_BOOTSTRAP_DIR, 'common/bootstrap_common.sh').read_text()
TEMPLATES = Path(IDEA_BOOTSTRAP_DIR, '_templates/linux')


def _run(tmp_path, units, disable_rc=0):
    function = re.search(
        r'function disable_sssd_responder_sockets \(\) \{\n.*?\n\}\n', COMMON, re.S
    )[0]
    calls = tmp_path / 'calls'
    calls.write_text('')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stub = bin_dir / 'systemctl'
    stub.write_text(
        '#!/bin/bash\n'
        f'if [[ "$1" == cat ]]; then [[ " {" ".join(units)} " == *" $2 "* ]]; exit; fi\n'
        f'echo "$*" >> {calls}\n'
        f'[[ "$1" != disable ]] || exit {disable_rc}\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(
        [
            BASH,
            '-euc',
            f'log_warning() {{ echo "warning $*" >> {calls}; }}\n'
            + function
            + 'disable_sssd_responder_sockets nss pam\n',
        ],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text().splitlines()


def test_the_sockets_of_the_responders_sssd_starts_itself_are_disabled(tmp_path):
    # what Ubuntu ships; ssh and sudo are not in `services`, so their sockets stay
    units = [
        'sssd-nss.socket',
        'sssd-pam.socket',
        'sssd-pam-priv.socket',
        'sssd-ssh.socket',
        'sssd-sudo.socket',
    ]
    assert _run(tmp_path, units) == [
        'disable --now sssd-nss.socket',
        'reset-failed sssd-nss.socket',
        'disable --now sssd-pam.socket',
        'reset-failed sssd-pam.socket',
        'disable --now sssd-pam-priv.socket',
        'reset-failed sssd-pam-priv.socket',
    ]


def test_a_host_without_the_socket_units_is_left_alone(tmp_path):
    assert _run(tmp_path, []) == []


def test_a_socket_that_cannot_be_disabled_only_warns(tmp_path):
    calls = _run(tmp_path, ['sssd-nss.socket'], disable_rc=1)
    assert 'warning Could not disable sssd-nss.socket' in calls


@pytest.mark.parametrize('join', ['join_activedirectory.jinja2', 'join_openldap.jinja2'])
def test_each_directory_join_disables_them_for_the_services_it_names(join):
    join = (TEMPLATES / join).read_text()
    services = re.search(r'^services = (.+)$', join, re.M).group(1).replace(',', '')
    call = f'disable_sssd_responder_sockets {services}\n'
    assert join.index('" > /etc/sssd/sssd.conf') < join.index(call)
    # the last restart: nothing after the call starts sssd again with the sockets back
    assert join.index(call) < join.rindex('systemctl restart sssd')
