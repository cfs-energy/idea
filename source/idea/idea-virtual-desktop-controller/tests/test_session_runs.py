"""
Test Cases for the recorded desktop runs

A desktop is billed for the time its instance ran. The controller writes one run row per
start and closes it on the stop, on the write that changes the state, so the cost pages
no longer bill one interval from creation to the last stop. No AWS calls are made.
"""

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_history_db import (
    VirtualDesktopSessionHistoryDB,
)

from boto3.dynamodb.conditions import Key
from unittest.mock import Mock


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


class FakeRunTable:
    """keyed like the history table; query honours the begins_with prefix and open filter."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item['owner'], Item['idea_session_id'])] = dict(Item)

    def query(self, **kwargs):
        # owner = :owner and begins_with(idea_session_id, '<session>#run#'), with the
        # open-run filter applied the way DynamoDB would
        owner, prefix = (
            condition.get_expression()['values'][1]
            for condition in kwargs['KeyConditionExpression'].get_expression()['values']
        )
        return {
            'Items': [
                dict(item)
                for item in self.items.values()
                if item['owner'] == owner
                and item['idea_session_id'].startswith(prefix)
                and 'stopped_on' not in item
            ]
        }

    def update_item(self, Key, ExpressionAttributeValues, **kwargs):
        item = self.items[(Key['owner'], Key['idea_session_id'])]
        item['stopped_on'] = ExpressionAttributeValues[':stopped_on']


def build_history():
    history = VirtualDesktopSessionHistoryDB(FakeContext(), logger=Mock())
    history._table_obj = FakeRunTable()
    history._initialized = True
    return history


def build_db(history, old_state):
    db = object.__new__(VirtualDesktopSessionDB)
    db._logger = Mock()
    db._history_db = history
    db._table_obj = Mock()
    db._table_obj.update_item.return_value = {
        'Attributes': {}
        if old_state is None
        else {sessions_constants.USER_SESSION_DB_STATE_KEY: old_state}
    }
    db.convert_session_object_to_db_dict = lambda session: {
        sessions_constants.USER_SESSION_DB_HASH_KEY: session.owner,
        sessions_constants.USER_SESSION_DB_RANGE_KEY: session.idea_session_id,
        sessions_constants.USER_SESSION_DB_STATE_KEY: session.state,
    }
    db.convert_db_dict_to_session_object = lambda entry: entry
    db.trigger_update_event = Mock()
    return db


def session(state):
    return VirtualDesktopSession(
        idea_session_id='sess-1',
        owner='user-a',
        state=state,
        server=VirtualDesktopServer(instance_id='i-0abc', instance_type='m5.large'),
    )


def runs(history):
    return list(history._table_obj.items.values())


def test_a_start_opens_a_run_and_the_stop_closes_it():
    history = build_history()

    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.RESUMING))
    [run] = runs(history)
    assert run['run_of'] == 'sess-1'
    assert run['idea_session_id'].startswith('sess-1#run#')
    assert run['instance_id'] == 'i-0abc'
    assert run['instance_type'] == 'm5.large'
    assert 'stopped_on' not in run

    # resuming to ready is the same run, not a second one
    build_db(history, 'RESUMING').update(session(VirtualDesktopSessionState.READY))
    assert len(runs(history)) == 1

    build_db(history, 'READY').update(session(VirtualDesktopSessionState.STOPPED))
    [run] = runs(history)
    assert run['stopped_on'] >= run['started_on']


def test_a_write_that_does_not_change_whether_it_runs_records_nothing():
    history = build_history()
    # a rename of a running desktop, and the cleanup sweep writing a stopped one
    build_db(history, 'READY').update(session(VirtualDesktopSessionState.READY))
    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.STOPPED))
    assert runs(history) == []


def test_a_desktop_terminated_from_running_closes_its_run():
    history = build_history()
    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.READY))
    build_db(history, 'READY').update(session(VirtualDesktopSessionState.DELETING))
    [run] = runs(history)
    assert 'stopped_on' in run


def test_a_new_desktop_opens_its_first_run():
    history = build_history()
    db = build_db(history, None)
    db.trigger_create_event = Mock()
    db.convert_db_dict_to_session_object = Mock()
    db.create(session(VirtualDesktopSessionState.PROVISIONING))
    [run] = runs(history)
    assert 'stopped_on' not in run


def test_a_deletion_closes_any_run_still_open():
    history = build_history()
    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.READY))
    db = build_db(history, None)
    db._table_obj.delete_item.return_value = {}
    db.delete(session(VirtualDesktopSessionState.READY))
    run = next(item for item in runs(history) if 'run_of' in item)
    assert 'stopped_on' in run


def test_a_failed_run_write_never_fails_the_state_change():
    history = build_history()
    history._table_obj.put_item = Mock(side_effect=RuntimeError('throttled'))
    history._table_obj.query = Mock(side_effect=RuntimeError('throttled'))
    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.READY))
    build_db(history, 'READY').update(session(VirtualDesktopSessionState.STOPPED))


def test_reconcile_opens_one_run_for_a_running_desktop_without_one():
    history = build_history()
    # one desktop running with an open run already, one running without, one stopped
    build_db(history, 'STOPPED').update(session(VirtualDesktopSessionState.READY))
    without = session(VirtualDesktopSessionState.READY)
    without.idea_session_id = 'sess-2'
    stopped = session(VirtualDesktopSessionState.STOPPED)
    stopped.idea_session_id = 'sess-3'
    listing = [session(VirtualDesktopSessionState.READY), without, stopped]

    def open_runs(desktop):
        return history._table_obj.query(
            KeyConditionExpression=Key('owner').eq(desktop.owner)
            & Key('idea_session_id').begins_with(f'{desktop.idea_session_id}#run#')
        )['Items']

    db = build_db(history, None)
    db.list_all_from_db = lambda request: type(
        'Page', (), {'listing': listing, 'cursor': None}
    )()

    assert db.reconcile_runs() == 1
    reconciled = [item for item in runs(history) if item.get('reconciled')]
    assert [item['run_of'] for item in reconciled] == ['sess-2']
    assert len(open_runs(listing[0])) == 1
    assert open_runs(stopped) == []

    # a restart, or another replica that becomes leader, opens nothing more
    assert db.reconcile_runs() == 0
    assert len(runs(history)) == 2
