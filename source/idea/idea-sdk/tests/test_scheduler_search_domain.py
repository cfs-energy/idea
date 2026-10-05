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
    block = script[script.index('RESOLVER_IF=') : script.index('echo -e "PBS_SERVER=')]
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
