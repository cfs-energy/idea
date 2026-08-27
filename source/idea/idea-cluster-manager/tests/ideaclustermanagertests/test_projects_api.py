"""
Test Cases for ProjectsAPI ACL dispatch

These tests isolate the authorization routing in ProjectsAPI.invoke() from
token decoding and DynamoDB by faking the AppContext/ApiInvocationContext
collaborators, so they exercise only the dispatch logic under test.
"""

from ideaclustermanager.app.api.projects_api import ProjectsAPI
from ideadatamodel import GetUserProjectsRequest, GetUserProjectsResult, Project


class FakeProjectsService:
    def __init__(self):
        self.requested_usernames = []

    def get_user_projects(
        self, request: GetUserProjectsRequest
    ) -> GetUserProjectsResult:
        self.requested_usernames.append(request.username)
        return GetUserProjectsResult(
            projects=[Project(name=f'project-for-{request.username}')]
        )


class FakeAppContext:
    def __init__(self, projects_service: FakeProjectsService):
        self.projects = projects_service

    def module_id(self) -> str:
        return 'cluster-manager'


class FakeApiInvocationContext:
    """
    Minimal stand-in for ideasdk.api.ApiInvocationContext, implementing only
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


def test_get_user_projects_non_elevated_user_cannot_view_other_users_projects():
    """
    a non-elevated authenticated user requesting another user's projects
    must get back their OWN projects, not the requested user's
    """
    fake_service = FakeProjectsService()
    api = ProjectsAPI(context=FakeAppContext(fake_service))

    invocation = FakeApiInvocationContext(
        namespace='Projects.GetUserProjects',
        username='alice',
        payload={'username': 'bob'},
        elevated=False,
    )

    api.invoke(invocation)

    assert fake_service.requested_usernames == ['alice']
    assert invocation.response_payload.projects[0].name == 'project-for-alice'


def test_get_user_projects_non_elevated_user_no_username_defaults_to_self():
    """
    a non-elevated authenticated user who omits the username still gets
    their own projects
    """
    fake_service = FakeProjectsService()
    api = ProjectsAPI(context=FakeAppContext(fake_service))

    invocation = FakeApiInvocationContext(
        namespace='Projects.GetUserProjects',
        username='alice',
        payload={},
        elevated=False,
    )

    api.invoke(invocation)

    assert fake_service.requested_usernames == ['alice']


def test_get_user_projects_elevated_user_can_view_other_users_projects():
    """
    an elevated (admin/manager) caller may still request another user's
    projects explicitly - the fix must not regress the admin path
    """
    fake_service = FakeProjectsService()
    api = ProjectsAPI(context=FakeAppContext(fake_service))

    invocation = FakeApiInvocationContext(
        namespace='Projects.GetUserProjects',
        username='admin',
        payload={'username': 'bob'},
        elevated=True,
    )

    api.invoke(invocation)

    assert fake_service.requested_usernames == ['bob']
