"""
RHEL 10, Rocky Linux 10 and Ubuntu 26.04 desktops. DCV 2026.0 runs virtual sessions there on
GDM's Wayland GNOME: no Xdcv, no DCV GL, no X server and no console sessions, so the desktop
bootstrap must leave Wayland on and install only the packages the Wayland tarballs ship.
"""

import pytest

from test_dcv_host_ami_builder_bootstrap import render, run_set_kernel

WAYLAND = ['rhel10', 'rocky10', 'ubuntu2604']
X11 = [
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rocky8',
    'rocky9',
    'ubuntu2204',
    'ubuntu2404',
]

DCV_TARBALL = {
    'rhel10': 'nice-dcv-el10',
    'rocky10': 'nice-dcv-el10',
    'ubuntu2604': 'nice-dcv-ubuntu2604',
}
AGENT = {
    'rhel10': 'nice-dcv-session-manager-agent-el10',
    'rocky10': 'nice-dcv-session-manager-agent-el10',
    'ubuntu2604': 'nice-dcv-session-manager-agent_amd64.ubuntu2604',
}


@pytest.mark.parametrize('base_os', WAYLAND)
def test_dcv_server_installs_the_wayland_packages(base_os):
    # the image bake renders the console superset; the X dummy driver must still stay out
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    for arch in ('x86_64', 'aarch64'):
        assert (
            f'https://d1uj6qtbmh3dt5.cloudfront.net/{DCV_TARBALL[base_os]}-{arch}.tgz"'
            in text
        )
        assert f'{DCV_TARBALL[base_os]}-{arch}.tgz.sha256sum"' in text
    assert 'WaylandEnable=false' not in text
    assert '"nice-xdcv-*' not in text and 'nice-xdcv_*' not in text
    assert 'install_x_dummy_driver' not in text
    assert '/etc/X11/Xsession.d' not in text and 'im-config' not in text
    assert 'systemctl set-default graphical.target' in text
    if base_os == 'ubuntu2604':
        assert (
            'apt install -y ./nice-dcv-server_*.deb ./nice-dcv-web-viewer_*.deb' in text
        )
        assert 'apt install -y ubuntu-desktop-minimal\n' in text
        assert 'dpkg -l gnome-shell' in text
        assert 'systemctl restart gdm3' in text
    else:
        assert (
            'dnf install -y ./nice-dcv-server-*.${machine}.rpm ./nice-dcv-web-viewer-*.${machine}.rpm'
            in text
        )
        assert 'groupinstall "Server with GUI"' in text
        assert 'rpm -qa gnome-shell' in text
        assert '/etc/polkit-1/rules.d/49-idea-disable-package-refresh.rules' in text
        assert 'repos.pkla' not in text


@pytest.mark.parametrize('base_os', X11)
def test_x11_desktops_keep_xdcv_and_disable_wayland(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert '"nice-xdcv-*' in text or 'nice-xdcv_*' in text
    assert 'install_x_dummy_driver' in text
    if base_os != 'amazonlinux2023':
        assert 'WaylandEnable=false' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_session_manager_agent_has_a_package(base_os):
    text = render('_templates/linux/dcv_session_manager_agent.jinja2', base_os)
    assert f'{AGENT[base_os]}' in text
    assert 'if [[ -z "$(' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_host_configuration_skips_dcv_gl(base_os):
    from unittest.mock import MagicMock

    from ideasdk.utils import Jinja2Utils
    from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR

    # a MagicMock context is a GPU instance type, which is the dcvgladmin path on X11
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2'
    )
    text = template.render(context=MagicMock(base_os=base_os))
    assert 'dcvgladmin' in template.render(context=MagicMock(base_os='rocky9'))
    gl = text.split('function configure_gl() {')[1].split('\n}\n')[0]
    assert 'no need for this configuration' in gl
    assert 'dcvgladmin' not in gl
    assert 'pulseaudio-utils' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_image_checks_require_a_wayland_gdm(base_os):
    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os)
    assert "check wayland '" in text
    assert 'WaylandEnable' in text
    gdm = 'gdm3' if base_os == 'ubuntu2604' else 'gdm'
    assert f'unit_installed {gdm}.service' in text


@pytest.mark.parametrize('base_os', X11)
def test_image_checks_for_x11_desktops_have_no_wayland_check(base_os):
    assert 'check wayland' not in render(
        'dcv-host-ami-builder/image_checks.sh.jinja2', base_os
    )


