"""
Test Cases for CostsAPI and MyCostsAPI ACL dispatch

These isolate the authorization routing from token decoding and OpenSearch by faking
the AppContext/ApiInvocationContext collaborators. What is under test is who is allowed
to ask about whom, which is the whole reason the admin API is a separate class.
"""

from ideaclustermanager.app.api.costs_api import CostsAPI
from ideaclustermanager.app.api.my_costs_api import MyCostsAPI
from ideadatamodel import exceptions, GetMyCostsSummaryResult, ListUserCostsResult

import pytest


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        pass


class FakeAppContext:
    def module_id(self) -> str:
        return 'cluster-manager'

    def logger(self, _name=None):
        return FakeLogger()


class FakeApiInvocationContext:
    """
    Minimal stand-in for ideasdk.api.ApiInvocationContext, implementing only the
    surface the two invoke() methods touch.
    """

    def __init__(
        self, namespace, username, payload=None, elevated=False, authorized_user=True
    ):
        self.namespace = namespace
        self._username = username
        self._payload = payload if payload is not None else {}
        self._elevated = elevated
        self._authorized_user = authorized_user
        self.response_payload = None

    def is_authorized(self, elevated_access: bool, scopes=None) -> bool:
        return elevated_access and self._elevated

    def is_authenticated_user(self) -> bool:
        return True

    def is_authorized_user(self) -> bool:
        return self._authorized_user

    def get_username(self) -> str:
        return self._username

    def get_request_payload_as(self, payload_type):
        return payload_type(**self._payload)

    def success(self, payload):
        self.response_payload = payload


class RecordingService:
    """records who the service was asked about, which is what these tests assert on."""

    def __init__(self):
        self.summary_calls = []
        self.list_calls = 0

    def get_summary(self, username):
        self.summary_calls.append(username)
        return GetMyCostsSummaryResult(username=username)

    def list_user_costs(self):
        self.list_calls += 1
        return ListUserCostsResult(listing=[])


def build_costs_api():
    api = CostsAPI(context=FakeAppContext())
    service = RecordingService()
    api.my_costs = service
    return api, service


def build_my_costs_api():
    api = MyCostsAPI(context=FakeAppContext())
    service = RecordingService()
    api.my_costs = service
    return api, service


# admin api


def test_get_user_summary_rejects_a_non_elevated_caller():
    api, service = build_costs_api()
    context = FakeApiInvocationContext(
        'Costs.GetUserSummary', 'user-a', {'username': 'user-b'}, elevated=False
    )

    with pytest.raises(exceptions.SocaException):
        api.invoke(context)

    # the read must not have happened at all
    assert service.summary_calls == []


def test_list_user_costs_rejects_a_non_elevated_caller():
    api, service = build_costs_api()
    context = FakeApiInvocationContext('Costs.ListUserCosts', 'user-a', elevated=False)

    with pytest.raises(exceptions.SocaException):
        api.invoke(context)

    assert service.list_calls == 0


def test_get_user_summary_serves_the_named_user_to_an_elevated_caller():
    api, service = build_costs_api()
    context = FakeApiInvocationContext(
        'Costs.GetUserSummary', 'admin', {'username': 'user-b'}, elevated=True
    )

    api.invoke(context)

    assert service.summary_calls == ['user-b']
    assert context.response_payload.username == 'user-b'


def test_get_user_summary_requires_a_username():
    api, _ = build_costs_api()
    context = FakeApiInvocationContext(
        'Costs.GetUserSummary', 'admin', {}, elevated=True
    )

    with pytest.raises(exceptions.SocaException):
        api.invoke(context)


def test_costs_api_rejects_an_unknown_namespace():
    api, _ = build_costs_api()
    context = FakeApiInvocationContext('Costs.Whatever', 'admin', elevated=True)

    with pytest.raises(exceptions.SocaException):
        api.invoke(context)


# self scoped api


def test_my_costs_serves_the_caller_and_ignores_any_username_in_the_payload():
    api, service = build_my_costs_api()
    # a caller-supplied username must not change whose costs come back. the request
    # model has no such field, so this pins the dispatch itself.
    context = FakeApiInvocationContext(
        'MyCosts.GetSummary', 'user-a', {'username': 'user-b'}, elevated=False
    )

    api.invoke(context)

    assert service.summary_calls == ['user-a']
    assert context.response_payload.username == 'user-a'


def test_my_costs_rejects_the_admin_namespaces():
    api, service = build_my_costs_api()
    # the self scoped class must not answer for the admin namespace even if the
    # invoker ever routed one to it by mistake.
    for namespace in ('Costs.ListUserCosts', 'Costs.GetUserSummary'):
        context = FakeApiInvocationContext(namespace, 'user-a', elevated=True)
        with pytest.raises(exceptions.SocaException):
            api.invoke(context)

    assert service.summary_calls == []


def test_my_costs_rejects_a_user_who_is_not_in_the_module_group():
    api, service = build_my_costs_api()
    # authenticated but no longer authorized: taking someone out of the users group
    # has to take the page with it
    context = FakeApiInvocationContext(
        'MyCosts.GetSummary', 'user-a', elevated=False, authorized_user=False
    )

    with pytest.raises(exceptions.SocaException):
        api.invoke(context)

    assert service.summary_calls == []
