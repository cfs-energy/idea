"""
Every PBS client names the batch server exactly as the server names itself. qsub completes a
short job id (afterok:70) with the client's PBS_SERVER, and the server releases a dependency
only on the exact id it generated, <n>.<server_name>. A client on any other spelling, such as
the full DNS name, held every such dependent job forever.

The server's name comes from roles/scheduler.sh; each client derivation is read from the
file that ships it, so a change on either side fails here.
"""

import re
import subprocess
from pathlib import Path

import pytest

from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    bootstrap_refresh_variables,
)
from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR, Config
from test_image_bake_bootstrap import render

ROOT = Path(IDEA_BOOTSTRAP_DIR).parents[2]
ROLES = ROOT / 'deployment/ecr/idea-control-plane/roles'
ZONE = 'idea-collab.us-east-2.local'
STABLE_NAME = f'scheduler.{ZONE}'


def server_name(dns_name):
    """SERVER_NAME as roles/scheduler.sh derives it, run from the script's own line"""
    line = re.search(
        r'^SERVER_NAME=.*$', (ROLES / 'scheduler.sh').read_text(), re.M
    ).group(0)
    assert 'PBS_SERVER=${SERVER_NAME}' in (ROLES / 'scheduler.sh').read_text()
    return subprocess.run(
        [BASH, '-c', f'{line}; printf %s "$SERVER_NAME"'],
        env={'IDEA_SCHEDULER_DNS_NAME': dns_name},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def config(stable):
    c = Config()
    c.put('scheduler.use_stable_server_name', stable)
    c.put('scheduler.private_dns_name', STABLE_NAME)
    c.put('cluster.route53.private_hosted_zone_name', ZONE)
    return c


def written(script, pattern):
    return re.findall(pattern, script, re.M)


def test_the_server_is_named_by_the_first_label():
    assert server_name(STABLE_NAME) == 'scheduler'


@pytest.mark.parametrize('stable', [True, False])
@pytest.mark.parametrize(
    'template',
    [
        '_templates/linux/openpbs_client.jinja2',
        'compute-node/_templates/configure_openpbs_compute_node.jinja2',
    ],
)
def test_first_bootstrap_clients_use_the_server_name(template, stable):
    script = render(template, 'rocky9', config=config(stable))
    expected = server_name(STABLE_NAME)
    assert written(script, r'^(?:echo -e ")?PBS_SERVER=(\S+)$') == [expected]
    assert written(script, r'^\\\$clienthost (\S+)$') == [expected]
    # with the stable name the short name resolves through the private zone, not a pin
    assert ('idea-scheduler.conf' in script) == stable


@pytest.mark.parametrize('stable', [True, False])
def test_the_desktop_refresh_uses_the_server_name(stable):
    variables = bootstrap_refresh_variables(config(stable))
    assert variables['pbs_server_name'] == server_name(STABLE_NAME)
    assert variables['private_zone'] == (ZONE if stable else None)


def test_the_refresh_keeps_the_full_name_without_a_private_zone():
    # the short name resolves only through the zone in the search path; without it the
    # refreshed pbs would fail check_hostname and stop the desktop's qsub
    c = Config()
    c.put('scheduler.use_stable_server_name', True)
    c.put('scheduler.private_dns_name', STABLE_NAME)
    variables = bootstrap_refresh_variables(c)
    assert variables['pbs_server_name'] == STABLE_NAME
    assert variables['private_zone'] is None


def test_the_bastion_uses_the_server_name(tmp_path):
    text = (ROLES / 'bastion.sh').read_text()
    start = text.index('  cat > /etc/pbs.conf <<EOF')
    end = text.index('\nEOF\n', start) + len('\nEOF\n')
    pbs_conf = tmp_path / 'pbs.conf'
    subprocess.run(
        [BASH, '-c', text[start:end].replace('/etc/pbs.conf', str(pbs_conf))],
        env={'IDEA_PBS_SERVER': STABLE_NAME},
        check=True,
    )
    assert written(pbs_conf.read_text(), r'^PBS_SERVER=(\S+)$') == [
        server_name(STABLE_NAME)
    ]
