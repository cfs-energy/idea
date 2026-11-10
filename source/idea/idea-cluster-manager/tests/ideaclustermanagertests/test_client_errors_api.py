"""
Test Cases for the browser error reporting API.

What matters: only the allowed fields reach the log, whatever a browser sends, identity comes from
the session, and a chatty browser cannot fill the logs.
"""

from unittest.mock import Mock

import pytest
from ideaclustermanager.app.api.api_invoker import ClusterManagerApiInvoker
from ideaclustermanager.app.api.client_errors_api import (
    MAX_REPORTS_PER_HOUR,
    ROUTES,
    ClientErrorsAPI,
    sanitize_message,
)
from ideadatamodel import exceptions

GOOD = {
    'error_type': 'uncaught_error',
    'route': '/home/file-browser',
    'app_version': '26.11.2-rc.1',
    'api_namespace': 'Scheduler.ListActiveJobs',
    'message': 'TypeError Cannot read properties of undefined',
}


def make_api():
    app_context = Mock()
    logger = Mock()
    app_context.logger.return_value = logger
    return ClientErrorsAPI(app_context), logger


def make_context(payload, username='user-a', authenticated=True):
    context = Mock()
    context.namespace = 'Portal.ReportClientError'
    context.request_payload = payload
    context.get_username.return_value = username
    context.is_authenticated_user.return_value = authenticated
    context.has_access_token.return_value = authenticated
    context.is_unix_domain_socket_invocation.return_value = False
    return context


def test_writes_one_structured_line_and_no_username():
    api, logger = make_api()
    context = make_context(GOOD)
    api.invoke(context)
    context.success.assert_called_once_with({'accepted': True})
    line = logger.warning.call_args[0][0]
    assert line == (
        '[client-error] type=uncaught_error route=/home/file-browser '
        'version=26.11.2-rc.1 api=Scheduler.ListActiveJobs '
        'message="TypeError Cannot read properties of undefined"'
    )
    assert 'user-a' not in line


@pytest.mark.parametrize(
    'message',
    [
        'failed to open /data/home/user-b/run7/in.dat',
        'job fusion_shot_42 failed: quench',
        'Unexpected token secretword in JSON at position 12',
        "Cannot read properties of undefined (reading 'mysecret')",
        'https://portal/x?token=abc failed',
        'alice@example.com failed',
        'FUSION_RUN7 FUSIONSHOT failed',
    ],
)
def test_message_keeps_known_words_only(message):
    cleaned = sanitize_message(message)
    for leak in (
        'data',
        'home',
        'user-b',
        'fusion',
        'quench',
        'secretword',
        'mysecret',
        'portal',
        'abc',
        'alice',
        '12',
        'FUSION',
    ):
        assert leak not in cleaned
    assert 'failed' in cleaned or 'Unexpected' in cleaned or 'Cannot' in cleaned


def test_message_is_bounded_and_keeps_error_codes():
    assert sanitize_message('NETWORK_ERROR') == 'NETWORK_ERROR'
    assert sanitize_message('GENERAL_ERROR') == 'GENERAL_ERROR'
    assert len(sanitize_message('failed ' * 500)) <= 160
    assert sanitize_message(None) == ''


@pytest.mark.parametrize(
    'change',
    [
        {'error_type': 'anything'},
        {'route': '/home/file-browser?cwd=/data'},
        {'route': '/virtual-desktop/sessions/abc123'},
        {'route': '/home\nforged=line'},
        # a live path with the shape of a route: dev usernames passed the old pattern
        {'route': '/home/guien'},
        {'route': '/data/home/choso/secret'},
        {'route': '/cluster/users/guien'},
        {'route': '/virtual-desktop/sessions/:anything'},
        {'app_version': '1 2'},
        {'api_namespace': 'not a namespace'},
        {'message': 'x' * 1001},
        {'username': 'someone-else'},
    ],
)
def test_rejects_anything_outside_the_shape_without_logging(change):
    api, logger = make_api()
    with pytest.raises(exceptions.SocaException):
        api.invoke(make_context({**GOOD, **change}))
    logger.warning.assert_not_called()


def test_needs_a_signed_in_person():
    api, logger = make_api()
    with pytest.raises(exceptions.SocaException):
        api.invoke(make_context(GOOD, authenticated=False))
    logger.warning.assert_not_called()


def test_rate_limit_is_per_person_and_says_so_quietly():
    api, logger = make_api()
    for _ in range(MAX_REPORTS_PER_HOUR):
        api.invoke(make_context(GOOD))
    context = make_context(GOOD)
    api.invoke(context)
    context.success.assert_called_once_with({'accepted': False})
    assert logger.warning.call_count == MAX_REPORTS_PER_HOUR
    api.invoke(make_context(GOOD, username='user-b'))
    assert logger.warning.call_count == MAX_REPORTS_PER_HOUR + 1


def test_the_request_log_never_carries_the_payload():
    invoker = ClusterManagerApiInvoker.__new__(ClusterManagerApiInvoker)
    context = Mock()
    context.namespace = 'Portal.ReportClientError'
    context.get_request.return_value = {'payload': GOOD}
    assert invoker.get_request_logging_payload(context)['payload'] == {'redacted': True}


def test_the_routes_accepted_are_the_ones_the_portal_sends():
    """the page reports a pattern from its own list, or /unknown: the server takes nothing else"""
    import re
    from pathlib import Path

    reporter = Path(__file__).parents[2] / 'webapp/src/common/client-error-reporter.ts'
    source = reporter.read_text()
    listed = source[source.index('export const ROUTE_PATTERNS = [') :]
    sent = set(re.findall(r'^\s+"([^"]+)",$', listed[: listed.index('];')], re.M))
    assert len(sent) > 40
    assert "?? '/unknown'" in source
    assert ROUTES == sent | {'/unknown'}


@pytest.mark.parametrize('route', sorted(ROUTES))
def test_every_portal_route_is_accepted(route):
    api, logger = make_api()
    context = make_context({**GOOD, 'route': route})
    api.invoke(context)
    context.success.assert_called_once_with({'accepted': True})
