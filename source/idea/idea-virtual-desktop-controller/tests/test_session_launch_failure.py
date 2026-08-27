"""
unit tests for the reason a virtual desktop request could not be satisfied
"""

from typing import Any, Dict, List, Optional

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    Project,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopArchitecture,
    VirtualDesktopGPU,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSoftwareStack,
    exceptions,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    LAUNCH_FAILURE_DEFAULT_MESSAGE,
    PASS_PROJECT_ROLE_FAILURE_MESSAGE,
    VirtualDesktopControllerUtils,
    build_launch_failure_message,
)

CONFIG_ALLOW_KEY = 'virtual-desktop-controller.dcv_session.instance_types.allow'
CONFIG_DENY_KEY = 'virtual-desktop-controller.dcv_session.instance_types.deny'
SUBNETS_KEY = 'cluster.network.private_subnets'
VDI_SUBNETS_KEY = 'vdc.dcv_session.network.private_subnets'
METADATA_TOKENS_KEY = 'virtual-desktop-controller.dcv_session.metadata_http_tokens'
HOST_INSTANCE_PROFILE_KEY = 'virtual-desktop-controller.dcv_host_instance_profile_arn'
SHARED_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/idea-test-vdc-host-instance-profile'
)
PROJECT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/idea/idea-test/projects/'
    'idea-test-p1-project'
)


def launch_config(subnets: List[str]) -> Dict[str, Any]:
    return {
        SUBNETS_KEY: subnets,
        VDI_SUBNETS_KEY: [],
        METADATA_TOKENS_KEY: 'required',
        'global-settings.custom_tags': [],
        'vdc.dcv_session.network.randomize_subnets': False,
        'vdc.dcv_session.network.subnet_autoretry': True,
    }


INSTANCE_TYPES: Dict[str, Dict[str, Any]] = {
    'm6a.48xlarge': {
        'InstanceType': 'm6a.48xlarge',
        'MemoryInfo': {'SizeInMiB': 786432},
        'HibernationSupported': False,
        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
    },
    'g5.xlarge': {
        'InstanceType': 'g5.xlarge',
        'MemoryInfo': {'SizeInMiB': 16384},
        'HibernationSupported': True,
        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
        'GpuInfo': {'Gpus': [{'Manufacturer': 'NVIDIA'}]},
    },
    't3.micro': {
        'InstanceType': 't3.micro',
        'MemoryInfo': {'SizeInMiB': 1024},
        'HibernationSupported': True,
        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
    },
    'm6g.large': {
        'InstanceType': 'm6g.large',
        'MemoryInfo': {'SizeInMiB': 8192},
        'HibernationSupported': True,
        'ProcessorInfo': {'SupportedArchitectures': ['arm64']},
    },
    'p9.96xlarge': {
        'InstanceType': 'p9.96xlarge',
        'MemoryInfo': {'SizeInMiB': 2097152},
        'HibernationSupported': False,
        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
        'GpuInfo': {'Gpus': [{'Manufacturer': 'NVIDIA'}]},
    },
}


class MockCache:
    def __init__(self):
        self._values = {
            'aws.ec2.all-instance-types-names-list': list(INSTANCE_TYPES.keys()),
            'aws.ec2.all-instance-types-data': INSTANCE_TYPES,
        }

    def get(self, key: str):
        return self._values.get(key)

    def set(self, key: str, value):
        self._values[key] = value


class MockClusterConfig:
    def __init__(self, values: Dict[str, Any]):
        self._values = values

    def get_list(self, key: str, default=None, required: bool = False):
        if key not in self._values:
            if required:
                raise KeyError(f'config key not found: {key}')
            return default
        return self._values[key]

    def get_bool(self, key: str, default=None, required: bool = False):
        return self._values.get(key, default)

    def get_string(self, key: str, default=None, required: bool = False):
        return self._values.get(key, default)


class MockAccountsClient:
    @staticmethod
    def get_user(_request):
        return None


class MockContext:
    def __init__(self, values: Dict[str, Any]):
        self._config = MockClusterConfig(values)
        self._cache = MockCache()
        self.accounts_client = MockAccountsClient()

    def config(self) -> MockClusterConfig:
        return self._config

    def cache(self):
        return self

    def long_term(self) -> MockCache:
        return self._cache

    @staticmethod
    def cluster_name() -> str:
        return 'test-cluster'

    @staticmethod
    def module_id() -> str:
        return 'vdc'

    @staticmethod
    def module_name() -> str:
        return 'virtual-desktop-controller'

    @staticmethod
    def module_version() -> str:
        return '0.0.0-test'


