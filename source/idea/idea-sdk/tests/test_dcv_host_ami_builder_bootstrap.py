"""
the dcv host build bootstrap must render without any per-session state: no session id,
no broker registration, no host-ready notification, and it must both honor and write
the first-boot skip markers the session bootstrap checks.
"""

import os

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils
from ideadatamodel import SocaAnyPayload
from ideatestutils import MockConfig
from ideasdk.config.soca_config import SocaConfig
from test_bootstrap_shell_syntax import Config

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)


def render(
    template_path: str,
    base_os: str = 'amazonlinux2023',
    instance_type: str = 'c5.large',
    lustre: bool = True,
    bake: bool = False,
) -> str:
    config_data = MockConfig().get_config()
    config_data['global-settings'] = Config().get_config('global-settings')
    config_data['shared-storage'] = {
        name: storage
        for name, storage in config_data['shared-storage'].items()
        if storage.get('provider') not in ('fsx_lustre', 'fsx_cache')
    }
    if lustre:
        config_data['shared-storage']['lustre'] = {
            'provider': 'fsx_lustre',
            'mount_dir': '/lustre',
            'mount_options': 'defaults,_netdev',
            'fsx_lustre': {'dns': 'fs.example.invalid', 'mount_name': 'test'},
        }
    config = SocaConfig(config=config_data)
    context = BootstrapContext(
        config=config,
        module_name='virtual-desktop-controller',
        module_id='vdc',
        module_set='default',
        base_os=base_os,
        instance_type=instance_type,
    )
    context.vars.ami_dir = '/apps/idea-mock/vdc/ami_builder/idea-dcv-host/1'
    context.vars.ami_name = 'idea-dcv-host-amazonlinux2023-v01011970-000000'
    context.vars.session = SocaAnyPayload(type='console')
    context.vars.bedrock_env = {}
    context.vars.bedrock_model_messages = []
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(template_path).render(
        context=context, ubuntu_desktop_bake=bake
    )


def test_build_setup_has_no_session_state():
    rendered = render('dcv-host-ami-builder/setup.sh.jinja2')
    assert 'dcv_host_ami_builder.sh' in rendered
    assert 'IDEA_SESSION_ID' not in rendered
    assert 'IDEA_SESSION_OWNER' not in rendered
    assert 'sqs send-message' not in rendered


def test_build_records_package_stage_without_stamping_an_unchecked_release():
    rendered = render('dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2')
    assert 'touch /var/lib/idea/bake-packages.ok' in rendered
    assert 'idea_preinstalled_packages.log' not in rendered
    assert 'idea_system_upgraded.log' not in rendered
    assert '> /var/lib/idea/baked-release' not in rendered


def test_rocky9_kernel_reboot_selects_the_installed_kernel_once():
    rendered = render('_templates/linux/set_kernel.jinja2', base_os='rocky9')
    retry_check = 'grep -Fq "kernel version change to ${target_version}"'

    assert 'grubby --set-default "${target_kernel}"' in rendered
    assert 'GRUB_DEFAULT=0' not in rendered
    assert 'fail_kernel_bootstrap kernel-boot-mismatch' in rendered
    assert rendered.index(retry_check) < rendered.index(
        '# Check if target kernel is already installed'
    )
    assert rendered.index('grubby --set-default') < rendered.index(
        '      reboot_after_cloud_init'
    )


def test_build_post_reboot_installs_dcv_but_never_registers():
    rendered = render('dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2')
    assert 'image_checks.sh' in rendered
    assert 'image_scrub.sh' in rendered
    # the session-only actions must not be in the built image path
    assert 'sqs send-message' not in rendered
    assert 'dcv_host_ready_message' not in rendered
    # the markers survive the image clean-up
    assert 'rm -rf /root/bootstrap/logs' not in rendered
    assert 'rm -rf /root/bootstrap\n' not in rendered


