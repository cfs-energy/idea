"""Run bootstrap failure paths with fake commands, never host packages or AWS."""

import os
import json
from pathlib import Path
import subprocess

import pytest

from ideasdk.bootstrap import BootstrapUserDataBuilder
from test_image_bake_bootstrap import render

COMMON = (
    Path(__file__).resolve().parents[2] / 'idea-bootstrap/common/bootstrap_common.sh'
)


@pytest.mark.parametrize('implementation', ['sdk', 'cdk'])
@pytest.mark.parametrize('substitution', [True, False])
@pytest.mark.parametrize(
    'scenario',
    [
        'success',
        'update-lag',
        'update',
        'curl',
        'unzip',
        'installer',
        'no-cli',
        'download',
        'archive',
    ],
)
def test_userdata_prerequisites(tmp_path, implementation, substitution, scenario):
    body = (
        BootstrapUserDataBuilder(
            base_os='ubuntu2604',
            aws_region='us-east-1',
            bootstrap_package_uri='s3://bucket/bootstrap.tar.gz',
            install_commands=['echo SETUP_REACHED'],
            substitution_support=substitution,
        )
        .build()
        .replace('${!', '${')
    )
    if implementation == 'cdk':
        module = COMMON.parents[2] / 'ideactl/src/cdk/userdata.ts'
        params = dict(
            baseOs='ubuntu2604',
            awsRegion='us-east-1',
            bootstrapPackageUri='s3://bucket/bootstrap.tar.gz',
            installCommands=['echo SETUP_REACHED'],
            substitutionSupport=substitution,
        )
        cdk = subprocess.check_output(
            [
                'node',
                '--input-type=module',
                '-e',
                f'import {{ buildBootstrapUserData }} from {json.dumps(str(module))}; console.log(buildBootstrapUserData({json.dumps(params)}));',
            ],
            text=True,
        ).rstrip('\n')
        assert cdk.replace('${!', '${') == body.rstrip('\n')
        body = cdk.replace('${!', '${')
    body = body.replace('/root/bootstrap', str(tmp_path))
    body = body.replace('/usr/local/aws-cli', str(tmp_path / 'cli'))
    body = body.replace(' -x /bin/aws', f' -x {tmp_path}/bin/aws')
    # The installer and extraction only create files inside tmp_path.
    mocks = r"""
command() {
  [[ "$*" != '-v unzip' ]] || return 1
  [[ "$*" != '-v aws' || "$SCENARIO" != no-cli ]] || return 1
  builtin command "$@"
}
which() { command -v "$@"; }
sleep() { :; }
uname() { echo aarch64; }
curl() { [[ "$SCENARIO" != curl ]] || return 22; echo test-region; }
attempts=0
apt-get() {
  echo "$*" >> "$CALLS"
  if [[ " $* " == *' update '* ]]; then
    [[ "$SCENARIO" != update ]] || return 100
    attempts=$((attempts + 1))
    if [[ "$SCENARIO" == update-lag && $attempts -lt 3 ]]; then
      echo 'E: Failed to fetch package index'
      return 100
    fi
    touch "$UPDATED"
  else
    [[ -f "$UPDATED" ]] || return 100
  fi
}
apt() { apt-get "$@"; }
unzip() {
  [[ "$SCENARIO" != unzip ]] || return 9
  mkdir -p aws
  printf '#!/bin/bash\n[[ "$SCENARIO" != installer ]]\n' > aws/install
  chmod +x aws/install
}
aws() {
  [[ "$SCENARIO" != no-cli ]] || return 127
  echo "$*" >> "$CALLS"
  if [[ " $* " == *' s3 cp '* ]]; then [[ "$SCENARIO" != download ]] || return 1; fi
}
tar() { [[ "$SCENARIO" != archive ]] || return 2; }
export -f aws curl tar
"""
    result = subprocess.run(
        ['bash', '-c', mocks + body],
        text=True,
        capture_output=True,
        env=dict(
            os.environ,
            SCENARIO=scenario,
            CALLS=str(tmp_path / 'calls'),
            UPDATED=str(tmp_path / 'updated'),
        ),
        timeout=10,
    )
    log = (tmp_path / 'logs/userdata.log').read_text()
    calls = (tmp_path / 'calls').read_text() if (tmp_path / 'calls').exists() else ''
    if scenario in ('success', 'update-lag'):
        assert result.returncode == 0, log
        assert 'SETUP_REACHED' in log
        assert calls.index('update') < calls.index('install -y unzip')
        assert 'APT::Update::Error-Mode=any' in calls
    else:
        assert result.returncode != 0, log
        assert '+ echo SETUP_REACHED' not in log
        assert 'userdata failed:' in log
        if scenario in ('download', 'archive'):
            assert 'Value=bootstrap-download-failed' in calls
    if scenario == 'update-lag':
        assert calls.count('update') == 3
        assert '[retried] E: Failed to fetch package index' in log
        assert '\nE: Failed to fetch package index' not in log
    if scenario == 'no-cli':
        assert 'create-tags' not in calls
    if scenario == 'update':
        assert calls.count('update') == 4
        assert 'install -y unzip' not in calls


