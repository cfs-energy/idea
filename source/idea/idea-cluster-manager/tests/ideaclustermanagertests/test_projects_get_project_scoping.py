"""
Test Cases for Projects.GetProject membership scoping

these tests isolate the authorization routing in ProjectsAPI.invoke() from
token decoding and DynamoDB by faking the AppContext/ApiInvocationContext
collaborators, so they exercise only the access-scoping logic under test.
"""

import pytest

from ideaclustermanager.app.api.projects_api import ProjectsAPI
from ideadatamodel import (
    exceptions,
    errorcodes,
    GetProjectRequest,
    GetProjectResult,
    Project,
)

PROJECT_ONE = Project(
    project_id='p-1',
    name='proj-one',
    title='Project One',
    ldap_groups=['proj-one-group'],
    enabled=True,
)


class FakeProjectsService:
    """
    serves a single project (p-1 / proj-one). alice is a member, mallory is not.
    """

    def __init__(self):
        self.membership_checks = []

    def get_project(self, request: GetProjectRequest) -> GetProjectResult:
        if request.project_id == 'p-1' or request.project_name == 'proj-one':
            return GetProjectResult(project=PROJECT_ONE.model_copy(deep=True))
        raise exceptions.soca_exception(
            error_code=errorcodes.PROJECT_NOT_FOUND,
            message='project not found',
        )

    def is_project_member(self, username: str, project_id: str) -> bool:
        self.membership_checks.append((username, project_id))
        return username == 'alice' and project_id == 'p-1'


class FakeAppContext:
    def __init__(self, projects_service: FakeProjectsService):
        self.projects = projects_service

    def module_id(self) -> str:
        return 'cluster-manager'


class FakeApiInvocationContext:
    """
    minimal stand-in for ideasdk.api.ApiInvocationContext, implementing only
    the surface ProjectsAPI.invoke() touches.
    """

    def __init__(self, namespace: str, username: str, payload: dict, elevated: bool):
        self.namespace = namespace
        self._username = username
        self._payload = payload
        self._elevated = elevated
        self.response_payload = None

    def is_authorized(self, elevated_access: bool, scopes=None) -> bool:
        return elevated_access and self._elevated

    def is_authenticated_user(self) -> bool:
        return True

    def get_username(self) -> str:
        return self._username

    def get_request_payload_as(self, payload_type):
        return payload_type(**self._payload)

    def success(self, payload):
        self.response_payload = payload


def invoke(service, namespace, username, payload, elevated):
    api = ProjectsAPI(context=FakeAppContext(service))
    invocation = FakeApiInvocationContext(
        namespace=namespace, username=username, payload=payload, elevated=elevated
    )
    api.invoke(invocation)
    return invocation


def test_get_project_elevated_caller_gets_project_without_membership():
    """
    an admin/manager who is not a member still gets the full project
    """
    service = FakeProjectsService()
    invocation = invoke(
        service,
        namespace='Projects.GetProject',
        username='admin',
        payload={'project_id': 'p-1'},
        elevated=True,
    )
    assert invocation.response_payload.project.name == 'proj-one'
    assert invocation.response_payload.project.ldap_groups == ['proj-one-group']
    assert service.membership_checks == []


def test_get_project_member_can_read_by_id():
    service = FakeProjectsService()
    invocation = invoke(
        service,
        namespace='Projects.GetProject',
        username='alice',
        payload={'project_id': 'p-1'},
        elevated=False,
    )
    assert invocation.response_payload.project.project_id == 'p-1'
    assert service.membership_checks == [('alice', 'p-1')]


def test_get_project_member_can_read_by_name():
    """
    a by-name request resolves the project first, then checks membership on
    the resolved project_id
    """
    service = FakeProjectsService()
    invocation = invoke(
        service,
        namespace='Projects.GetProject',
        username='alice',
        payload={'project_name': 'proj-one'},
        elevated=False,
    )
    assert invocation.response_payload.project.project_id == 'p-1'
    assert service.membership_checks == [('alice', 'p-1')]


def test_get_project_non_member_is_denied():
    service = FakeProjectsService()
    with pytest.raises(exceptions.SocaException) as exc_info:
        invoke(
            service,
            namespace='Projects.GetProject',
            username='mallory',
            payload={'project_id': 'p-1'},
            elevated=False,
        )
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_get_project_nonexistent_denied_same_as_non_member():
    """
    non-elevated callers must not be able to probe which project names exist:
    a missing project reports UNAUTHORIZED_ACCESS, not PROJECT_NOT_FOUND
    """
    service = FakeProjectsService()
    with pytest.raises(exceptions.SocaException) as exc_info:
        invoke(
            service,
            namespace='Projects.GetProject',
            username='mallory',
            payload={'project_name': 'does-not-exist'},
            elevated=False,
        )
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
