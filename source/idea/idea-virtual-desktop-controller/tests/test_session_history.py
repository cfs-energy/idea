"""
Test Cases for the terminated desktop record

The sessions table hard deletes and the search document goes with it, so this row is
all that remains of a desktop that ran and was then deleted. No AWS calls are made.
"""

from ideadatamodel import (
    Project,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_history_db import (
    DEFAULT_RETENTION_DAYS,
    VirtualDesktopSessionHistoryDB,
)

import arrow
import pathlib
from datetime import timedelta
from unittest.mock import Mock

import pytest


class FakeTable:
    def __init__(self, raises=False):
        self.items = []
        self.raises = raises

    def put_item(self, Item):
        if self.raises:
            raise RuntimeError('ProvisionedThroughputExceededException')
        self.items.append(Item)


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))


class FakeConfig:
    @staticmethod
    def get_int(key, default=None):
        return default


class FakeContext:
    def cluster_name(self):
        return 'idea-test'

    def module_id(self):
        return 'vdc'

    def config(self):
        return FakeConfig()


def build_db(raises=False):
    db = VirtualDesktopSessionHistoryDB(FakeContext(), logger=FakeLogger())
    db._table_obj = FakeTable(raises=raises)
    db._initialized = True
    return db


def build_session(state, stopped_on=None, created_on=None, updated_on=None):
    return VirtualDesktopSession(
        idea_session_id='sess-1',
        owner='user-a',
        name='my-desktop',
        state=state,
        created_on=created_on,
        updated_on=updated_on,
        stopped_on=stopped_on,
        server=VirtualDesktopServer(instance_type='m5.large'),
        project=Project(project_id='project-1', name='project-a'),
    )


def test_the_table_is_named_for_the_controller():
    db = build_db()
    assert db.table_name == 'idea-test.vdc.controller.session-history'


def test_a_desktop_stopped_before_deletion_is_recorded_at_its_stop_time():
    now = arrow.utcnow()
    stopped_at = now.shift(days=-4).floor('second').datetime
    db = build_db()

    db.record_termination(
        build_session(VirtualDesktopSessionState.DELETED, stopped_on=stopped_at),
        deleted_on=now.int_timestamp * 1000,
    )

    entry = db._table_obj.items[0]
    # stopped Monday, deleted Friday: it stopped costing on Monday
    assert entry['stopped_on'] == int(stopped_at.timestamp() * 1000)
    assert entry['deleted_on'] > entry['stopped_on']


def test_a_desktop_terminated_while_running_is_recorded_at_the_deletion():
    deleted_on = arrow.utcnow().int_timestamp * 1000
    db = build_db()

    # no stop stamp: it went straight from running to gone
    db.record_termination(
        build_session(VirtualDesktopSessionState.DELETED), deleted_on=deleted_on
    )

    entry = db._table_obj.items[0]
    assert entry['stopped_on'] == deleted_on
    assert entry['deleted_on'] == deleted_on


def test_a_legacy_stopped_desktop_uses_its_last_update_as_an_estimate():
    now = arrow.utcnow()
    updated_at = now.shift(days=-4).floor('second').datetime
    db = build_db()

    db.record_termination(
        build_session(VirtualDesktopSessionState.STOPPED, updated_on=updated_at),
        deleted_on=now.int_timestamp * 1000,
    )

    entry = db._table_obj.items[0]
    assert entry['stopped_on'] == int(updated_at.timestamp() * 1000)
    assert entry['stop_time_estimated'] is True
    assert entry['deleted_on'] > entry['stopped_on']


def test_the_record_carries_what_pricing_needs():
    db = build_db()
    db.record_termination(build_session(VirtualDesktopSessionState.DELETED))

    entry = db._table_obj.items[0]
    assert entry['owner'] == 'user-a'
    assert entry['idea_session_id'] == 'sess-1'
    assert entry['name'] == 'my-desktop'
    assert entry['instance_type'] == 'm5.large'
    assert entry['project_id'] == 'project-1'
    assert entry['ttl'] > 0


