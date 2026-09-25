from unittest.mock import Mock

import pytest

from ideadatamodel import ListSessionsResponse, exceptions
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideatestutils.api_tokens import api_token_environment, api_token_invocation


def test_personal_token_lists_sessions_with_current_module_access():
    env = api_token_environment('virtual-desktop-controller')
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = env.context
    api.session_db = Mock()
    api.session_db.list_all_for_user.return_value = ListSessionsResponse(listing=[])
    api.namespace_handler_map = {'VirtualDesktop.ListSessions': api.list_sessions}
    invocation = api_token_invocation(env, 'VirtualDesktop.ListSessions')
    api.invoke(invocation)
    assert invocation.response_payload['listing'] == []
    assert api.session_db.list_all_for_user.call_args.args[1] == 'user-a'
    assert any(
        call.args[0].endswith('.cluster-manager.api-tokens')
        for call in env.context.aws().dynamodb_table().Table.call_args_list
    )
    env.groups.return_value = {'Groups': []}
    with pytest.raises(exceptions.SocaException):
        api.invoke(api_token_invocation(env, 'VirtualDesktop.ListSessions'))
