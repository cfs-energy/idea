"""
every host bootstrap package must parse on every supported base_os. a syntax error in an
os-specific branch only surfaces at boot, on that os, so each package a desktop host or
compute node receives is rendered for each base_os and instance class and then parsed:
bash -n always, shellcheck -S error when installed, and the windows powershell parser when
pwsh is installed.
"""

import os
import pathlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

from ideadatamodel import SocaAnyPayload, VirtualDesktopBaseOS, constants
from ideadatamodel.scheduler.scheduler_model import (
    SocaFSxLustreConfig,
    SocaJob,
    SocaJobParams,
    SocaJobProvisioningOptions,
)
from ideadatamodel.common.common_model import SocaMemory, SocaMemoryUnit
from ideasdk.bootstrap import BootstrapPackageBuilder
from ideasdk.config.soca_config import SocaConfig
from ideasdk.context import BootstrapContext
from ideatestutils import MockConfig

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
BASH = shutil.which('bash')
SHELLCHECK = shutil.which('shellcheck')
PWSH = shutil.which('pwsh')
SHELLCHECKED = set()

# each host role has its own base_os allowlist: desktops (and desktop images) are limited to
# the software stack enum, which has no EL10 since there are no DCV packages for it.
COMPUTE_BASE_OS = tuple(constants.ALLOWED_BASEOS)
DESKTOP_BASE_OS = tuple(
    base_os.value
    for base_os in VirtualDesktopBaseOS
    if base_os.value not in constants.EOL_BASEOS
)
LINUX_BASE_OS = tuple(
    b
    for b in dict.fromkeys(COMPUTE_BASE_OS + DESKTOP_BASE_OS)
    if not b.startswith('windows')
)
WINDOWS_BASE_OS = tuple(b for b in DESKTOP_BASE_OS if b.startswith('windows'))

# the templates branch on the instance family (gpu vendor), cpu architecture is resolved at
# runtime - one family per jinja branch, on both architectures.
INSTANCE_TYPES = (
    'm7i.large',  # x86_64, no gpu
    'm7g.large',  # arm64, no gpu
    'g5.xlarge',  # x86_64, nvidia
    'g5g.xlarge',  # arm64, nvidia
    'g4ad.xlarge',  # x86_64, amd
)


SETTINGS_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'ideactl',
        'resources',
        'config',
        'templates',
    )
)


class Config(SocaConfig):
    """
    the mock cluster with the shipped global-settings (package urls per os and architecture),
    plus the controller keys and cluster lookups the host templates make.
    """

    def __init__(self):
        config = MockConfig().get_config()
        env = Environment(loader=FileSystemLoader(SETTINGS_TEMPLATE_DIR))
        config['global-settings'] = yaml.safe_load(
            env.get_template('global-settings/settings.yml').render(
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
        )
        config['virtual-desktop-controller'] = {
            'events_sqs_queue_url': 'https://sqs.us-east-1.amazonaws.com/123456789012/events',
            'dcv_broker': {
                'agent_communication_port': 8445,
                'client_communication_port': 8446,
                'gateway_communication_port': 8447,
                'session_token_validity': 1440,
            },
            'dcv_session': {'idle_timeout': 1440, 'idle_timeout_warning': 300},
        }
        super().__init__(config=config)

    def get_cluster_internal_endpoint(self) -> str:
        return 'https://internal.example.invalid'

    def get_cluster_external_endpoint(self) -> str:
        return 'https://external.example.invalid'


# one job per jinja branch of the compute node bootstrap: scratch storage, efa and
# hyperthreading, and an existing or new fsx for lustre file system.
JOB_PARAMS = {
    'defaults': dict(),
    'efa, ht, scratch': dict(
        enable_efa_support=True,
        enable_ht_support=True,
        scratch_storage_size=SocaMemory(value=100, unit=SocaMemoryUnit.GB),
    ),
    'existing fsx': dict(
        fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx='fs-0123')
    ),
    'new fsx': dict(fsx_lustre=SocaFSxLustreConfig(enabled=True)),
}


def job(**params) -> SocaJob:
    return SocaJob(
        cluster_name='idea-mock',
        job_id='1',
        job_uid='job-uid',
        job_group='job-group',
        job_name='sample-job',
        project='sample-project',
        owner='sample-user',
        owner_email='user@example.invalid',
        queue='normal',
        params=SocaJobParams(
            **{
                'compute_stack': 'sample-stack',
                'scratch_storage_size': SocaMemory(value=0, unit=SocaMemoryUnit.GB),
                'fsx_lustre': SocaFSxLustreConfig(enabled=False),
                **params,
            }
        ),
        provisioning_options=SocaJobProvisioningOptions(stack_uuid='stack-uuid'),
    )