class FailingEC2Client:
    """run_instances that always fails with the given error code"""

    def __init__(self, error_code: str):
        self._error_code = error_code
        self.calls: List[str] = []

    def run_instances(self, **kwargs):
        self.calls.append(kwargs['NetworkInterfaces'][0]['SubnetId'])
        raise ClientError(
            {'Error': {'Code': self._error_code, 'Message': 'from the test'}},
            'RunInstances',
        )


def build_controller_utils(values: Dict[str, Any]) -> VirtualDesktopControllerUtils:
    # __init__ builds every AWS client, none of which these tests need
    utils = object.__new__(VirtualDesktopControllerUtils)
    utils.context = MockContext(values)
    utils.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY = 'aws.ec2.all-instance-types-names-list'
    utils.INSTANCE_INFO_CACHE_KEY = 'aws.ec2.all-instance-types-data'
    utils._logger = _NullLogger()
    return utils


class _NullLogger:
    def isEnabledFor(self, _level) -> bool:
        return False

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def build_software_stack(
    min_ram_gib: int = 4,
    architecture: VirtualDesktopArchitecture = VirtualDesktopArchitecture.X86_64,
    gpu: VirtualDesktopGPU = VirtualDesktopGPU.NO_GPU,
    base_os: str = 'amazonlinux2023',
    allowed_instance_types: Optional[List[str]] = None,
) -> VirtualDesktopSoftwareStack:
    return VirtualDesktopSoftwareStack(
        stack_id='ss-test',
        base_os=base_os,
        min_ram=SocaMemory(value=min_ram_gib, unit=SocaMemoryUnit.GiB),
        architecture=architecture,
        gpu=gpu,
        allowed_instance_types=allowed_instance_types,
    )


def build_session(
    instance_type: str,
    instance_profile_arn: str = 'arn:aws:iam::123456789012:instance-profile/test',
) -> VirtualDesktopSession:
    session = VirtualDesktopSession(
        name='test-session',
        owner='test-user',
        software_stack=build_software_stack(),
        project=Project(project_id='p-test', name='test-project'),
    )
    session.server = VirtualDesktopServer(
        instance_type=instance_type,
        security_groups=['sg-test'],
        instance_profile_arn=instance_profile_arn,
        root_volume_size=SocaMemory(value=100, unit=SocaMemoryUnit.GB),
    )
    session.hibernation_enabled = False
    return session


@pytest.mark.parametrize(
    'error_code, expected_fragment',
    [
        ('InsufficientInstanceCapacity', 'no m6a.48xlarge capacity'),
        ('Unsupported', 'is not offered in the networks'),
        ('VcpuLimitExceeded', 'reached its limit'),
        ('InstanceLimitExceeded', 'reached its limit'),
        ('OptInRequired', 'AWS Marketplace'),
        ('InvalidAMIID.NotFound', 'no longer available'),
        ('RequestLimitExceeded', 'rate limiting'),
    ],
)
def test_launch_failure_message_names_the_real_reason(
    error_code: str, expected_fragment: str
):
    message = build_launch_failure_message(
        error_code=error_code, instance_type='m6a.48xlarge'
    )
    assert expected_fragment in message
    assert message != LAUNCH_FAILURE_DEFAULT_MESSAGE


def test_launch_failure_message_falls_back_for_unknown_codes():
    assert (
        build_launch_failure_message(
            error_code='SomethingNew', instance_type='m6a.large'
        )
        == LAUNCH_FAILURE_DEFAULT_MESSAGE
    )


def test_launch_failure_message_never_leaks_aws_identifiers():
    for error_code in ('InsufficientInstanceCapacity', 'OptInRequired', 'Unknown'):
        message = build_launch_failure_message(
            error_code=error_code, instance_type='m6a.48xlarge'
        )
        assert 'ami-' not in message
        assert 'subnet-' not in message
        assert error_code not in message


def test_capacity_failure_reaches_the_user_after_every_subnet_is_tried():
    utils = build_controller_utils(launch_config(['subnet-1', 'subnet-2', 'subnet-3']))
    ec2 = FailingEC2Client('InsufficientInstanceCapacity')
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException) as raised:
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    assert 'no m6a.48xlarge capacity' in raised.value.message
    # every configured subnet is attempted before the request is refused
    assert ec2.calls == ['subnet-1', 'subnet-2', 'subnet-3']


def test_unauthorized_launch_under_a_project_role_names_the_pass_role_grant():
    values = launch_config(['subnet-1'])
    values[HOST_INSTANCE_PROFILE_KEY] = SHARED_INSTANCE_PROFILE_ARN
    utils = build_controller_utils(values)
    utils.ec2_client = FailingEC2Client('UnauthorizedOperation')
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException) as raised:
        utils.provision_dcv_host_for_session(
            build_session(
                'm6a.48xlarge', instance_profile_arn=PROJECT_INSTANCE_PROFILE_ARN
            )
        )

    assert raised.value.message == PASS_PROJECT_ROLE_FAILURE_MESSAGE
    # the size the user picked is not the reason, so it is not mentioned
    assert 'm6a.48xlarge' not in raised.value.message