def test_build_post_reboot_scrubs_the_builder_identity():
    rendered = render('dcv-host-ami-builder/image_scrub.sh.jinja2')
    for line in (
        ': > /etc/machine-id',
        'rm -f /var/lib/dbus/machine-id',
        'cloud-init clean --logs --seed',
        '/etc/dcv/dcv.key /etc/dcv/dcv.pem',
        'realm leave "$domain"',
        '/var/lib/sss/db/*',
        '/etc/ssh/ssh_host_*',
        '/var/lib/dcv-session-manager-agent/*',
        '/opt/idea/.services',
    ):
        assert line in rendered, line
    post = render('dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2')
    assert post.index('image_checks.sh') < post.index('image_scrub.sh')
    assert post.index('image_scrub.sh') < post.index('> /var/lib/idea/baked-release')
    assert post.index('> /var/lib/idea/baked-release') < post.index('--publish')


# set_kernel run against the package state of a stock image, every command it reaches stubbed.
KERNEL_STUBS = r"""
log_info() { :; }
log_warning() { echo "warning $*" >> "$CALLS"; }
log_error() { echo "error $*" >> "$CALLS"; }
instance_id() { echo i-0; }
uname() { if [[ "$1" == "-r" ]]; then echo "$RUNNING_KERNEL"; else echo x86_64; fi; }
rpm() {
  case "$*" in
    *--provides*) [[ -n "$NO_LUSTRE_MODULE" ]] || echo "kmod-lustre-client = 2.15.6" ;;
    *--queryformat*) echo -n "$KERNEL_PACKAGE" ;;
    *) : ;;
  esac
}
dnf() {
  echo "dnf $*" >> "$CALLS"
  while (( $# )); do
    case "$1" in
      --setopt=*) shift ;;
      -o|--setopt) shift 2 ;;
      *) break ;;
    esac
  done
  [[ "$1 $2" != "install -y" || "$*" == *versionlock* ]]
}
aws() { echo "aws $*" >> "$CALLS"; }
apt-get() { echo "apt-get $*" >> "$CALLS"; }
apt-mark() {
  echo "apt-mark $*" >> "$CALLS"
  [[ -z "$FAIL_HOLD" ]] || return 100
  shift
  local package
  for package in "$@"; do
    [[ -z "$FAIL_META_HOLD" || "$package" != linux-aws ]] || return 100
    dpkg -s "$package" >/dev/null 2>&1 || [[ -n "$(apt-cache show "$package")" ]] || return 100
  done
}
apt() {
  echo "apt $*" >> "$CALLS"
  [[ -z "$FAIL_INSTALL" ]] || return 1
  for arg in "$@"; do
    case "$arg" in linux-image-*) touch "${BOOTSTRAP_DIR}/vmlinuz-${arg#linux-image-}" ;; esac
  done
}
dpkg() {
  if [[ "$1" == --print-architecture ]]; then
    echo "$KERNEL_ARCH"
  elif [[ " $INSTALLED_PACKAGES " == *" $2 "* ]]; then
    echo 'Status: install ok installed'
  else
    return 1
  fi
}
apt-cache() {
  local abi="${2#lustre-client-modules-}"
  abi="${abi%:*}"
  case "$1" in
    pkgnames)
      if [[ "$2" == linux-image- ]]; then
        for abi in $IMAGE_ABIS; do echo "linux-image-${abi}"; done
      else
        for abi in $REPO_ABIS; do echo "lustre-client-modules-${abi}"; done
      fi ;;
    policy)
      if [[ " $FSX_ABIS " == *" $abi "* ]]; then
        echo "500 https://fsx-lustre-client-repo.s3.amazonaws.com/ubuntu ${VERSION_CODENAME}/main ${KERNEL_ARCH} Packages"
      else
        echo "500 https://example.org/ubuntu ${VERSION_CODENAME}/main ${KERNEL_ARCH} Packages"
      fi ;;
    show)
      [[ " $MISSING_PACKAGES " != *" $2 "* ]] || return 100
      if [[ "$2" == linux-image-[0-9]* ]]; then
        abi="${2#linux-image-}"; abi="${abi%:*}"
        [[ " $IMAGE_ABIS " != *" $abi "* ]] || echo "Package: linux-image-${abi}"
      else
        [[ -n "$NO_LUSTRE_MODULE" ]] || echo "Package: $2"
      fi ;;
  esac
}
sed() {
  if [[ "$1" == -i ]]; then echo "sed $*" >> "$CALLS"; else command sed "$@"; fi
}
wget() { :; }
gpg() { :; }
grubby() { echo "grubby $*" >> "$CALLS"; }
update-grub() { echo update-grub >> "$CALLS"; }
set_reboot_required() { echo "reboot_required $*" >> "$CALLS"; }
check_reboot_loop() { :; }
crontab() { :; }
reboot_after_cloud_init() { echo reboot >> "$CALLS"; }
"""

