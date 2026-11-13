"""
The distribution's newest OpenSSH reaches every image: a bake upgrades it explicitly, since no
other step does on compute images or the dnf family, and an image with an older one fails its
checks. AL2023 repositories are locked to the base image's release, so only --releasever=latest
sees a newer package there, and check-update's exit status is the answer, not its output.
"""

import os
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

from ideasdk.context import BootstrapContext
from test_bootstrap_shell_syntax import BASH, Config, IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import LINUX, render, run_checks

UBUNTU = [base_os for base_os in LINUX if base_os.startswith('ubuntu')]
TEMPLATE = '_templates/linux/openssh_update.jinja2'
BUILDERS = (
    'dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2',
    'compute-node-ami-builder/compute_node_ami_builder.sh.jinja2',
)
UNBAKED = (
    'compute-node/compute_node.sh.jinja2',
    'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
)


@pytest.mark.parametrize('name', BUILDERS + UNBAKED)
def test_every_package_stage_includes_the_update(name):
    with open(os.path.join(IDEA_BOOTSTRAP_DIR, name)) as source:
        assert TEMPLATE in source.read()


@pytest.mark.parametrize('base_os', LINUX)
def test_the_update_names_the_newest_source_for_each_distribution(base_os):
    script = render(TEMPLATE, base_os)
    if base_os in UBUNTU:
        assert 'apt_update' in script
        assert '--only-upgrade' in script and 'openssh-server openssh-client' in script
    elif base_os == 'amazonlinux2023':
        assert "dnf -y upgrade --releasever=latest 'openssh*'" in script
    else:
        assert "dnf -y upgrade 'openssh*'" in script
        assert 'releasever' not in script


def _run_update(tmp_path, required, status):
    variables = '{% set openssh_required = true %}' if required else ''
    env = Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
    template = env.from_string(variables + "{% include '" + TEMPLATE + "' %}")
    script = template.render(
        context=BootstrapContext(
            config=Config(),
            module_name='scheduler',
            module_id='scheduler',
            module_set='default',
            base_os='rocky9',
            instance_type='m7i.large',
        )
    )
    stubs = (
        f'package_transaction() {{ return {status}; }}\n'
        'log_error() { echo "ERROR $*"; }\nlog_warning() { echo "WARNING $*"; }\n'
    )
    return subprocess.run(
        [BASH, '-c', stubs + script + '\necho reached-the-end\n'],
        text=True,
        capture_output=True,
        cwd=tmp_path,
    )


def test_a_bake_that_cannot_update_fails_but_a_booting_host_carries_on(tmp_path):
    assert _run_update(tmp_path, True, 0).returncode == 0
    failed = _run_update(tmp_path, True, 1)
    assert failed.returncode == 1 and 'reached-the-end' not in failed.stdout
    booting = _run_update(tmp_path, False, 1)
    assert booting.returncode == 0 and 'reached-the-end' in booting.stdout
    assert 'WARNING' in booting.stdout


@pytest.mark.parametrize(
    ('fail', 'phrase'),
    [('openssh', 'has a newer package'), ('openssh-repo', 'Could not ask')],
)
def test_a_newer_package_or_a_silent_repository_fails_the_check(tmp_path, fail, phrase):
    result, report, calls = run_checks(tmp_path, fail=fail)
    assert result.returncode == 1
    failing = [check for check in report['checks'] if not check['ok']]
    assert [check['name'] for check in failing] == ['openssh']
    assert phrase in failing[0]['detail']
    assert '--releasever=latest openssh*' in calls


def test_a_current_package_passes_and_rpm_distributions_check_their_own_release(
    tmp_path,
):
    result, report, calls = run_checks(tmp_path, base_os='rocky9')
    assert result.returncode == 0, result.stdout
    assert any(c['name'] == 'openssh' and c['ok'] for c in report['checks'])
    assert 'dnf -q check-update openssh*' in calls


@pytest.mark.parametrize(
    ('policy', 'ok'),
    [
        (
            '  Installed: 1:9.6p1-3ubuntu13.20\n  Candidate: 1:9.6p1-3ubuntu13.20\n',
            True,
        ),
        (
            '  Installed: 1:9.6p1-3ubuntu13.14\n  Candidate: 1:9.6p1-3ubuntu13.20\n',
            False,
        ),
        ('  Installed: (none)\n  Candidate: (none)\n', True),
        ('', False),
    ],
)
def test_ubuntu_compares_installed_with_candidate(tmp_path, policy, ok):
    script = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2404')
    start = script.index('openssh_current() {')
    function = script[start : script.index('ssm_ok() {', start)]
    (tmp_path / 'policy').write_text(policy)
    stub = tmp_path / 'apt-cache'
    stub.write_text(f'#!/bin/bash\ncat {tmp_path}/policy\n')
    stub.chmod(0o755)
    result = subprocess.run(
        [BASH, '-c', function + '\nopenssh_current'],
        text=True,
        capture_output=True,
        env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'},
    )
    assert (result.returncode == 0) is ok, result.stdout
