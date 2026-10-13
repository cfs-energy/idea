"""
A host whose /etc/resolv.conf is a plain file (Ubuntu bootstrap replaces resolved's link with an
immutable copy) still gets the cluster zone in its search path while systemd-resolved runs:
resolvectl never reaches that file, so pbs_init.d could not resolve the short server name and
the compute node's mom never started.
"""

import os
import re
import subprocess

import pytest

from test_bootstrap_shell_syntax import BASH, Config
from test_image_bake_bootstrap import gnu_sed, render

ZONE = 'cluster.example.local'
TEMPLATES = (
    'compute-node/_templates/configure_openpbs_compute_node.jinja2',
    '_templates/linux/openpbs_client.jinja2',
)


def _search_domain_block(script):
    # the compute node applies it before writing pbs.conf, the client after (shared with the refresh)
    start = script.index('if [[ -L /etc/resolv.conf')
    ends = [
        script.find(m, start)
        for m in ('echo -e "PBS_SERVER=', '\nif [[ -f /etc/pbs.conf')
    ]
    return script[start : min(i for i in ends if i > 0)]


def config():
    c = Config()
    c.put('scheduler.use_stable_server_name', True)
    c.put('scheduler.private_dns_name', f'scheduler.{ZONE}')
    c.put('cluster.route53.private_hosted_zone_name', ZONE)
    return c


@pytest.mark.parametrize('template', TEMPLATES)
@pytest.mark.parametrize('search', ['search ec2.internal\n', ''])
def test_a_static_resolv_conf_gets_the_cluster_zone_while_resolved_runs(
    tmp_path, template, search
):
    script = render(template, 'ubuntu2204', config=config())
    block = _search_domain_block(script)
    resolv = tmp_path / 'resolv.conf'
    resolv.write_text('nameserver 10.0.0.2\n' + search)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls'
    stubs = {
        'ip': 'echo "default via 10.0.0.1 dev eth0"',
        'systemctl': 'exit 0',  # systemd-resolved is active
        'resolvectl': f'echo "resolvectl $*" >> {calls}; echo "Link 2 (eth0): ec2.internal"',
        'lsattr': 'echo "----i---------e------- $1"',
        'chattr': f'echo "chattr $*" >> {calls}',
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f'#!/bin/bash\n{body}\n')
        path.chmod(0o755)
    block = block.replace('/etc/resolv.conf', str(resolv))
    if search:
        gnu_sed()
    result = subprocess.run(
        [BASH, '-euo', 'pipefail', '-c', block],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert re.search(rf'^search .*{re.escape(ZONE)}', resolv.read_text(), re.M)
    # the immutable copy is unlocked for the edit and locked again
    assert calls.read_text().splitlines() == [
        f'chattr -i {resolv}',
        f'chattr +i {resolv}',
    ]


def _run_block(tmp_path, stubs, link):
    script = render(TEMPLATES[1], 'amazonlinux2023', config=config())
    block = _search_domain_block(script)
    target = tmp_path / 'stub-resolv.conf'
    target.write_text('nameserver 127.0.0.53\nsearch ec2.internal\n')
    resolv = tmp_path / 'resolv.conf'
    if link:
        resolv.symlink_to(target)
    else:
        resolv.write_text(target.read_text())
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in stubs.items():
        (bin_dir / name).write_text(f'#!/bin/bash\n{body}\n')
        (bin_dir / name).chmod(0o755)
    block = block.replace('/etc/resolv.conf', str(resolv)).replace(
        '/etc/systemd/resolved.conf.d', str(tmp_path / 'resolved.conf.d')
    )
    gnu_sed()
    result = subprocess.run(
        [BASH, '-euo', 'pipefail', '-c', block],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_resolved_hosts_keep_the_zone_across_a_reboot(tmp_path):
    # resolvectl domain is runtime state: the bootstrap reboot dropped it on a commercial desktop
    calls = tmp_path / 'calls'
    _run_block(
        tmp_path,
        {
            'systemctl': f'echo "systemctl $*" >> {calls}',
            'lsattr': 'exit 0',
            'chattr': 'exit 0',
        },
        link=True,
    )
    assert (tmp_path / 'resolved.conf.d' / 'idea-scheduler.conf').read_text() == (
        f'[Resolve]\nDomains={ZONE}\n'
    )
    assert 'systemctl restart systemd-resolved' in calls.read_text()


@pytest.mark.parametrize('device_answers', [True, False])
def test_network_manager_hosts_keep_the_zone_across_a_reboot(tmp_path, device_answers):
    # NetworkManager regenerated resolv.conf at boot on a rocky9 compute node and pbs_mom never started;
    # newer NetworkManager lists lo first among the active connections, which must never be picked
    calls = tmp_path / 'calls'
    device_line = 'echo "System eth0"' if device_answers else ':'
    _run_block(
        tmp_path,
        {
            'systemctl': '[[ "$2" == NetworkManager ]]',  # NetworkManager up, resolved not
            'ip': 'echo "default via 10.0.0.1 dev eth0 proto dhcp"',
            'nmcli': (
                f'echo "nmcli $*" >> {calls}\n'
                f'case "$*" in *"device show eth0"*) {device_line};; '
                '*"connection show --active"*) printf "lo:loopback\\nSystem eth0:ethernet\\n";; esac\ntrue'
            ),
            'lsattr': 'exit 0',
            'chattr': 'exit 0',
        },
        link=False,
    )
    assert (
        f'nmcli connection modify System eth0 +ipv4.dns-search {ZONE}'
        in calls.read_text()
    )
    assert 'modify lo' not in calls.read_text()
