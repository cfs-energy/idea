"""
A reboot that EC2 refuses.

The state write and the success list both wait on the reboot result, so a refused
reboot is reported as a failure and the row still describes the state the desktop
is actually in.
"""

from unittest.mock import Mock
from typing import List, Optional

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


class FakeSessionDB:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def get_from_db(
        self, idea_session_owner: Optional[str], idea_session_id: Optional[str]
    ):
        return self._rows.get(idea_session_id)

    def update(self, session):
        self.updates.append((session.idea_session_id, session.state))
        return session


class FakeServerUtils:
    """Stands in for EC2 - either it refuses the reboot or it accepts it."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def reboot_dcv_hosts(self, servers: List[VirtualDesktopServer]):
        self.calls.append([s.instance_id for s in servers])
        return self._response


class FakeBrokerClient:
    def get_active_counts_for_sessions(self, sessions):
        return []


class FakeContext:
    def __init__(self):
        self.dcv_broker_client = FakeBrokerClient()


def a_session(session_id='sess-1', owner='alice', instance_id='i-1'):
    return VirtualDesktopSession(
        idea_session_id=session_id,
        name='desktop',
        owner=owner,
        state=VirtualDesktopSessionState.READY,
        force=True,  # skips the broker connection-count check
        server=VirtualDesktopServer(instance_id=instance_id),
    )


def build_utils(rows, reboot_response):
    utils = object.__new__(VirtualDesktopSessionUtils)
    utils._session_db = FakeSessionDB(rows)
    utils._server_utils = FakeServerUtils(reboot_response)
    utils._logger = Mock()
    utils.context = FakeContext()
    return utils


def test_a_refused_reboot_is_reported_as_a_failure():
    row = a_session()
    utils = build_utils({'sess-1': row}, {'ERROR': 'Client.UnsupportedOperation'})

    success, failed = utils.reboot_sessions([a_session()])

    assert success == []
    assert len(failed) == 1
    assert 'Client.UnsupportedOperation' in failed[0].failure_reason


def test_a_refused_reboot_does_not_write_resuming():
    row = a_session()
    utils = build_utils({'sess-1': row}, {'ERROR': 'Client.UnsupportedOperation'})

    utils.reboot_sessions([a_session()])

    assert utils._session_db.updates == [], 'state must not advance on a refused reboot'


def test_an_accepted_reboot_still_succeeds():
    row = a_session()
    utils = build_utils({'sess-1': row}, {'RequestId': 'abc'})

    success, failed = utils.reboot_sessions([a_session()])

    assert failed == []
    assert [s.idea_session_id for s in success] == ['sess-1']
    assert utils._session_db.updates == [
        ('sess-1', VirtualDesktopSessionState.RESUMING)
    ]


def test_the_reboot_reaches_ec2_with_the_right_instance():
    row = a_session(instance_id='i-abc123')
    utils = build_utils({'sess-1': row}, {'RequestId': 'abc'})

    utils.reboot_sessions([a_session()])

    assert utils._server_utils.calls == [['i-abc123']]


def test_a_session_the_database_does_not_have_is_refused_not_crashed():
    utils = build_utils({}, {'RequestId': 'abc'})
    requested = a_session(session_id='missing')

    success, failed = utils.reboot_sessions([requested])

    assert success == []
    assert failed[0] is requested, 'the refusal must describe the request, not the miss'
    assert 'missing' in failed[0].failure_reason