STORAGE_STUBS = r"""
mkdir() { :; }
nproc() { echo 2; }
free() { echo 'Mem: 16'; }
modinfo() { [[ "$RUNNING_KERNEL" == 6.8.0-1057-aws ]]; }
modprobe() { echo "modprobe $*" >> "$CALLS"; modinfo; }
grep() {
  if [[ "$*" == *'/proc/filesystems'* ]]; then modinfo; else command grep "$@"; fi
}
add_efs_to_fstab() { :; }
add_fsx_lustre_to_fstab() { echo 'fstab lustre' >> "$CALLS"; }
mount() {
  echo "mount $*" >> "$CALLS"
  if [[ "$*" != *nolustre* ]]; then MOUNTED=1; fi
}
findmnt() { [[ "$MOUNTED" == 1 ]]; }
lctl() { echo "lctl $*" >> "$CALLS"; [[ "$MOUNTED" == 1 ]]; }
"""


def run_set_kernel(
    tmp_path,
    base_os: str,
    running_kernel: str,
    kernel_package: str = 'kernel',
    lustre_module: bool = True,
    lustre: bool = True,
    storage_stage: str = '',
    **environment,
):
    import subprocess

    rendered = render(
        '_templates/linux/set_kernel.jinja2',
        base_os=base_os,
        lustre=lustre,
        bake=bool(environment.get('BAKE_STAGE')),
    )
    if storage_stage:
        name = (
            'setup' if storage_stage == 'setup' else 'dcv_host_ami_builder_post_reboot'
        )
        text = render(f'dcv-host-ami-builder/{name}.sh.jinja2', base_os, lustre=lustre)
        storage = text.split('# Begin: Mount Shared Storage')[1].split(
            '# End: Mount Shared Storage'
        )[0]
        rendered = (rendered if storage_stage == 'setup' else '') + storage
    rendered = (
        rendered.replace('. /etc/os-release', ': ')
        .replace('/etc/apt/sources.list.d/', f'{tmp_path}/')
        .replace('/usr/share/keyrings/', f'{tmp_path}/')
        .replace('/etc/default/grub', f'{tmp_path}/grub')
        .replace('/boot/', f'{tmp_path}/')
        .replace('/etc/modprobe.d/', f'{tmp_path}/')
    )
    (tmp_path / 'grub').write_text('GRUB_DEFAULT=0\n')
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = tmp_path / 'set_kernel.sh'
    script.write_text(
        f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
        + f'BOOTSTRAP_DIR="{tmp_path}"\n'
        + 'sleep() { :; }\n'
        + KERNEL_STUBS
        + (STORAGE_STUBS if storage_stage else '')
        + rendered
    )
    result = subprocess.run(
        ['bash', str(script)],
        env={
            'PATH': os.environ['PATH'],
            'CALLS': str(calls),
            'BOOTSTRAP_DIR': str(tmp_path),
            'RUNNING_KERNEL': running_kernel,
            'KERNEL_PACKAGE': kernel_package,
            'NO_LUSTRE_MODULE': '' if lustre_module else '1',
            'VERSION_CODENAME': {'ubuntu2204': 'jammy', 'ubuntu2604': 'resolute'}.get(
                base_os, 'noble'
            ),
            **environment,
        },
        capture_output=True,
        text=True,
    )
    return result.returncode, calls.read_text()


