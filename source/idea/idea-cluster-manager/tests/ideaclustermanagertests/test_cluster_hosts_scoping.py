"""
Test Cases for ClusterSettings.ListClusterHosts access scoping

fake the AppContext/ApiInvocationContext collaborators so the tests exercise
only the elevated-access gate: admins get the infrastructure host listing,
non-elevated users are denied before any EC2 call is made.
"""

import pytest

from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI
from ideadatamodel import exceptions, errorcodes


class FakeEC2Instance:
    def __init__(self, instance_id: str):
        self._instance_id = instance_id

    def instance_data(self) -> dict:
        return {'InstanceId': self._instance_id, 'PrivateIpAddress': '10.0.0.10'}


class FakeAwsUtil:
    def __init__(self):
        self.describe_calls = 0

    def ec2_describe_instances(self, filters, page_size=None):
        self.describe_calls += 1
        return [FakeEC2Instance('i-0123456789abcdef0')]


class FakeAppContext:
    def __init__(self):
        self._aws_util = FakeAwsUtil()

    def aws_util(self) -> FakeAwsUtil:
        return self._aws_util

    def cluster_name(self) -> str:
        return 'idea-mock'

    def module_id(self) -> str:
        return 'cluster-manager'


class FakeApiInvocationContext:
    def __init__(self, namespace: str, elevated: bool, app_scopes=None):
        self.namespace = namespace
        self._elevated = elevated
        # an app (client-credentials) token carries scopes and is never elevated
        self._app_scopes = app_scopes
        self.response_payload = None

    def is_authenticated(self) -> bool:
        return True

    def is_authorized(self, elevated_access: bool, scopes=None) -> bool:
        if self._app_scopes is not None:
            return bool(scopes) and all(s in self._app_scopes for s in scopes)
        return elevated_access and self._elevated

    def get_request_payload_as(self, payload_type):
        return payload_type()

    def success(self, payload):
        self.response_payload = payload


def test_list_cluster_hosts_elevated_caller_gets_listing():
    context = FakeAppContext()
    api = ClusterSettingsAPI(context=context)
    invocation = FakeApiInvocationContext(
        namespace='ClusterSettings.ListClusterHosts', elevated=True
    )

    api.invoke(invocation)

    assert invocation.response_payload is not None
    assert invocation.response_payload.listing == [
        {'InstanceId': 'i-0123456789abcdef0', 'PrivateIpAddress': '10.0.0.10'}
    ]
    assert context.aws_util().describe_calls == 1


def test_list_cluster_hosts_non_elevated_caller_is_denied_before_ec2_call():
    context = FakeAppContext()
    api = ClusterSettingsAPI(context=context)
    invocation = FakeApiInvocationContext(
        namespace='ClusterSettings.ListClusterHosts', elevated=False
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api.invoke(invocation)

    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.aws_util().describe_calls == 0


def test_list_cluster_hosts_app_token_with_the_module_read_scope_is_served():
    """an api client authorized by client credentials is never elevated; its scope admits it"""
    context = FakeAppContext()
    api = ClusterSettingsAPI(context=context)
    invocation = FakeApiInvocationContext(
        namespace='ClusterSettings.ListClusterHosts',
        elevated=False,
        app_scopes=['cluster-manager/read'],
    )

    api.invoke(invocation)

    assert invocation.response_payload is not None
    assert context.aws_util().describe_calls == 1


def test_list_cluster_hosts_app_token_with_another_modules_scope_is_denied():
    context = FakeAppContext()
    api = ClusterSettingsAPI(context=context)
    invocation = FakeApiInvocationContext(
        namespace='ClusterSettings.ListClusterHosts',
        elevated=False,
        app_scopes=['scheduler/read'],
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api.invoke(invocation)

    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.aws_util().describe_calls == 0
