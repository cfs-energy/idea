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
    name,
    base_os='amazonlinux2023',
    instance_type='m7i.large',
    lustre=True,
    drivers=(),
    config=None,
    variables=None,
):
    context = BootstrapContext(
        config=config or Config(),
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
    for key, value in (variables or {}).items():
        setattr(context.vars, key, value)
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
    assert 'cloud-init.disabled' not in scrub
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
ldd() {
  if [[ "$FAIL" == dcv-libs ]]; then
    echo "  libgtk-3.so.0 => not found"
    return 0
  fi
  echo "    libc.so.6 => /lib64/libc.so.6 (0x1)"
}
"""


def run_checks(
    tmp_path,
    fail='',
    mode='',
    base_os='amazonlinux2023',
    stages=('setup', 'packages', 'dcv'),
):
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    logs = tmp_path / 'logs'
    logs.mkdir(exist_ok=True)
    (logs / 'bootstrap.log').write_text(
        'ERROR: installation failed\n'
        if fail == 'bootstrap'
        else 'INFO: stages finished\n'
    )
    for stage in stages:
        (state / f'bake-{stage}.ok').touch()
    (state / 'baked-release').write_text('test-release\n')
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os, 'g5.xlarge')
    script = script.replace('source /etc/environment', ':').replace(
        '/var/lib/idea', str(state)
    )
    qstat = tmp_path / 'pbs' / 'qstat'
    if fail != 'scheduler':
        qstat.parent.mkdir(exist_ok=True)
        qstat.write_text('#!/bin/bash\n')
        qstat.chmod(0o755)
    script = script.replace('/opt/pbs/bin/qstat', str(qstat))
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
    'failed',
    ['bootstrap', 'kernel', 'lustre', 'dcv', 'directory', 'scheduler', 'ssm', 'gpu'],
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
    # the record names the stage, the command and its status
    assert (tmp_path / 'bake-failed').read_text().strip() == 'packages: false (exit 1)'
    result, report, calls = run_checks(tmp_path)
    # Move the persistent error into the check state and prove it overrides stage OK files.
    (tmp_path / 'state' / 'bake-failed').write_text(
        'setup: source "$HOME/.cargo/env" (exit 1)\nsetup: make rpm (exit 2)\n'
    )
    result, report, calls = run_checks(tmp_path)
    assert result.returncode == 1
    assert not report['checks'][0]['ok']
    # the first failure is the reason the page shows
    assert (
        report['checks'][0]['detail']
        == 'a command failed: setup: source "$HOME/.cargo/env" (exit 1)'
    )
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


def run_kernel_check(tmp_path, grub_cfg, running, saved_entry=''):
    """kernel_ok from the Ubuntu checks against a grub.cfg; (returncode, stdout, stderr)"""
    script = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2204')
    function = script[
        script.index('kernel_ok() {') : script.index('packages_installed() {')
    ]
    (tmp_path / 'grub.cfg').write_text(grub_cfg)
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
            'GRUB_SELECTION': saved_entry,
            'RUNNING': running,
        },
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def test_ubuntu_check_reads_the_vendor_grub_layout(tmp_path):
    # the stock image's grub.cfg: tab-separated "linux<TAB>/boot/vmlinuz-..." and set default="0"
    grub_cfg = Path(__file__).with_name('fixtures').joinpath('ubuntu2204-grub.cfg')
    config = grub_cfg.read_text()
    assert 'linux\t/boot/vmlinuz-' in config
    assert run_kernel_check(tmp_path, config, '6.8.0-1066-aws') == (0, '', '')
    code, out, _ = run_kernel_check(tmp_path, config, '6.8.0-1000-aws')
    assert code == 1
    assert out == 'running 6.8.0-1000-aws, default boot entry is 6.8.0-1066-aws'


@pytest.mark.parametrize(
    'log,expected',
    [
        # package and file names, make's ignored errors and rustup's warning are not failures
        (
            'Installing : perl-Error-1:0.17030-2.noarch\n'
            '-rw-r--r-- root/root 1661 src/error.rs\n'
            'make[1]: [Makefile:272: libhogweed.so] Error 1 (ignored)\n'
            'warn: continuing (because the -y flag is set and the error is ignorable)\n'
            'Failed to enable unit: Unit file chrony.service does not exist.\n',
            None,
        ),
        (
            'Dependencies resolved.\nError: Transaction test error:\n',
            'install.log:2:Error: Transaction test error:',
        ),
        (
            "dpkg: error: failed to write status database stanza about 'x'\n",
            "install.log:1:dpkg: error: failed to write status database stanza about 'x'",
        ),
        (
            'E: Write error - write (28: No space left on device)\n',
            'install.log:1:E: Write error - write (28: No space left on device)',
        ),
        (
            'make: *** [Makefile:66: rpm-only] Error 1\n',
            'install.log:1:make: *** [Makefile:66: rpm-only] Error 1',
        ),
        (
            './x.sh: line 3: remove_from_fstab: command not found\n',
            'install.log:1:./x.sh: line 3: remove_from_fstab: command not found',
        ),
        (
            '[2026-10-02 18:41:16,865] [ERROR] mount failed\n',
            'install.log:1:[2026-10-02 18:41:16,865] [ERROR] mount failed',
        ),
        # xtrace lines quote the literals a template may print later
        ("+ echo 'Error: never printed'\n", None),
    ],
)
def test_bootstrap_log_scan_matches_fatal_lines_only(tmp_path, log, expected):
    state = tmp_path / 'state'
    logs = tmp_path / 'logs'
    state.mkdir()
    logs.mkdir()
    # run_checks writes a clean bootstrap.log of its own; the scan covers every *.log
    (logs / 'install.log').write_text(log)
    result, report, _ = run_checks(tmp_path)
    bootstrap = report['checks'][0]
    assert bootstrap['name'] == 'bootstrap'
    if expected is None:
        assert bootstrap['ok'], bootstrap['detail']
        assert result.returncode == 0
    else:
        assert not bootstrap['ok']
        assert bootstrap['detail'] == expected
        assert result.returncode == 1


@pytest.mark.parametrize(
    'failed,detail',
    [
        ('kernel', 'running test-kernel, default boot entry is other'),
        ('dcv', 'package nice-dcv-server is not installed'),
        ('directory', 'package adcli is not installed'),
        ('ssm', 'package amazon-ssm-agent is not installed'),
        ('scheduler', 'OpenPBS was not installed'),
    ],
)
def test_a_failed_check_reports_what_it_found(tmp_path, failed, detail):
    _, report, _ = run_checks(tmp_path, failed)
    assert {c['name']: c['detail'] for c in report['checks']}[failed] == detail
    # passing checks keep their description
    assert {c['name']: c['detail'] for c in report['checks']}['desktop'] == (
        'GNOME display manager is installed'
    )


def test_dcv_check_fails_when_a_library_is_missing(tmp_path):
    """package presence is not enough: a %post 127 left libgtk unresolved and the check said ok"""
    result, report, _ = run_checks(tmp_path, 'dcv-libs')
    assert result.returncode == 1
    dcv = next(check for check in report['checks'] if check['name'] == 'dcv')
    assert dcv['ok'] is False
    assert 'not found' in dcv['detail']
    assert 'Value=complete' not in _


def test_a_missing_stage_marker_names_the_stage(tmp_path):
    _, report, _ = run_checks(tmp_path, stages=('setup', 'packages'))
    assert report['checks'][0]['detail'] == 'stage dcv did not finish'


def test_templates_tolerate_the_builder_environment():
    # user data has no HOME; rustup and the efs-utils spec rely on it
    efs = render('_templates/linux/efs_mount_helper.jinja2', 'rhel9')
    assert 'export HOME="${HOME:-/root}"' in efs
    assert efs.index('export HOME=') < efs.index('curl -sSf https://sh.rustup.rs')
    # files that only RHEL ships, and a cron file AL2023 may not have, are not failures
    dcv = render('_templates/linux/dcv_server.jinja2', 'rocky9')
    for name in (
        '/etc/xdg/autostart/org.gnome.SettingsDaemon.Subscription.desktop',
        '/lib/systemd/user/org.gnome.SettingsDaemon.Subscription.service',
    ):
        assert f'[[ ! -f {name} ]] || sed -i' in dcv
    assert 'rm -f /etc/cron.d/update-motd' in render(
        '_templates/linux/disable_motd_update.jinja2', 'amazonlinux2023'
    )
    # the compute post-reboot scrub calls remove_from_fstab from the common library
    common = Path(IDEA_BOOTSTRAP_DIR, 'common/bootstrap_common.sh').read_text()
    assert 'function remove_from_fstab' in common
    # the EL package names are not asked of apt
    pbs = render('_templates/linux/openpbs.jinja2', 'ubuntu2204')
    assert 'apt install -y $(echo ${OPENPBS_PKGS[*]})' not in pbs
    assert 'OPENPBS_PKGS_DEB="' in pbs
    assert '[[ -z "${OPENPBS_PKGS_DEB}" ]] || apt install -y ${OPENPBS_PKGS_DEB}' in pbs
    for base_os in LINUX:
        checks = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os)
        unit = 'gdm3' if base_os.startswith('ubuntu') else 'gdm'
        assert 'check desktop ' in checks and f'unit_installed {unit}.service' in checks
    assert 'check desktop ' not in render(
        'compute-node-ami-builder/image_checks.sh.jinja2', 'rocky9'
    )
    windows = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', 'windows2022')
    assert 'throw "Could not remove $Path"' in windows
    assert 'foreach ($Attempt in 1..5)' in windows


@pytest.mark.parametrize('has_dbus_dir', [False, True])
def test_scrub_machine_id_works_with_and_without_var_lib_dbus(tmp_path, has_dbus_dir):
    """dbus-broker images (EL9 and later, AL2023) have no /var/lib/dbus"""
    scrub = render('compute-node-ami-builder/image_scrub.sh.jinja2', 'rocky9')
    start = scrub.index(': > /etc/machine-id')
    block = scrub[start : scrub.index('rm -f /etc/ssh/ssh_host_*')]
    root = tmp_path / 'root'
    (root / 'etc').mkdir(parents=True)
    (root / 'etc/machine-id').write_text('abc\n')
    if has_dbus_dir:
        (root / 'var/lib/dbus').mkdir(parents=True)
    block = block.replace('/etc/', f'{root}/etc/').replace(
        '/var/lib/dbus', f'{root}/var/lib/dbus'
    )
    result = subprocess.run(['bash', '-ec', block], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / 'etc/machine-id').read_text() == ''
    link = root / 'var/lib/dbus/machine-id'
    assert link.is_symlink() == has_dbus_dir


def test_a_failed_scrub_names_the_command(tmp_path):
    scrub = render('compute-node-ami-builder/image_scrub.sh.jinja2', 'rocky9')
    trap = next(line for line in scrub.splitlines() if line.startswith('trap '))
    state = tmp_path / 'bake-failed'
    script = (
        trap.replace('/var/lib/idea/bake-failed', str(state))
        + '\nset -e\nfalse --scrub-step\n'
    )
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode != 0
    assert state.read_text().strip() == 'scrub: false --scrub-step (exit 1)'


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
def test_ubuntu_desktop_install_survives_a_snap_store_error(tmp_path, base_os):
    """the firefox deb's preinst installs the snap once; a store 408 failed the bake"""
    dcv = render('_templates/linux/dcv_server.jinja2', base_os)
    apt = dcv.index('apt install -y ubuntu-desktop-minimal')
    loop = dcv[dcv.rindex('for attempt in', 0, apt) : dcv.rindex('done', 0, apt) + 4]
    calls = tmp_path / 'calls'
    stubs = (
        f'snap() {{ echo "$*" >> {calls}; [[ $(wc -l < {calls}) -ge 3 ]]; }}\n'
        'sleep() { :; }\nlog_warning() { :; }\nset -e\n'
    )
    result = subprocess.run(
        ['bash', '-c', stubs + loop], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ['install firefox'] * 3


@pytest.mark.parametrize('base_os', WINDOWS)
def test_windows_log_is_read_before_ec2launch_v2_replaces_v1(base_os):
    """the v2 installer removes the v1 Launch folder that holds the installer log (2019)"""
    import re

    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', base_os)
    assert setup.index('Select-String -Path $LogFile') < setup.index(
        'AmazonEC2Launch.msi'
    )
    assert "Add-ImageCheck 'bootstrap' ($Installed -and $CleanLog) $LogDetail" in setup
    pattern = re.search(r"-CaseSensitive -Pattern '([^']+)'", setup).group(1)
    assert re.search(pattern, '2026-10-02 22:00:01 ERROR: DCV install failed')
    assert re.search(pattern, '2026-10-02 22:00:01 FATAL: no network')
    for line in (
        '2026-10-02 22:00:01 INFO: retrying after error 3010',
        '2026-10-02 22:00:01 INFO: Set-Service -ErrorAction Stop',
        '2026-10-02 22:00:01 WARNING: Error : {}',
    ):
        assert not re.search(pattern, line), line


def test_desktop_bake_installs_openpbs_and_checks_it():
    """a desktop from the image otherwise compiles OpenPBS on first boot (minutes past the gate)"""
    post = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2', 'rocky9'
    )
    assert 'install_openpbs_' in post
    assert post.index('install_openpbs_') < post.index('image_checks.sh')
    assert 'systemctl start pbs' not in post  # the host configures and starts it
    checks = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'rocky9')
    assert "check scheduler 'OpenPBS is installed' pbs_ok" in checks