def test_al2023_keeps_the_kernel_its_image_boots(tmp_path):
    # the stock image runs a kernel6.12 / kernel6.18 family kernel that carries lustre.ko itself:
    # nothing is installed, nothing reboots, and the lock names the family that owns the kernel.
    for running, family in (
        ('6.12.100-125.179.amzn2023.x86_64', 'kernel6.12'),
        ('6.18.41-94.142.amzn2023.aarch64', 'kernel6.18'),
        ('6.1.180-225.360.amzn2023.x86_64', 'kernel'),
    ):
        code, calls = run_set_kernel(tmp_path, 'amazonlinux2023', running, family)
        assert code == 0, calls
        assert 'reboot' not in calls
        assert 'dnf install -y kernel' not in calls
        assert f'dnf versionlock {family}-{running} ' in calls
        assert 'warning' not in calls


def test_el_on_its_target_series_does_not_reboot(tmp_path):
    code, calls = run_set_kernel(tmp_path, 'rocky9', '5.14.0-687.5.1.el9_8.x86_64')
    assert code == 0, calls
    assert 'reboot' not in calls


def test_ubuntu_keeps_the_kernel_its_image_boots(tmp_path):
    # the repository has a module for the running ABI, even when a newer one exists
    code, calls = run_set_kernel(tmp_path, 'ubuntu2204', '6.8.0-1061-aws')
    assert code == 0, calls
    assert 'reboot' not in calls


def test_a_running_kernel_without_a_lustre_module_fails_fast(tmp_path):
    # no kernel swap: the host stops with a status naming what is missing
    for base_os, running in (
        ('ubuntu2404', '6.17.0-1099-aws'),
        ('amazonlinux2023', '6.12.100-125.179.amzn2023.x86_64'),
    ):
        code, calls = run_set_kernel(tmp_path, base_os, running, lustre_module=False)
        assert code == 1
        assert 'Value=lustre-module-missing' in calls
        assert 'reboot' not in calls
        assert 'install' not in calls


def test_a_kernel_that_cannot_be_installed_aborts_with_its_own_status(tmp_path):
    # rebooting after a failed install only to report a boot mismatch sends people the wrong way
    code, calls = run_set_kernel(tmp_path, 'rocky9', '5.14.0-570.1.1.el9_6.x86_64')
    assert code == 1
    assert 'Value=kernel-install-failed' in calls
    assert 'reboot' not in calls


def test_ubuntu2404_keeps_networkd_during_bootstrap():
    # switching the netplan renderer mid-bootstrap took the 24.04 host offline and killed
    # cloud-init, which runs the bootstrap
    from unittest.mock import MagicMock

    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2'
    )
    noble = template.render(context=MagicMock(base_os='ubuntu2404'))
    jammy = template.render(context=MagicMock(base_os='ubuntu2204'))
    assert 'netplan apply' not in noble
    assert "renderer'] = 'NetworkManager'" not in noble
    assert 'netplan apply' in jammy


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
@pytest.mark.parametrize('instance_type', ['m7i.large', 'm7g.large'])
def test_ubuntu2404_bake_selects_fsx_abi_before_upgrades(base_os, instance_type):
    text = render(
        'dcv-host-ami-builder/setup.sh.jinja2',
        base_os,
        instance_type,
    )
    kernel = text.split('# Begin: Kernel Version Management')[1].split(
        '# End: Kernel Version Management'
    )[0]
    assert 'apt-cache pkgnames lustre-client-modules-' in kernel
    assert text.index('BAKE_STAGE=setup') < text.index('apt-cache pkgnames')
    assert 'dpkg --print-architecture' in kernel
    assert (
        'apt-cache policy "lustre-client-modules-${candidate_abi}:${KERNEL_ARCH}"'
        in kernel
    )
    assert 'sort -Vr' in kernel
    assert 'TARGET_KERNEL_ABI="${SELECTED_KERNEL_ABI}"' in kernel
    assert 'linux-aws linux-image-aws linux-headers-aws linux-tools-aws' in kernel
    assert 'apt-mark hold "${kernel_packages[@]}"' in kernel
    assert (
        'Advanced options for Ubuntu>Ubuntu, with Linux ${TARGET_KERNEL_ABI}' in kernel
    )
    assert 'set_reboot_required "Ubuntu kernel version change' in kernel
    assert '\n      reboot_after_cloud_init\n' not in kernel
    assert text.index('apt-mark hold') < text.index('# Begin: Mount Shared Storage')
    assert 'lustre-client-modules-$(uname -r)' not in text
    assert (
        'apt install -y lustre-client-utils "lustre-client-modules-${TARGET_KERNEL_ABI}"'
        in text
    )
    assert 'FSx storage mount reboot' not in text
    packages = render('dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2', base_os)
    assert 'apt-cache pkgnames' not in packages
    assert 'apt upgrade -y' in packages
    post = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2', base_os
    )
    assert 'apt-cache pkgnames' not in post
    assert (
        post.index('# Begin: Mount Shared Storage')
        < post.index('lctl lustre_build_version')
        < post.index('image_checks.sh')
    )
    assert 'if findmnt -rn -t lustre >/dev/null; then' in post
    checks = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os)
    assert (
        'packages_installed lustre-client-utils "lustre-client-modules-$(uname -r)"'
        in checks
    )
    assert 'command -v mount.lustre' in checks
    assert 'findmnt -rn -t lustre --mountpoint "/lustre"' in checks


