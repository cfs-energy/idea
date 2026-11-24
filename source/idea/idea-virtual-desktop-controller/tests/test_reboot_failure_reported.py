"""
A reboot that EC2 refuses.

The reboot intent is saved before EC2 is called and rolled back if EC2 refuses.
"""

import pytest
from botocore.exceptions import ClientError
from ideadatamodel.exceptions import SocaException
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
    VirtualDesktopEventType,
)
from unittest.mock import Mock
from typing import List, Optional

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client_utils import (
    BROKER_UNAVAILABLE_MESSAGE,
    DCVBrokerClientUtils,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    REBOOT_HOST_UNKNOWN_MESSAGE,
    REBOOT_NO_HOST_MESSAGE,
    VirtualDesktopSessionUtils,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)
from stored_session import read_back


class FakeSessionDB:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def get_from_db(
        self, idea_session_owner: Optional[str], idea_session_id: Optional[str]
    ):
        return self._rows.get(idea_session_id)

    def update(self, session, **kwargs):
        self.updates.append((session.idea_session_id, session.state))
        self._rows[session.idea_session_id] = session
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


def a_session(session_id='sess-1', owner='someuser', instance_id='i-1'):
    return VirtualDesktopSession(
        idea_session_id=session_id,
        name='desktop',
        owner=owner,
        state=VirtualDesktopSessionState.READY,
        dcv_session_id='dcv-1',
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


def test_a_refused_reboot_restores_the_original_state():
    row = a_session()
    utils = build_utils({'sess-1': row}, {'ERROR': 'Client.UnsupportedOperation'})

    utils.reboot_sessions([a_session()])

    stored = utils._session_db._rows['sess-1']
    assert stored.state == VirtualDesktopSessionState.READY
    assert stored.reboot_deadline == 0


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


# --- the broker connection-count check and the host check ----------------------------------

BROKER_TEXT = '(400) Reason: Bad Request HTTP response headers: marker-value'


def refuse_empty_ids(sessions):
    if any(not s.dcv_session_id for s in sessions):
        raise Exception(BROKER_TEXT)
    return {'sessions': {s.dcv_session_id: {'num_of_connections': 0} for s in sessions}}


def ec2_with(instance=None, error_code=None):
    """the instance EC2 describes for i-1, or the error it raises"""
    ec2 = Mock()
    if error_code:
        ec2.describe_instances.side_effect = ClientError(
            {'Error': {'Code': error_code, 'Message': 'marker'}}, 'DescribeInstances'
        )
    else:
        ec2.describe_instances.return_value = {
            'Reservations': [{'Instances': [instance]}]
        }
    return ec2


def broker_backed_utils(row, describe, ec2):
    """the real reboot, count helper and instance read; the broker call and EC2 are stubbed"""
    counts = object.__new__(DCVBrokerClientUtils)
    counts._logger = Mock()
    counts.context = Mock()
    counts.context.dcv_broker_client.describe_sessions.side_effect = describe
    utils = build_utils({row.idea_session_id: row}, {'RequestId': 'abc'})
    utils.context.dcv_broker_client = Mock()
    utils.context.dcv_broker_client.get_active_counts_for_sessions = (
        counts.get_active_counts_for_sessions
    )
    utils._controller_utils = object.__new__(VirtualDesktopControllerUtils)
    utils._controller_utils._logger = Mock()
    utils._controller_utils.ec2_client = ec2
    return utils, counts.context.dcv_broker_client.describe_sessions


def no_dcv_session():
    # what the host-ready handler leaves when broker session creation keeps failing
    return read_back(state='ERROR', dcv_session_id='')


def a_request():
    return VirtualDesktopSession(idea_session_id='s-1', owner='user-a', force=False)


@pytest.mark.parametrize(
    'ec2',
    [
        ec2_with(error_code='InvalidInstanceID.NotFound'),
        ec2_with({'InstanceId': 'i-1', 'State': {'Name': 'terminated'}}),
    ],
)
def test_a_desktop_whose_host_is_gone_is_refused_with_a_next_step(ec2):
    utils, describe = broker_backed_utils(no_dcv_session(), refuse_empty_ids, ec2)

    success, failed = utils.reboot_sessions([a_request()])

    assert success == []
    assert failed[0].failure_reason == REBOOT_NO_HOST_MESSAGE
    assert describe.call_count == 0
    assert utils._server_utils.calls == []


def test_a_running_host_with_no_dcv_session_is_rebooted_without_asking_the_broker():
    ec2 = ec2_with({'InstanceId': 'i-1', 'State': {'Name': 'running'}})
    utils, describe = broker_backed_utils(no_dcv_session(), refuse_empty_ids, ec2)

    success, failed = utils.reboot_sessions([a_request()])

    assert failed == []
    assert [s.idea_session_id for s in success] == ['s-1']
    assert utils._server_utils.calls == [['i-1']]
    assert describe.call_count == 0


def test_a_host_that_cannot_be_read_is_not_rebooted_and_says_so():
    ec2 = ec2_with(error_code='RequestLimitExceeded')
    utils, _ = broker_backed_utils(no_dcv_session(), refuse_empty_ids, ec2)

    success, failed = utils.reboot_sessions([a_request()])

    assert success == []
    assert failed[0].failure_reason == REBOOT_HOST_UNKNOWN_MESSAGE
    assert utils._server_utils.calls == []


def test_sessions_without_a_dcv_id_are_not_sent_to_the_broker():
    counts = object.__new__(DCVBrokerClientUtils)
    counts._logger = Mock()
    counts.context = Mock()
    counts.context.dcv_broker_client.describe_sessions.side_effect = refuse_empty_ids
    started = read_back(session_id='s-1', state='READY')
    never_started = read_back(session_id='s-2', state='ERROR', dcv_session_id='')

    result = counts.get_active_counts_for_sessions([started, never_started])

    assert [s.connection_count for s in result] == [0, 0]
    sent = counts.context.dcv_broker_client.describe_sessions.call_args.args[0]
    assert [s.dcv_session_id for s in sent] == ['dcv-1']


def test_a_broker_error_is_reported_as_a_product_message():
    def broker_down(sessions):
        raise Exception(BROKER_TEXT)

    utils, _ = broker_backed_utils(
        read_back(state='READY'), broker_down, ec2_with(error_code='unused')
    )

    success, failed = utils.reboot_sessions([a_request()])

    assert success == []
    assert failed[0].failure_reason == BROKER_UNAVAILABLE_MESSAGE
    assert 'HTTP' not in failed[0].failure_reason


# --- the host comes back after a reboot ------------------------------------------------------


def reboot_complete(monkeypatch, row, create):
    """the real reboot-complete and host-ready handlers; the tables and the broker are stubbed"""
    from types import SimpleNamespace

    import ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_reboot_complete_event_handler as module
    from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_ready_event_handler import (
        DCVHostReadyEventHandler,
    )

    rows = {row.idea_session_id: row}
    counter = SimpleNamespace(counter=0)
    broker = Mock()
    broker.create_session.side_effect = create

    def make(cls):
        handler = cls.__new__(cls)
        handler.context = Mock()
        handler.context.dcv_broker_client = broker
        handler._logger = Mock()
        handler.session_db = FakeSessionDB(rows)
        handler.session_counter_db = Mock()
        handler.session_counter_db.get.return_value = counter
        handler.controller_utils = Mock()
        handler.events_utils = Mock()
        return handler

    monkeypatch.setattr(
        module,
        'DCVHostReadyEventHandler',
        lambda context: make(DCVHostReadyEventHandler),
    )
    handler = make(module.DCVHostRebootCompleteEventHandler)
    event = VirtualDesktopEvent(
        event_group_id='s-1',
        event_type=VirtualDesktopEventType.DCV_HOST_REBOOT_COMPLETE_EVENT,
        detail={'idea_session_id': 's-1', 'idea_session_owner': 'user-a'},
    )
    return (
        handler,
        rows,
        counter,
        broker,
        lambda: handler.handle_event('m-1', 'host-role:i-1', event),
    )


def test_a_rebooted_host_with_no_dcv_session_gets_one_created(monkeypatch):
    def accepted(session):
        return session.model_copy(update={'dcv_session_id': 'dcv-new'})

    _, rows, _, broker, deliver = reboot_complete(
        monkeypatch, read_back(state='RESUMING', dcv_session_id=''), accepted
    )

    deliver()

    assert rows['s-1'].dcv_session_id == 'dcv-new'
    assert rows['s-1'].state == VirtualDesktopSessionState.INITIALIZING
    broker.resume_session.assert_not_called()


def test_a_redelivered_creation_keeps_its_attempt_count(monkeypatch):
    def refused(session):
        return session.model_copy(update={'failure_reason': 'marker refusal'})

    handler, rows, counter, _, deliver = reboot_complete(
        monkeypatch, read_back(state='RESUMING', dcv_session_id=''), refused
    )

    for _ in range(2):
        with pytest.raises(SocaException):
            deliver()

    assert counter.counter == 2
    assert rows['s-1'].state == VirtualDesktopSessionState.CREATING


@pytest.mark.parametrize('start', ['READY', 'ERROR'])
def test_a_refused_reboot_restores_the_stored_row(start):
    # through the real session table code, which writes only what changed since the read
    from stored_session import FakeTable, session_table_reader, stored_row

    db = session_table_reader()
    db._table_obj = FakeTable('owner', 'idea_session_id', [stored_row(state=start)])
    db.trigger_update_event = Mock()
    db._history_db = Mock()
    utils = build_utils({}, {'ERROR': 'Client.RequestLimitExceeded'})
    utils._session_db = db

    _, failed = utils.reboot_sessions([a_session(session_id='s-1', owner='user-a')])

    assert len(failed) == 1
    row = db._table_obj.rows[('user-a', 's-1')]
    assert row['state'] == start
    assert not row.get('reboot_deadline')
