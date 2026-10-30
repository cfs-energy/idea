"""
a Lustre client that cannot be installed stops a host that mounts Lustre with its reason; it
never reboots into the same failure until the reboot-loop guard gives up. a host that only
installs the client because the cluster has Lustre logs the failure and carries on.
"""

import subprocess

import pytest

from ideadatamodel import SocaAnyPayload
from test_bootstrap_shell_syntax import Config, IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render

# the commands the client install reaches, against a stock EL image with no client
STUBS = r"""
sleep() { :; }
nproc() { echo 2; }
free() { echo 'Mem: 16'; }
instance_id() { echo i-0; }
uname() { if [[ "$1" == -r ]]; then echo "$RUNNING_KERNEL"; else echo x86_64; fi; }
rpm() { :; }
curl() { :; }
sudo() { "$@"; }
sed() { if [[ "$1" == -i ]]; then :; else command sed "$@"; fi; }
yum() {
  echo "yum $*" >> "$CALLS"
  [[ "$1" != clean && -n "$FAIL_INSTALL" ]] || return 0
  echo 'Error: GPG check FAILED'
  return 1
}
grubby() { echo "/boot/vmlinuz-${DEFAULT_KERNEL:-$RUNNING_KERNEL}"; }
aws() { echo "aws $*" >> "$CALLS"; }
crontab() { :; }
reboot_after_cloud_init() { echo reboot >> "$CALLS"; }
"""


def lustre_config():
    # mounted by compute nodes of every queue profile, and by no infrastructure host
    config = Config()
    config.put(
        'shared-storage.scratch',
        {
            'provider': 'fsx_lustre',
            'mount_dir': '/scratch-lustre',
            'mount_options': 'lustre defaults,noatime,flock,_netdev 0 0',
            'scope': ['scheduler:queue-profile'],
            'fsx_lustre': {'dns': 'fs.example.invalid', 'mount_name': 'test'},
        },
    )
    return config


def run_install(tmp_path, template, base_os, running, **environment):
    job = SocaAnyPayload(
        job_id='1', job_uid='u', owner='o', job_group='g', job_name='n'
    )
    variables = {}
    if template.startswith('compute-node'):
        variables = {'job': job, 'job_directory': '/data/job'}
    if template.startswith('compute-node/'):
        variables['queue_profile'] = 'normal'
    text = render(
        template,
        base_os,
        drivers=('fsx_lustre',),
        config=lustre_config(),
        variables=variables,
    )
    # through the reboot decision; the mounts after it are not under test
    storage = text.split('# Begin: Mount Shared Storage')[1].split(
        'IDEA_SHARED_MOUNT_DIRS=('
    )[0]
    for path in ('/var/lib/idea/', '/etc/modprobe.d/', '/root/bootstrap/'):
        storage = storage.replace(path, f'{tmp_path}/')
    (tmp_path / 'image_checks.sh').write_text(
        f'echo "image_checks $(head -1 {tmp_path}/bake-failed)" >> "$CALLS"\nexit 1\n'
    )
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = tmp_path / 'run.sh'
    script.write_text(
        f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
        + f'BOOTSTRAP_DIR="{tmp_path}"\n'
        + f'SCRIPT_DIR="{tmp_path}"\n'
        + STUBS
        + storage
        # reaching here means the install neither stopped nor rebooted
        + '\necho fell-through >> "$CALLS"\n'
    )
    result = subprocess.run(
        ['bash', str(script)],
        env={
            'PATH': '/usr/bin:/bin',
            'CALLS': str(calls),
            'RUNNING_KERNEL': running,
            **environment,
        },
        capture_output=True,
        text=True,
    )
    history = tmp_path / 'reboot_history.log'
    return (
        result.returncode,
        result.stdout,
        calls.read_text(),
        history.read_text() if history.exists() else '',
    )


@pytest.mark.parametrize(
    'template, base_os, running',
    [
        (
            'compute-node-ami-builder/setup.sh.jinja2',
            'rocky10',
            '6.12.0-211.el10.x86_64',
        ),
        ('compute-node-ami-builder/setup.sh.jinja2', 'rhel9', '5.14.0-570.el9.x86_64'),
        ('compute-node/setup.sh.jinja2', 'rocky10', '6.12.0-124.el10.x86_64'),
        ('compute-node/setup.sh.jinja2', 'rhel8', '4.18.0-553.el8.x86_64'),
    ],
)
def test_a_failed_client_install_stops_with_its_reason(
    tmp_path, template, base_os, running
):
    code, out, calls, history = run_install(
        tmp_path, template, base_os, running, FAIL_INSTALL='1'
    )
    assert code == 1
    assert 'reboot' not in calls and 'fell-through' not in calls
    assert 'FSx for Lustre client installed' not in out + history
    assert (tmp_path / 'reboot_required.txt').read_text() == 'no'
    errors = [line for line in out.splitlines() if 'reason code:' in line]
    assert len(errors) == 1 and '[ERROR]' in errors[0]
    assert '(reason code: lustre-client-install-failed)' in errors[0]
    # a compute bake records the reason where its in-bake checks report it, then tags
    bake = template.startswith('compute-node-ami-builder/')
    assert ('image_checks lustre: Unable to install' in calls) == bake


@pytest.mark.parametrize(
    'template',
    ['compute-node-ami-builder/setup.sh.jinja2', 'compute-node/setup.sh.jinja2'],
)
def test_an_installed_client_still_reboots_once(tmp_path, template):
    code, out, calls, history = run_install(
        tmp_path, template, 'rocky10', '6.12.0-211.el10.x86_64'
    )
    assert code == 0
    assert calls.splitlines()[-1] == 'reboot'
    assert 'FSx for Lustre client installed' in history


def test_an_unsupported_kernel_reboots_only_into_a_new_kernel(tmp_path):
    template = 'compute-node/setup.sh.jinja2'
    # the image boots a kernel the FSx repository has no client for: terminal
    code, out, calls, _ = run_install(
        tmp_path, template, 'rocky10', '6.12.0-55.el10.x86_64'
    )
    assert code == 1 and 'reboot' not in calls
    assert '(reason code: lustre-module-missing)' in out
    # a kernel was just installed as the boot default: the reboot can help
    code, out, calls, history = run_install(
        tmp_path,
        template,
        'rhel9',
        '5.14.0-284.el9.x86_64',
        DEFAULT_KERNEL='5.14.0-687.el9.x86_64',
    )
    assert code == 0 and calls.splitlines()[-1] == 'reboot'
    assert 'Kernel installed for FSx for Lustre client' in history


def test_a_compute_node_reports_why_it_will_not_join(tmp_path):
    code, out, _, _ = run_install(
        tmp_path,
        'compute-node/setup.sh.jinja2',
        'rocky10',
        '6.12.0-211.el10.x86_64',
        FAIL_INSTALL='1',
    )
    assert code == 1
    assert '(reason code: lustre-client-install-failed)' in out
    assert 'Aborting bootstrap, this node will not join the cluster.' in out


def test_optional_lustre_failure_preserves_an_earlier_reboot(tmp_path):
    text = render(
        '_templates/linux/fsx_lustre_client.jinja2', 'rocky10', config=lustre_config()
    )
    body = text.split('function fail_lustre_client () {', 1)[1].split('\n}', 1)[0]
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
            f'BOOTSTRAP_DIR="{tmp_path}"\nLUSTRE_REQUIRED=no\n'
            'set_reboot_required "Earlier kernel update"\n'
            'function fail_lustre_client () {' + body + '\n}\n'
            'fail_lustre_client missing "Optional client missing"\n',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'reboot_required.txt').read_text() == 'yes'
