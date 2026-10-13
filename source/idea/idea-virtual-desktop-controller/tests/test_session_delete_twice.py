"""a desktop deleted twice, by two terminations racing or a retry, is deleted once and quietly"""

from unittest.mock import Mock

from ideadatamodel import VirtualDesktopSession
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)


def test_a_second_delete_of_a_session_is_quiet():
    db = object.__new__(VirtualDesktopSessionDB)
    db._logger = Mock()
    db._history_db = Mock()
    db._table_obj = Mock()
    db._table_obj.delete_item.side_effect = [
        {
            'Attributes': {
                sessions_constants.USER_SESSION_DB_HASH_KEY: 'user',
                sessions_constants.USER_SESSION_DB_RANGE_KEY: 'session-1',
            }
        },
        {},
    ]
    db.trigger_delete_event = Mock()
    session = VirtualDesktopSession(owner='user', idea_session_id='session-1')
    db.delete(session)
    db.delete(session)
    assert db._table_obj.delete_item.call_count == 2
    assert db.trigger_delete_event.call_count == 1
    assert db._history_db.record_termination.call_count == 2