@pytest.mark.parametrize('installed', [True, False])
@pytest.mark.parametrize('base_os', ['rocky9', 'rocky8'])
def test_openpbs_dependencies_install_only_with_openpbs(tmp_path, installed, base_os):
    """a baked host (OpenPBS present) must not spend first-boot time on build dependencies"""
    config = Config()
    version = config.get_string('global-settings.package_config.openpbs.version')
    commit = config.get_string(
        'global-settings.package_config.openpbs.commit', default=''
    )
    script = render('_templates/linux/openpbs_client.jinja2', base_os)
    script = script[: script.index('# End: Install OpenPBS')]
    pbs = tmp_path / 'opt/pbs'
    script = script.replace('/opt/pbs', str(pbs))
    if installed:
        (pbs / 'bin').mkdir(parents=True)
        (pbs / 'bin/qstat').write_text(f'#!/bin/bash\necho "pbs_version = {version}"\n')
        (pbs / 'bin/qstat').chmod(0o755)
        (pbs / '.idea_openpbs_commit').write_text(commit)
    calls = tmp_path / 'calls'
    stubs = ''.join(
        f'{cmd}() {{ echo "{cmd} $*" >> {calls}; return 1; }}\n'
        for cmd in ('yum', 'apt', 'git', 'wget', 'pushd', 'popd', 'mkdir', 'log_info')
    )
    stubs = stubs.replace(
        f'log_info() {{ echo "log_info $*" >> {calls}; return 1; }}',
        'log_info() { :; }',
    )
    subprocess.run(['bash', '-c', stubs + script], capture_output=True, text=True)
    called = calls.read_text().split('\n') if calls.exists() else []
    assert any(c.startswith('yum ') for c in called) is (not installed), called


