"""What a refused desktop action tells the person: no ids, no usernames, a plain reason."""

from unittest.mock import Mock

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client import (
    DCVBrokerClient,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)

RAW = ('sess-raw-1', 'someuser')


def a_session(state):
    return VirtualDesktopSession(
        idea_session_id='sess-raw-1',
        dcv_session_id='dcv-1',
        name='desktop',
        owner='someuser',
        state=state,
        server=VirtualDesktopServer(instance_id='i-raw'),
    )


def build_utils(session, connection_count=0):
    def with_count(sessions):
        for each in sessions:
            each.connection_count = connection_count
        return sessions

    utils = object.__new__(VirtualDesktopSessionUtils)
    utils._session_db = Mock()
    utils._session_db.get_from_db.return_value = session
    utils._logger = Mock()
    utils.context = Mock()
    utils.context.accounts_client.get_user.return_value.user.enabled = True
    utils.context.dcv_broker_client.get_active_counts_for_sessions.side_effect = (
        with_count
    )
    utils.context.dcv_broker_client.delete_sessions.return_value = ([], [])
    utils._server_utils = Mock()
    return utils


def assert_plain(reason):
    for raw in RAW:
        assert raw not in reason


def test_starting_a_desktop_that_is_not_stopped():
    session = a_session(VirtualDesktopSessionState.READY)
    utils = build_utils(session)
    _success, failed = utils.resume_sessions(
        [a_session(VirtualDesktopSessionState.READY)]
    )
    assert (
        failed[0].failure_reason
        == "This desktop isn't stopped, so it can't be started right now."
    )


def test_stopping_a_desktop_that_is_not_running():
    session = a_session(VirtualDesktopSessionState.STOPPED)
    utils = build_utils(session)
    _success, failed = utils.stop_sessions(
        [a_session(VirtualDesktopSessionState.STOPPED)]
    )
    assert_plain(failed[0].failure_reason)


def test_rebooting_a_desktop_that_is_not_running():
    session = a_session(VirtualDesktopSessionState.STOPPED)
    utils = build_utils(session)
    _success, failed = utils.reboot_sessions(
        [a_session(VirtualDesktopSessionState.STOPPED)]
    )
    assert_plain(failed[0].failure_reason)


def test_rebooting_with_people_connected_keeps_the_count_for_the_page():
    session = a_session(VirtualDesktopSessionState.READY)
    utils = build_utils(session, connection_count=2)
    _success, failed = utils.reboot_sessions(
        [a_session(VirtualDesktopSessionState.READY)]
    )
    assert failed[0].connection_count == 2
    assert_plain(failed[0].failure_reason)


def test_terminating_with_people_connected_keeps_the_count_for_the_page():
    client = object.__new__(DCVBrokerClient)
    client._logger = Mock()
    session = a_session(VirtualDesktopSessionState.READY)

    def with_count(sessions):
        sessions[0].connection_count = 1
        return sessions

    client.get_active_counts_for_sessions = with_count
    _success, failed = client.delete_sessions([session])
    assert failed == [session]
    assert session.connection_count == 1
    assert_plain(session.failure_reason)


def test_launching_in_a_project_the_owner_is_not_in():
    api = object.__new__(VirtualDesktopAPI)
    api._logger = Mock()
    api.context = Mock()
    api.context.projects_client.get_user_projects.return_value = []
    api.context.config.return_value.get_string.return_value = 'clusteradmin'
    session = VirtualDesktopSession(
        owner='someuser',
        project={'project_id': 'project-raw'},
    )
    session, valid = api.validate_create_session_request(session)
    assert not valid
    assert session.failure_reason == "You aren't a member of this project."
