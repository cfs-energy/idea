"""
Test Cases for persisting a desktop stop time

These drive the real converter and the real update(), because the stop time is written
by one and read by another and the two have to agree on what a timestamp is. An epoch
int stored in a field the converter calls .timestamp() on breaks every update of a
stopping or terminating desktop, and a test of the stamp alone cannot see it.
"""

from ideadatamodel import (
    Project,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    VirtualDesktopSessionType,
    VirtualDesktopSoftwareStack,
    VirtualDesktopWeekSchedule,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_history_db import (
    VirtualDesktopSessionHistoryDB,
    to_epoch_ms,
)

import arrow


class StubChildDB:
    """the collaborator conversions, under both spellings the session db uses"""

    @staticmethod
    def convert_server_object_to_db_dict(_server):
        return {}

    @staticmethod
    def convert_db_entry_to_server_object(_entry):
        return None

    @staticmethod
    def convert_db_dict_to_server_object(_entry):
        return None

    @staticmethod
    def convert_software_stack_object_to_db_dict(_stack):
        return {}

    @staticmethod
    def convert_db_entry_to_software_stack_object(_entry):
        return None

    @staticmethod
    def convert_db_dict_to_software_stack_object(_entry):
        return None

    @staticmethod
    def convert_schedule_object_to_db_dict(_schedule):
        return {}

    @staticmethod
    def convert_db_entry_to_schedule_object(_entry):
        return None

    @staticmethod
    def convert_db_dict_to_schedule_object(_entry):
        return None


class FakeTable:
    def __init__(self):
        self.updated = None
        self.deleted = None

    def update_item(self, **kwargs):
        self.updated = kwargs
        return {'Attributes': {}}

    def delete_item(self, **kwargs):
        self.deleted = kwargs
        return {'Attributes': {'owner': 'user-a', 'idea_session_id': 'sess-1'}}


class FakeHistoryDB:
    def __init__(self, raises=False):
        self.recorded = []
        self.raises = raises

    def record_termination(self, session, deleted_on=None):
        if self.raises:
            raise RuntimeError('ResourceNotFoundException')
        self.recorded.append(session)


class FakeLogger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args, **kwargs):
        self.messages.append(str(message))

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def build_db(history_raises=False):
    db = object.__new__(VirtualDesktopSessionDB)
    child = StubChildDB()
    db._server_db = child
    db._software_stack_db = child
    db._schedule_db = child
    db._table_obj = FakeTable()
    db._logger = FakeLogger()
    db._history_db = FakeHistoryDB(raises=history_raises)
    # the notifiable base publishes to a queue; nothing here needs that
    db.trigger_update_event = lambda *args, **kwargs: None
    db.trigger_delete_event = lambda *args, **kwargs: None
    return db


def build_session(state, stopped_on=None, created_on=None):
    return VirtualDesktopSession(
        idea_session_id='sess-1',
        owner='user-a',
        name='MyDesktop1',
        type=VirtualDesktopSessionType.VIRTUAL,
        state=state,
        created_on=created_on,
        stopped_on=stopped_on,
        server=VirtualDesktopServer(instance_type='m5.large'),
        software_stack=VirtualDesktopSoftwareStack(base_os='amazonlinux2023'),
        project=Project(project_id='project-1', name='project-a', title='Project A'),
        schedule=VirtualDesktopWeekSchedule(),
    )


def written(db):
    return db._table_obj.updated['ExpressionAttributeValues']


def test_stopping_a_desktop_persists_a_stop_time_without_raising():
    db = build_db()
    session = build_session(
        VirtualDesktopSessionState.STOPPED,
        created_on=arrow.utcnow().shift(hours=-3).datetime,
    )

    db.update(session)

    # the stamp must leave a datetime here: the next line calls .timestamp() on it
    stopped_on = written(db)[':stopped_on']
    assert isinstance(stopped_on, int)
    assert stopped_on > 1_600_000_000_000


