"""
the dcv host build bootstrap must render without any per-session state: no session id,
no broker registration, no host-ready notification, and it must both honor and write
the first-boot skip markers the session bootstrap checks.
"""

import os

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils
from ideadatamodel import SocaAnyPayload
from ideatestutils import MockConfig
from ideasdk.config.soca_config import SocaConfig

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)


def render(
    template_path: str,
    base_os: str = 'amazonlinux2023',
    instance_type: str = 'c5.large',
) -> str:
    config = SocaConfig(config=MockConfig().get_config())
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
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(template_path).render(context=context)


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
    assert rendered.index('grubby --set-default') < rendered.index('      reboot')


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
log_error() { :; }
instance_id() { echo i-0; }
uname() { if [[ "$1" == "-r" ]]; then echo "$RUNNING_KERNEL"; else echo x86_64; fi; }
rpm() {
  case "$*" in
    *--provides*) [[ -n "$NO_LUSTRE_MODULE" ]] || echo "kmod-lustre-client = 2.15.6" ;;
    *--queryformat*) echo -n "$KERNEL_PACKAGE" ;;
    *) : ;;
  esac
}
dnf() { echo "dnf $*" >> "$CALLS"; [[ "$1 $2" != "install -y" || "$*" == *versionlock* ]]; }
aws() { echo "aws $*" >> "$CALLS"; }
apt-get() { :; }
apt-mark() { :; }
apt() { :; }
apt-cache() { [[ -n "$NO_LUSTRE_MODULE" ]] || echo "Package: $2"; }
wget() { :; }
gpg() { :; }
grubby() { echo "grubby $*" >> "$CALLS"; }
update-grub() { :; }
set_reboot_required() { echo "reboot_required $*" >> "$CALLS"; }
check_reboot_loop() { :; }
crontab() { :; }
reboot() { echo reboot >> "$CALLS"; }
"""


def run_set_kernel(
    tmp_path,
    base_os: str,
    running_kernel: str,
    kernel_package: str = 'kernel',
    lustre_module: bool = True,
):
    import subprocess

    rendered = render('_templates/linux/set_kernel.jinja2', base_os=base_os)
    rendered = (
        rendered.replace('. /etc/os-release', ': ')
        .replace('/etc/apt/sources.list.d/', f'{tmp_path}/')
        .replace('/usr/share/keyrings/', f'{tmp_path}/')
    )
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = tmp_path / 'set_kernel.sh'
    script.write_text(
        f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
        + 'sleep() { :; }\n'
        + KERNEL_STUBS
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