# EL kernel snapshots from 81d856e9, updated only to defer reboot until cloud-final exits.
@pytest.mark.parametrize(
    'base_os, expected',
    [
        (
            'amazonlinux2023',
            '36c9f9c95552f62ae279ca039d96f72f74f91d2990e827d384c01c903efcd292',
        ),
        ('rhel8', '9d74e3bb04021634acf1796ac5af170a4b9574f028a81b4c3cc291a9b6f2d53f'),
        ('rocky8', '9d74e3bb04021634acf1796ac5af170a4b9574f028a81b4c3cc291a9b6f2d53f'),
        ('rhel9', '04c17982890718982d8e4b6a8bc7e1cc87341c36b0999e31951e26d0c66e2b47'),
        ('rocky9', '04c17982890718982d8e4b6a8bc7e1cc87341c36b0999e31951e26d0c66e2b47'),
    ],
)
def test_non_ubuntu_kernel_renders_unchanged(base_os, expected):
    import hashlib
    from pathlib import Path
    from jinja2 import Template

    text = Template(
        Path(IDEA_BOOTSTRAP_DIR, '_templates/linux/set_kernel.jinja2').read_text()
    ).render(context={'base_os': base_os, 'aws_region': 'us-east-1'})
    assert hashlib.sha256(text.encode()).hexdigest() == expected


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
@pytest.mark.parametrize('arch', ['amd64', 'arm64'])
@pytest.mark.parametrize('running', ['6.8.0-1063-aws', '6.8.0-1050-aws'])
def test_ubuntu_bake_pins_supported_kernel_without_rebooting(
    tmp_path, base_os, arch, running
):
    code, calls = run_set_kernel(
        tmp_path,
        base_os,
        running,
        BAKE_STAGE='packages',
        KERNEL_ARCH=arch,
        REPO_ABIS='7.0.0-1014-aws 6.17.0-1020-aws 6.8.0-1050-aws 6.8.0-1057-aws 6.8.0-1063-aws',
        FSX_ABIS='7.0.0-1014-aws 6.17.0-1020-aws 6.8.0-1050-aws 6.8.0-1057-aws',
        IMAGE_ABIS='7.0.0-1014-aws 6.17.0-1020-aws 6.8.0-1050-aws 6.8.0-1057-aws 6.8.0-1063-aws',
    )
    target = '6.8.0-1057-aws' if running.endswith('1063-aws') else running
    assert code == 0, calls
    assert (
        f'apt-mark hold linux-image-{target} linux-headers-{target} linux-modules-{target}'
        in calls
    )
    assert 'linux-aws linux-image-aws linux-headers-aws linux-tools-aws' in calls
    assert f'Advanced options for Ubuntu>Ubuntu, with Linux {target}' in calls
    assert 'update-grub' in calls
    assert '\nreboot\n' not in calls
    assert 'apt purge' not in calls and 'apt remove' not in calls
    if target != running:
        assert (
            f'apt install -y linux-image-{target} linux-modules-{target} linux-modules-extra-{target} linux-headers-{target}'
            in calls
        )
        assert 'reboot_required Ubuntu kernel version change' in calls
    else:
        assert 'apt install' not in calls
        assert 'reboot_required' not in calls


