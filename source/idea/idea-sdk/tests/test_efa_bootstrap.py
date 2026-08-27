"""
EFA bootstrap must fail closed: the job asked for EFA and the instance was launched with an EFA
ENI, so a node whose driver install fails aborts the bootstrap instead of joining over TCP.
"""

import os
import shutil
import stat
import subprocess

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
EFA_TEMPLATE = 'compute-node/_templates/efa.jinja2'

BASH = shutil.which('bash')


def render_efa(config, base_os: str = 'rhel9') -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(EFA_TEMPLATE)
    return template.render(
        context=BootstrapContext(
            config=config,
            module_name='scheduler',
            module_id='scheduler',
            module_set='default',
            base_os=base_os,
            instance_type='c5n.18xlarge',
        )
    )


HARNESS = """
set -o pipefail
function log_info () { echo "[INFO] ${1}"; }
function log_warning () { echo "[WARNING] ${1}"; }
function log_error () { echo "[ERROR] ${1}"; }
function exit_fail () { echo "Failed: ${1}"; exit 1; }
function set_reboot_required () { echo "reboot required: ${1}"; }
"""

# the real bootstrap starts the scheduler after this block - stand in for it, so a node that
# carried on without the driver is distinguishable from one that aborted.
TAIL = 'log_info "scheduler started"\n'

# curl writes the tarball name the template expects, openssl reports the checksum the rendered
# script was given, and tar unpacks a stub installer whose exit code the test controls.
FAKE_BINS = {
    'curl': """#!/bin/bash
for arg in "$@"; do
  case "${arg}" in
    http*) : > "$(basename "${arg}")" ;;
  esac
done
""",
    'openssl': """#!/bin/bash
echo "SHA384(${2})= ${TEST_EFA_HASH}"
""",
    'tar': """#!/bin/bash
mkdir -p aws-efa-installer
printf '#!/bin/bash\\nexit %s\\n' "${TEST_EFA_INSTALLER_EXIT}" > aws-efa-installer/efa_installer.sh
""",
    'yum': """#!/bin/bash
exit 0
""",
    'apt': """#!/bin/bash
exit 0
""",
    'rpm': """#!/bin/bash
exit 1
""",
    'dpkg': """#!/bin/bash
exit 1
""",
}


def build_fake_bin(base_dir: str) -> str:
    bin_dir = os.path.join(base_dir, 'bin')
    os.makedirs(bin_dir, exist_ok=True)
    for name, script in FAKE_BINS.items():
        path = os.path.join(bin_dir, name)
        with open(path, 'w') as f:
            f.write(script)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return bin_dir


def run_efa(tmp_path, config, rendered: str, installer_exit: int):
    base_dir = str(tmp_path)
    bootstrap_dir = os.path.join(base_dir, 'bootstrap')
    os.makedirs(bootstrap_dir, exist_ok=True)
    bin_dir = build_fake_bin(base_dir)

    # the template hardcodes /root/bootstrap, which the test cannot write to.
    rendered = rendered.replace('/root/bootstrap', bootstrap_dir)

    script = os.path.join(base_dir, 'efa.sh')
    with open(script, 'w') as f:
        f.write(f'#!/bin/bash\n{HARNESS}\n{rendered}\n{TAIL}')

    env = dict(os.environ)
    env.update(
        {
            'BOOTSTRAP_DIR': bootstrap_dir,
            'TEST_EFA_HASH': config.get_string(
                'global-settings.package_config.efa.checksum', required=True
            ).lower(),
            'TEST_EFA_INSTALLER_EXIT': str(installer_exit),
            'PATH': f'{bin_dir}:{env["PATH"]}',
        }
    )
    return subprocess.run(
        [BASH, script], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_efa_install_success_does_not_fail_bootstrap(context, tmp_path):
    result = run_efa(tmp_path, context.config(), render_efa(context.config()), 0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EFA Driver installed successfully' in result.stdout
    assert 'scheduler started' in result.stdout


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_failed_efa_install_fails_bootstrap(context, tmp_path):
    result = run_efa(tmp_path, context.config(), render_efa(context.config()), 1)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Failed to install EFA Driver' in result.stdout
    assert 'EFA Driver installation failed' in result.stdout
    # the node must not go on to join the scheduler with no RDMA stack
    assert 'scheduler started' not in result.stdout