def test_terminating_a_running_desktop_persists_a_stop_time_without_raising():
    db = build_db()
    session = build_session(VirtualDesktopSessionState.DELETING)

    db.update(session)

    assert isinstance(written(db)[':stopped_on'], int)


def test_a_termination_carrying_epoch_milliseconds_is_recorded_correctly():
    """
    the whole termination path with an int timestamp on the session, through the real
    history record builder. converting one of these as though it were a datetime raises.
    """
    stamped = arrow.utcnow().shift(hours=-2)
    written = []

    class FakeHistoryTable:
        @staticmethod
        def put_item(Item):
            written.append(Item)

    class FakeConfig:
        @staticmethod
        def get_int(key, default=None):
            return default

    class FakeContext:
        @staticmethod
        def cluster_name():
            return 'idea-test'

        @staticmethod
        def module_id():
            return 'vdc'

        @staticmethod
        def config():
            return FakeConfig()

    history = VirtualDesktopSessionHistoryDB(FakeContext(), logger=FakeLogger())
    history._table_obj = FakeHistoryTable()
    history._initialized = True

    db = build_db()
    db._history_db = history

    session = build_session(VirtualDesktopSessionState.DELETING)
    # whatever the model declares, a caller can hand this over as milliseconds
    session.stopped_on = stamped.int_timestamp * 1000
    session.created_on = stamped.shift(hours=-1).int_timestamp * 1000

    db.delete(session)

    # taken at face value, not run through a datetime conversion that would raise
    assert written[0]['stopped_on'] == stamped.int_timestamp * 1000
    assert written[0]['created_on'] == stamped.shift(hours=-1).int_timestamp * 1000
    assert db._table_obj.deleted is not None


def test_the_written_stop_time_reads_back_as_the_same_moment():
    db = build_db()
    stopped_at = arrow.utcnow().shift(hours=-5).floor('second')
    session = build_session(
        VirtualDesktopSessionState.STOPPED, stopped_on=stopped_at.datetime
    )

    db.update(session)
    restored = db.convert_db_dict_to_session_object(
        {
            'owner': 'user-a',
            'idea_session_id': 'sess-1',
            'base_os': 'amazonlinux2023',
            'session_type': 'VIRTUAL',
            'state': 'STOPPED',
            'stopped_on': written(db)[':stopped_on'],
        }
    )

    assert restored.stopped_on == stopped_at.datetime


def test_deleting_a_desktop_records_its_history_and_removes_the_row():
    db = build_db()
    session = build_session(VirtualDesktopSessionState.DELETING)

    db.delete(session)

    assert db._history_db.recorded[0].idea_session_id == 'sess-1'
    assert db._table_obj.deleted is not None


def test_a_history_write_that_raises_still_lets_the_deletion_finish():
    db = build_db(history_raises=True)
    session = build_session(VirtualDesktopSessionState.DELETING)

    # the portal must not report a failure for a desktop that is on its way out
    db.delete(session)

    assert db._table_obj.deleted is not None


def test_epoch_conversion_accepts_both_shapes_and_nothing_else():
    moment = arrow.utcnow().floor('second')
    assert to_epoch_ms(moment.datetime) == moment.int_timestamp * 1000
    assert to_epoch_ms(moment.int_timestamp * 1000) == moment.int_timestamp * 1000
    assert to_epoch_ms(None) is None
    assert to_epoch_ms('not a time') is None


def test_the_stamp_stores_something_the_converter_can_write():
    """the write and read halves have to agree on what a timestamp is"""
    from ideasdk.utils import Utils

    session = build_session(VirtualDesktopSessionState.STOPPED)
    VirtualDesktopSessionDB._stamp_stopped_on(session)

    # not an int of epoch seconds: to_milliseconds would raise, and a coerced one would
    # land in 1970
    assert Utils.to_milliseconds(session.stopped_on) > 1_600_000_000_000