@pytest.mark.parametrize(
    'images, expected',
    [
        ('6.8.0-1050-aws', '6.8.0-1050-aws'),
        ('', None),
    ],
)
def test_ubuntu_bake_requires_available_image_and_reports_newest_fsx_abi(
    tmp_path, images, expected
):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        '6.8.0-1063-aws',
        BAKE_STAGE='packages',
        KERNEL_ARCH='arm64',
        REPO_ABIS='6.8.0-1050-aws 6.8.0-1057-aws',
        FSX_ABIS='6.8.0-1050-aws 6.8.0-1057-aws',
        IMAGE_ABIS=images,
    )
    if expected:
        assert code == 0, calls
        assert f'apt-mark hold linux-image-{expected}' in calls
    else:
        assert code == 1, calls
        assert 'Value=lustre-module-missing' in calls
        assert 'Newest kernel supported by the FSx repository: 6.8.0-1057-aws.' in calls
        assert 'Newest Ubuntu kernel ABI considered: none.' in calls
        assert 'apt install' not in calls
        assert 'reboot' not in calls


@pytest.mark.parametrize(
    'failure, status',
    [
        ('FAIL_INSTALL', 'kernel-install-failed'),
        ('FAIL_HOLD', 'kernel-hold-failed'),
    ],
)
def test_ubuntu_bake_stops_when_install_or_hold_fails(tmp_path, failure, status):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        '6.8.0-1063-aws',
        BAKE_STAGE='packages',
        KERNEL_ARCH='amd64',
        REPO_ABIS='6.8.0-1057-aws',
        FSX_ABIS='6.8.0-1057-aws',
        IMAGE_ABIS='6.8.0-1057-aws',
        **{failure: '1'},
    )
    assert code == 1, calls
    assert f'Value={status}' in calls
    assert 'reboot' not in calls


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
@pytest.mark.parametrize('lustre', [False, True])
def test_ubuntu_runtime_holds_the_running_kernel_without_selecting_one(
    tmp_path, base_os, lustre
):
    # a desktop that is not baking keeps its kernel and holds it, as 26.10.4 did: without the
    # holds an apt upgrade boots a newer -aws kernel that has no Lustre module
    text = render('virtual-desktop-host-linux/setup.sh.jinja2', base_os, lustre=lustre)
    kernel = text.split('# Begin: Kernel Version Management')[1].split(
        '# End: Kernel Version Management'
    )[0]
    for unexpected in ('GRUB', 'apt install', 'pkgnames', 'BAKE_STAGE'):
        assert unexpected not in kernel
    assert (
        'apt-mark hold linux-image-${current_kernel} linux-headers-${current_kernel}'
        in kernel
    )
    assert (
        'apt-mark hold linux-aws linux-image-aws linux-headers-aws linux-tools-aws'
        in kernel
    )
    assert ('lustre-client-modules-${TARGET_KERNEL_ABI}' in kernel) == lustre
    assert ('fsx-lustre-client-repo' in kernel) == lustre
    running = '6.8.0-1040-aws'
    code, calls = run_set_kernel(tmp_path, base_os, running, lustre=lustre)
    assert code == 0, calls
    assert (
        f'apt-mark hold linux-image-{running} linux-headers-{running} '
        f'linux-modules-{running} linux-modules-extra-{running}'
    ) in calls
    # the stubbed host has no linux-aws pointing at the running series: the metapackages go
    assert (
        'apt remove -y linux-aws linux-image-aws linux-headers-aws linux-tools-aws'
        in calls
    )
    for unexpected in ('apt install', 'grub', 'reboot'):
        assert unexpected not in calls


