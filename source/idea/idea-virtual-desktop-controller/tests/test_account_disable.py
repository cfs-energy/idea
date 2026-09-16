from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    DayOfWeek,
    VirtualDesktopScheduleType,
    SocaPaginator,
)
from ideavirtualdesktopcontroller.app.events.handlers.user_management_event_handlers.user_disabled_event_handler import (
    UserDisabledEventHandler,
)
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


def handler():
    result = UserDisabledEventHandler.__new__(UserDisabledEventHandler)
    result.is_sender_controller_role = Mock(return_value=True)
    result.session_db = Mock()
    result.schedule_utils = Mock()
    result.schedule_db = Mock()
    result.schedule_db.get_empty_schedule.side_effect = (
        VirtualDesktopScheduleDB.get_empty_schedule
    )
    result.session_utils = Mock()
    result.session_utils.stop_sessions.return_value = ([], [])
    return result


def test_disable_clears_schedules_stops_sessions_and_paginates():
    h = handler()
    sessions = [
        VirtualDesktopSession(owner='user', idea_session_id=str(i), state=state)
        for i, state in enumerate(
            (VirtualDesktopSessionState.READY, VirtualDesktopSessionState.STOPPED)
        )
    ]
    h.session_db.list_all_for_user.side_effect = [
        SimpleNamespace(listing=[sessions[0]], paginator=SocaPaginator(cursor='next')),
        SimpleNamespace(listing=[sessions[1]], paginator=None),
    ]
    h.handle_event(
        'message', 'role:session', SimpleNamespace(detail={'username': 'user'})
    )
    assert h.schedule_utils.delete_schedules_for_session.call_count == 2
    assert h.session_utils.stop_sessions.call_count == 1
    assert sessions[0].force is True
    assert h.session_db.list_all_for_user.call_args.kwargs['request'].cursor == 'next'
    for session in sessions:
        for day in DayOfWeek:
            schedule = getattr(session.schedule, day.value)
            assert schedule.schedule_type == VirtualDesktopScheduleType.NO_SCHEDULE
            VirtualDesktopScheduleDB.convert_schedule_object_to_db_dict(schedule)


def test_untrusted_role_is_rejected_before_any_write():
    h = handler()
    h.is_sender_controller_role.return_value = False
    with pytest.raises(Exception):
        h.handle_event(
            'message', 'wrong-role', SimpleNamespace(detail={'username': 'user'})
        )
    h.session_db.list_all_for_user.assert_not_called()


def test_stop_failure_keeps_event_retryable():
    h = handler()
    session = VirtualDesktopSession(
        owner='user', idea_session_id='1', state=VirtualDesktopSessionState.READY
    )
    h.session_db.list_all_for_user.return_value = SimpleNamespace(
        listing=[session], paginator=None
    )
    h.session_utils.stop_sessions.return_value = ([], [session])
    with pytest.raises(Exception) as error:
        h.handle_event(
            'message', 'role:session', SimpleNamespace(detail={'username': 'user'})
        )
    assert error.value.error_code == 'DO_NOT_DELETE_MESSAGE'


def test_already_queued_resume_cannot_restart_disabled_owner():
    util = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    util.context = Mock()
    util.context.accounts_client.get_user.return_value.user.enabled = False
    session = VirtualDesktopSession(
        owner='user', idea_session_id='1', state=VirtualDesktopSessionState.STOPPED
    )
    util._session_db = Mock()
    util._session_db.get_from_db.return_value = session
    util._server_utils = Mock()
    result = util.resume_sessions([session])
    assert result == ([], [session])
    util._server_utils.start_servers.assert_not_called()