def packages(base_os: str):
    """(label, component, vars) for each package a host of this base_os receives."""
    session_vars = dict(
        session_owner='sample-user',
        idea_session_id='session-id',
        project='sample-project',
        base_os=base_os,
        bedrock_env={},
        bedrock_model_messages=[],
        auto_mode_environment='',
        claude_code_output_style='default',
        claude_code_permission_mode='default',
        dcv_host_ready_message='{}',
    )
    if base_os.startswith('windows'):
        return [
            (
                'virtual-desktop-host-windows',
                'virtual-desktop-host-windows',
                dict(session_vars, session=SocaAnyPayload(type='console')),
            ),
            ('windows image builder', 'dcv-host-ami-builder-windows', {}),
        ]
    ami_vars = dict(ami_dir='/apps/idea-mock/ami', ami_name='sample-ami')
    result = []
    if base_os in DESKTOP_BASE_OS:
        result += [
            (
                f'virtual-desktop-host-linux ({session_type})',
                'virtual-desktop-host-linux',
                dict(session_vars, session=SocaAnyPayload(type=session_type)),
            )
            for session_type in ('console', 'virtual')
        ]
        result.append(
            (
                'dcv-host-ami-builder',
                'dcv-host-ami-builder',
                dict(ami_vars, session=SocaAnyPayload(type='console')),
            )
        )
    if base_os in COMPUTE_BASE_OS:
        result += [
            (
                f'compute-node ({name})',
                'compute-node',
                dict(
                    job=job(**params),
                    project='sample-project',
                    queue_profile='compute',
                    job_directory='/apps/idea-mock/jobs/1',
                ),
            )
            for name, params in JOB_PARAMS.items()
        ]
        result.append(
            (
                'compute-node-ami-builder',
                'compute-node-ami-builder',
                dict(ami_vars, enabled_drivers=('efa', 'fsx_lustre')),
            )
        )
    if base_os == 'amazonlinux2023':
        result += [
            (component, component, {})
            for component in (
                'bastion-host',
                'cluster-manager',
                'dcv-broker',
                'dcv-connection-gateway',
                'openldap-server',
                'scheduler',
                'virtual-desktop-controller',
            )
        ]
    return result


def render(
    tmp_path, base_os: str, instance_type: str, component: str, variables
) -> list:
    context = BootstrapContext(
        config=Config(),
        module_name='virtual-desktop-controller',
        module_id='vdc',
        module_set='default',
        base_os=base_os,
        instance_type=instance_type,
    )
    for key, value in variables.items():
        setattr(context.vars, key, value)
    basename = f'{component}-{len(os.listdir(tmp_path))}'
    BootstrapPackageBuilder(
        bootstrap_context=context,
        source_directory=IDEA_BOOTSTRAP_DIR,
        target_package_basename=basename,
        components=[component],
        tmp_dir=str(tmp_path),
        force_build=True,
        base_os=base_os,
        logger=SocaAnyPayload(info=lambda message: None),
    ).build()
    files = []
    for root, _, names in os.walk(tmp_path / basename):
        files += [os.path.join(root, name) for name in sorted(names)]
    return files


def run(command) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    return '' if result.returncode == 0 else (result.stdout + result.stderr).strip()


BOOTSTRAP_COMMON = (
    pathlib.Path(__file__).resolve().parents[2]
    / 'idea-bootstrap'
    / 'common'
    / 'bootstrap_common.sh'
)

PACKAGE_TRANSACTION = re.compile(
    r'\b(?:dnf|yum)\s+(?:--?[^\s;|&]+\s+)*'
    r'(?:install|upgrade|update|groupinstall|localinstall|group\s+install)\b'
)


def assert_package_transactions_wrapped(content):
    for line in content.replace('\\\n', ' ').splitlines():
        if line.lstrip().startswith(('#', 'log_', 'echo ')):
            continue
        for call in PACKAGE_TRANSACTION.finditer(line):
            assert line[: call.start()].rstrip().endswith('package_transaction'), line
    # --skip-broken and strict=0 stay where a caller already used them for packages a distribution
    # does not ship; the helper only retries and falls back to --nobest, it never skips packages.
    helper = (
        open(BOOTSTRAP_COMMON)
        .read()
        .split('function package_transaction()', 1)[1]
        .split('\n}\n', 1)[0]
    )
    assert '--skip-broken' not in helper and 'strict=0' not in helper


@pytest.mark.parametrize(
    'command',
    [
        'dnf -y install git',
        'if yum install -y git; then',
        'sudo yum --exclude=foo groupinstall "Server with GUI" -y',
        'rpm -q git || dnf install git',
        'dnf upgrade -y',
        'command="dnf install git"',
        'dnf -y \\\ninstall git',
    ],
)
def test_direct_package_transactions_are_rejected(command):
    with pytest.raises(AssertionError):
        assert_package_transactions_wrapped(command)


