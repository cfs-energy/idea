"""Update requests re-check the instance type only when it changes."""

from unittest.mock import Mock

from ideadatamodel import VirtualDesktopServer, VirtualDesktopSession
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)


def build_api(current_type, reason):
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api._logger = Mock()
    api._validate_owner = lambda owner, context: ('', True)
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = VirtualDesktopSession(
        idea_session_id='s-1',
        owner='user-a',
        server=VirtualDesktopServer(instance_type=current_type),
    )
    api.controller_utils = Mock()
    api.controller_utils.get_instance_type_rejection_reason.return_value = reason
    return api


def request(instance_type):
    return VirtualDesktopSession(
        idea_session_id='s-1',
        owner='user-a',
        name='renamed',
        server=VirtualDesktopServer(instance_type=instance_type),
    )


def test_rename_keeps_working_on_a_type_that_is_no_longer_allowed():
    api = build_api('g5.xlarge', 'g5.xlarge is not allowed')
    context = Mock()
    context.get_username.return_value = 'user-a'
    session, valid = api._validate_update_session_request(request('g5.xlarge'), context)
    assert valid and session.failure_reason is None
    api.controller_utils.get_instance_type_rejection_reason.assert_not_called()


def test_a_changed_type_is_still_checked():
    api = build_api('g5.xlarge', 'g5.2xlarge is not allowed')
    context = Mock()
    context.get_username.return_value = 'user-a'
    session, valid = api._validate_update_session_request(
        request('g5.2xlarge'), context
    )
    assert not valid and session.failure_reason == 'g5.2xlarge is not allowed'