@pytest.mark.parametrize('arch', ['amd64', 'arm64'])
@pytest.mark.parametrize('optional_available', [False, True])
def test_ubuntu_bake_installs_only_available_optional_kernel_packages(
    tmp_path, arch, optional_available
):
    target = '6.8.0-1057-aws'
    optional = [f'linux-modules-extra-{target}', f'linux-headers-{target}']
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        '6.8.0-1063-aws',
        BAKE_STAGE='setup',
        KERNEL_ARCH=arch,
        REPO_ABIS=target,
        FSX_ABIS=target,
        IMAGE_ABIS=target,
        MISSING_PACKAGES='' if optional_available else ' '.join(optional),
    )
    assert code == 0, calls
    install = next(
        line for line in calls.splitlines() if line.startswith('apt install')
    )
    hold = next(
        line
        for line in calls.splitlines()
        if line.startswith('apt-mark hold linux-image-6')
    )
    for package in optional:
        assert (package in install) == optional_available
        assert (package in hold) == optional_available
    assert f'linux-image-{target}' in install and f'linux-modules-{target}' in install
    assert '-o DPkg::Lock::Timeout=300' in install
    updates = [line for line in calls.splitlines() if line.startswith('apt-get')]
    assert len(updates) == 2
    assert all('-o DPkg::Lock::Timeout=300 update' in line for line in updates)


@pytest.mark.parametrize('installed', [False, True])
def test_ubuntu_bake_holds_old_abi_only_if_installed_when_unindexed(
    tmp_path, installed
):
    target = '6.8.0-1040-aws'
    packages = [
        f'linux-{name}-{target}'
        for name in ('image', 'headers', 'modules', 'modules-extra')
    ]
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        target,
        lustre=False,
        BAKE_STAGE='setup',
        MISSING_PACKAGES=' '.join(packages + ['linux-tools-aws']),
        INSTALLED_PACKAGES=' '.join(packages[:3]) if installed else '',
    )
    assert code == 0, calls
    assert ('apt-mark hold linux-image-6.8.0-1040-aws' in calls) == installed
    assert 'linux-modules-extra-' not in calls
    assert 'linux-tools-aws' not in calls
    assert 'apt-mark hold linux-aws linux-image-aws linux-headers-aws' in calls


@pytest.mark.parametrize('failure', ['', 'FAIL_META_HOLD'])
def test_ubuntu_bake_metapackage_hold_is_best_effort(tmp_path, failure):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        '6.8.0-1057-aws',
        lustre=False,
        BAKE_STAGE='setup',
        IMAGE_ABIS='6.8.0-1057-aws',
        **({failure: '1'} if failure else {}),
    )
    assert code == 0, calls
    assert 'apt-mark hold linux-image-6.8.0-1057-aws' in calls
    assert ('warning Unable to hold kernel metapackages.' in calls) == bool(failure)
    assert 'update-grub' in calls


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
def test_ubuntu_bake_without_lustre_never_selects_or_installs_lustre(tmp_path, base_os):
    for name in (
        'setup',
        'dcv_host_ami_builder',
        'dcv_host_ami_builder_post_reboot',
        'image_checks',
    ):
        text = render(f'dcv-host-ami-builder/{name}.sh.jinja2', base_os, lustre=False)
        assert 'apt-cache pkgnames' not in text
        assert 'lustre-client-' not in text
        assert 'lustre-module-missing' not in text
        assert 'check lustre ' not in text
    code, calls = run_set_kernel(
        tmp_path,
        base_os,
        '6.8.0-1063-aws',
        lustre=False,
        lustre_module=False,
        BAKE_STAGE='setup',
    )
    assert code == 0, calls
    assert 'lustre' not in calls
    assert 'apt install' not in calls


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404'])
def test_ubuntu_bake_defers_lustre_until_selected_kernel_boots(tmp_path, base_os):
    code, calls = run_set_kernel(
        tmp_path,
        base_os,
        '6.8.0-1063-aws',
        storage_stage='setup',
        BAKE_STAGE='setup',
        KERNEL_ARCH='amd64',
        REPO_ABIS='6.8.0-1057-aws',
        FSX_ABIS='6.8.0-1057-aws',
        IMAGE_ABIS='6.8.0-1057-aws 6.8.0-1063-aws',
    )
    assert code == 0, calls
    assert 'apt install -y linux-image-6.8.0-1057-aws' in calls
    assert (
        'apt install -y lustre-client-utils lustre-client-modules-6.8.0-1057-aws'
        in calls
    )
    assert 'lustre-client-modules-6.8.0-1063-aws' not in calls
    assert 'reboot_required Ubuntu kernel version change' in calls
    assert 'mount -a -t nolustre' in calls
    assert 'fstab lustre' not in calls
    assert 'lctl' not in calls
    assert '\nreboot\n' not in calls

    code, calls = run_set_kernel(
        tmp_path,
        base_os,
        '6.8.0-1057-aws',
        storage_stage='post',
        BAKE_STAGE='dcv',
    )
    assert code == 0, calls
    assert 'apt install' not in calls
    assert 'mount -a\n' in calls
    assert 'nolustre' not in calls
    assert (
        calls.index('fstab lustre')
        < calls.index('mount -a')
        < calls.index('lctl lustre_build_version')
    )


