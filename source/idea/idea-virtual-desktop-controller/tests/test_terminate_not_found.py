"""
Deleting a session the database does not have.

An admin who deletes another user's session without naming the owner makes the
lookup miss. That path used to report against the row it failed to find, which is
None, so the caller got `'NoneType' object has no attribute 'idea_session_id'`
instead of a refusal naming the session.
"""

from unittest.mock import Mock
from typing import Optional

from ideadatamodel import VirtualDesktopSession
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


class FakeSessionDB:
    """Nothing is ever found - every lookup misses, as it does for the wrong owner."""

    def __init__(self):
        self.lookups = []

    def get_from_db(
        self, idea_session_owner: Optional[str], idea_session_id: Optional[str]
    ):
        self.lookups.append((idea_session_owner, idea_session_id))
        return None


class FakeBrokerClient:
    def __init__(self):
        self.calls = []

    def delete_sessions(self, sessions):
        self.calls.append(list(sessions))
        return [], []


class FakeContext:
    def __init__(self):
        self.dcv_broker_client = FakeBrokerClient()


def build_utils(session_db):
    # when nothing is found there is nothing to delete, so only the db, the logger
    # and the broker call are reached; the constructor and its aws clients are skipped
    utils = object.__new__(VirtualDesktopSessionUtils)
    utils._session_db = session_db
    utils._logger = Mock()
    utils.context = FakeContext()
    return utils


def a_session(**kwargs):
    return VirtualDesktopSession(
        idea_session_id=kwargs.get('idea_session_id', 'sess-1'),
        name=kwargs.get('name', 'bedrock-ip-check'),
        owner=kwargs.get('owner'),
    )


def test_a_session_the_database_does_not_have_is_refused_not_crashed():
    db = FakeSessionDB()
    utils = build_utils(db)
    requested = a_session()

    success, failed = utils.terminate_sessions([requested])

    assert success == []
    assert len(failed) == 1
    assert failed[0] is requested, 'the refusal must describe the request, not the miss'
    assert 'Nothing to delete' in failed[0].failure_reason


def test_the_refusal_names_the_session_that_was_asked_for():
    db = FakeSessionDB()
    utils = build_utils(db)
    requested = a_session(idea_session_id='abc-123', name='bedrock-ip-check')

    _success, failed = utils.terminate_sessions([requested])

    reason = failed[0].failure_reason
    assert 'abc-123' in reason
    assert 'bedrock-ip-check' in reason


def test_an_absent_owner_still_produces_a_readable_refusal():
    # this is the real trigger: an admin deleting someone else's session and not
    # naming the owner, so the composite key misses
    db = FakeSessionDB()
    utils = build_utils(db)

    _success, failed = utils.terminate_sessions([a_session(owner=None)])

    assert failed[0].failure_reason  # no exception, and something to show the caller
    assert db.lookups == [(None, 'sess-1')]


def test_every_requested_session_is_accounted_for():
    db = FakeSessionDB()
    utils = build_utils(db)
    requested = [
        a_session(idea_session_id='one', name='a'),
        a_session(idea_session_id='two', name='b'),
        a_session(idea_session_id='three', name='c'),
    ]

    success, failed = utils.terminate_sessions(requested)

    assert success == []
    assert [f.idea_session_id for f in failed] == ['one', 'two', 'three']


def test_nothing_is_sent_to_the_broker_when_nothing_was_found():
    db = FakeSessionDB()
    utils = build_utils(db)

    utils.terminate_sessions([a_session()])

    assert utils.context.dcv_broker_client.calls == [[]]
