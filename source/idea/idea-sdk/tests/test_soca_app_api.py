"""
Test Cases for SocaAppAPI (App.GetModuleInfo)

App.GetModuleInfo leaks the module name/version. It must require a valid token,
matching the ClusterSettings posture - an unauthenticated caller is rejected.
"""

from ideasdk.context import SocaContext, SocaContextOptions
from ideasdk.api import ApiInvocationContext
from ideasdk.app.soca_app_api import SocaAppAPI
from ideasdk.utils import Utils, GroupNameHelper

from ideatestutils.config.mock_config import MockConfig

from ideadatamodel import constants, exceptions, errorcodes

from typing import Dict, Optional
import pytest


@pytest.fixture(scope='session')
def context():
    mock_config = MockConfig()
    return SocaContext(
        options=SocaContextOptions(
            module_id='cluster-manager',
            module_name='cluster-manager',
            config=mock_config.get_config(),
        )
    )


def build_invocation_context(
    context: SocaContext,
    invocation_source: Optional[str] = None,
) -> ApiInvocationContext:
    if Utils.is_empty(invocation_source):
        invocation_source = constants.API_INVOCATION_SOURCE_HTTP
    request: Dict = {
        'header': {'namespace': 'App.GetModuleInfo', 'request_id': Utils.uuid()},
        'payload': {},
    }
    return ApiInvocationContext(
        context=context,
        request=request,
        invocation_source=invocation_source,
        group_name_helper=GroupNameHelper(context=context),
        logger=context.logger(),
    )


def test_get_module_info_rejects_unauthenticated(context):
    """no token -> unauthorized, before any module info is disclosed."""
    api = SocaAppAPI(context=context)
    api_context = build_invocation_context(
        context, invocation_source=constants.API_INVOCATION_SOURCE_HTTP
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api.invoke(api_context)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    # nothing was written to the response
    assert api_context.response is None


def test_get_module_info_returns_info_when_authenticated(context):
    """
    an authenticated invocation (unix-socket == administrator) returns module info.
    """
    api = SocaAppAPI(context=context)
    api_context = build_invocation_context(
        context, invocation_source=constants.API_INVOCATION_SOURCE_UNIX_SOCKET
    )

    api.invoke(api_context)

    response = api_context.response
    assert response is not None
    assert response['success'] is True
    assert response['payload']['module']['module_id'] == 'cluster-manager'
