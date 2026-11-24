import json
from unittest.mock import Mock

import pytest

from ideaclustermanager import AppContext
from ideaclustermanager.app.api.api_invoker import ClusterManagerApiInvoker
from ideasdk.api import ApiInvocationContext
from ideasdk.utils import GroupNameHelper


def logged(context: AppContext, namespace, payload):
    invocation = ApiInvocationContext(
        context=context,
        request={'header': {'namespace': namespace}, 'payload': payload},
        invocation_source='http',
        group_name_helper=GroupNameHelper(context),
        logger=Mock(),
        token=None,
        token_service=Mock(),
    )
    invoker = ClusterManagerApiInvoker.__new__(ClusterManagerApiInvoker)
    return json.dumps(invoker.get_request_logging_payload(invocation), default=str)


@pytest.mark.parametrize(
    'namespace, payload',
    [
        (
            'Auth.InitiateAuth',
            {
                'auth_flow': 'REFRESH_TOKEN_AUTH',
                'username': 'user',
                'refresh_token': 'refresh-marker',
            },
        ),
        (
            'Auth.RespondToAuthChallenge',
            {
                'username': 'user',
                'challenge_name': 'NEW_PASSWORD_REQUIRED',
                'session': 'session-marker',
                'new_password': 'password-marker',
            },
        ),
        (
            'Auth.ConfirmForgotPassword',
            {
                'username': 'user',
                'password': 'password-marker',
                'confirmation_code': 'code-marker',
            },
        ),
        # a payload that does not parse is not logged as sent
        ('Auth.ConfirmForgotPassword', {'username': ['code-marker']}),
    ],
)
def test_traced_sign_in_requests_never_log_their_codes_or_tokens(
    context: AppContext, namespace, payload
):
    text = logged(context, namespace, payload)
    for marker in (
        'refresh-marker',
        'session-marker',
        'password-marker',
        'code-marker',
    ):
        assert marker not in text


@pytest.mark.parametrize(
    'namespace, payload',
    [
        (
            'Auth.InitiateAuth',
            {'challenge_name': 'NEW_PASSWORD_REQUIRED', 'session': 'session-marker'},
        ),
        (
            'Auth.RespondToAuthChallenge',
            {
                'session': 'session-marker',
                'auth': {
                    'access_token': 'access-marker',
                    'id_token': 'id-marker',
                    'refresh_token': 'refresh-marker',
                },
            },
        ),
    ],
)
def test_traced_sign_in_answers_never_log_their_session_or_tokens(
    context: AppContext, namespace, payload
):
    invocation = ApiInvocationContext(
        context=context,
        request={'header': {'namespace': namespace}, 'payload': {}},
        invocation_source='http',
        group_name_helper=GroupNameHelper(context),
        logger=Mock(),
        token=None,
        token_service=Mock(),
    )
    invocation.success(payload)
    invoker = ClusterManagerApiInvoker.__new__(ClusterManagerApiInvoker)
    text = json.dumps(invoker.get_response_logging_payload(invocation), default=str)
    for marker in ('session-marker', 'access-marker', 'id-marker', 'refresh-marker'):
        assert marker not in text