@pytest.mark.parametrize('scenario', ['lag', 'missing', 'wrong-tree'])
def test_matching_kernel_devel(tmp_path, scenario):
    script = f'''source "{COMMON}"
BOOTSTRAP_DIR="{tmp_path}"
uname() {{ echo 6.12.0-211.65.1.el10_2.x86_64; }}
sleep() {{ echo "$1" >> "{tmp_path}/sleeps"; }}
rpm() {{ echo kernel-devel-6.12.0-211.64.1.el10_2.x86_64; return 1; }}
dnf() {{
  echo "$*" >> "{tmp_path}/calls"
  [[ "$1" == clean ]] && return 0
  count=$((count + 1))
  if [[ {scenario} == lag && $count -ge 3 ]]; then
    mkdir -p "{tmp_path}/kernels/$(uname -r)"
    touch "{tmp_path}/kernels/$(uname -r)/Makefile"
    return 0
  fi
  [[ {scenario} == wrong-tree ]] && return 0
  echo 'Error: Unable to find a match: kernel-devel-6.12.0-211.65.1.el10_2.x86_64'
  return 1
}}
KERNEL_SRC_DIR="{tmp_path}/kernels"
count=0
install_matching_kernel_devel
'''
    result = subprocess.run(
        ['bash', '-c', script], text=True, capture_output=True, timeout=10
    )
    assert result.returncode == (0 if scenario == 'lag' else 1), (
        result.stdout + result.stderr
    )
    calls = (tmp_path / 'calls').read_text()
    assert 'kernel-devel-6.12.0-211.65.1.el10_2.x86_64' in calls
    assert 'kernel-headers-6.12.0-211.65.1.el10_2.x86_64' in calls
    assert '--skip-broken' not in calls and '--nobest' not in calls
    sleeps = tmp_path / 'sleeps'
    assert (sleeps.read_text().splitlines() if sleeps.exists() else []) == (
        []
        if scenario == 'wrong-tree'
        else ['15', '30', '60'][: 2 if scenario == 'lag' else 3]
    )
    if scenario != 'lag':
        assert 'running kernel 6.12.0-211.65.1' in result.stdout
        assert 'kernel-devel-6.12.0-211.64.1' in result.stdout


@pytest.mark.parametrize(
    'base_os', ['rhel8', 'rocky8', 'rhel9', 'rocky9', 'rhel10', 'rocky10']
)
def test_el_module_builds_require_matching_devel(base_os):
    for template in [
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
    ]:
        text = render(template, base_os=base_os, lustre=False)
        usb = text[text.index('if ! modinfo eveusb') :]
        assert usb.index('install_matching_kernel_devel || exit 1') < usb.index(
            '$DCV_USB_DRIVER_INSTALLER --quiet'
        )
    gpu = render(
        '_templates/linux/gpu_drivers.jinja2',
        base_os=base_os,
        instance_type='g5.xlarge',
    )
    for function in ['install_nvidia_grid_drivers', 'install_nvidia_public_drivers']:
        body = gpu.split('function ' + function, 1)[1].split('\n}', 1)[0]
        assert body.index('install_matching_kernel_devel') < body.index('--dkms')


@pytest.mark.parametrize('recover', [True, False])
def test_apt_update_retries_without_rpm_options(tmp_path, recover):
    script = f'''source "{COMMON}"
count=0
sleep() {{ :; }}
apt-get() {{
  echo "$*" >> "{tmp_path}/calls"
  count=$((count + 1))
  [[ "{recover}" == True && $count -ge 3 ]]
}}
apt_update
'''
    result = subprocess.run(
        ['bash', '-c', script], text=True, capture_output=True, timeout=10
    )
    assert result.returncode == (0 if recover else 1)
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert len(calls) == (3 if recover else 4)
    assert all(
        line == '-o APT::Update::Error-Mode=any -o DPkg::Lock::Timeout=300 update'
        for line in calls
    )


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404', 'ubuntu2604'])
@pytest.mark.parametrize(
    'template', ['nfs_utils', 'stunnel', 'efs_mount_helper', 'jq', 'system_packages']
)
def test_ubuntu_entry_paths_refresh_before_install(base_os, template):
    text = render(f'_templates/linux/{template}.jinja2', base_os=base_os)
    assert text.index('apt_update || exit 1') < text.index('apt install')


@pytest.mark.parametrize('substitution', [True, False])
def test_builder_userdata_failure_uses_existing_status_tag(substitution):
    text = BootstrapUserDataBuilder(
        base_os='ubuntu2604',
        aws_region='us-east-1',
        bootstrap_package_uri='s3://bucket/bootstrap.tar.gz',
        install_commands=['/bin/bash dcv-host-ami-builder/setup.sh'],
        substitution_support=substitution,
    ).build()
    assert 'BOOTSTRAP_FAILURE_TAG="idea:AmiBuilderStatus"' in text
    assert 'status="failed:userdata-$1"' in text
