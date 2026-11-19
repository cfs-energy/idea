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
        'signature',
        'signature-unreachable',
        'signature-once',
        'no-gpg',
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
  [[ "$*" != '-v gpg' || "$SCENARIO" != no-gpg ]] || return 1
  [[ "$*" != '-v aws' || "$SCENARIO" != no-cli ]] || return 1
  builtin command "$@"
}
which() { command -v "$@"; }
sleep() { :; }
uname() { echo aarch64; }
curl() {
  [[ "$SCENARIO" != curl ]] || return 22
  [[ "$* " != *'.sig '* || "$SCENARIO" != signature-unreachable ]] || return 7
  echo test-region
}
# a valid signature names the primary key fingerprint last on its VALIDSIG status line
gpg() {
  [[ " $* " == *' --verify '* ]] || return 0
  local fingerprint=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
  [[ "$SCENARIO" != signature ]] || fingerprint=0000000000000000000000000000000000000000
  # a release published between the two downloads: the first pair does not match
  if [[ "$SCENARIO" == signature-once && ! -f "$UPDATED.sig-seen" ]]; then
    touch "$UPDATED.sig-seen"
    fingerprint=0000000000000000000000000000000000000000
  fi
  echo "[GNUPG:] VALIDSIG $fingerprint 2026-01-01 0 0 4 0 1 10 00 $fingerprint"
}
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
    if scenario in ('success', 'update-lag', 'signature-once', 'no-gpg'):
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
        if scenario.startswith('signature'):
            # nothing unverified is unpacked or installed
            assert 'unzip' not in calls and not (tmp_path / 'aws').exists()
            assert 'userdata failed: aws-cli' in log
        if scenario == 'signature':
            assert 'is not signed by the AWS CLI key FB5DB77F' in log
        if scenario == 'signature-unreachable':
            assert 'Could not download the AWS CLI signature' in log
    if scenario == 'signature-once':
        assert 'fetching it and its signature again' in log
    if scenario == 'no-gpg':
        # the package index is refreshed before gpg is installed
        assert calls.index('update') < calls.index('install -y gpg')
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
    assert 'kernel-headers' not in calls
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
        assert usb.index('install_matching_kernel_devel ||') < usb.index(
            'DCV_USB_DRIVER_INSTALLER'
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
        line == '-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 '
        '-o Acquire::Retries=2 '
        '-o APT::Update::Error-Mode=any -o DPkg::Lock::Timeout=300 update'
        for line in calls
    )


@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404', 'ubuntu2604'])
@pytest.mark.parametrize(
    'template', ['nfs_utils', 'stunnel', 'efs_mount_helper', 'jq', 'system_packages']
)
def test_ubuntu_entry_paths_refresh_before_install(base_os, template):
    text = render(f'_templates/linux/{template}.jinja2', base_os=base_os)
    assert text.index('apt_update || log_warning') < text.index('apt install')


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


@pytest.mark.parametrize('failure', ['devel', 'driver', 'installer'])
def test_optional_usb_failure_warns_and_bootstrap_continues(tmp_path, failure):
    text = render(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        base_os='rocky9',
        lustre=False,
    )
    body = text.split('function install_usb_support() {', 1)[1].split('\n}', 1)[0]
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''set -e
source "{COMMON}"
modinfo() {{ return 1; }}
install_matching_kernel_devel() {{ [[ "{failure}" != devel ]]; }}
which() {{ [[ "{failure}" != installer ]] && echo false; }}
function install_usb_support() {{{body}
}}
install_usb_support
echo BOOTSTRAP_CONTINUED
''',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '[WARNING]' in result.stdout
    assert 'BOOTSTRAP_CONTINUED' in result.stdout


@pytest.mark.parametrize('required', [True, False])
@pytest.mark.parametrize('driver', ['grid', 'public'])
def test_el_gpu_missing_devel_honors_fail_setting(tmp_path, required, driver):
    from test_bootstrap_shell_syntax import Config

    config = Config()
    config.put('global-settings.gpu_settings.fail_on_missing_driver', required)
    text = render(
        '_templates/linux/gpu_drivers.jinja2',
        base_os='rocky9',
        instance_type='g5.xlarge',
        config=config,
    )
    name = f'install_nvidia_{driver}_drivers'
    body = text.split('function ' + name, 1)[1].split('{', 1)[1].split('\n}', 1)[0]
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''set -e
source "{COMMON}"
command() {{ [[ "$*" != '-v nvidia-smi' ]] && builtin command "$@"; }}
install_matching_kernel_devel() {{ return 1; }}
fail_gpu_drivers() {{ echo "FATAL:$1"; exit 1; }}
function {name} () {{{body}
}}
{name}
echo BOOTSTRAP_CONTINUED
''',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == int(required), result.stdout + result.stderr
    assert ('BOOTSTRAP_CONTINUED' in result.stdout) != required
    assert ('FATAL:gpu-kernel-devel-missing' in result.stdout) == required
    assert ('[WARNING]' in result.stdout) != required