def test_all_templates_wrap_package_transactions():
    # Also cover dormant OS branches and controller templates outside the host matrix.
    for template in Path(IDEA_BOOTSTRAP_DIR).rglob('*.jinja2'):
        assert_package_transactions_wrapped(template.read_text())


@pytest.mark.parametrize('instance_type', INSTANCE_TYPES)
@pytest.mark.parametrize('base_os', LINUX_BASE_OS)
def test_linux_bootstrap_packages_parse(tmp_path, base_os, instance_type):
    assert BASH is not None, 'bash is required to syntax-check the bootstrap packages'
    errors = []
    for label, component, variables in packages(base_os):
        scripts = [
            f
            for f in render(tmp_path, base_os, instance_type, component, variables)
            if f.endswith('.sh')
        ]
        assert scripts, f'{label}: rendered no shell scripts'
        for script in scripts:
            output = run([BASH, '-n', script])
            if output:
                errors.append(f'[{base_os} {instance_type} {label}] bash -n:\n{output}')
        # most files render identically across the matrix: shellcheck each distinct one once.
        unchecked = []
        for script in scripts:
            with open(script) as f:
                content = f.read()
            assert_package_transactions_wrapped(content)
            if re.search(
                r'^\s*(?:if |.*\|\| )?package_transaction ', content, re.MULTILINE
            ):
                assert content.index(
                    'source "${SCRIPT_DIR}/../common/bootstrap_common.sh"'
                ) < content.index('package_transaction '), script
            if content not in SHELLCHECKED:
                SHELLCHECKED.add(content)
                unchecked.append(script)
        if SHELLCHECK and unchecked:
            output = run([SHELLCHECK, '-S', 'error', '-s', 'bash', *unchecked])
            if output:
                errors.append(
                    f'[{base_os} {instance_type} {label}] shellcheck:\n{output}'
                )
    assert not errors, '\n\n'.join(errors)


# a missing pwsh skips only the parse, never the render.
@pytest.mark.parametrize('instance_type', ('m7i.large', 'g5.xlarge', 'g4ad.xlarge'))
@pytest.mark.parametrize('base_os', WINDOWS_BASE_OS)
def test_windows_bootstrap_package_parses(tmp_path, base_os, instance_type):
    label = 'windows host and image builder'
    scripts = [
        f
        for _, component, variables in packages(base_os)
        for f in render(tmp_path, base_os, instance_type, component, variables)
        if f.endswith('.ps1')
    ]
    assert scripts, f'{label}: rendered no powershell scripts'
    if PWSH is None:
        pytest.skip(
            'pwsh not installed: rendered the windows bootstrap, skipped the parse'
        )
    errors = []
    for script in scripts:
        output = run(
            [
                PWSH,
                '-NoProfile',
                '-Command',
                '$errors = $null; '
                '[System.Management.Automation.Language.Parser]::ParseFile('
                f"'{script}', [ref]$null, [ref]$errors) | Out-Null; "
                'foreach ($e in $errors) { "line $($e.Extent.StartLineNumber): $($e.Message)" }; '
                'exit $errors.Count',
            ]
        )
        if output:
            errors.append(f'[{base_os} {instance_type} {label}] {script}:\n{output}')
    assert not errors, '\n\n'.join(errors)


# rocky installs from the public mirrorlist: dnf is told to download in parallel from the
# fastest mirror before its first call. rhel uses in-region rhui and is left alone.
FIRST_PACKAGE_MANAGER_CALL = re.compile(
    r'^\s*(?:package_transaction )?(dnf|yum)\s', re.MULTILINE
)


@pytest.mark.parametrize('base_os', ('rocky8', 'rocky9', 'rhel9'))
def test_rocky_dnf_downloads_in_parallel_before_the_first_dnf_call(tmp_path, base_os):
    checked = []
    for label, component, variables in packages(base_os):
        if component not in ('virtual-desktop-host-linux', 'compute-node'):
            continue
        files = render(tmp_path, base_os, 'm7i.large', component, variables)
        setup = next(f for f in files if f.endswith(f'{component}/setup.sh'))
        with open(setup) as f:
            content = f.read()
        checked.append(label)
        if base_os == 'rhel9':
            assert 'max_parallel_downloads' not in content, label
            continue
        first_call = FIRST_PACKAGE_MANAGER_CALL.search(content)
        assert first_call is not None, label
        settings = content.index('max_parallel_downloads=10 fastestmirror=True')
        assert settings < first_call.start(), label
    assert checked
