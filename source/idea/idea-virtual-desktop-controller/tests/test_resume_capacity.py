from unittest.mock import Mock

from botocore.exceptions import ClientError

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_utils import (
    VirtualDesktopServerUtils,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


def capacity_error():
    return ClientError(
        {'Error': {'Code': 'InsufficientInstanceCapacity', 'Message': 'unavailable'}},
        'StartInstances',
    )


def setup_resume():
    server = VirtualDesktopServer(
        instance_id='i-test',
        instance_type='g4dn.xlarge',
        idea_session_id='session-test',
    )
    session = VirtualDesktopSession(
        idea_session_id='session-test',
        owner='testuser',
        state=VirtualDesktopSessionState.STOPPED,
        server=server,
    )
    hosts = object.__new__(VirtualDesktopServerUtils)
    hosts.ec2_client = Mock()
    hosts.ec2_client.start_instances.side_effect = capacity_error()
    hosts.ec2_client.describe_instances.return_value = {
        'Reservations': [
            {
                'Instances': [
                    {
                        'InstanceId': server.instance_id,
                        'InstanceType': server.instance_type,
                        'Placement': {'AvailabilityZone': 'us-east-1a'},
                    },
                ]
            }
        ]
    }
    hosts._logger = Mock()
    sessions = object.__new__(VirtualDesktopSessionUtils)
    sessions.context = Mock()
    sessions.context.accounts_client.get_user.return_value.user.enabled = True
    sessions._logger = Mock()
    sessions._server_utils = hosts
    sessions._session_db = Mock()
    sessions._session_db.get_from_db.return_value = session
    sessions._session_db.update.side_effect = lambda row: row
    return hosts, sessions, session


def test_capacity_failure_is_actionable_persisted_and_can_resume_later():
    hosts, sessions, session = setup_resume()
    success, failed = sessions.resume_sessions([session])
    assert success == [] and failed == [session]
    assert session.state == VirtualDesktopSessionState.STOPPED
    assert 'us-east-1a' in session.failure_reason
    assert 'g4dn.xlarge' in session.failure_reason
    assert 'Retry Resume' in session.failure_reason
    assert 'g4dn family' in session.failure_reason
    sessions._session_db.update.assert_called_once_with(session)
    hosts.ec2_client.start_instances.assert_called_once_with(InstanceIds=['i-test'])
    logged = ' '.join(str(call) for call in hosts._logger.error.call_args_list)
    assert 'i-test' in logged and 'session-test' in logged

    hosts.ec2_client.start_instances.side_effect = None
    hosts.ec2_client.start_instances.return_value = {
        'StartingInstances': [{'InstanceId': 'i-test'}]
    }
    success, failed = sessions.resume_sessions([session])
    assert failed == [] and success == [session]
    assert session.state == VirtualDesktopSessionState.RESUMING
    assert session.failure_reason is None
    hosts.ec2_client.run_instances.assert_not_called()
    hosts.ec2_client.terminate_instances.assert_not_called()


def test_capacity_lookup_failure_still_returns_recoverable_error():
    hosts, sessions, session = setup_resume()
    hosts.ec2_client.describe_instances.side_effect = RuntimeError('lookup unavailable')
    _, failed = sessions.resume_sessions([session])
    assert 'Retry Resume' in failed[0].failure_reason
    assert failed[0].state == VirtualDesktopSessionState.STOPPED


def test_one_capacity_failure_does_not_prevent_other_desktops_resuming():
    hosts, sessions, session = setup_resume()
    other = session.model_copy(deep=True)
    other.idea_session_id = 'other-session'
    other.server.instance_id = 'i-other'
    sessions._session_db.get_from_db.side_effect = [session, other]
    hosts.ec2_client.start_instances.side_effect = [
        capacity_error(),
        {'StartingInstances': [{'InstanceId': 'i-other'}]},
    ]
    success, failed = sessions.resume_sessions([session, other])
    assert success == [other] and failed == [session]
    assert other.state == VirtualDesktopSessionState.RESUMING


def test_empty_resume_does_not_call_ec2():
    hosts, sessions, _ = setup_resume()
    assert sessions.resume_sessions([]) == ([], [])
    hosts.ec2_client.start_instances.assert_not_called()


def test_scheduled_capacity_failure_waits_for_a_later_trigger():
    from types import SimpleNamespace
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_state_event_handlers.idea_session_scheduled_resume_event_handler import (
        IDEASessionScheduledResumeEventHandler,
    )

    _, sessions, session = setup_resume()
    handler = object.__new__(IDEASessionScheduledResumeEventHandler)
    handler.session_db = sessions._session_db
    handler.session_utils = sessions
    handler.is_sender_controller_role = Mock(return_value=True)
    handler._logger = Mock()
    handler.do_not_delete_message_exception = Mock(return_value=RuntimeError('retry'))
    handler.handle_event(
        'message',
        'controller',
        SimpleNamespace(
            detail={
                'idea_session_id': session.idea_session_id,
                'idea_session_owner': session.owner,
            }
        ),
    )
    handler.do_not_delete_message_exception.assert_not_called()
    assert session.state == VirtualDesktopSessionState.STOPPED


def test_other_start_errors_keep_their_reason():
    hosts, sessions, session = setup_resume()
    hosts.ec2_client.start_instances.side_effect = ClientError(
        {'Error': {'Code': 'UnauthorizedOperation', 'Message': 'denied'}},
        'StartInstances',
    )
    _, failed = sessions.resume_sessions([session])
    assert 'UnauthorizedOperation' in failed[0].failure_reason
    hosts.ec2_client.describe_instances.assert_not_called()
