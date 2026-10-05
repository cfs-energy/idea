"""
Deleting desktops whose instances are partly gone.

EC2 refuses a whole TerminateInstances call when one id no longer exists and terminates
none of the others. The live instances must still be terminated, and a session whose
instance could not be terminated for any other reason must not be dropped from the
database (its instance would run on with nothing pointing at it).
"""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    SocaException,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    errorcodes,
)
from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_deletion_event_handler import (
    ValidateDCVSessionDeletionEventHandler,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_utils import (
    VirtualDesktopServerUtils,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)

LIVE = ['i-0000000000000000a', 'i-0000000000000000b']
GONE = ['i-0000000000000000c', 'i-0000000000000000d']


class FakeEc2:
    """names one missing id per refusal, so the retry has to converge, not get lucky"""

    def __init__(self, existing, error=None):
        self.existing = set(existing)
        self.error = error
        self.calls = []

    def terminate_instances(self, InstanceIds):
        self.calls.append(list(InstanceIds))
        if self.error:
            raise self.error
        gone = [i for i in InstanceIds if i not in self.existing]
        if gone:
            raise ClientError(
                {
                    'Error': {
                        'Code': 'InvalidInstanceID.NotFound',
                        'Message': f"The instance ID '{gone[0]}' does not exist",
                    }
                },
                'TerminateInstances',
            )
        return {'TerminatingInstances': [{'InstanceId': i} for i in InstanceIds]}


def server_utils(ec2):
    utils = object.__new__(VirtualDesktopServerUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils.ec2_client = ec2
    utils._server_db = Mock()
    utils._server_db.get.return_value = None
    utils._controller_utils = Mock()
    utils._controller_utils.is_active_directory.return_value = False
    return utils


def a_session(instance_id, state=VirtualDesktopSessionState.STOPPED):
    return VirtualDesktopSession(
        idea_session_id=f'sess-{instance_id}',
        owner='someone',
        name=instance_id,
        state=state,
        dcv_session_id=f'dcv-{instance_id}',
        server=VirtualDesktopServer(instance_id=instance_id),
    )


def servers(ids):
    return [VirtualDesktopServer(instance_id=i) for i in ids]


def test_live_instances_are_terminated_when_some_ids_are_gone():
    ec2 = FakeEc2(LIVE)
    utils = server_utils(ec2)

    response = utils.terminate_dcv_hosts(servers(LIVE + GONE), force=True)

    assert 'ERROR' not in response
    assert ec2.calls[-1] == LIVE
    assert sorted(response['MissingInstanceIds']) == GONE
    released = {c.args[0].instance_id for c in utils._server_db.delete.call_args_list}
    assert released == set(LIVE + GONE)


def test_all_ids_gone_is_a_success():
    utils = server_utils(FakeEc2([]))

    response = utils.terminate_dcv_hosts(servers(GONE))

    assert 'ERROR' not in response
    assert sorted(response['MissingInstanceIds']) == GONE


def test_another_error_terminates_nothing_and_says_so():
    error = ClientError(
        {'Error': {'Code': 'UnauthorizedOperation', 'Message': 'no'}},
        'TerminateInstances',
    )
    utils = server_utils(FakeEc2(LIVE, error=error))

    response = utils.terminate_dcv_hosts(servers(LIVE))

    assert 'UnauthorizedOperation' in response['ERROR']
    utils._server_db.delete.assert_not_called()


def session_utils(ec2, sessions):
    utils = object.__new__(VirtualDesktopSessionUtils)
    by_id = {s.idea_session_id: s for s in sessions}
    utils._session_db = Mock()
    utils._session_db.get_from_db.side_effect = (
        lambda idea_session_owner, idea_session_id: by_id[idea_session_id]
    )
    utils._logger = Mock()
    utils.context = Mock()
    utils.context.dcv_broker_client.delete_sessions.return_value = ([], [])
    utils._server_utils = server_utils(ec2)
    utils._schedule_utils = Mock()
    utils._session_permission_utils = Mock()
    return utils


def deleted(utils):
    return {c.args[0].idea_session_id for c in utils._session_db.delete.call_args_list}


def test_deleting_stopped_desktops_with_gone_instances_removes_them_all():
    sessions = [a_session(i) for i in LIVE + GONE]
    ec2 = FakeEc2(LIVE)
    utils = session_utils(ec2, sessions)

    success, failed = utils.terminate_sessions(sessions)

    assert failed == []
    assert len(success) == 4
    assert ec2.calls[-1] == LIVE
    assert deleted(utils) == {s.idea_session_id for s in sessions}


def test_a_desktop_whose_instance_was_not_terminated_is_kept():
    error = ClientError(
        {'Error': {'Code': 'RequestLimitExceeded', 'Message': 'slow down'}},
        'TerminateInstances',
    )
    sessions = [a_session(i) for i in LIVE]
    utils = session_utils(FakeEc2(LIVE, error=error), sessions)

    success, failed = utils.terminate_sessions(sessions)

    assert success == []
    assert [s.idea_session_id for s in failed] == [s.idea_session_id for s in sessions]
    assert 'RequestLimitExceeded' in failed[0].failure_reason
    utils._session_db.delete.assert_not_called()


def test_the_deletion_event_keeps_the_row_and_retries_when_termination_fails():
    handler = object.__new__(ValidateDCVSessionDeletionEventHandler)
    handler._logger = Mock()
    handler.server_utils = Mock()
    handler.server_utils.terminate_dcv_hosts.return_value = {'ERROR': 'throttled'}
    handler.session_db = Mock()
    handler.schedule_utils = Mock()
    handler.session_permission_utils = Mock()

    with pytest.raises(SocaException) as raised:
        handler._continue_delete_session(
            'msg-1', a_session(LIVE[0], VirtualDesktopSessionState.DELETING)
        )

    assert raised.value.error_code == errorcodes.DO_NOT_DELETE_MESSAGE
    handler.session_db.delete.assert_not_called()


def test_the_deletion_event_deletes_the_row_once_the_host_is_gone():
    handler = object.__new__(ValidateDCVSessionDeletionEventHandler)
    handler._logger = Mock()
    handler.server_utils = Mock()
    handler.server_utils.terminate_dcv_hosts.return_value = {
        'TerminatingInstances': [],
        'MissingInstanceIds': [GONE[0]],
    }
    handler.session_db = Mock()
    handler.schedule_utils = Mock()
    handler.session_permission_utils = Mock()

    handler._continue_delete_session(
        'msg-1', a_session(GONE[0], VirtualDesktopSessionState.DELETING)
    )

    handler.session_db.delete.assert_called_once()
