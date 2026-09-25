"""Reporting gates run before request parsing, source reads and snapshot lookups."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import ReportingPeriodRequest, ReportingPaginator, exceptions
from ideaclustermanager.app.api.reporting_api import ReportingAPI
from ideaclustermanager.app.api.api_invoker import ClusterManagerApiInvoker
from pydantic import ValidationError


def invocation(
    namespace='Reporting.GetCapabilities', allowed=False, human=True, payload=None
):
    return SimpleNamespace(
        namespace=namespace,
        header={},
        request_payload=payload or {},
        has_access_token=lambda: True,
        is_unix_domain_socket_invocation=lambda: False,
        is_authenticated_user=lambda: human,
        get_username=lambda: 'reader',
        can_read_reporting=Mock(return_value=allowed),
        success=Mock(),
    )


def api():
    result = ReportingAPI.__new__(ReportingAPI)
    result.service = Mock()
    return result


@pytest.mark.parametrize('allowed', [False, True])
def test_capabilities_admit_humans_without_read_grant(allowed):
    request = invocation(allowed=allowed)
    result = api()
    result.invoke(request)
    request.success.assert_called_once_with(dict(can_read_reporting=allowed))
    assert not result.service.mock_calls


@pytest.mark.parametrize(
    'namespace', ['Reporting.GetSummary', 'Reporting.ListRows', 'Reporting.ExportCsv']
)
def test_denial_precedes_invalid_filters_and_snapshot_resolution(namespace):
    request = invocation(namespace, payload={'actor': 'forged', 'unknown': True})
    result = api()
    with pytest.raises(exceptions.SocaException) as error:
        result.invoke(request)
    assert error.value.error_code == 'UNAUTHORIZED_ACCESS'
    assert not result.service.mock_calls


@pytest.mark.parametrize(
    'namespace',
    [
        'Reporting.GetCapabilities',
        'Reporting.GetSummary',
        'Reporting.ListRows',
        'Reporting.ExportCsv',
        'Reporting.Unknown',
        'Unknown.GetSummary',
    ],
)
def test_apps_and_generic_read_tokens_never_acquire_reporting(namespace):
    with pytest.raises(exceptions.SocaException):
        api().invoke(invocation(namespace, allowed=True, human=False))


@pytest.mark.parametrize('field', ['actor', 'username', 'cluster_id', 'cluster_name'])
def test_forged_identity_is_rejected_in_payload_and_header(field):
    for location in ('request_payload', 'header'):
        request = invocation(allowed=True)
        setattr(request, location, {field: 'forged'})
        with pytest.raises(exceptions.SocaException):
            api().invoke(request)


def test_unauthenticated_socket_and_missing_predicate_fail_closed():
    for field, value in [
        ('has_access_token', lambda: False),
        ('is_unix_domain_socket_invocation', lambda: True),
        ('get_username', lambda: None),
    ]:
        request = invocation(allowed=True)
        setattr(request, field, value)
        with pytest.raises(exceptions.SocaException):
            api().invoke(request)
    request = invocation('Reporting.GetSummary', allowed=True)
    del request.can_read_reporting
    with pytest.raises(exceptions.SocaException):
        api().invoke(request)


@pytest.mark.parametrize(
    'role,allowed',
    [
        ('ordinary', False),
        ('module_user', False),
        ('module_admin', False),
        ('operations_lead', True),
        ('manager', True),
        ('administrator', True),
    ],
)
def test_dispatch_uses_only_shared_capability_result(role, allowed):
    request = invocation(
        'Reporting.GetSummary', allowed=allowed, payload={'period': 'this_month'}
    )
    request.is_administrator = Mock(
        side_effect=AssertionError('Secondary role decision')
    )
    request.is_manager = Mock(side_effect=AssertionError('Secondary role decision'))
    result = api()
    result.service.get_summary.return_value.model_dump.return_value = {}
    if allowed:
        result.invoke(request)
        assert result.service.get_summary.call_args.args[0] == 'reader'
    else:
        with pytest.raises(exceptions.SocaException):
            result.invoke(request)
    request.can_read_reporting.assert_called()


def test_rows_and_csv_logging_suppress_payload_on_success_and_failure():
    invoker = ClusterManagerApiInvoker.__new__(ClusterManagerApiInvoker)
    for namespace in (
        'Reporting.ListRows',
        'Reporting.ExportCsv',
        'Reporting.GetSummary',
    ):
        for success in (False, True):
            response = dict(
                success=success, payload=dict(content='private', listing=['private'])
            )
            request = invocation(namespace)
            request.get_response = lambda **kwargs: copy.deepcopy(response)
            request.get_request = lambda **kwargs: {'payload': {'cursor': 'private'}}
            sanitized = invoker.get_response_logging_payload(request)
            assert sanitized['payload'] == {'redacted': True}
            assert invoker.get_request_logging_payload(request)['payload'] == {
                'redacted': True
            }
            assert response['payload']['content'] == 'private'


def test_request_models_forbid_unknown_fields_and_coerced_pagination():
    for payload in (
        {'page_size': 0},
        {'page_size': 201},
        {'page_size': '50'},
        {'page_size': True},
        {'offset': 1},
    ):
        with pytest.raises(ValidationError):
            ReportingPaginator(**payload)
    assert ReportingPaginator().page_size == 50
    with pytest.raises(ValidationError):
        ReportingPeriodRequest(period='unknown')
    with pytest.raises(ValidationError):
        ReportingPeriodRequest(period='this_month', username='forged')
