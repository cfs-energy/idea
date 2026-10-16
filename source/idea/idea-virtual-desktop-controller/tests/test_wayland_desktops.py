"""RHEL 10, Rocky Linux 10 and Ubuntu 26.04 desktops: DCV runs them on Wayland, virtual sessions only."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from ideadatamodel import (
    Project,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopArchitecture,
    VirtualDesktopBaseOS,
    VirtualDesktopGPU,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionType,
    VirtualDesktopSoftwareStack,
    constants,
)
from ideasdk.aws.stock_amis import stock_ami_pattern
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder import (
    BUILD_SUPPORTED_BASE_OS,
)
from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
    base_stack_id,
    parse_base_stack_id,
)

WAYLAND = ('rhel10', 'rocky10', 'ubuntu2604')
STACK_CONFIG = (
    Path(__file__).resolve().parents[1]
    / 'resources'
    / 'base-software-stack-config.yaml'
)


def api_for(base_os: str) -> VirtualDesktopAPI:
    api = VirtualDesktopAPI.__new__(VirtualDesktopAPI)
    project = Project(project_id='p1', name='p1')
    api.context = Mock()
    api.context.config().get_string.return_value = 'clusteradmin'
    api.context.config().get_bool.return_value = False
    api.context.projects_client.get_user_projects.return_value = [project]
    api._logger = Mock()
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = VirtualDesktopSoftwareStack(
        stack_id=f'ss-base-{base_os}-x86-64-base',
        base_os=VirtualDesktopBaseOS(base_os),
        projects=[project],
        min_storage=SocaMemory(value=20, unit=SocaMemoryUnit.GB),
    )
    api.controller_utils = Mock()
    api.controller_utils.get_instance_type_rejection_reason.return_value = None
    api.controller_utils.get_gpu_manufacturer.return_value = VirtualDesktopGPU.NO_GPU
    api.controller_utils.get_architecture.return_value = (
        VirtualDesktopArchitecture.X86_64
    )
    return api


def session_for(base_os: str, session_type) -> VirtualDesktopSession:
    return VirtualDesktopSession(
        owner='alice',
        type=session_type,
        hibernation_enabled=False,
        project=Project(project_id='p1'),
        software_stack=VirtualDesktopSoftwareStack(
            stack_id=f'ss-base-{base_os}-x86-64-base',
            base_os=VirtualDesktopBaseOS(base_os),
        ),
        server=VirtualDesktopServer(instance_type='m7i.large', root_volume_size=None),
    )


@pytest.mark.parametrize('base_os', WAYLAND)
def test_wayland_desktops_are_desktop_base_os(base_os):
    assert VirtualDesktopBaseOS(base_os).value == base_os
    assert base_os in constants.WAYLAND_DESKTOP_BASEOS
    assert base_os in constants.SUPPORTED_LINUX_OS
    assert base_os in BUILD_SUPPORTED_BASE_OS


def test_ubuntu2604_stays_out_of_the_compute_allowlist():
    assert 'ubuntu2604' not in constants.ALLOWED_BASEOS


@pytest.mark.parametrize('base_os', WAYLAND)
def test_console_sessions_are_refused_on_wayland(base_os):
    session, ok = api_for(base_os).validate_create_session_request(
        session_for(base_os, VirtualDesktopSessionType.CONSOLE)
    )
    assert not ok
    assert 'does not support Console Sessions' in session.failure_reason


@pytest.mark.parametrize('base_os', WAYLAND)
def test_virtual_sessions_pass_the_session_type_checks(base_os):
    # stops at the root volume check, which comes after every session type check
    session, ok = api_for(base_os).validate_create_session_request(
        session_for(base_os, VirtualDesktopSessionType.VIRTUAL)
    )
    assert not ok
    assert session.failure_reason == 'missing session.server.root_volume_size'


def test_console_sessions_stay_allowed_on_x11_desktops():
    session, ok = api_for('rocky9').validate_create_session_request(
        session_for('rocky9', VirtualDesktopSessionType.CONSOLE)
    )
    assert session.failure_reason == 'missing session.server.root_volume_size'


@pytest.mark.parametrize('base_os', WAYLAND)
@pytest.mark.parametrize('arch_key', ['x86-64', 'arm64'])
def test_base_stack_rows_exist_per_architecture(base_os, arch_key):
    config = yaml.safe_load(STACK_CONFIG.read_text())
    arch_config = config[base_os][arch_key]
    regions = {k: v for k, v in arch_config.items() if isinstance(v, list)}
    assert 'us-east-2' in regions
    for region, rows in regions.items():
        assert rows[0]['ss-id-suffix'] == 'base'
        assert rows[0]['ami-id'].startswith('ami-'), region
    assert arch_config['default-min-storage-value'] >= 20
    # the stock image lookup and the stack id round trip for the row
    architecture = 'x86_64' if arch_key == 'x86-64' else 'arm64'
    assert stock_ami_pattern(base_os, architecture) is not None
    stack_id = base_stack_id(base_os, architecture)
    assert stack_id == f'ss-base-{base_os}-{arch_key}-base'
    assert parse_base_stack_id(stack_id) == (base_os, architecture)
