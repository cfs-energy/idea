"""Release gating, scrub ordering and executable failure checks for baked desktops."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from ideasdk.context import BootstrapContext
from ideadatamodel import SocaAnyPayload
from test_bootstrap_shell_syntax import Config, DESKTOP_BASE_OS, IDEA_BOOTSTRAP_DIR

LINUX = [os for os in DESKTOP_BASE_OS if not os.startswith('windows')]
WINDOWS = [os for os in DESKTOP_BASE_OS if os.startswith('windows')]


def render(
    name, base_os='amazonlinux2023', instance_type='m7i.large', lustre=True, drivers=()
):
    context = BootstrapContext(
        config=Config(),
        module_name='virtual-desktop-controller',
        module_id='vdc',
        module_set='default',
        base_os=base_os,
        instance_type=instance_type,
    )
    context.vars.ami_dir = '/apps/example/ami'
    context.vars.ami_name = 'sample-image'
    context.vars.session = SocaAnyPayload(type='console')
    context.vars.bedrock_env = {}
    context.vars.bedrock_model_messages = []
    context.vars.enabled_drivers = drivers
    if lustre:
        context.has_storage_provider = lambda provider: provider == 'fsx_lustre'
    else:
        context.has_storage_provider = lambda provider: False
    return (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template(name)
        .render(context=context)
    )


@pytest.mark.parametrize('base_os', LINUX)
@pytest.mark.parametrize('instance_type', ['m7i.large', 'g5.xlarge', 'g4ad.xlarge'])
def test_linux_bake_checks_and_host_release_guards(base_os, instance_type):
    checks = render(
        'dcv-host-ami-builder/image_checks.sh.jinja2', base_os, instance_type
    )
    for check in ('bootstrap', 'kernel', 'lustre', 'dcv', 'directory', 'ssm'):
        assert f'check {check} ' in checks
    assert ('check gpu ' in checks) == (instance_type != 'm7i.large')
    assert 'modprobe -n lustre' in checks
    assert 'LoadState' in checks
    assert 'systemctl is-enabled --quiet dcv' not in checks
    for name in ('setup', 'configure_dcv_host'):
        host = render(
            f'virtual-desktop-host-linux/{name}.sh.jinja2', base_os, instance_type
        )
        assert (
            '"$(cat /var/lib/idea/baked-release 2>/dev/null)" != "${IDEA_MODULE_VERSION}"'
            in host
        )
        assert 'idea_preinstalled_packages.log' not in host
        assert 'idea_system_upgraded.log' not in host
    setup = render('virtual-desktop-host-linux/setup.sh.jinja2', base_os, instance_type)
    assert setup.index('# Begin: Mount Shared Storage') < setup.index(
        'PACKAGES_INSTALLED=no'
    )
    # Join and host configuration remain outside the package-skip block.
    assert setup.index('Join') > setup.index('skipping package installation')
    assert '/configure_dcv_host.sh' in setup
    scrub = render('dcv-host-ami-builder/image_scrub.sh.jinja2', base_os, instance_type)
    assert 'cloud-init clean --logs --seed' in scrub
    assert 'rm -rf /root/bootstrap/logs' in scrub


def test_lustre_check_is_only_required_when_configured():
    assert 'check lustre ' not in render(
        'dcv-host-ami-builder/image_checks.sh.jinja2', lustre=False
    )


@pytest.mark.parametrize('drivers', [(), ('fsx_lustre',)])
def test_compute_bake_shares_checks_without_dcv(drivers):
    checks = render(
        'compute-node-ami-builder/image_checks.sh.jinja2', 'rocky9', drivers=drivers
    )
    for check in ('bootstrap', 'kernel', 'directory', 'ssm'):
        assert f'check {check} ' in checks
    assert 'check dcv ' not in checks
    assert ('check lustre ' in checks) == bool(drivers)
    assert 'for stage in setup packages drivers' in checks
    assert 'IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR' in checks
    scrub = render('compute-node-ami-builder/image_scrub.sh.jinja2', 'rocky9')
    assert 'dcvserver' not in scrub and ': > /etc/machine-id' in scrub
    post = render(
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'rocky9',
    )
    # only the publish step tags complete, after checks, scrub and the release marker
    assert 'Value=complete' not in post
    order = [
        post.index('image_checks.sh" ||'),
        post.index('image_scrub.sh"'),
        post.index('> /var/lib/idea/baked-release'),
        post.index('image_checks.sh" --publish'),
    ]
    assert order == sorted(order)
    node = Path(IDEA_BOOTSTRAP_DIR, 'compute-node/compute_node.sh.jinja2').read_text()
    assert (
        '"$(cat /var/lib/idea/baked-release 2>/dev/null)" != "${IDEA_MODULE_VERSION}"'
        in node
    )
    assert 'idea_preinstalled_packages.log' not in node


@pytest.mark.parametrize('base_os', WINDOWS)
@pytest.mark.parametrize('instance_type', ['m7i.large', 'g5.xlarge', 'g4ad.xlarge'])
def test_windows_bake_reuses_installer_and_separates_finalize(base_os, instance_type):
    script = render(
        'dcv-host-ami-builder-windows/Setup.ps1.jinja2', base_os, instance_type
    )
    assert 'Install-WindowsEC2Instance -Update -ModuleVersion $Release' in script
    assert 'C:\\ProgramData\\IDEA' in script
    for name in ('dcv', 'ssm', 'bootstrap', 'release'):
        assert f"Add-ImageCheck '{name}'" in script
    assert 'image-checks.json' in script
    assert 'failed:$($Failed.name)' in script
    assert 'if ($Finalize)' in script
    assert 'sysprep --shutdown=true' in script
    assert 'Rename-Computer' not in script
    assert 'Send-SQSMessage' not in script
    assert 'Join-Domain' not in script
    assert ('Install-NvidiaGpuDrivers' in script) == (instance_type == 'g5.xlarge')
    assert ('Install-AMDGpuDrivers' in script) == (instance_type == 'g4ad.xlarge')


STUBS = r"""
aws() { printf '%s\n' "$*" >> "$CALLS"; }
uname() { echo test-kernel; }
grubby() { [[ "$FAIL" != kernel ]] && echo /boot/vmlinuz-test-kernel || echo /boot/vmlinuz-other; }
rpm() {
  [[ "$FAIL" != directory || "$*" != *adcli* ]] &&
  [[ "$FAIL" != dcv || "$*" != *nice-dcv-server* ]] &&
  [[ "$FAIL" != ssm || "$*" != *amazon-ssm-agent* ]]
}
systemctl() { [[ "$*" != *LoadState* ]] || echo loaded; }
modprobe() { [[ "$FAIL" != lustre ]]; }
nvidia-smi() { [[ "$FAIL" != gpu ]]; }
"""


def run_checks(tmp_path, fail='', mode='', base_os='amazonlinux2023'):
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    logs = tmp_path / 'logs'
    logs.mkdir(exist_ok=True)
    (logs / 'bootstrap.log').write_text(
        'ERROR: installation failed\n'
        if fail == 'bootstrap'
        else 'INFO: stages finished\n'
    )
    for stage in ('setup', 'packages', 'dcv'):
        (state / f'bake-{stage}.ok').touch()
    (state / 'baked-release').write_text('test-release\n')
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os, 'g5.xlarge')
    script = script.replace('source /etc/environment', ':').replace(
        '/var/lib/idea', str(state)
    )
    path = tmp_path / 'checks.sh'
    path.write_text(STUBS + script)
    result = subprocess.run(
        ['bash', str(path), mode],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            'CALLS': str(calls),
            'FAIL': fail,
            'IDEA_MODULE_VERSION': 'test-release',
            'AWS_INSTANCE_ID': 'i-test',
            'AWS_REGION': 'us-east-1',
            'BOOTSTRAP_DIR': str(tmp_path),
            'IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR': str(logs),
        },
    )
    return (
        result,
        json.loads((state / 'image-checks.json').read_text()),
        calls.read_text(),
    )


@pytest.mark.parametrize(
    'failed', ['bootstrap', 'kernel', 'lustre', 'dcv', 'directory', 'ssm', 'gpu']
)
def test_failed_check_writes_json_and_never_tags_complete(tmp_path, failed):
    result, report, calls = run_checks(tmp_path, failed)
    assert result.returncode == 1, result.stderr
    assert report['release'] == 'test-release'
    assert [c['name'] for c in report['checks'] if not c['ok']] == [failed]
    assert all(isinstance(c['seconds'], int) for c in report['checks'])
    assert f'Key=idea:AmiBuilderStatus,Value=failed:{failed}' in calls
    assert 'Value=complete' not in calls
    assert not (tmp_path / 'state' / 'baked-release').exists()


def test_success_is_only_published_after_scrub(tmp_path):
    result, report, calls = run_checks(tmp_path)
    assert result.returncode == 0, result.stderr
    assert all(c['ok'] for c in report['checks'])
    assert 'create-tags' not in calls
    result, _, calls = run_checks(tmp_path, mode='--publish')
    assert result.returncode == 0, result.stderr
    assert 'bootstrap_i-test' in calls
    assert 'Value=complete' in calls


def test_stage_error_survives_later_success_and_subshell(tmp_path):
    source = Path(
        IDEA_BOOTSTRAP_DIR, 'dcv-host-ami-builder/image_bake_stage.sh'
    ).read_text()
    source = source.replace('/var/lib/idea', str(tmp_path))
    result = subprocess.run(
        ['bash', '-c', source + '\nBAKE_STAGE=packages; (false; true); true'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert (tmp_path / 'bake-failed').read_text().strip() == 'packages'
    result, report, calls = run_checks(tmp_path)
    # Move the persistent error into the check state and prove it overrides stage OK files.
    (tmp_path / 'state' / 'bake-failed').touch()
    result, report, calls = run_checks(tmp_path)
    assert result.returncode == 1
    assert not report['checks'][0]['ok']
    assert 'Value=failed:bootstrap' in calls


def test_parent_stage_preserves_the_reported_failure(tmp_path):
    report = tmp_path / 'image-checks.json'
    report.write_text(json.dumps({'checks': [{'name': 'kernel', 'ok': False}]}))
    source = Path(
        IDEA_BOOTSTRAP_DIR, 'dcv-host-ami-builder/image_bake_stage.sh'
    ).read_text()
    source = source.replace('/var/lib/idea', str(tmp_path))
    result = subprocess.run(
        ['bash', '-c', source + '\nBAKE_STAGE=packages; SCRIPT_DIR=/missing; exit 1'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert '/missing' not in result.stderr
    assert json.loads(report.read_text())['checks'][0]['name'] == 'kernel'


@pytest.mark.parametrize(
    'marker,expected', [(None, 'full'), ('old', 'full'), ('current', 'skip')]
)
def test_host_package_guard_uses_release_contents(tmp_path, marker, expected):
    import re

    host = render('virtual-desktop-host-linux/setup.sh.jinja2')
    guard = re.search(r'if \[\[.*baked-release.* != .*; then', host).group()
    guard = guard.replace('/var/lib/idea', str(tmp_path))
    if marker is not None:
        (tmp_path / 'baked-release').write_text(marker + '\n')
    result = subprocess.run(
        ['bash', '-c', guard + '\necho full\nelse\necho skip\nfi'],
        env={**os.environ, 'IDEA_MODULE_VERSION': 'current'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    'selection,running,expected',
    [
        ('0', 'first', 0),
        ('0', 'old', 1),
        ('Advanced options>second-id', 'second', 0),
        ('missing-id', 'first', 1),
    ],
)
def test_ubuntu_check_resolves_the_actual_default_grub_entry(
    tmp_path, selection, running, expected
):
    script = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2404')
    function = script[
        script.index('kernel_ok() {') : script.index('packages_installed() {')
    ]
    config = """set default="${next_entry}"
set default="${saved_entry}"
menuentry 'Ubuntu' --id first-id {
    linux /boot/vmlinuz-first root=UUID=example
}
submenu 'Advanced options' --id advanced-id {
    menuentry 'Second kernel' --id second-id {
        linux /boot/vmlinuz-second root=UUID=example
    }
}
"""
    (tmp_path / 'grub.cfg').write_text(config)
    command = tmp_path / 'grub-editenv'
    command.write_text('#!/bin/sh\nprintf "saved_entry=%s\\n" "$GRUB_SELECTION"\n')
    command.chmod(0o755)
    function = function.replace('/boot/grub', str(tmp_path))
    result = subprocess.run(
        [
            'bash',
            '-c',
            'BAKE_PYTHON=$(command -v python3)\nuname() { echo "$RUNNING"; }\n'
            + function
            + '\nkernel_ok',
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
            'GRUB_SELECTION': selection,
            'RUNNING': running,
        },
    )
    assert result.returncode == expected, result.stderr