def test_wayland_check_rejects_a_disabled_wayland(tmp_path):
    import os
    import subprocess

    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'rocky10')
    function = (
        'wayland_ok() {' + text.split('wayland_ok() {')[1].split('\n}\n')[0] + '\n}'
    )
    conf = tmp_path / 'custom.conf'
    for content, target, ok in (
        ('[daemon]\n#WaylandEnable=false\n', 'graphical.target', True),
        ('[daemon]\nWaylandEnable=false\n', 'graphical.target', False),
        ('[daemon]\n', 'multi-user.target', False),
    ):
        conf.write_text(content)
        result = subprocess.run(
            [
                'bash',
                '-c',
                'systemctl() { echo "$TARGET"; }\n'
                + function.replace('/etc/gdm/custom.conf', str(conf))
                + '\nwayland_ok',
            ],
            env={'PATH': os.environ['PATH'], 'TARGET': target},
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) is ok, (content, target, result.stdout)


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
def test_el10_desktop_bake_is_supported_end_to_end(base_os):
    setup = render('dcv-host-ami-builder/setup.sh.jinja2', base_os)
    assert 'systemctl enable --now amazon-ssm-agent' in setup
    packages = render('dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2', base_os)
    assert '# Begin: Kernel Version Management' in packages
    assert 'TARGET_KERNEL_VERSION="$(uname -r | cut -d. -f1-3)*"' in packages
    post = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2', base_os
    )
    assert 'nice-dcv-el10' in post
    assert 'package_transaction yum install -y pulseaudio-utils' in post


def test_ubuntu2604_bake_selects_a_kernel_the_fsx_repo_has_a_module_for():
    setup = render('dcv-host-ami-builder/setup.sh.jinja2', 'ubuntu2604')
    assert 'systemctl enable --now snap.amazon-ssm-agent' in setup
    kernel = setup.split('# Begin: Kernel Version Management')[1]
    assert 'apt-cache pkgnames lustre-client-modules-' in kernel
    assert (
        'apt install -y lustre-client-utils "lustre-client-modules-${TARGET_KERNEL_ABI}"'
        in setup
    )
    packages = render(
        'dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2', 'ubuntu2604'
    )
    assert '# Begin: Kernel Version Management' not in packages
    checks = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2604')
    assert (
        'packages_installed lustre-client-utils "lustre-client-modules-$(uname -r)"'
        in checks
    )


def test_ubuntu2604_lustre_client_uses_the_resolute_suite():
    text = render('_templates/linux/fsx_lustre_client.jinja2', 'ubuntu2604')
    assert 'not available for Ubuntu 26.04' not in text
    assert 'fsxlustreclientrepo.list' in text
    assert 'lustre-client-modules-$(uname -r)' in text


@pytest.mark.parametrize('arch', ['amd64', 'arm64'])
@pytest.mark.parametrize('running', ['7.0.0-1010-aws', '7.0.0-1014-aws'])
def test_ubuntu2604_bake_pins_the_fsx_kernel(tmp_path, arch, running):
    # resolute carries one module ABI, 7.0.0-1014-aws: an image on another ABI moves to it
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2604',
        running,
        BAKE_STAGE='packages',
        KERNEL_ARCH=arch,
        REPO_ABIS='7.0.0-1014-aws',
        FSX_ABIS='7.0.0-1014-aws',
        IMAGE_ABIS='7.0.0-1010-aws 7.0.0-1012-aws 7.0.0-1013-aws 7.0.0-1014-aws',
    )
    assert code == 0, calls
    assert 'apt-mark hold linux-image-7.0.0-1014-aws' in calls
    assert 'Advanced options for Ubuntu>Ubuntu, with Linux 7.0.0-1014-aws' in calls
    if running == '7.0.0-1014-aws':
        assert 'apt install' not in calls
    else:
        assert 'apt install -y linux-image-7.0.0-1014-aws' in calls
        assert 'reboot_required Ubuntu kernel version change' in calls
    assert '\nreboot\n' not in calls


def test_ubuntu2604_bake_stops_when_no_kernel_has_a_module(tmp_path):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2604',
        '7.0.0-1010-aws',
        BAKE_STAGE='packages',
        KERNEL_ARCH='amd64',
        REPO_ABIS='7.0.0-1014-aws',
        FSX_ABIS='7.0.0-1014-aws',
        IMAGE_ABIS='7.0.0-1010-aws',
    )
    assert code == 1, calls
    assert 'Value=lustre-module-missing' in calls


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
@pytest.mark.parametrize(
    'running', ['6.12.0-211.7.1.el10_2.x86_64', '6.12.0-124.8.1.el10_1.aarch64']
)
def test_el10_desktop_keeps_and_locks_the_kernel_its_image_boots(
    tmp_path, base_os, running
):
    code, calls = run_set_kernel(tmp_path, base_os, running)
    assert code == 0, calls
    assert 'reboot' not in calls
    assert 'dnf install -y kernel' not in calls
    assert f'dnf versionlock kernel-{running} kernel-core-{running}' in calls