def test_ad_authorization_is_polled_quickly_at_first(tmp_path):
    """the first authorization poll used to sleep 8-40 s even when the agent answered in 2 s"""
    # the loop has no template expressions; the mock cluster has no directory settings
    script = (
        Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/join_activedirectory.jinja2'
    ).read_text()
    start = script.index('function ad_automation_wait_for_authorization_and_join')
    loop = script[start : script.index('local AUTHORIZATION_STATUS', start)] + '\n}\n'
    sleeps = tmp_path / 'sleeps'
    stubs = (
        'log_info() { :; }\n'
        f'sleep() {{ echo "$1" >> {sleeps}; }}\n'
        # called in a subshell, so the count lives in the sleeps file
        'ad_automation_get_authorization() {\n'
        f'  [[ $(cat {sleeps} 2>/dev/null | wc -l) -ge 3 ]] && echo \'{{"status":"success"}}\'\n'
        '  return 0\n}\n'
    )
    result = subprocess.run(
        ['bash', '-c', stubs + loop + 'ad_automation_wait_for_authorization_and_join'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert sleeps.read_text().split() == ['3', '3', '3']


def gnu_sed():
    # the scrub runs on Linux hosts; BSD sed reads -i differently
    probe = subprocess.run(['sed', '--version'], capture_output=True, text=True)
    if probe.returncode != 0 or 'GNU' not in probe.stdout:
        pytest.skip('GNU sed is required to run the scrub block')


AL2023_FSTAB = """#
UUID=331f3ba8-b141-4edf-86a1-9b9e293a44cf     /           xfs    defaults,noatime  1   1
UUID=9C8A-6014        /boot/efi       vfat    defaults,noatime,uid=0,gid=0,umask=0077,shortname=winnt,x-systemd.automount 0 2
fs-1.efs.example.invalid:/ APPS/ nfs4 nfsvers=4.1 0 0
fs-2.efs.example.invalid:/\tDATA\tnfs4 nfsvers=4.1 0 0
fs-3.efs.example.invalid:/ APPS-archive nfs4 nfsvers=4.1 0 0
"""


@pytest.mark.parametrize(
    'template',
    [
        'dcv-host-ami-builder/image_scrub.sh.jinja2',
        'compute-node-ami-builder/image_scrub.sh.jinja2',
    ],
)
def test_scrub_removes_only_shared_storage_mounts_from_fstab(tmp_path, template):
    """
    shared-storage also carries plain settings; their empty mount_dir rendered a pattern
    that deleted the root entry and every line with two spaces, and AL2023 images booted
    with a read-only root (cloud-init, SSM and the bootstrap never ran)
    """
    gnu_sed()
    config = Config()
    storage = config.get_config('shared-storage')
    storage['deployment_id'] = 'sample'
    storage['module_id'] = 'shared-storage'
    scrub = render(template, 'amazonlinux2023', config=config)
    block = scrub[scrub.index('# Host setup recreates') : scrub.index('(crontab -l')]
    fstab = tmp_path / 'fstab'
    table = AL2023_FSTAB.replace('APPS', storage['apps']['mount_dir'])
    table = table.replace('DATA', storage['data']['mount_dir'])
    fstab.write_text(table)
    result = subprocess.run(
        ['bash', '-ec', block.replace('/etc/fstab', str(fstab))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    kept = fstab.read_text().splitlines()
    assert kept == [table.splitlines()[i] for i in (0, 1, 2, 5)]


def test_scrub_refuses_an_fstab_without_a_root_entry(tmp_path):
    gnu_sed()
    scrub = render('dcv-host-ami-builder/image_scrub.sh.jinja2', 'amazonlinux2023')
    block = scrub[scrub.index('# Host setup recreates') : scrub.index('(crontab -l')]
    fstab = tmp_path / 'fstab'
    fstab.write_text('#\nfs-1.efs.example.invalid:/ /apps nfs4 defaults 0 0\n')
    result = subprocess.run(
        ['bash', '-ec', block.replace('/etc/fstab', str(fstab))],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_windows_bootstrap_log_is_written_as_utf8():
    """
    Out-File writes UTF-16 on PowerShell 5.1: the bootstrap stream showed NUL-separated
    characters and the IDEA_BOOTSTRAP_COMPLETE filter could never match the line
    """
    install = (
        Path(IDEA_BOOTSTRAP_DIR) / 'virtual-desktop-host-windows/Install.ps1'
    ).read_text()
    body = install[
        install.index('function Write-ToLog') : install.index(
            'function Wait-ForService'
        )
    ]
    assert 'Out-File' not in body and 'Add-Content' not in body
    assert '[System.IO.File]::AppendAllText($LogFile' in body


@pytest.mark.parametrize(
    'marker,refreshed', [(None, True), ('old', True), ('current', False)]
)
def test_ubuntu_host_refreshes_apt_only_without_a_current_baked_release(
    tmp_path, marker, refreshed
):
    """the live failure: a baked Ubuntu host ran apt-get update on first boot past the gate"""
    host = render('virtual-desktop-host-linux/setup.sh.jinja2', 'ubuntu2404')
    start = host.index('TARGET_KERNEL_ABI=$(uname -r)')
    block = host[start : host.index('TARGET_KERNEL_VERSION=', start)]
    block = block.replace('/var/lib/idea', str(tmp_path))
    if marker is not None:
        (tmp_path / 'baked-release').write_text(marker)
    stubs = (
        'apt-get() { echo REFRESHED; }; wget() { :; }; gpg() { :; }\n'
        'apt-cache() { echo found; }; fail_kernel_bootstrap() { exit 9; }\n'
        f'mkdir -p {tmp_path}/etc; . () {{ :; }}\n'
    )
    block = block.replace('/etc/apt/sources.list.d/', f'{tmp_path}/').replace(
        '/usr/share/keyrings/', f'{tmp_path}/'
    )
    result = subprocess.run(
        ['bash', '-c', stubs + block],
        env={**os.environ, 'IDEA_MODULE_VERSION': 'current'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ('REFRESHED' in result.stdout) == refreshed


def test_desktop_host_keeps_the_baked_usb_module_and_cronie():
    """a baked host must not rebuild the DCV USB module through DKMS or reinstall cronie"""
    for name in (
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
    ):
        text = render(name)
        assert 'modinfo eveusb' in text and 'lsmod | grep eveusb' not in text
    setup = render('virtual-desktop-host-linux/setup.sh.jinja2', 'amazonlinux2023')
    assert 'rpm -q cronie >/dev/null 2>&1 || dnf -y install cronie' in setup


def test_windows_join_renames_first_so_the_preset_account_matches():
    """
    Add-Computer -NewName joins under the EC2AMAZ-* name, which has no preset account: the
    unsecured join then fails (NTLM disabled on 2025, PDC-only on 2019). Rename-Computer
    without a restart sets the pending name that JoinWithNewName joins under.
    """
    join = (
        Path(IDEA_BOOTSTRAP_DIR) / '_templates/windows/join_activedirectory.jinja2'
    ).read_text()
    assert '-NewName $($authorizationEntry.hostname) "' not in join
    rename = join.index('Rename-Computer -NewName $($authorizationEntry.hostname)')
    assert rename < join.index('Invoke-Expression $joinCmd')
    assert '$optionsString += ", JoinWithNewName"' in join
    # a failed join retries on a short bounded poll, not a fixed 30 s sleep
    assert 'Get-Random -Minimum $AD_JOIN_MIN_SLEEP' not in join
    assert '((Get-Date) -lt $joinDeadline)' in join


def test_windows_installers_download_in_process_and_name_the_failure():
    """
    a Start-Job download whose job died left no file and no error; msiexec then exited
    1619 and the bake only said 'Installer exited before checks finished'
    """
    install = (
        Path(IDEA_BOOTSTRAP_DIR) / 'virtual-desktop-host-windows/Install.ps1'
    ).read_text()
    assert 'Start-Job' not in install
    assert install.count('Save-Installer -Uri https://') == 2
    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', 'windows2022')
    finally_block = setup[setup.rindex('} finally {') :]
    assert 'Select-String -Path $LogFile' in finally_block


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
def test_ubuntu_desktop_boots_do_not_wait_for_networkd(base_os):
    # 22.04 hands the interface to NetworkManager; systemd-networkd-wait-online then times
    # out after 120 s on every later boot, including the bootstrap's own reboot
    host = render('virtual-desktop-host-linux/configure_dcv_host.sh.jinja2', base_os)
    disable = host.index('systemctl disable systemd-networkd-wait-online.service')
    assert disable < host.index('set_reboot_required "Reboot after DCV host')


@pytest.mark.parametrize('cat_rc,expected', [(5, 0), (0, 1)])
def test_amd_driver_install_stops_pbs_only_when_it_is_installed(
    tmp_path, cat_rc, expected
):
    """
    the desktop builder installs OpenPBS after the GPU drivers: stopping a unit that does
    not exist tripped the ERR trap and marked an otherwise good AMD bake failed. A unit
    that exists and refuses to stop still fails.
    """
    host = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'rocky9',
        instance_type='g4ad.xlarge',
        drivers=('amd',),
    )
    line = next(x for x in host.splitlines() if 'systemctl stop pbs' in x)
    stub = tmp_path / 'systemctl'
    stub.write_text(f'#!/bin/bash\n[ "$1" = cat ] && exit {cat_rc}\nexit 1\n')
    stub.chmod(0o755)
    result = subprocess.run(
        ['bash', '-eEc', f'trap "exit 9" ERR; {line.strip()}'],
        env={'PATH': f'{tmp_path}:/usr/bin:/bin'},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (expected == 0), result


@pytest.mark.parametrize('base_os', WINDOWS)
def test_windows_scrub_leaves_the_session_manager_agent_its_log_directory(base_os):
    # the agent service exits at start when its log directory is missing, so a scrub
    # that removes the agent's data directory must recreate the log directory, or every
    # desktop from the image stays CREATING with no agent registered at the broker
    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', base_os)
    agent_data = "'C:\\ProgramData\\NICE\\DCVSessionManagerAgent',"
    recreate = (
        'New-Item -ItemType Directory '
        "'C:\\ProgramData\\NICE\\DCVSessionManagerAgent\\log' -Force"
    )
    assert agent_data in setup
    assert recreate in setup
    assert setup.index(agent_data) < setup.index(recreate)
    assert setup.index(recreate) < setup.index("Add-ImageCheck 'bootstrap'")


@pytest.mark.parametrize('base_os', ['rocky8', 'rocky9'])
def test_the_compute_bake_drops_slow_rocky_mirrors_before_its_first_install(base_os):
    # the compute builder installs ~930 MB from the public mirrorlist; on one ~130 kB/s
    # mirror it missed its hour. it gets the same mirror settings as the hosts
    setup = render('compute-node-ami-builder/setup.sh.jinja2', base_os, config=Config())
    assert 'minrate=512k' in setup
    assert setup.index('minrate=512k') < setup.index('epel')


@pytest.mark.parametrize('base_os', WINDOWS)
def test_windows_user_data_that_runs_again_after_the_final_restart_is_a_no_op(base_os):
    """
    EC2Launch v2 records a finished user data run after its postReady stage; the restart at
    the end of configuration can take it down first, and 2019 then ran all of the user data
    again after IDEA_BOOTSTRAP_COMPLETE (a second join, a second restart after READY)
    """
    configure = render('virtual-desktop-host-windows/Configure.ps1.jinja2', base_os)
    body = configure[configure.index('function Configure-WindowsEC2Instance') :]
    guard = body.index('if (Test-Path $ConfiguredMarker) {')
    assert body.index('/meta-data/instance-id') < guard
    # nothing that renames, joins or restarts runs before the guard
    for step in ('Get-IdeaHostname', 'Rename-Computer', 'Restart-Computer'):
        assert guard < body.index(step), step
    assert 'exit 0' in body[guard : guard + 300]
    # the marker is per instance, so an image made from this desktop still configures
    assert 'configured-$InstanceId' in body
    written = body.index('[IO.File]::WriteAllText($ConfiguredMarker')
    assert body.index('IDEA_BOOTSTRAP_COMPLETE') < written
    assert written < body.rindex('Restart-Computer -Force')


@pytest.mark.parametrize('base_os', WINDOWS)
def test_windows_repeat_run_marker_waits_for_a_successful_domain_join(
    tmp_path, monkeypatch, base_os
):
    """
    the marker makes the next boot skip user data; a join that failed must run again, so
    the marker is written only once the instance is in the domain
    """
    import test_bootstrap_shell_syntax as shell

    class ActiveDirectory(shell.Config):
        def __init__(self):
            super().__init__()
            self.put('directoryservice.provider', 'aws_managed_activedirectory')
            for key, value in {
                'ad_short_name': 'EXAMPLE',
                'name': 'example.invalid',
                'ad_automation.sqs_queue_url': 'https://sqs.example.invalid/ad',
                'ad_automation.ad_join_max_sleep': 10,
                'ad_automation.ad_join_retry_count': 3,
                'ad_automation.hostname_prefix': 'IDEA-',
            }.items():
                self.put(f'directoryservice.{key}', value)

    monkeypatch.setattr(shell, 'Config', ActiveDirectory)
    _, component, variables = shell.packages(base_os)[0]
    scripts = shell.render(tmp_path, base_os, 'm7i.large', component, variables)
    configure = next(f for f in scripts if f.endswith('Configure.ps1'))
    text = Path(configure).read_text()
    write = text.index('[IO.File]::WriteAllText($ConfiguredMarker')
    guard = text.rindex('if ($Joined) {', 0, write)
    joined = text[text.rindex('$Joined =', 0, guard) : guard]
    assert 'PartOfDomain' in joined and '$global:IdeaDomainJoined' in joined
    assert '$global:IdeaDomainJoined = $joined' in text


USERDATA_HOOKS = (
    'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
    'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
    'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
    'compute-node/compute_node_post_reboot.sh.jinja2',
)


def _hook_variables(name):
    if name.startswith('compute-node/'):
        from test_bootstrap_shell_syntax import job

        return {'job': job()}
    return None


def test_optional_userdata_hooks_are_guarded():
    for name in USERDATA_HOOKS:
        text = render(name, 'rocky9', variables=_hook_variables(name))
        assert 'no userdata customizations' in text, name
        assert '|| exit' in _hook_block(text), name


def _hook_block(text):
    start = text.index('userdata_customizations.sh')
    start = text.rindex('if [[ -f', 0, start)
    end = text.index('fi', start)
    return text[start : text.index('\n', end)]


@pytest.mark.parametrize('case', ['absent', 'fail', 'pass'])
def test_userdata_customization_hook(tmp_path, case):
    """a missing hook is skipped; one that exists and fails stops the bake; one that passes runs"""
    text = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2', 'rocky9'
    )
    home = tmp_path / 'cluster'
    logs = tmp_path / 'logs'
    logs.mkdir()
    hook = home / 'vdc' / 'ami_builder' / 'userdata_customizations.sh'
    if case != 'absent':
        hook.parent.mkdir(parents=True)
        body = 'echo ran-hook\n' if case == 'pass' else 'echo hook-failed >&2\nexit 1\n'
        hook.write_text('#!/bin/bash\n' + body)
        hook.chmod(0o755)
    script = 'log_info() { echo "$*"; }\n' + _hook_block(text) + '\necho continued\n'
    result = subprocess.run(
        ['bash', '-c', script],
        capture_output=True,
        text=True,
        env={
            'IDEA_CLUSTER_HOME': str(home),
            'IDEA_MODULE_ID': 'vdc',
            'IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR': str(logs),
            'PATH': os.environ['PATH'],
        },
    )
    if case == 'absent':
        assert result.returncode == 0, result.stderr
        assert 'no userdata customizations' in result.stdout
        assert 'continued' in result.stdout
    elif case == 'fail':
        assert result.returncode != 0
        assert 'continued' not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert 'ran-hook' in (logs / 'userdata_customizations.log').read_text()
        assert 'continued' in result.stdout


def _environment_loader(text):
    source_at = text.index('source /etc/environment')
    line_start = text.rfind('\n', 0, source_at) + 1
    previous = text.rfind('\n', 0, line_start - 1) + 1
    if text.startswith('set -a', previous):
        line_start = previous
    line_end = text.find('\n', source_at)
    line_end = len(text) if line_end == -1 else line_end + 1
    if text.startswith('set +a', line_end):
        next_end = text.find('\n', line_end)
        line_end = len(text) if next_end == -1 else next_end + 1
    return text[line_start:line_end]


@pytest.mark.parametrize(
    'name,hook_rel',
    [
        (
            'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
            'vdc/ami_builder/userdata_customizations.sh',
        ),
        (
            'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
            'vdc/ami_builder/userdata_customizations.sh',
        ),
        (
            'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
            'dcv_host/userdata_customizations.sh',
        ),
        (
            'compute-node/compute_node_post_reboot.sh.jinja2',
            'vdc/compute_node/userdata_customizations.sh',
        ),
    ],
)
@pytest.mark.parametrize('preexported', [False, True])
def test_site_hook_sees_cluster_home(tmp_path, name, hook_rel, preexported):
    """
    preexported is the reboot path (cron pam_env already exported /etc/environment).
    the other path is cloud-init, which only sources the file.
    """
    text = render(name, 'amazonlinux2023', variables=_hook_variables(name))
    home = tmp_path / 'cluster'
    logs = tmp_path / 'logs'
    logs.mkdir()
    hook = home / hook_rel
    hook.parent.mkdir(parents=True)
    hook.write_text('#!/bin/bash\nprintf "%s\\n" "$IDEA_CLUSTER_HOME"\n')
    envfile = tmp_path / 'environment'
    envfile.write_text(
        f'IDEA_CLUSTER_HOME={home}\n'
        'IDEA_MODULE_ID=vdc\n'
        f'IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR={logs}\n'
        f'IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR={logs}\n'
        f'IDEA_COMPUTE_NODE_LOGS_DIR={logs}\n'
        f'BOOTSTRAP_DIR={tmp_path}\n'
    )
    loader = _environment_loader(text).replace(
        'source /etc/environment', f'source "{envfile}"'
    )
    script = 'log_info() { echo "$*"; }\n' + loader + '\n' + _hook_block(text) + '\n'
    parent = {'PATH': os.environ['PATH']}
    if preexported:
        parent.update(
            {
                'IDEA_CLUSTER_HOME': str(home),
                'IDEA_MODULE_ID': 'vdc',
                'IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR': str(logs),
                'IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR': str(logs),
                'IDEA_COMPUTE_NODE_LOGS_DIR': str(logs),
                'BOOTSTRAP_DIR': str(tmp_path),
            }
        )
    result = subprocess.run(
        ['bash', '-c', script], capture_output=True, text=True, env=parent
    )
    assert result.returncode == 0, result.stderr
    assert (logs / 'userdata_customizations.log').read_text().strip() == str(home)
