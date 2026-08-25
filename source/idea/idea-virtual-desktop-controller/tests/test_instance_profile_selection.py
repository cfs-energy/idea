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
unit tests for server-side selection of the eVDI host instance profile
"""

import logging
from typing import Any, Dict, List, Optional

import pytest

from ideadatamodel import (
    Project,
    ProjectBedrockBudget,
    ProjectBedrockConfig,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopBaseOS,
    VirtualDesktopGPU,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSoftwareStack,
    BEDROCK_BUDGET_ACTION_BLOCK,
    BEDROCK_BUDGET_ACTION_WARN,
    BEDROCK_BUDGET_STATUS_EXHAUSTED,
    BEDROCK_BUDGET_STATUS_UNAVAILABLE,
    BEDROCK_BUDGET_STATUS_WARNING,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)

CONFIGURED_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea-test-vdc-host-instance-profile'
)
CLIENT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/some-other-instance-profile'
)
PROJECT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea/idea-test/projects/'
    'idea-test-p1-project'
)
FORGED_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea/idea-test/projects/forged'
)
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'

CONFIG: Dict[str, Any] = {
    'virtual-desktop-controller.dcv_host_instance_profile_arn': CONFIGURED_INSTANCE_PROFILE_ARN,
    'virtual-desktop-controller.dcv_host_security_group_id': 'sg-00000000000000001',
    'virtual-desktop-controller.dcv_session.additional_security_groups': [],
    'virtual-desktop-controller.dcv_session.metadata_http_tokens': 'required',
    'cluster.network.ssh_key_pair': 'idea-test-key-pair',
    'cluster.network.private_subnets': ['subnet-00000000000000001'],
    'cluster.ebs.kms_key_id': None,
    'global-settings.custom_tags': [],
    'vdc.dcv_session.network.private_subnets': [],
    'vdc.dcv_session.network.randomize_subnets': False,
    'vdc.dcv_session.network.subnet_autoretry': True,
}


class MockClusterConfig:
    def get_string(self, key: str, default: str = None, required: bool = False) -> str:
        return self._get(key, default, required)

    def get_list(self, key: str, default: List = None, required: bool = False) -> List:
        return self._get(key, default, required)

    def get_bool(self, key: str, default: bool = None, required: bool = False) -> bool:
        return self._get(key, default, required)

    @staticmethod
    def _get(key: str, default: Any, required: bool) -> Any:
        if key not in CONFIG:
            if required:
                raise KeyError(f'config key not found: {key}')
            value = default
        else:
            value = CONFIG[key]
        # the real config returns a fresh list per read; the provisioning path
        # pops from the list it gets, so a shared one drains across tests.
        return list(value) if isinstance(value, list) else value


class MockAccountsClient:
    @staticmethod
    def get_user(_request) -> None:
        return None


class MockProjectsClient:
    """serves the stored project, so a payload supplied block is never read"""

    def __init__(self, project: Optional[Project] = None, raises: bool = False):
        self.project = project
        self.raises = raises
        self.requested_project_ids: List[str] = []

    def get_project(self, request):
        self.requested_project_ids.append(request.project_id)
        if self.raises:
            raise Exception('projects client unavailable')
        return type('GetProjectResult', (), {'project': self.project})()


class MockContext:
    def __init__(self, projects_client=None):
        self._config = MockClusterConfig()
        self.accounts_client = MockAccountsClient()
        self.projects_client = projects_client or MockProjectsClient()

    def config(self) -> MockClusterConfig:
        return self._config

    @staticmethod
    def cluster_name() -> str:
        return 'idea-test'

    @staticmethod
    def module_id() -> str:
        return 'vdc'

    @staticmethod
    def module_name() -> str:
        return 'virtual-desktop-controller'

    @staticmethod
    def module_version() -> str:
        return '0.0.0'


class MockApiInvocationContext:
    @staticmethod
    def get_username() -> str:
        return 'test-user'


class MockControllerUtils:
    @staticmethod
    def get_gpu_manufacturer(_instance_type: str) -> VirtualDesktopGPU:
        return VirtualDesktopGPU.NO_GPU


class RecordingEc2Client:
    """captures the RunInstances call instead of making it"""

    def __init__(self):
        self.run_instances_kwargs: Optional[Dict[str, Any]] = None

    def run_instances(self, **kwargs) -> Dict[str, Any]:
        self.run_instances_kwargs = kwargs
        return {'Instances': [{'InstanceId': 'i-00000000000000001'}]}


def build_api(projects_client=None) -> VirtualDesktopAPI:
    # VirtualDesktopAPI.__init__ builds every DDB/EC2 client, none of which this test needs
    api = object.__new__(VirtualDesktopAPI)
    api.context = MockContext(projects_client)
    api._logger = logging.getLogger('test-virtual-desktop-api')
    api.controller_utils = MockControllerUtils()
    api.DEFAULT_ROOT_VOL_IOPS = '100'
    return api


def build_controller_utils(
    ec2_client: RecordingEc2Client,
) -> VirtualDesktopControllerUtils:
    utils = object.__new__(VirtualDesktopControllerUtils)
    utils.context = MockContext()
    utils._logger = logging.getLogger('test-virtual-desktop-controller-utils')
    utils.ec2_client = ec2_client
    utils._build_userdata = lambda session: 'test-userdata'
    return utils


def build_session(
    instance_profile_arn: Optional[str], project: Optional[Project] = None
) -> VirtualDesktopSession:
    return VirtualDesktopSession(
        name='test-session',
        owner='test-user',
        project=project or Project(name='test-project'),
        software_stack=VirtualDesktopSoftwareStack(
            base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023,
            ami_id='ami-00000000000000001',
        ),
        server=VirtualDesktopServer(
            instance_type='m5.large',
            root_volume_size=SocaMemory(value=50, unit=SocaMemoryUnit.GB),
            instance_profile_arn=instance_profile_arn,
        ),
    )


@pytest.mark.parametrize(
    'requested_instance_profile_arn',
    [
        None,  # nothing supplied, configured profile applies
        CLIENT_INSTANCE_PROFILE_ARN,  # supplied by the caller, discarded
        CONFIGURED_INSTANCE_PROFILE_ARN,  # supplied and identical, no change
    ],
)
def test_instance_profile_comes_from_configuration(requested_instance_profile_arn):
    session = build_api().complete_create_session_request(
        build_session(requested_instance_profile_arn), MockApiInvocationContext()
    )
    assert session.server.instance_profile_arn == CONFIGURED_INSTANCE_PROFILE_ARN


def test_client_supplied_instance_profile_does_not_reach_run_instances():
    session = build_api().complete_create_session_request(
        build_session(CLIENT_INSTANCE_PROFILE_ARN), MockApiInvocationContext()
    )

    ec2_client = RecordingEc2Client()
    build_controller_utils(ec2_client).provision_dcv_host_for_session(session)

    run_instances_kwargs = ec2_client.run_instances_kwargs
    assert run_instances_kwargs is not None
    assert (
        run_instances_kwargs['IamInstanceProfile']['Arn']
        == CONFIGURED_INSTANCE_PROFILE_ARN
    )
    assert CLIENT_INSTANCE_PROFILE_ARN not in str(run_instances_kwargs)


# per-project instance profile


def bedrock_project(instance_profile_arn: Optional[str]) -> Project:
    return Project(
        project_id=PROJECT_ID,
        name='test-project',
        bedrock=ProjectBedrockConfig(
            enabled=True,
            model_ids=['vendor.model'],
            instance_profile_arn=instance_profile_arn,
        ),
    )


def test_bedrock_project_profile_reaches_run_instances():
    projects_client = MockProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN))
    session = build_api(projects_client).complete_create_session_request(
        build_session(None, Project(project_id=PROJECT_ID, name='test-project')),
        MockApiInvocationContext(),
    )
    assert session.server.instance_profile_arn == PROJECT_INSTANCE_PROFILE_ARN

    ec2_client = RecordingEc2Client()
    build_controller_utils(ec2_client).provision_dcv_host_for_session(session)
    assert (
        ec2_client.run_instances_kwargs['IamInstanceProfile']['Arn']
        == PROJECT_INSTANCE_PROFILE_ARN
    )


def test_a_payload_supplied_bedrock_block_is_never_used():
    # the project travels in the request payload, so the stored record is re-read
    # rather than trusted: otherwise a caller could name any project role.
    projects_client = MockProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN))
    session = build_api(projects_client).complete_create_session_request(
        build_session(
            CLIENT_INSTANCE_PROFILE_ARN, bedrock_project(FORGED_INSTANCE_PROFILE_ARN)
        ),
        MockApiInvocationContext(),
    )
    assert session.server.instance_profile_arn == PROJECT_INSTANCE_PROFILE_ARN
    assert projects_client.requested_project_ids == [PROJECT_ID]


def test_bedrock_project_without_a_provisioned_profile_uses_the_shared_profile():
    projects_client = MockProjectsClient(bedrock_project(None))
    session = build_api(projects_client).complete_create_session_request(
        build_session(None, Project(project_id=PROJECT_ID, name='test-project')),
        MockApiInvocationContext(),
    )
    assert session.server.instance_profile_arn == CONFIGURED_INSTANCE_PROFILE_ARN


def test_a_non_bedrock_project_uses_the_shared_profile():
    projects_client = MockProjectsClient(
        Project(project_id=PROJECT_ID, name='test-project')
    )
    session = build_api(projects_client).complete_create_session_request(
        build_session(None, Project(project_id=PROJECT_ID, name='test-project')),
        MockApiInvocationContext(),
    )
    assert session.server.instance_profile_arn == CONFIGURED_INSTANCE_PROFILE_ARN


def test_an_unreadable_project_falls_back_to_the_shared_profile():
    # a projects client failure must not fail session creation, and must not
    # fall through to a caller supplied value either.
    projects_client = MockProjectsClient(raises=True)
    session = build_api(projects_client).complete_create_session_request(
        build_session(
            CLIENT_INSTANCE_PROFILE_ARN,
            Project(project_id=PROJECT_ID, name='test-project'),
        ),
        MockApiInvocationContext(),
    )
    assert session.server.instance_profile_arn == CONFIGURED_INSTANCE_PROFILE_ARN


# per-project bedrock budget


def budgeted_bedrock_project(status: str, action: str) -> Project:
    project = bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)
    project.bedrock_budget = ProjectBedrockBudget(
        action=action, status=status, budget_name='research-budget'
    )
    return project


def resolve_with_budget(status: str, action: str) -> str:
    projects_client = MockProjectsClient(budgeted_bedrock_project(status, action))
    session = build_api(projects_client).complete_create_session_request(
        build_session(None, Project(project_id=PROJECT_ID, name='test-project')),
        MockApiInvocationContext(),
    )
    return session.server.instance_profile_arn


def test_a_budget_inside_its_limit_keeps_model_access():
    assert (
        resolve_with_budget(BEDROCK_BUDGET_STATUS_WARNING, BEDROCK_BUDGET_ACTION_BLOCK)
        == PROJECT_INSTANCE_PROFILE_ARN
    )


def test_an_exhausted_budget_launches_the_desktop_without_model_access():
    assert (
        resolve_with_budget(
            BEDROCK_BUDGET_STATUS_EXHAUSTED, BEDROCK_BUDGET_ACTION_BLOCK
        )
        == CONFIGURED_INSTANCE_PROFILE_ARN
    )


def test_a_budget_that_could_not_be_evaluated_withholds_model_access():
    # an unknown must not read as room to spend
    assert (
        resolve_with_budget(
            BEDROCK_BUDGET_STATUS_UNAVAILABLE, BEDROCK_BUDGET_ACTION_BLOCK
        )
        == CONFIGURED_INSTANCE_PROFILE_ARN
    )


def test_the_warn_action_leaves_model_access_in_place():
    assert (
        resolve_with_budget(BEDROCK_BUDGET_STATUS_EXHAUSTED, BEDROCK_BUDGET_ACTION_WARN)
        == PROJECT_INSTANCE_PROFILE_ARN
    )
