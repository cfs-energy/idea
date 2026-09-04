"""
Test Cases for the recorded desktop stop time

updated_on moves on every write to a session record, so anything measuring how long a
desktop ran needs a stop time of its own. The rule lives on the one path every state
change is persisted through.
"""

from ideadatamodel import VirtualDesktopSession, VirtualDesktopSessionState
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)

import arrow


def stamp(session):
    VirtualDesktopSessionDB._stamp_stopped_on(session)
    return session


def test_stop_time_is_recorded_when_the_session_stops():
    session = stamp(VirtualDesktopSession(state=VirtualDesktopSessionState.STOPPED))
    assert session.stopped_on is not None


def test_stop_time_is_not_moved_by_a_later_write():
    stopped_at = arrow.utcnow().shift(days=-25).datetime
    session = VirtualDesktopSession(
        state=VirtualDesktopSessionState.STOPPED, stopped_on=stopped_at
    )

    # a rename, a schedule edit or the cleanup sweep writes the record again; none of
    # them restarted the desktop
    stamp(session)
    assert session.stopped_on == stopped_at


def test_a_running_session_carries_no_stop_time():
    for state in (
        VirtualDesktopSessionState.READY,
        VirtualDesktopSessionState.RESUMING,
        VirtualDesktopSessionState.PROVISIONING,
        VirtualDesktopSessionState.CREATING,
        VirtualDesktopSessionState.INITIALIZING,
    ):
        session = VirtualDesktopSession(
            state=state, stopped_on=arrow.utcnow().shift(days=-2).datetime
        )
        stamp(session)
        assert session.stopped_on is None, state


def test_stopping_still_counts_as_running():
    # the instance is winding down and still costing, so it has not stopped yet
    session = stamp(VirtualDesktopSession(state=VirtualDesktopSessionState.STOPPING))
    assert session.stopped_on is None


def test_a_deleted_session_keeps_the_stop_time_it_had():
    """stopped on Monday, deleted on Friday: the bill ends Monday, not Friday"""
    stopped_at = arrow.utcnow().shift(days=-3).datetime
    session = VirtualDesktopSession(
        state=VirtualDesktopSessionState.DELETED, stopped_on=stopped_at
    )
    stamp(session)
    assert session.stopped_on == stopped_at


def test_a_session_deleted_while_running_is_stamped_at_the_deletion():
    """
    a desktop terminated straight from running never passes through STOPPED, so
    without this it carries no stop time and its hours cannot be measured at all.
    """
    for state in (
        VirtualDesktopSessionState.DELETING,
        VirtualDesktopSessionState.DELETED,
    ):
        session = VirtualDesktopSession(state=state)
        stamp(session)
        assert session.stopped_on is not None, state


def test_the_stop_time_is_written_on_the_update_that_records_the_deletion():
    """
    the table row is hard deleted, so the last update() before it goes is the only
    chance to persist a stop time. it has to carry one.
    """
    import inspect
    from ideavirtualdesktopcontroller.app.sessions import (
        virtual_desktop_session_db as module,
    )

    # the stamp runs before the entry is built, so whatever update() writes carries it
    update_source = inspect.getsource(module.VirtualDesktopSessionDB.update)
    stamp_line = update_source.index('_stamp_stopped_on')
    convert_line = update_source.index('convert_session_object_to_db_dict')
    assert stamp_line < convert_line

    assert VirtualDesktopSessionState.DELETING in module.STOP_TIME_STATES
    assert VirtualDesktopSessionState.DELETED in module.STOP_TIME_STATES


def test_the_stop_time_is_wired_into_both_db_conversions():
    """
    The rule is inert unless the field is actually persisted and read back. A full
    round trip needs a software stack, a project and a seven day schedule built just to
    reach one key, so this asserts the wiring structurally instead: delete either half
    and it fails. The behavioural contract, that a consumer sees the field on a session
    document, is covered by the cost tests in idea-cluster-manager.
    """
    import inspect
    from ideavirtualdesktopcontroller.app.sessions import (
        virtual_desktop_session_db as module,
    )

    write = inspect.getsource(
        module.VirtualDesktopSessionDB.convert_session_object_to_db_dict
    )
    read = inspect.getsource(
        module.VirtualDesktopSessionDB.convert_db_dict_to_session_object
    )

    assert 'USER_SESSION_DB_STOPPED_ON_KEY' in write
    assert 'USER_SESSION_DB_STOPPED_ON_KEY' in read
    assert 'stopped_on=' in read
    # None rather than 0: to_milliseconds(None) is 0, which reads back as 1970
    assert 'if session.stopped_on is None' in write


def test_the_stop_time_is_stamped_on_the_persisting_path():
    """every state change goes through update(), which is why the rule lives there."""
    import inspect
    from ideavirtualdesktopcontroller.app.sessions import (
        virtual_desktop_session_db as module,
    )

    assert '_stamp_stopped_on' in inspect.getsource(
        module.VirtualDesktopSessionDB.update
    )
