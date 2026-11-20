from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    DayOfWeek,
    VirtualDesktopScheduleType,
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


def test_disable_reaches_every_desktop_of_the_owner_on_every_page():
    from stored_session import session_table_reader, stored_row

    reader = session_table_reader()
    rows = [
        stored_row(session_id=f's-{n:02d}', state='STOPPED' if n == 0 else 'READY')
        for n in range(11)
    ]
    reader._table_obj = Mock()
    # the table answers in two pages; the search index would have shown the first 10 only
    reader._table_obj.query.side_effect = [
        {'Items': rows[:6], 'LastEvaluatedKey': {'owner': 'user-a'}},
        {'Items': rows[6:]},
    ]
    h = handler()
    h.session_db.list_for_owner = reader.list_for_owner

    h.handle_event(
        'message', 'role:session', SimpleNamespace(detail={'username': 'user-a'})
    )

    assert h.schedule_utils.delete_schedules_for_session.call_count == 11
    assert h.session_utils.stop_sessions.call_count == 10
    assert reader._table_obj.query.call_args_list[0].kwargs['ConsistentRead'] is True
    for call in h.session_db.update.call_args_list:
        session = call.args[0]
        assert call.kwargs == {'check_version': True}
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
    h.session_db.list_for_owner.assert_not_called()


def test_stop_failure_keeps_event_retryable():
    h = handler()
    session = VirtualDesktopSession(
        owner='user', idea_session_id='1', state=VirtualDesktopSessionState.READY
    )
    h.session_db.list_for_owner.return_value = [session]
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
    util._server_utils.start_dcv_hosts.assert_not_called()


def test_retry_reissues_ec2_stop_after_stopping_was_persisted():
    from ideadatamodel import VirtualDesktopServer

    h = handler()
    session = VirtualDesktopSession(
        owner='user',
        idea_session_id='1',
        state=VirtualDesktopSessionState.READY,
        server=VirtualDesktopServer(instance_id='i-example'),
    )
    h.session_db.list_for_owner.return_value = [session]
    util = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    util.context = Mock()
    util.context.dcv_broker_client.delete_sessions.return_value = ([], [])
    util._logger = Mock()
    util._session_db = h.session_db
    util._session_db.get_from_db.return_value = session
    util._session_db.update.side_effect = lambda value, **kwargs: value
    util._server_utils = Mock()
    util._server_utils.stop_or_hibernate_servers.side_effect = [
        RuntimeError('transient stop failure'),
        None,
    ]
    h.session_utils = util
    event = SimpleNamespace(detail={'username': 'user'})
    with pytest.raises(RuntimeError):
        h.handle_event('message', 'role:session', event)
    assert session.state == VirtualDesktopSessionState.STOPPING
    h.handle_event('message', 'role:session', event)
    assert util._server_utils.stop_or_hibernate_servers.call_count == 2
    assert util._server_utils.stop_or_hibernate_servers.call_args.args == (
        [session.server],
        [],
    )
