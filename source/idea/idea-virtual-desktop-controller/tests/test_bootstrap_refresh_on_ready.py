from unittest.mock import Mock

import pytest

from ideadatamodel import VirtualDesktopBaseOS, VirtualDesktopSessionState
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_creation_event_handler import (
    ValidateDCVSessionCreationEventHandler,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    BOOTSTRAP_REFRESH_VERSION,
)


@pytest.fixture
def ready(refresh):
    """
    the validation handler that moves a started or resumed desktop to READY, wired to the
    refresh harness so the bootstrap refresh it triggers lands in the fake ssm and tables.
    """
    handler = ValidateDCVSessionCreationEventHandler.__new__(
        ValidateDCVSessionCreationEventHandler
    )
    handler.context = refresh.context
    handler._logger = refresh.context.logger()
    handler.session_counter_db = Mock()
    handler.session_utils = refresh.sweep
    sessions = {}
    handler.session_db = Mock()
    handler.session_db.get_from_db.side_effect = (
        lambda idea_session_owner, idea_session_id: (sessions[idea_session_id])
    )
    handler.session_db.update.side_effect = lambda session: session
    refresh.context.dcv_broker_client.describe_sessions.side_effect = lambda found: {
        'sessions': {session.dcv_session_id: {'state': 'READY'} for session in found}
    }

    def resume(version=None, base_os=VirtualDesktopBaseOS.ROCKY9):
        session = refresh.add_session(
            state=VirtualDesktopSessionState.INITIALIZING,
            version=version,
            base_os=base_os,
        )
        session.dcv_session_id = f'dcv-{session.idea_session_id}'
        sessions[session.idea_session_id] = session
        handler.handle_event(
            'message-1',
            'virtual-desktop-controller.controller_iam_role_id',
            VirtualDesktopEvent(
                detail={
                    'idea_session_id': session.idea_session_id,
                    'idea_session_owner': session.owner,
                }
            ),
        )
        assert session.state == VirtualDesktopSessionState.READY
        return session

    return resume


@pytest.mark.parametrize('version', [None, BOOTSTRAP_REFRESH_VERSION - 1])
def test_resumed_desktop_behind_on_refresh_gets_exactly_one(refresh, ready, version):
    session = ready(version=version)
    assert [request['InstanceIds'] for request in refresh.ssm.requests] == [
        [session.server.instance_id]
    ]
    refresh.complete('Success')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        == BOOTSTRAP_REFRESH_VERSION
    )


def test_current_desktop_gets_no_refresh(refresh, ready):
    ready(version=BOOTSTRAP_REFRESH_VERSION)
    assert refresh.ssm.requests == []


def test_windows_desktop_gets_no_refresh(refresh, ready):
    ready(base_os=VirtualDesktopBaseOS.WINDOWS)
    assert refresh.ssm.requests == []


def test_submit_failure_still_makes_the_desktop_ready_and_leaves_it_to_the_sweep(
    refresh, ready
):
    refresh.ssm.failure = RuntimeError('throttled\nretry later')
    session = ready()
    assert len(refresh.ssm.requests) == 1
    refresh.context.logger().warning.assert_called_once_with(
        f'Bootstrap refresh failed for session {session.idea_session_id}, '
        'the sweep retries it: throttled retry later'
    )
    refresh.ssm.failure = None
    assert refresh.sweep.refresh_bootstrap() == 1


def test_refresh_already_in_flight_is_not_sent_again(refresh, ready):
    session = refresh.add_session()
    assert refresh.sweep.refresh_bootstrap() == 1
    # the sweep's command is still out when the same desktop becomes ready
    assert refresh.sweep.refresh_bootstrap_on_ready(session) is False
    assert len(refresh.ssm.requests) == 1
    refresh.complete('Failed')
    assert refresh.sweep.refresh_bootstrap_on_ready(session) is True
    assert len(refresh.ssm.requests) == 2


def test_command_from_an_older_refresh_version_does_not_count_as_in_flight(refresh):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    refresh.commands.rows['command-1']['additional_payload']['refresh_version'] = (
        BOOTSTRAP_REFRESH_VERSION - 1
    )
    assert refresh.sweep.refresh_bootstrap_on_ready(session) is True