@pytest.mark.parametrize(
    'template',
    ['system_packages', 'universe', 'jq', 'nfs_utils', 'stunnel', 'efs_mount_helper'],
)
def test_apt_index_failure_uses_existing_index(template):
    text = render(f'_templates/linux/{template}.jinja2', base_os='ubuntu2404')
    refresh = next(
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith('apt_update')
    )
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''set -e
source "{COMMON}"
apt_update() {{ return 1; }}
{refresh}
echo INSTALL_REACHED
''',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert '[WARNING]' in result.stdout and 'INSTALL_REACHED' in result.stdout


# EC2 accepts at most 16384 bytes of user data before base64. The largest script either
# builder makes must leave 4096 bytes of that free.
USER_DATA_LIMIT = 16384
USER_DATA_MARGIN = 4096
LINUX_BASE_OS = [
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
    'ubuntu2604',
]


def largest_user_data_inputs():
    """Worst case of what the product passes: the long GovCloud no_proxy list from the
    cluster settings template, a long proxy URL, a long package URI, the four OpenLDAP
    secret ARNs, and every install command any caller uses."""
    region, suffix = 'us-gov-west-1', 'amazonaws.com'
    services = (
        's3 s3.dualstack dynamodb sqs ec2 secretsmanager sns cloudformation '
        'elasticloadbalancing monitoring logs ssm application-autoscaling events '
        'kinesis autoscaling'
    ).split()
    no_proxy = ','.join(
        ['127.0.0.1', '169.254.169.254', 'localhost', f'{region}.local']
        + [f'{region}.vpce.{suffix}', f'{region}.es.{suffix}', f'{region}.elb.{suffix}']
        + [f'{name}.{region}.{suffix}' for name in services]
    )
    proxy = 'http://proxy.' + 'x' * 60 + '.example.invalid:3128'
    arn = f'arn:aws-us-gov:secretsmanager:{region}:123456789012:secret:' + 'x' * 120
    return dict(
        aws_region=region,
        bootstrap_package_uri='s3://'
        + 'b' * 63
        + '/idea/'
        + 'm' * 40
        + '/bootstrap/'
        + 'p' * 120
        + '.tar.gz',
        install_commands=[
            f'/bin/bash {component}/setup.sh'
            for component in (
                'compute-node',
                'compute-node-ami-builder',
                'dcv-host-ami-builder',
                'virtual-desktop-host-linux',
                'openldap-server',
            )
        ],
        infra_config={
            f'LDAP_{name}_SECRET_ARN': arn
            for name in (
                'ROOT_USERNAME',
                'ROOT_PASSWORD',
                'TLS_CERTIFICATE',
                'TLS_PRIVATE_KEY',
            )
        },
        proxy_config={'http_proxy': proxy, 'https_proxy': proxy, 'no_proxy': no_proxy},
    )


def test_largest_user_data_fits_the_ec2_limit():
    inputs = largest_user_data_inputs()
    module = COMMON.parents[2] / 'ideactl/src/cdk/userdata.ts'
    sizes = {}
    for base_os in LINUX_BASE_OS:
        for substitution in (True, False):
            sdk = BootstrapUserDataBuilder(
                base_os=base_os,
                substitution_support=substitution,
                **{
                    **inputs,
                    'infra_config': inputs['infra_config'] if substitution else None,
                },
            ).build()
            params = dict(
                baseOs=base_os,
                awsRegion=inputs['aws_region'],
                bootstrapPackageUri=inputs['bootstrap_package_uri'],
                installCommands=inputs['install_commands'],
                infraConfig=inputs['infra_config'] if substitution else None,
                proxyConfig=inputs['proxy_config'],
                substitutionSupport=substitution,
            )
            cdk = subprocess.check_output(
                [
                    'node',
                    '--input-type=module',
                    '-e',
                    f'import {{ buildBootstrapUserData }} from {json.dumps(str(module))}; process.stdout.write(buildBootstrapUserData({json.dumps(params)}));',
                ],
                text=True,
            )
            sizes[(base_os, substitution, 'sdk')] = len(sdk.encode())
            sizes[(base_os, substitution, 'cdk')] = len(cdk.encode())
    largest = max(sizes.values())
    assert largest <= USER_DATA_LIMIT - USER_DATA_MARGIN, sizes
    # the check is in both builders, in the form the CloudFormation variant leaves alone
    assert 'verify_aws_cli_signature || return $?' in cdk
    assert 'AWS_CLI_KEY_FINGERPRINT=FB5DB77FD5C118B80511ADA8A6310ACC4672475C' in cdk
