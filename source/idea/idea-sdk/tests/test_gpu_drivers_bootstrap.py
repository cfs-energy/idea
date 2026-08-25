#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
GPU driver bootstrap must fail closed: a node that reports GPU hardware and ends up without a
usable driver aborts the bootstrap instead of joining the cluster driverless.
"""

import os
import shutil
import stat
import subprocess

import pytest

from ideasdk.config.soca_config import SocaConfig
from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
GPU_DRIVERS_TEMPLATE = '_templates/linux/gpu_drivers.jinja2'

BASH = shutil.which('bash')


def build_bootstrap_context(
    config, base_os: str = 'amazonlinux2023', instance_type: str = 'c5.large'
) -> BootstrapContext:
    return BootstrapContext(
        config=config,
        module_name='scheduler',
        module_id='scheduler',
        module_set='default',
        base_os=base_os,
        instance_type=instance_type,
    )


def render_gpu_drivers(
    config,
    base_os: str = 'amazonlinux2023',
    instance_type: str = 'c5.large',
    node_type: str = 'compute',
) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(GPU_DRIVERS_TEMPLATE)
    return template.render(
        context=build_bootstrap_context(
            config, base_os=base_os, instance_type=instance_type
        ),
        node_type=node_type,
    )


def build_pci_devices(base_dir: str, devices) -> str:
    pci_dir = os.path.join(base_dir, 'pci_devices')
    for index, (device_class, vendor) in enumerate(devices):
        device_dir = os.path.join(pci_dir, f'0000:00:0{index}.0')
        os.makedirs(device_dir, exist_ok=True)
        with open(os.path.join(device_dir, 'class'), 'w') as f:
            f.write(f'{device_class}\n')
        with open(os.path.join(device_dir, 'vendor'), 'w') as f:
            f.write(f'{vendor}\n')
    os.makedirs(pci_dir, exist_ok=True)
    return pci_dir


def build_fake_bin(base_dir: str, names) -> str:
    bin_dir = os.path.join(base_dir, 'bin')
    os.makedirs(bin_dir, exist_ok=True)
    for name in names:
        path = os.path.join(bin_dir, name)
        with open(path, 'w') as f:
            f.write('#!/bin/bash\nexit 0\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return bin_dir


HARNESS = """
set -o pipefail
function log_info () { echo "[INFO] ${1}"; }
function log_warning () { echo "[WARNING] ${1}"; }
function log_error () { echo "[ERROR] ${1}"; }
function instance_type () { echo -n "${TEST_INSTANCE_TYPE}"; }
function instance_family () { echo -n "${TEST_INSTANCE_TYPE%%.*}"; }
function instance_id () { echo -n "i-0abcdef0123456789"; }
function aws () { echo "aws $*" >> "${BOOTSTRAP_DIR}/aws-calls.log"; }
"""


def run_gpu_drivers(
    tmp_path, rendered: str, pci_devices, instance_type: str, driver_installed=False
):
    base_dir = str(tmp_path)
    bootstrap_dir = os.path.join(base_dir, 'bootstrap')
    os.makedirs(bootstrap_dir, exist_ok=True)
    pci_dir = build_pci_devices(base_dir, pci_devices)
    bin_dir = build_fake_bin(base_dir, ['nvidia-smi'] if driver_installed else [])

    script = os.path.join(base_dir, 'gpu_drivers.sh')
    with open(script, 'w') as f:
        f.write(f'#!/bin/bash\n{HARNESS}\n{rendered}\n')

    env = dict(os.environ)
    env.update(
        {
            'BOOTSTRAP_DIR': bootstrap_dir,
            'GPU_PCI_DEVICES_DIR': pci_dir,
            'TEST_INSTANCE_TYPE': instance_type,
            'PATH': f'{bin_dir}:{env["PATH"]}',
        }
    )
    result = subprocess.run(
        [BASH, script], env=env, capture_output=True, text=True, timeout=60
    )
    return result, bootstrap_dir


NVIDIA_3D_CONTROLLER = ('0x030200', '0x10de')
AMD_VGA_CONTROLLER = ('0x030000', '0x1002')
AMAZON_VGA_CONTROLLER = ('0x030000', '0x1d0f')
NVME_CONTROLLER = ('0x010802', '0x1d0f')


def test_gpu_instance_family_detection(context):
    config = context.config()
    assert build_bootstrap_context(config, instance_type='g4dn.xlarge').is_nvidia_gpu()
    assert (
        build_bootstrap_context(config, instance_type='g4ad.xlarge').is_amd_gpu()
        is True
    )
    assert (
        build_bootstrap_context(config, instance_type='c5.large').is_gpu_instance_type()
        is False
    )
    # the hyphenated families added this release resolve through the same split
    assert (
        build_bootstrap_context(
            config, instance_type='p6-b200.48xlarge'
        ).is_nvidia_gpu()
        is True
    )
    # instance family not in global-settings.gpu_settings.instance_families
    assert (
        build_bootstrap_context(
            config, instance_type='p99.48xlarge'
        ).is_gpu_instance_type()
        is False
    )


def test_fail_on_missing_gpu_driver_defaults_to_enabled(context):
    # fail_on_missing_driver is absent from the mock cluster config, as it is on clusters
    # deployed before the setting existed.
    assert (
        build_bootstrap_context(context.config()).fail_on_missing_gpu_driver() is True
    )


def test_gpu_detection_without_gpu_settings_config():
    config = SocaConfig(config={'cluster': {'aws': {'region': 'us-east-1'}}})
    bootstrap_context = build_bootstrap_context(config, instance_type='g4dn.xlarge')
    assert bootstrap_context.is_gpu_instance_type() is False
    assert bootstrap_context.is_nvidia_gpu() is False
    assert bootstrap_context.fail_on_missing_gpu_driver() is True


def test_unmapped_instance_family_renders_default_case_arm(context):
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    assert 'No GPU driver mapping for instance family' in rendered
    assert rendered.count('GPU_DRIVER_MAPPING_FOUND="no"') == 2


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_no_gpu_hardware_does_not_fail_bootstrap(context, tmp_path):
    rendered = render_gpu_drivers(context.config(), instance_type='c5.large')
    result, _ = run_gpu_drivers(
        tmp_path,
        rendered,
        [AMAZON_VGA_CONTROLLER, NVME_CONTROLLER],
        instance_type='c5.large',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'No GPU hardware detected' in result.stdout


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_gpu_hardware_with_driver_does_not_fail_bootstrap(context, tmp_path):
    rendered = render_gpu_drivers(context.config(), instance_type='c5.large')
    result, _ = run_gpu_drivers(
        tmp_path,
        rendered,
        [NVIDIA_3D_CONTROLLER],
        instance_type='g4dn.xlarge',
        driver_installed=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Verified nvidia GPU driver is installed' in result.stdout


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_gpu_hardware_without_driver_mapping_fails_bootstrap(context, tmp_path):
    # the bootstrap was rendered for a non gpu instance family, but a gpu was launched.
    rendered = render_gpu_drivers(context.config(), instance_type='c5.large')
    result, bootstrap_dir = run_gpu_drivers(
        tmp_path, rendered, [NVIDIA_3D_CONTROLLER], instance_type='p6-b200.48xlarge'
    )
    assert result.returncode == 1
    assert 'no GPU driver mapping exists for instance family: p6-b200' in result.stdout
    with open(os.path.join(bootstrap_dir, 'gpu_drivers_failed.txt')) as f:
        assert 'p6-b200' in f.read()
    with open(os.path.join(bootstrap_dir, 'aws-calls.log')) as f:
        assert 'Key=idea:BootstrapStatus,Value=gpu-driver-mapping-missing' in f.read()


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_amd_gpu_hardware_without_driver_fails_bootstrap(context, tmp_path):
    rendered = render_gpu_drivers(context.config(), instance_type='c5.large')
    result, _ = run_gpu_drivers(
        tmp_path, rendered, [AMD_VGA_CONTROLLER], instance_type='g4ad.xlarge'
    )
    assert result.returncode == 1
    assert 'has amd GPU hardware' in result.stdout


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_failed_driver_install_fails_bootstrap(context, tmp_path):
    # relocate the verify call so the driver mapping can be marked as found without running the
    # installers, which would download and build kernel modules.
    rendered = render_gpu_drivers(context.config(), instance_type='c5.large')
    rendered = rendered.replace(
        '\nverify_gpu_drivers\n',
        '\nGPU_DRIVER_MAPPING_FOUND="yes"\nverify_gpu_drivers\n',
    )
    result, _ = run_gpu_drivers(
        tmp_path, rendered, [NVIDIA_3D_CONTROLLER], instance_type='g4dn.xlarge'
    )
    assert result.returncode == 1
    assert 'did not produce a usable driver' in result.stdout


GLOBAL_SETTINGS_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'idea-administrator',
        'resources',
        'config',
        'templates',
    )
)

# AMD families: in instance_families but intentionally absent from the NVIDIA driver map
AMD_GPU_FAMILIES = {'g4ad'}


def load_real_gpu_settings() -> dict:
    import yaml
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(GLOBAL_SETTINGS_TEMPLATE_DIR))
    rendered = env.get_template('global-settings/settings.yml').render(
        enabled_modules=[
            'bastion-host',
            'scheduler',
            'virtual-desktop-controller',
            'metrics',
            'directoryservice',
        ],
        kms_key_id=None,
        metrics_provider='cloudwatch',
        base_os='amazonlinux2023',
    )
    return yaml.safe_load(rendered)['gpu_settings']


def extract_case_arm_families(rendered: str) -> set:
    import re

    families = set()
    for arms in re.findall(r'^\s{4}([a-z0-9|\-]+)\)\s*$', rendered, re.MULTILINE):
        families.update(arms.split('|'))
    return families


def test_gpu_family_lockstep_invariant(context):
    """
    every family in gpu_settings.instance_families must have a case arm in
    gpu_drivers.jinja2 and (unless AMD) an entry in nvidia_public_driver_versions -
    in the real global-settings template AND in the test fixture.
    """
    real = load_real_gpu_settings()
    real_families = set(real['instance_families'])
    real_nvidia = set(real['nvidia_public_driver_versions']) - {
        'ltsb_version',
        'production_version',
    }
    assert real_families - AMD_GPU_FAMILIES == real_nvidia

    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    case_families = extract_case_arm_families(rendered)
    missing_arms = real_families - case_families
    assert missing_arms == set(), (
        f'families with no case arm in gpu_drivers.jinja2: {missing_arms}'
    )

    # the fixture every unit test renders against must not drift from the template
    fixture_families = set(
        context.config().get_list(
            'global-settings.gpu_settings.instance_families', required=True
        )
    )
    fixture_nvidia = set(
        context.config()
        .get_config('global-settings.gpu_settings.nvidia_public_driver_versions')
        .as_plain_ordered_dict()
    ) - {'ltsb_version', 'production_version'}
    assert fixture_families == real_families
    assert fixture_nvidia == real_nvidia


RUNNING_KERNEL = '6.12.55-74.119.amzn2023.x86_64'
REPO_KERNEL = '6.12.100-125.179.amzn2023.x86_64'
STALE_KERNEL = '6.1.158-178.288.amzn2023.x86_64'

# dnf/rpm/crontab/reboot are shimmed as functions so they take precedence over anything on PATH:
# this block reboots the host on the alignment path.
KERNEL_HARNESS = """
function set_reboot_required () { echo -n "yes" > "${BOOTSTRAP_DIR}/reboot_required.txt"; }
function check_reboot_loop () { echo "[INFO] check_reboot_loop: ${1}"; }
function reboot () { echo "reboot" >> "${BOOTSTRAP_DIR}/reboot.log"; }
function grubby () { echo "grubby $*" >> "${BOOTSTRAP_DIR}/grubby.log"; }
function systemctl () { echo "systemctl $*" >> "${BOOTSTRAP_DIR}/systemctl.log"; }
function uname () {
  if [[ "${1}" == "-r" ]]; then echo -n "${TEST_KERNEL}"; else command uname "$@"; fi
}
function crontab () {
  if [[ "${1}" == "-l" ]]; then cat "${BOOTSTRAP_DIR}/crontab.txt" 2>/dev/null; else cat > "${BOOTSTRAP_DIR}/crontab.txt"; fi
}
function rpm () {
  echo "rpm $*" >> "${BOOTSTRAP_DIR}/rpm.log"
  if [[ -z "${TEST_KERNEL_PKG_OWNER}" ]]; then return 1; fi
  echo "${TEST_KERNEL_PKG_OWNER}"
}
function dnf () {
  echo "dnf $*" >> "${BOOTSTRAP_DIR}/dnf.log"
  if [[ "${1}" != "install" ]]; then return 0; fi
  local SPEC
  for SPEC in "$@"; do
    case "${SPEC}" in
      install|-y|--disableplugin=*) continue ;;
    esac
    # dnf fails the whole transaction when one spec does not resolve
    if [[ " ${TEST_DNF_RESOLVES} " != *" ${SPEC} "* ]]; then
      echo "Error: Unable to find a match: ${SPEC}"
      return 1
    fi
    if [[ "${SPEC}" == *-devel* ]]; then
      mkdir -p "${KERNEL_SRC_DIR}/${TEST_DNF_KERNEL}"
    elif [[ "${SPEC}" == kernel* && "${SPEC}" != *modules-extra* ]]; then
      touch "${KERNEL_BOOT_DIR}/vmlinuz-${TEST_DNF_KERNEL}"
    fi
  done
}
"""


def run_kernel_header_prep(
    tmp_path, rendered: str, env_extra: dict, stale_kernel=False, aligned_kernel=None
):
    base_dir = str(tmp_path)
    bootstrap_dir = os.path.join(base_dir, 'bootstrap')
    kernel_src_dir = os.path.join(base_dir, 'usr-src-kernels')
    kernel_boot_dir = os.path.join(base_dir, 'boot')
    for path in (bootstrap_dir, kernel_src_dir, kernel_boot_dir):
        os.makedirs(path, exist_ok=True)
    if aligned_kernel:
        with open(os.path.join(bootstrap_dir, 'gpu_kernel_alignment.txt'), 'w') as f:
            f.write(f'{aligned_kernel}\n')
    if stale_kernel:
        # header tree left behind by an older kernel of a different series, which is what the
        # driver build silently picked up
        os.makedirs(os.path.join(kernel_src_dir, STALE_KERNEL), exist_ok=True)
        open(os.path.join(kernel_boot_dir, f'vmlinuz-{STALE_KERNEL}'), 'w').close()
    pci_dir = build_pci_devices(base_dir, [NVIDIA_3D_CONTROLLER])

    # the driver installers download and build kernel modules: run only the block that prepares
    # the kernel headers ahead of them.
    block = rendered.split('function install_nvidia_grid_drivers')[0]
    script = os.path.join(base_dir, 'configure_dcv_host.sh')
    with open(script, 'w') as f:
        f.write(f'#!/bin/bash\n{HARNESS}\n{KERNEL_HARNESS}\n{block}\n')

    env = dict(os.environ)
    env.update(
        {
            'BOOTSTRAP_DIR': bootstrap_dir,
            'GPU_PCI_DEVICES_DIR': pci_dir,
            'TEST_INSTANCE_TYPE': 'g4dn.xlarge',
            'SCRIPT_DIR': base_dir,
            'KERNEL_SRC_DIR': kernel_src_dir,
            'KERNEL_MODULES_DIR': os.path.join(base_dir, 'lib-modules'),
            'KERNEL_BOOT_DIR': kernel_boot_dir,
            'TEST_KERNEL': RUNNING_KERNEL,
            'TEST_KERNEL_PKG_OWNER': 'kernel6.12',
            'TEST_DNF_RESOLVES': 'dkms',
            'TEST_DNF_KERNEL': '',
        }
    )
    env.update(env_extra)
    result = subprocess.run(
        [BASH, script], env=env, capture_output=True, text=True, timeout=60
    )
    return result, bootstrap_dir


def read_file(*parts) -> str:
    path = os.path.join(*parts)
    if not os.path.exists(path):
        return ''
    with open(path) as f:
        return f.read()


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_kernel_devel_installed_from_the_running_kernel_package_family(
    context, tmp_path
):
    # amazonlinux packages a non-default kernel series as its own family: the headers for a 6.12
    # kernel are kernel6.12-devel, and kernel-devel does not resolve for it at all.
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    result, bootstrap_dir = run_kernel_header_prep(
        tmp_path,
        rendered,
        {
            'TEST_DNF_RESOLVES': (
                f'dkms kernel6.12-devel-{RUNNING_KERNEL} '
                f'kernel6.12-modules-extra-{RUNNING_KERNEL}'
            ),
            'TEST_DNF_KERNEL': RUNNING_KERNEL,
        },
        stale_kernel=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    dnf_log = read_file(bootstrap_dir, 'dnf.log')
    assert f'install -y kernel6.12-devel-{RUNNING_KERNEL}' in dnf_log
    assert f'install -y kernel6.12-modules-extra-{RUNNING_KERNEL}' in dnf_log
    assert f'Installed kernel6.12-devel-{RUNNING_KERNEL}' in result.stdout
    # headers for the running kernel were obtained, so no kernel change and no failure
    assert read_file(bootstrap_dir, 'reboot.log') == ''
    assert read_file(bootstrap_dir, 'gpu_drivers_failed.txt') == ''


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_missing_kernel_devel_fails_bootstrap_instead_of_building(context, tmp_path):
    # no repo carries headers for the running kernel and no other kernel of its series is
    # installed: abort loudly rather than build against the stale tree of another series.
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    result, bootstrap_dir = run_kernel_header_prep(
        tmp_path, rendered, {}, stale_kernel=True
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'no kernel-devel is available for the running kernel' in result.stdout
    assert 'no kernel-devel is available' in read_file(
        bootstrap_dir, 'gpu_drivers_failed.txt'
    )
    assert 'Key=idea:BootstrapStatus,Value=gpu-kernel-devel-missing' in read_file(
        bootstrap_dir, 'aws-calls.log'
    )
    # the stale 6.1 tree is not a valid build target, so the host must not be booted into it
    assert read_file(bootstrap_dir, 'reboot.log') == ''


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_superseded_ami_kernel_boots_the_kernel_the_repo_carries(context, tmp_path):
    # the running kernel has aged out of the repo: install the kernel the repo does carry and
    # re-enter the calling bootstrap script after a reboot.
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    result, bootstrap_dir = run_kernel_header_prep(
        tmp_path,
        rendered,
        {
            'TEST_DNF_RESOLVES': (
                'dkms kernel6.12 kernel6.12-devel kernel6.12-modules-extra'
            ),
            'TEST_DNF_KERNEL': REPO_KERNEL,
        },
        stale_kernel=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'reboot' in read_file(bootstrap_dir, 'reboot.log')
    assert read_file(bootstrap_dir, 'reboot_required.txt') == 'yes'
    # the same series only: the stale 6.1 header tree is present and must not be selected
    assert read_file(bootstrap_dir, 'gpu_kernel_alignment.txt').strip() == REPO_KERNEL
    boot_dir = os.path.join(str(tmp_path), 'boot')
    assert f'--set-default {boot_dir}/vmlinuz-{REPO_KERNEL}' in read_file(
        bootstrap_dir, 'grubby.log'
    )
    resume_entry = f'@reboot /bin/bash {tmp_path}/configure_dcv_host.sh crontab'
    assert resume_entry in read_file(bootstrap_dir, 'crontab.txt')
    assert '--disableplugin=versionlock' in read_file(bootstrap_dir, 'dnf.log')
    assert read_file(bootstrap_dir, 'gpu_drivers_failed.txt') == ''


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_kernel_alignment_is_attempted_once(context, tmp_path):
    # a kernel that still has no headers after the alignment reboot must fail, not reboot again
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    result, bootstrap_dir = run_kernel_header_prep(
        tmp_path,
        rendered,
        {
            'TEST_DNF_RESOLVES': (
                'dkms kernel6.12 kernel6.12-devel kernel6.12-modules-extra'
            ),
            'TEST_DNF_KERNEL': REPO_KERNEL,
        },
        aligned_kernel=REPO_KERNEL,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f'Already booted {REPO_KERNEL} to obtain kernel headers' in result.stdout
    assert read_file(bootstrap_dir, 'reboot.log') == ''
    assert 'Key=idea:BootstrapStatus,Value=gpu-kernel-devel-missing' in read_file(
        bootstrap_dir, 'aws-calls.log'
    )


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_kernel_header_prep_skipped_without_nvidia_hardware(context, tmp_path):
    # rendered for an nvidia family, launched on hardware that has no nvidia gpu: no kernel work
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    base_dir = str(tmp_path)
    bootstrap_dir = os.path.join(base_dir, 'bootstrap')
    os.makedirs(bootstrap_dir, exist_ok=True)
    pci_dir = build_pci_devices(base_dir, [AMAZON_VGA_CONTROLLER])
    block = rendered.split('function install_nvidia_grid_drivers')[0]
    script = os.path.join(base_dir, 'configure_dcv_host.sh')
    with open(script, 'w') as f:
        f.write(f'#!/bin/bash\n{HARNESS}\n{KERNEL_HARNESS}\n{block}\n')
    env = dict(os.environ)
    env.update(
        {
            'BOOTSTRAP_DIR': bootstrap_dir,
            'GPU_PCI_DEVICES_DIR': pci_dir,
            'TEST_INSTANCE_TYPE': 'g4dn.xlarge',
            'SCRIPT_DIR': base_dir,
            'TEST_KERNEL': RUNNING_KERNEL,
            'TEST_KERNEL_PKG_OWNER': '',
            'TEST_DNF_RESOLVES': 'dkms',
            'TEST_DNF_KERNEL': '',
        }
    )
    result = subprocess.run(
        [BASH, script], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'No NVIDIA GPU hardware detected' in result.stdout
    assert read_file(bootstrap_dir, 'reboot.log') == ''
    assert 'kernel-devel' not in read_file(bootstrap_dir, 'dnf.log')


X_CONFIG_START = 'log_info "X server configuration for GPU start..."'
X_CONFIG_END = 'log_info "X server configuration for GPU end..."'


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_x_server_configuration_skipped_when_nvidia_xconfig_is_missing(
    context, tmp_path
):
    # a failed driver install leaves no nvidia-xconfig, and an unguarded run of it reports
    # "--preserve-busid: command not found" in the log an operator is reading at that moment.
    rendered = render_gpu_drivers(context.config(), instance_type='g4dn.xlarge')
    fragments = [
        X_CONFIG_START + part.split(X_CONFIG_END)[0] + X_CONFIG_END
        for part in rendered.split(X_CONFIG_START)[1:]
    ]
    # grid and public driver installers
    assert len(fragments) == 2
    for index, fragment in enumerate(fragments):
        script = os.path.join(str(tmp_path), f'x_config_{index}.sh')
        with open(script, 'w') as f:
            f.write(
                f'#!/bin/bash\n{HARNESS}\n'
                'function which () { if [[ "${1}" == "nvidia-xconfig" ]]; then return 1; fi; command which "$@"; }\n'
                'function set_reboot_required () { :; }\n'
                f'function configure_x_for_gpu () {{\n{fragment}\n}}\n'
                'configure_x_for_gpu\n'
            )
        env = dict(os.environ)
        env.update({'BOOTSTRAP_DIR': str(tmp_path)})
        result = subprocess.run(
            [BASH, script], env=env, capture_output=True, text=True, timeout=60
        )
        output = result.stdout + result.stderr
        assert 'command not found' not in output, output
        assert 'nvidia-xconfig not found' in output


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_fail_on_missing_driver_disabled_continues(context, tmp_path):
    config = context.config()
    config.put('global-settings.gpu_settings.fail_on_missing_driver', False)
    try:
        assert build_bootstrap_context(config).fail_on_missing_gpu_driver() is False
        rendered = render_gpu_drivers(config, instance_type='c5.large')
        result, _ = run_gpu_drivers(
            tmp_path, rendered, [NVIDIA_3D_CONTROLLER], instance_type='g4dn.xlarge'
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'fail_on_missing_driver is disabled' in result.stdout
    finally:
        config.put('global-settings.gpu_settings.fail_on_missing_driver', True)
