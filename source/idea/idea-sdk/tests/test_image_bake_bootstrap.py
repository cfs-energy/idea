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
    ],
)
def test_a_failed_check_reports_what_it_found(tmp_path, failed, detail):
    _, report, _ = run_checks(tmp_path, failed)
    assert {c['name']: c['detail'] for c in report['checks']}[failed] == detail
    # passing checks keep their description
    assert {c['name']: c['detail'] for c in report['checks']}['desktop'] == (
        'GNOME display manager is installed'
    )


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
    script = trap.replace('/var/lib/idea/bake-failed', str(state)) + '\nset -e\nfalse --scrub-step\n'
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
    result = subprocess.run(['bash', '-c', stubs + loop], capture_output=True, text=True)
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