def test_unauthorized_launch_under_the_shared_profile_still_names_the_size():
    values = launch_config(['subnet-1'])
    values[HOST_INSTANCE_PROFILE_KEY] = SHARED_INSTANCE_PROFILE_ARN
    utils = build_controller_utils(values)
    utils.ec2_client = FailingEC2Client('UnauthorizedOperation')
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException) as raised:
        utils.provision_dcv_host_for_session(
            build_session(
                'm6a.48xlarge', instance_profile_arn=SHARED_INSTANCE_PROFILE_ARN
            )
        )

    assert 'not permitted to start m6a.48xlarge' in raised.value.message


def test_pass_role_message_replaces_nothing_but_unauthorized_operation():
    assert (
        build_launch_failure_message(
            error_code='InsufficientInstanceCapacity',
            instance_type='m6a.48xlarge',
            project_instance_profile=True,
        )
        != PASS_PROJECT_ROLE_FAILURE_MESSAGE
    )


def test_unretryable_failure_stops_at_the_first_subnet():
    utils = build_controller_utils(launch_config(['subnet-1', 'subnet-2']))
    ec2 = FailingEC2Client('OptInRequired')
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException) as raised:
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    assert 'AWS Marketplace' in raised.value.message
    assert ec2.calls == ['subnet-1']


def test_bootstrap_package_is_built_once_for_the_whole_retry_loop():
    utils = build_controller_utils(launch_config(['subnet-1', 'subnet-2', 'subnet-3']))
    utils.ec2_client = FailingEC2Client('InsufficientInstanceCapacity')
    builds = []
    utils._build_userdata = lambda session: builds.append(session.name) or 'userdata'

    with pytest.raises(exceptions.SocaException):
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    assert len(builds) == 1


def test_request_without_a_network_is_refused_before_any_launch():
    utils = build_controller_utils(launch_config([]))
    ec2 = FailingEC2Client('InsufficientInstanceCapacity')
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException) as raised:
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    assert 'no network configured' in raised.value.message
    assert ec2.calls == []


def test_rejection_reason_reports_the_administrator_allow_list():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['t3'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='m6a.48xlarge',
        hibernation_support=False,
        software_stack=build_software_stack(),
    )
    assert reason is not None
    assert 'administrator' in reason


def test_rejection_reason_reports_the_deny_list():
    utils = build_controller_utils(
        {CONFIG_ALLOW_KEY: ['m6a'], CONFIG_DENY_KEY: ['m6a.48xlarge']}
    )
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='m6a.48xlarge',
        hibernation_support=False,
        software_stack=build_software_stack(),
    )
    assert reason is not None
    assert 'administrator' in reason


def test_rejection_reason_reports_insufficient_memory():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['t3'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='t3.micro',
        hibernation_support=False,
        software_stack=build_software_stack(min_ram_gib=8),
    )
    assert reason == (
        't3.micro has 1 GB of memory. This desktop image needs at least 8 GB.'
    )


def test_rejection_reason_reports_hibernation():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['m6a'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='m6a.48xlarge',
        hibernation_support=True,
        software_stack=build_software_stack(),
    )
    assert reason is not None
    assert 'hibernated' in reason


def test_rejection_reason_reports_processor_mismatch():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['m6g'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='m6g.large',
        hibernation_support=False,
        software_stack=build_software_stack(
            architecture=VirtualDesktopArchitecture.X86_64
        ),
    )
    assert reason is not None
    assert 'processor' in reason


def test_rejection_reason_reports_a_missing_gpu():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['m6a'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='m6a.48xlarge',
        hibernation_support=False,
        software_stack=build_software_stack(gpu=VirtualDesktopGPU.NVIDIA),
    )
    assert reason is not None
    assert 'graphics card' in reason


def test_rejection_reason_reports_an_unknown_instance_type():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['m6a'], CONFIG_DENY_KEY: []})
    reason = utils.get_instance_type_rejection_reason(
        instance_type_name='zz9.plural-z-alpha',
        hibernation_support=False,
        software_stack=build_software_stack(),
    )
    assert reason is not None
    assert 'not available in this region' in reason


def test_allowed_size_has_no_rejection_reason():
    utils = build_controller_utils({CONFIG_ALLOW_KEY: ['m6a'], CONFIG_DENY_KEY: []})
    assert (
        utils.get_instance_type_rejection_reason(
            instance_type_name='m6a.48xlarge',
            hibernation_support=False,
            software_stack=build_software_stack(),
        )
        is None
    )