def test_ubuntu_missing_common_abi_reports_both_repository_heads(tmp_path):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2404',
        '6.8.0-1063-aws',
        BAKE_STAGE='setup',
        KERNEL_ARCH='amd64',
        REPO_ABIS='6.8.0-1057-aws',
        FSX_ABIS='6.8.0-1057-aws',
        IMAGE_ABIS='6.8.0-1063-aws 6.8.0-1061-aws',
    )
    assert code == 1, calls
    assert 'Newest kernel supported by the FSx repository: 6.8.0-1057-aws.' in calls
    assert 'Newest Ubuntu kernel ABI considered: 6.8.0-1063-aws.' in calls


@pytest.mark.parametrize('missing', ['', 'utils', 'module', 'mount'])
def test_ubuntu_lustre_image_check_requires_client_and_mounts(missing):
    import subprocess

    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2404')
    function = (
        'lustre_ok() {' + text.split('lustre_ok() {')[1].split('\ncheck lustre ')[0]
    )
    result = subprocess.run(
        [
            'bash',
            '-c',
            r"""
packages_installed() { [[ "$MISSING" != utils ]]; }
mount.lustre() { :; }
modprobe() { [[ "$MISSING" != module ]]; }
findmnt() { [[ "$MISSING" != mount ]]; }
uname() { echo 6.8.0-1057-aws; }
"""
            + function
            + '\nlustre_ok',
        ],
        env={'PATH': os.environ['PATH'], 'MISSING': missing},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if missing else 0), result.stderr


@pytest.mark.parametrize(
    'base_os', ['rhel8', 'rocky8', 'rhel9', 'rocky9', 'rhel10', 'rocky10']
)
def test_el_fsx_lustre_imports_a_key_the_os_accepts_and_logs_a_failed_import(base_os):
    # rpm-sequoia on EL10 refuses fsx-rpm-public-key.asc (SHA-1 binding signature only);
    # the el/10 packages are signed by fsx-rpm-public-key-2.asc instead
    rendered = render('_templates/linux/fsx_lustre_client.jinja2', base_os=base_os)
    key = 'https://fsx-lustre-client-repo-public-keys.s3.amazonaws.com/'
    if base_os.endswith('10'):
        assert f'{key}fsx-rpm-public-key-2.asc ' in rendered
        assert f'{key}fsx-rpm-public-key.asc ' not in rendered
    else:
        assert f'{key}fsx-rpm-public-key.asc ' in rendered
        assert f'{key}fsx-rpm-public-key-2.asc ' not in rendered
    imports = [line for line in rendered.splitlines() if 'rpm --import' in line]
    assert imports
    for line in imports:
        assert (
            '|| log_error "Failed to import the FSx for Lustre RPM signing key' in line
        )