def test_rows_expire_after_the_retention_window():
    db = build_db()
    db.record_termination(build_session(VirtualDesktopSessionState.DELETED))

    entry = db._table_obj.items[0]
    expected = arrow.utcnow().shift(days=DEFAULT_RETENTION_DAYS).int_timestamp
    assert DEFAULT_RETENTION_DAYS == 400
    assert abs(entry['ttl'] - expected) < 120


def test_a_failed_write_never_blocks_the_deletion():
    db = build_db(raises=True)

    # a desktop the user asked to remove is removed whether or not bookkeeping worked
    db.record_termination(build_session(VirtualDesktopSessionState.DELETED))

    assert db._logger.warnings != []


def test_a_session_without_an_owner_is_not_recorded():
    db = build_db()
    db.record_termination(VirtualDesktopSession(idea_session_id='sess-1'))
    assert db._table_obj.items == []


def test_the_record_is_written_on_the_one_path_every_deletion_takes():
    """
    the running desktop path reaches delete() by way of the deletion validation
    handler and the stopped one calls it directly, so writing the record there covers
    both, and it is written before the row is removed.
    """
    import inspect
    from ideavirtualdesktopcontroller.app.sessions import (
        virtual_desktop_session_db as module,
    )

    source = inspect.getsource(module.VirtualDesktopSessionDB.delete)
    assert 'record_termination' in source
    assert source.index('record_termination') < source.index('delete_item')


@pytest.mark.parametrize('previous_stop', ['missing', 'recorded', 'already-warned'])
def test_cleanup_warning_deletion_and_pricing_preserve_stop_time(
    monkeypatch, previous_stop
):
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
        VirtualDesktopSessionDB,
    )
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    monkeypatch.syspath_prepend(
        str(
            pathlib.Path(__file__).resolve().parents[2] / 'idea-cluster-manager' / 'src'
        )
    )
    from ideaclustermanager.app.costs.my_costs_service import MyCostsService

    now = arrow.utcnow().floor('second')
    stopped = now.shift(days=-60).datetime
    created = now.shift(days=-61).datetime
    session = build_session(
        VirtualDesktopSessionState.STOPPED,
        created_on=created,
        updated_on=stopped,
        stopped_on=stopped if previous_stop == 'recorded' else None,
    )
    history = build_db()
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._table_obj = Mock()
    db._server_db = Mock()
    db._software_stack_db = Mock()
    db._schedule_db = Mock()
    from ideadatamodel import VirtualDesktopSoftwareStack

    session.software_stack = VirtualDesktopSoftwareStack()
    db._table.update_item.return_value = {'Attributes': {}}
    db._table.delete_item.return_value = {
        'Attributes': {
            'owner': session.owner,
            'idea_session_id': session.idea_session_id,
        }
    }
    db._history_db = history
    db.trigger_update_event = Mock()
    db.trigger_delete_event = Mock()
    db.convert_db_dict_to_session_object = Mock(return_value=session)
    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils.context.config().get_bool.return_value = False
    utils._logger = Mock()
    utils._session_db = db
    if previous_stop == 'already-warned':
        session.cleanup_warning_stop_time = stopped
        session.stopped_on = now.shift(days=-7).datetime
    else:
        utils._warn_owner(session, stopped, timedelta(days=60), now.datetime, False)
        assert session.stopped_on == (stopped if previous_stop == 'recorded' else None)
    db.delete(session)
    entry = history._table_obj.items[0]
    assert entry['stopped_on'] == int(stopped.timestamp() * 1000)
    assert entry['stop_time_estimated'] is (previous_stop != 'recorded')
    pricing = MyCostsService.__new__(MyCostsService)
    pricing._ondemand_price = Mock(return_value=1)
    priced = pricing._desktop_session(
        {
            **entry,
            'state': 'DELETED',
            'server': {'instance_type': entry['instance_type']},
        },
        int(created.timestamp() * 1000),
        now.int_timestamp * 1000,
    )
    assert priced.hours == 24
    assert priced.cost == 24
    assert bool(priced.stop_time_estimated) is (previous_stop != 'recorded')