@pytest.mark.parametrize(
    'allow, deny, hibernation, gpu, expected',
    [
        (
            ['m6a', 'g5', 't3', 'm6g', 'p9'],
            [],
            False,
            None,
            ['m6a.48xlarge', 'g5.xlarge', 'p9.96xlarge'],
        ),
        (
            ['m6a', 'g5', 't3', 'm6g', 'p9'],
            ['p9'],
            False,
            None,
            ['m6a.48xlarge', 'g5.xlarge'],
        ),
        (['m6a', 'g5', 't3', 'm6g', 'p9'], [], True, None, ['g5.xlarge']),
        (
            ['m6a', 'g5', 't3', 'm6g', 'p9'],
            [],
            False,
            VirtualDesktopGPU.NVIDIA,
            ['g5.xlarge', 'p9.96xlarge'],
        ),
    ],
)
def test_listing_and_single_size_checks_agree(
    allow: List[str],
    deny: List[str],
    hibernation: bool,
    gpu: Optional[VirtualDesktopGPU],
    expected: List[str],
):
    # the listed sizes and the per-size reason come from the same filter
    utils = build_controller_utils({CONFIG_ALLOW_KEY: allow, CONFIG_DENY_KEY: deny})
    software_stack = build_software_stack()
    listed = [
        instance_type['InstanceType']
        for instance_type in utils.get_valid_instance_types(
            hibernation_support=hibernation, software_stack=software_stack, gpu=gpu
        )
    ]
    assert listed == expected

    for instance_type_name in INSTANCE_TYPES:
        reason = utils.get_instance_type_rejection_reason(
            instance_type_name=instance_type_name,
            hibernation_support=hibernation,
            software_stack=software_stack,
            gpu=gpu,
        )
        assert (reason is None) == (instance_type_name in listed)


# a tag value of None fails botocore parameter validation before RunInstances is called, so
# the launch dies with a generic message and nothing naming the tag.


def test_a_session_whose_project_did_not_resolve_is_refused_with_a_reason():
    utils = build_controller_utils(launch_config(['subnet-1']))
    session = build_session('m6a.48xlarge')
    # the controller resolves the project against the projects the owner can access. an
    # admin creating a session in a project the owner cannot see leaves the name unset.
    session.project = Project(project_id='p-test', name=None)

    with pytest.raises(exceptions.SocaException) as exc_info:
        utils.provision_dcv_host_for_session(session)

    message = exc_info.value.message
    assert 'could not be resolved' in message
    assert 'test-user' in message, 'the refusal should name whose access to check'


class RecordingEC2Client:
    """Captures what would have been sent, then refuses so no success shape is needed."""

    def __init__(self):
        self.requests = []
        self.calls = []

    def run_instances(self, **kwargs):
        self.requests.append(kwargs)
        subnet = kwargs.get('SubnetId')
        if subnet is None:
            for ni in kwargs.get('NetworkInterfaces', []) or []:
                subnet = ni.get('SubnetId')
        self.calls.append(subnet)
        raise ClientError(
            {'Error': {'Code': 'InsufficientInstanceCapacity', 'Message': 'none'}},
            'RunInstances',
        )


def test_an_unresolved_project_launches_nothing():
    utils = build_controller_utils(launch_config(['subnet-1']))
    ec2 = RecordingEC2Client()
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'
    session = build_session('m6a.48xlarge')
    session.project = Project(project_id='p-test', name=None)

    with pytest.raises(exceptions.SocaException):
        utils.provision_dcv_host_for_session(session)

    assert ec2.requests == [], 'no instance may be requested'


def test_no_tag_is_ever_sent_with_an_empty_value():
    utils = build_controller_utils(launch_config(['subnet-1']))
    ec2 = RecordingEC2Client()
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException):
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    assert len(ec2.requests) == 1
    tags = ec2.requests[0]['TagSpecifications'][0]['Tags']
    assert len(tags) > 0
    for tag in tags:
        assert tag['Value'] not in (None, ''), f'{tag["Key"]} sent with an empty value'
        assert isinstance(tag['Value'], str)


def test_a_resolved_project_still_reaches_the_launch():
    # positive control: the ordinary path is unaffected and carries the project tag
    utils = build_controller_utils(launch_config(['subnet-1']))
    ec2 = RecordingEC2Client()
    utils.ec2_client = ec2
    utils._build_userdata = lambda session: 'userdata'

    with pytest.raises(exceptions.SocaException):
        utils.provision_dcv_host_for_session(build_session('m6a.48xlarge'))

    tags = {
        t['Key']: t['Value'] for t in ec2.requests[0]['TagSpecifications'][0]['Tags']
    }
    assert tags['idea:Project'] == 'test-project'
