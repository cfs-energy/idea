"""
CVE-2026-98432 interim hardening on every Linux host IDEA bootstraps: no MPTCP, no request
migration, and an nftables table dropping IPv4 TCP packets with IP options, behind a flag.
The block runs at first bootstrap and in the desktop refresh, so a re-run must change nothing.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
TEMPLATE = '_templates/linux/kernel_net_hardening.jinja2'
FLAG_KEY = 'global-settings.kernel_hardening.drop_tcp_ip_options'
LINUX = (
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
    'ubuntu2604',
)
RULE = 'ip hdrlength > 5 meta l4proto tcp counter drop'


def render(config, base_os: str) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(TEMPLATE).render(
        context=BootstrapContext(
            config=config,
            module_name='vdc',
            module_id='vdc',
            module_set='default',
            base_os=base_os,
            instance_type='m6a.large',
        )
    )


@pytest.mark.parametrize(
    'script',
    [
        'virtual-desktop-host-linux/setup.sh.jinja2',
        'compute-node/setup.sh.jinja2',
        'openldap-server/setup.sh.jinja2',
    ],
)
def test_every_linux_host_bootstrap_includes_the_hardening(script):
    assert (
        f"{{% include '{TEMPLATE}' %}}" in Path(IDEA_BOOTSTRAP_DIR, script).read_text()
    )


@pytest.mark.parametrize('base_os', LINUX)
def test_sysctls_and_drop_rule_render_by_default(context, base_os):
    rendered = render(context.config(), base_os)
    assert 'net.mptcp.enabled = 0' in rendered
    assert 'net.ipv4.tcp_migrate_req = 0' in rendered
    assert 'type filter hook prerouting priority -300; policy accept;' in rendered
    assert RULE in rendered
    assert 'systemctl restart idea-tcp-ip-options.service' in rendered


def test_flag_off_removes_the_table_and_keeps_the_sysctls(context):
    config = context.config()
    config.put(FLAG_KEY, False)
    try:
        rendered = render(config, 'rocky9')
    finally:
        config.put(FLAG_KEY, True)
    assert 'net.mptcp.enabled = 0' in rendered
    assert RULE not in rendered
    assert 'nft delete table inet idea_tcp_ip_options' in rendered
    assert 'systemctl disable --now idea-tcp-ip-options.service' in rendered


def test_windows_renders_nothing(context):
    assert 'sysctl' not in render(context.config(), 'windows2022')


class Host:
    """the rendered block run against a temporary root with stub sysctl, nft and systemctl"""

    def __init__(self, tmp_path: Path, shell: str):
        self.root = tmp_path / 'root'
        self.bin = tmp_path / 'bin'
        self.calls = tmp_path / 'calls'
        self.state = tmp_path / 'state'
        for path in (self.root, self.bin, self.state):
            path.mkdir()
        self.calls.write_text('')
        self.shell = shell
        for name in 'cat mkdir dirname printf rm touch'.split():
            (self.bin / name).symlink_to(shutil.which(name))
        log = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        (self.bin / 'basename').symlink_to(shutil.which('basename'))
        self.stub('sysctl', log)
        # list succeeds once the unit has loaded the table
        self.stub('nft', log + f'[ "$1" != list ] || [ -f {self.state}/loaded ]')
        self.stub(
            'systemctl',
            log + 'case "$1" in\n'
            f'  is-enabled) [ -f {self.state}/enabled ] ;;\n'
            f'  enable) touch {self.state}/enabled ;;\n'
            f'  restart) touch {self.state}/loaded ;;\n'
            'esac',
        )

    def stub(self, name, body):
        (self.bin / name).write_text('#!/bin/sh\n' + body + '\n')
        (self.bin / name).chmod(0o755)

    def run(self, block: str):
        before = len(self.calls.read_text().splitlines())
        script = 'log_warning() { echo "$@"; }\n' + block.replace(
            '/etc/', f'{self.root}/etc/'
        )
        result = subprocess.run(
            [self.shell],
            input=script,
            text=True,
            capture_output=True,
            env={'PATH': str(self.bin)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return self.calls.read_text().splitlines()[before:]


SHELLS = [s for s in ('bash', 'dash') if shutil.which(s)]


@pytest.mark.parametrize('shell', SHELLS)
def test_first_run_installs_and_a_rerun_changes_nothing(context, tmp_path, shell):
    host = Host(tmp_path, shutil.which(shell))
    block = render(context.config(), 'ubuntu2404')
    first = host.run(block)
    sysctl = host.root / 'etc/sysctl.d/90-idea-net-hardening.conf'
    assert 'net.mptcp.enabled = 0\nnet.ipv4.tcp_migrate_req = 0\n' in sysctl.read_text()
    assert f'sysctl -q -e -p {sysctl}' in first
    assert RULE in (host.root / 'etc/idea/idea-tcp-ip-options.nft').read_text()
    unit = (host.root / 'etc/systemd/system/idea-tcp-ip-options.service').read_text()
    assert 'After=nftables.service' in unit
    assert 'systemctl daemon-reload' in first
    assert 'systemctl enable idea-tcp-ip-options.service' in first
    assert 'systemctl restart idea-tcp-ip-options.service' in first

    files = {p: p.read_text() for p in host.root.rglob('*') if p.is_file()}
    again = host.run(block)
    assert [
        c for c in again if c.split()[1] in ('daemon-reload', 'enable', 'restart')
    ] == []
    assert {p: p.read_text() for p in host.root.rglob('*') if p.is_file()} == files

    # an nftables.service restart dropped the table: the next run loads it again
    (host.state / 'loaded').unlink()
    assert 'systemctl restart idea-tcp-ip-options.service' in host.run(block)


@pytest.mark.parametrize('shell', SHELLS)
def test_turning_the_flag_off_removes_the_rule(context, tmp_path, shell):
    host = Host(tmp_path, shutil.which(shell))
    config = context.config()
    host.run(render(config, 'rocky9'))
    config.put(FLAG_KEY, False)
    try:
        calls = host.run(render(config, 'rocky9'))
    finally:
        config.put(FLAG_KEY, True)
    assert 'systemctl disable --now idea-tcp-ip-options.service' in calls
    assert 'nft delete table inet idea_tcp_ip_options' in calls
    assert not (host.root / 'etc/systemd/system/idea-tcp-ip-options.service').exists()
    assert not (host.root / 'etc/idea/idea-tcp-ip-options.nft').exists()
    assert (host.root / 'etc/sysctl.d/90-idea-net-hardening.conf').exists()
