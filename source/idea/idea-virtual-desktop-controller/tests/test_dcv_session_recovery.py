"""Lost DCV sessions recover with durable limits and the normal readiness checks."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopServer,
    VirtualDesktopSessionState,
    VirtualDesktopSessionConnectionInfo,
)
from ideadatamodel import errorcodes
from ideadatamodel.exceptions import SocaException
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.sessions.dcv_session_recovery import (
    DcvSessionRecovery,
    RESTARTING_MESSAGE,
    RECOVERY_FAILED_MESSAGE,
    SESSION_UNAVAILABLE_MESSAGE,
    REBOOT_SECONDS,
)
from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_creation_event_handler import (
    ValidateDCVSessionCreationEventHandler,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)


@pytest.fixture
def recovery(monkeypatch):
    now = [1000000]
    monkeypatch.setattr(Utils, 'current_time_ms', lambda: now[0])
    row = VirtualDesktopSession(
        owner='someuser',
        idea_session_id='desktop',
        dcv_session_id='old-dcv',
        state=VirtualDesktopSessionState.READY,
        server=VirtualDesktopServer(instance_id='i-test'),
        updated_on=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    row._updated_on_ms = Utils.to_milliseconds(row.updated_on)
    db = Mock()
    store = [row]
    db.get_from_db.side_effect = lambda *args, **kwargs: deepcopy(store[0])

    def update(session, **kwargs):
        if (
            kwargs.get('check_version')
            and session._updated_on_ms != store[0]._updated_on_ms
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
            )
        store[0] = deepcopy(session)
        store[0].updated_on += timedelta(milliseconds=1)
        store[0]._updated_on_ms += 1
        return deepcopy(store[0])

    db.update.side_effect = update
    handler = ValidateDCVSessionCreationEventHandler.__new__(
        ValidateDCVSessionCreationEventHandler
    )
    handler.context = Mock()
    handler.context.distributed_lock.return_value = Mock(
        spec=['acquire', 'assert_held', 'release']
    )
    handler.session_db = db
    handler.session_utils = Mock()
    handler.controller_utils = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler._logger = Mock()
    handler.context.dcv_broker_client.describe_sessions.return_value = {'sessions': {}}
    handler.context.dcv_broker_client.describe_sessions_for_host.return_value = []
    handler.context.dcv_broker_client.create_session.return_value = (
        VirtualDesktopSession(dcv_session_id='new-dcv')
    )
    handler.context.aws().ec2().describe_instance_status.return_value = {
        'InstanceStatuses': [
            {
                'InstanceState': {'Name': 'running'},
                'InstanceStatus': {'Status': 'ok'},
                'SystemStatus': {'Status': 'ok'},
            }
        ]
    }
    return SimpleNamespace(
        handler=handler,
        now=now,
        store=store,
        db=db,
        broker=handler.context.dcv_broker_client,
        check=lambda **kwargs: DcvSessionRecovery(handler).check(
            deepcopy(store[0]), **kwargs
        ),
    )


@pytest.mark.parametrize('state', [None, 'DELETED', 'UNKNOWN', 'CREATING'])
def test_detects_missing_or_unready_session(recovery, state):
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': state}}
    }
    recovery.check()
    session = recovery.store[0]
    assert session.state == 'INITIALIZING'
    assert session.recovering
    assert session.failure_reason == RESTARTING_MESSAGE
    assert session.recovery_next_attempt_at == recovery.now[0] + 60000
    recovery.broker.create_session.assert_not_called()


def test_healthy_session_and_broker_outage_do_not_recreate(recovery):
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': 'READY'}}
    }
    recovery.check()
    recovery.db.update.assert_not_called()
    recovery.broker.describe_sessions.side_effect = RuntimeError('unavailable')
    with pytest.raises(RuntimeError):
        recovery.check()
    assert recovery.store[0].state == 'READY'
    recovery.broker.create_session.assert_not_called()


def test_backoff_and_three_failed_recoveries_survive_new_handlers(recovery):
    recovery.check()
    for attempt, delay in enumerate([60, 300, 900], 1):
        before = recovery.now[0]
        assert recovery.store[0].recovery_next_attempt_at == before + delay * 1000
        recovery.now[0] += delay * 1000 - 1
        recovery.check()
        assert recovery.broker.create_session.call_count == attempt - 1
        recovery.now[0] += 1
        recovery.check()
        assert recovery.store[0].recovery_attempts == attempt
        assert recovery.broker.create_session.call_count == attempt
        recovery.check()
        assert recovery.broker.create_session.call_count == attempt
        recovery.now[0] += 180000
        recovery.check()
    assert recovery.store[0].state == 'ERROR'
    assert not recovery.store[0].recovering
    assert recovery.store[0].failure_reason == RECOVERY_FAILED_MESSAGE
    recovery.now[0] += 9999999
    recovery.check()
    assert recovery.broker.create_session.call_count == 3


def test_lost_create_response_is_adopted_after_restart(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.create_session.side_effect = RuntimeError('response lost')
    with pytest.raises(RuntimeError):
        recovery.check()
    assert recovery.store[0].recovery_attempts == 1
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'accepted-dcv', 'state': 'READY'}
    ]
    recovery.check()
    assert recovery.store[0].state == 'INITIALIZING'
    recovery.now[0] += 180000
    recovery.check()
    assert recovery.store[0].dcv_session_id == 'accepted-dcv'
    assert recovery.store[0].state == 'READY'
    assert recovery.store[0].failure_reason is None
    recovery.broker.create_session.assert_called_once()
    recovery.handler.session_utils.refresh_bootstrap_on_ready.assert_called_once()


@pytest.mark.parametrize('state', ['STOPPED', 'STOPPING', 'DELETING', 'ERROR'])
def test_stale_validation_cannot_restart_inactive_session(recovery, state):
    recovery.store[0].state = state
    recovery.store[0].recovering = True
    recovery.check()
    recovery.broker.describe_sessions.assert_not_called()
    recovery.broker.create_session.assert_not_called()


def test_unhealthy_instance_does_not_spend_an_attempt(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.handler.context.aws().ec2().describe_instance_status.return_value = {
        'InstanceStatuses': []
    }
    recovery.check()
    assert recovery.store[0].recovery_attempts == 0
    recovery.broker.create_session.assert_not_called()


def test_failed_claim_or_lost_lock_never_creates(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.db.update.side_effect = ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
    )
    with pytest.raises(SocaException) as error:
        recovery.check()
    assert error.value.error_code == errorcodes.DO_NOT_DELETE_MESSAGE
    recovery.broker.create_session.assert_not_called()
    recovery.handler.context.distributed_lock().acquire.side_effect = RuntimeError(
        'held by another worker'
    )
    with pytest.raises(RuntimeError):
        recovery.check()
    recovery.broker.create_session.assert_not_called()


@pytest.mark.parametrize(
    'state', ['UNKNOWN', 'CREATING', 'DELETING', None, 'FUTURE_STATE']
)
@pytest.mark.parametrize('healthy', [False, True])
def test_unready_session_never_deletes_or_recreates_and_times_out(
    recovery, state, healthy
):
    if not healthy:
        recovery.handler.context.aws().ec2().describe_instance_status.return_value = {
            'InstanceStatuses': []
        }
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': state}}
    }
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'old-dcv', 'state': state}
    ]
    recovery.check()
    for delay in [60, 300, 900]:
        recovery.now[0] += delay * 1000
        recovery.check()
        recovery.now[0] += 180000
        recovery.check()
        recovery.broker.delete_sessions.assert_not_called()
        recovery.broker.create_session.assert_not_called()
    assert recovery.store[0].state == 'ERROR'
    assert not recovery.store[0].recovering
    assert recovery.store[0].dcv_session_id == 'old-dcv'
    assert recovery.store[0].failure_reason == SESSION_UNAVAILABLE_MESSAGE
    recovery.now[0] += 9999999
    recovery.check()
    recovery.broker.delete_sessions.assert_not_called()
    recovery.broker.create_session.assert_not_called()


def test_unknown_recovers_in_place_when_broker_contact_returns(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'old-dcv', 'state': 'UNKNOWN'}
    ]
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value[0]['state'] = 'READY'
    recovery.check()
    assert recovery.store[0].state == 'READY'
    assert recovery.store[0].dcv_session_id == 'old-dcv'
    assert recovery.store[0].failure_reason is None
    recovery.broker.delete_sessions.assert_not_called()
    recovery.broker.create_session.assert_not_called()


@pytest.mark.parametrize('entries', [[], [{'id': 'old-dcv', 'state': 'DELETED'}]])
def test_only_missing_or_deleted_sessions_are_recreated(recovery, entries):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value = entries
    recovery.check()
    recovery.broker.create_session.assert_called_once()
    recovery.broker.delete_sessions.assert_not_called()


def test_resume_waits_for_live_broker_session(recovery):
    recovery.store[0].state = 'RESUMING'
    recovery.check(resume=True)
    assert recovery.store[0].state == 'INITIALIZING'
    assert recovery.store[0].recovering
    recovery.now[0] += 60000
    recovery.check()
    recovery.broker.create_session.assert_called_once()
    assert recovery.store[0].state != 'READY'


@pytest.mark.parametrize('validation', [False, True])
def test_planned_reboot_does_not_start_recovery(recovery, validation):
    from ideavirtualdesktopcontroller.app.sessions.image_validation import (
        ImageTestLauncher,
    )

    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = recovery.handler.context
    utils._session_db = recovery.db
    utils._server_utils = Mock()
    utils._server_utils.reboot_dcv_hosts.return_value = {}
    utils._logger = Mock()
    recovery.broker.get_active_counts_for_sessions.return_value = []

    def reboot(servers):
        stored = recovery.store[0]
        assert stored.state == 'RESUMING'
        assert stored.reboot_deadline == recovery.now[0] + REBOOT_SECONDS * 1000
        recovery.check()
        assert not recovery.store[0].recovering
        return {}

    utils._server_utils.reboot_dcv_hosts.side_effect = reboot
    session = deepcopy(recovery.store[0])
    session.force = True
    if validation:
        launcher = ImageTestLauncher.__new__(ImageTestLauncher)
        launcher.api = SimpleNamespace(session_utils=utils)
        launcher.clock = lambda: recovery.now[0] / 1000
        launcher._wait_ready = Mock()
        launcher._reboot(session, 900)
    else:
        success, failed = utils.reboot_sessions([session])
        assert len(success) == 1
        assert failed == []
    recovery.db.update.reset_mock()
    recovery.check()
    recovery.check(resume=True)
    assert recovery.store[0].state == 'RESUMING'
    assert not recovery.store[0].recovering
    recovery.db.update.assert_not_called()
    recovery.broker.create_session.assert_not_called()


@pytest.mark.parametrize('state', ['RESUMING', 'CREATING', 'INITIALIZING'])
def test_planned_reboot_deadline_starts_recovery(recovery, state):
    recovery.store[0].state = state
    recovery.store[0].reboot_deadline = recovery.now[0] + 900000
    recovery.now[0] += 899999
    recovery.check()
    recovery.db.update.assert_not_called()
    recovery.now[0] += 1
    recovery.check()
    assert recovery.store[0].recovering
    assert recovery.store[0].reboot_deadline == 0
    assert recovery.store[0].recovery_next_attempt_at == recovery.now[0] + 60000
    recovery.now[0] += 60000
    recovery.check()
    recovery.broker.create_session.assert_called_once()


def test_planned_reboot_that_never_becomes_healthy_ends_in_error(recovery):
    recovery.store[0].state = 'RESUMING'
    recovery.store[0].reboot_deadline = recovery.now[0]
    recovery.handler.context.aws().ec2().describe_instance_status.return_value = {
        'InstanceStatuses': []
    }
    recovery.check()
    assert recovery.store[0].state == 'ERROR'
    assert recovery.store[0].reboot_deadline == 0
    assert '15 minutes' in recovery.store[0].failure_reason
    recovery.broker.create_session.assert_not_called()


@pytest.mark.parametrize('state', ['RESUMING', 'CREATING'])
def test_planned_reboot_ignores_stale_broker_readiness(recovery, state):
    recovery.store[0].state = state
    recovery.store[0].reboot_deadline = recovery.now[0] + REBOOT_SECONDS * 1000
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': 'READY'}}
    }
    event = SimpleNamespace(
        detail={'idea_session_id': 'desktop', 'idea_session_owner': 'someuser'}
    )
    recovery.handler.handle_event('validation', 'controller', event)
    recovery.db.update.assert_not_called()
    assert recovery.store[0].state == state


def test_connection_failure_uses_authenticated_owner_and_queues_recovery(recovery):
    api = VirtualDesktopAPI.__new__(VirtualDesktopAPI)
    api.context = recovery.handler.context
    api.session_db = recovery.db
    api.events_utils = Mock()
    context = Mock()
    context.get_username.return_value = 'someuser'
    recovery.broker.get_session_connection_data.return_value = (
        VirtualDesktopSessionConnectionInfo(failure_reason='session absent')
    )
    result = api._get_session_connection_info(
        VirtualDesktopSessionConnectionInfo(
            idea_session_id='desktop', dcv_session_id='old-dcv'
        ),
        context,
    )
    recovery.db.get_from_db.assert_called_once_with(
        idea_session_id='desktop', idea_session_owner='someuser'
    )
    assert result.failure_reason == RESTARTING_MESSAGE
    api.events_utils.publish_validate_dcv_session_creation_event.assert_called_once_with(
        idea_session_id='desktop', idea_session_owner='someuser'
    )
    recovery.db.update.assert_not_called()


def test_permission_failure_does_not_restart_healthy_session(recovery):
    api = VirtualDesktopAPI.__new__(VirtualDesktopAPI)
    api.context, api.session_db, api.events_utils = (
        recovery.handler.context,
        recovery.db,
        Mock(),
    )
    context = Mock()
    context.get_username.return_value = 'someuser'
    recovery.broker.get_session_connection_data.return_value = (
        VirtualDesktopSessionConnectionInfo(failure_reason='Permission denied')
    )
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': 'READY'}}
    }
    result = api._get_session_connection_info(
        VirtualDesktopSessionConnectionInfo(idea_session_id='desktop'), context
    )
    assert result.failure_reason == 'Permission denied'
    api.events_utils.publish_validate_dcv_session_creation_event.assert_not_called()


def test_periodic_scan_schedules_only_active_sessions(recovery):
    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils._session_db = Mock()
    utils.events_utils = Mock()
    utils._session_db.list_all_from_db.return_value = SimpleNamespace(
        listing=[
            recovery.store[0],
            VirtualDesktopSession(state='STOPPED'),
            VirtualDesktopSession(
                state='INITIALIZING',
                recovering=True,
                owner='someuser',
                idea_session_id='recovering',
            ),
            VirtualDesktopSession(
                state='RESUMING',
                reboot_deadline=2000000,
                owner='someuser',
                idea_session_id='rebooting',
            ),
        ],
        cursor='next-page',
    )
    utils.check_dcv_session_health()
    assert (
        utils.events_utils.publish_validate_dcv_session_creation_event.call_count == 3
    )
    assert utils._dcv_health_cursor == 'next-page'


def test_conditional_update_uses_read_version(recovery):
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._table_obj = Mock()
    db._stamp_stopped_on = Mock()
    db._record_run = Mock()
    db.trigger_update_event = Mock()
    row = recovery.store[0]
    db.convert_session_object_to_db_dict = Mock(
        return_value={
            'owner': row.owner,
            'idea_session_id': row.idea_session_id,
            'updated_on': Utils.to_milliseconds(row.updated_on),
            'state': 'INITIALIZING',
        }
    )
    db.convert_db_dict_to_session_object = Mock()
    db._table_obj.update_item.return_value = {'Attributes': {}}
    db.update(row, check_version=True)
    request = db._table_obj.update_item.call_args.kwargs
    assert request['ConditionExpression'] == '#updated_on = :expected_updated_on'
    assert request['ExpressionAttributeValues'][
        ':expected_updated_on'
    ] == Utils.to_milliseconds(row.updated_on)
    assert (
        request['ExpressionAttributeValues'][':updated_on']
        > request['ExpressionAttributeValues'][':expected_updated_on']
    )


def test_unknown_session_that_disappears_can_be_recreated(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'old-dcv', 'state': 'UNKNOWN'}
    ]
    recovery.check()
    assert recovery.store[0].recovery_pending_create
    recovery.now[0] += 60000
    recovery.check()
    recovery.broker.delete_sessions.assert_not_called()
    recovery.broker.create_session.assert_not_called()
    recovery.broker.describe_sessions_for_host.return_value = []
    recovery.check()
    assert recovery.store[0].recovery_attempts == 1
    assert not recovery.store[0].recovery_pending_create
    recovery.broker.create_session.assert_called_once()
    recovery.check()
    recovery.broker.create_session.assert_called_once()


def test_recovery_fields_round_trip_through_database(recovery):
    from ideadatamodel import (
        Project,
        VirtualDesktopSoftwareStack,
        VirtualDesktopBaseOS,
        VirtualDesktopSessionType,
    )

    session = recovery.store[0]
    session.software_stack = VirtualDesktopSoftwareStack(
        base_os=VirtualDesktopBaseOS.UBUNTU2604
    )
    session.project = Project(project_id='project')
    session.type = VirtualDesktopSessionType.VIRTUAL
    session.recovering = True
    session.recovery_attempts = 2
    session.recovery_next_attempt_at = 2000988
    session.recovery_deadline = 3000989
    session.recovery_pending_create = True
    session.reboot_deadline = 4000000
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._server_db = Mock()
    db._server_db.convert_server_object_to_db_dict.return_value = {}
    db._server_db.convert_db_entry_to_server_object.return_value = session.server
    db._software_stack_db = Mock()
    db._software_stack_db.convert_software_stack_object_to_db_dict.return_value = {}
    db._software_stack_db.convert_db_dict_to_software_stack_object.return_value = (
        session.software_stack
    )
    db._schedule_db = Mock()
    db._schedule_db.convert_schedule_object_to_db_dict.return_value = {}
    db._schedule_db.convert_db_dict_to_schedule_object.return_value = None
    row = db.convert_session_object_to_db_dict(session)
    restored = db.convert_db_dict_to_session_object(row)
    for field in (
        'recovering',
        'recovery_attempts',
        'recovery_next_attempt_at',
        'recovery_deadline',
        'recovery_pending_create',
        'reboot_deadline',
    ):
        assert getattr(restored, field) == getattr(session, field)


def test_stop_start_resets_an_interrupted_recovery(recovery):
    recovery.store[0].state = 'RESUMING'
    recovery.store[0].recovering = True
    recovery.store[0].recovery_attempts = 3
    recovery.store[0].recovery_deadline = 9999999
    recovery.check(resume=True)
    assert recovery.store[0].state == 'INITIALIZING'
    assert recovery.store[0].recovery_attempts == 0
    assert recovery.store[0].recovery_deadline == 0
    recovery.check(resume=True)
    assert recovery.store[0].recovery_next_attempt_at == recovery.now[0] + 60000


def test_broker_discovery_is_paginated_and_scoped_to_the_host(recovery):
    from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client import (
        DCVBrokerClient,
    )

    client = DCVBrokerClient.__new__(DCVBrokerClient)
    client._describe_sessions = Mock(
        side_effect=[
            {
                'sessions': [
                    {
                        'id': 'other',
                        'server': {
                            'tags': [
                                {'key': 'idea_session_id', 'value': 'other-desktop'}
                            ]
                        },
                    }
                ],
                'next_token': 'page-2',
            },
            {
                'sessions': [
                    {
                        'id': 'accepted',
                        'server': {
                            'tags': [{'key': 'idea_session_id', 'value': 'desktop'}]
                        },
                    }
                ]
            },
        ]
    )
    found = client.describe_sessions_for_host(recovery.store[0])
    assert [entry['id'] for entry in found] == ['accepted']
    assert client._describe_sessions.call_args_list[1].kwargs == {
        'owner': 'someuser',
        'next_token': 'page-2',
    }


def test_stopping_clears_recovery_message_state(recovery):
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._table_obj = Mock()
    db._stamp_stopped_on = Mock()
    db._record_run = Mock()
    db.trigger_update_event = Mock()
    session = recovery.store[0]
    session.state = 'STOPPING'
    session.recovering = True
    session.recovery_pending_create = True
    db.convert_session_object_to_db_dict = Mock(
        return_value={
            'owner': session.owner,
            'idea_session_id': session.idea_session_id,
            'updated_on': Utils.to_milliseconds(session.updated_on),
            'state': 'STOPPING',
        }
    )
    db.convert_db_dict_to_session_object = Mock()
    db._table_obj.update_item.return_value = {'Attributes': {}}
    db.update(session)
    assert not session.recovering
    assert not session.recovery_pending_create


def test_create_request_cannot_supply_recovery_state():
    api = VirtualDesktopAPI.__new__(VirtualDesktopAPI)
    session = VirtualDesktopSession(
        recovering=True,
        recovery_attempts=99,
        recovery_next_attempt_at=9999999,
        recovery_deadline=9999999,
        recovery_pending_create=True,
        reboot_deadline=9999999,
    )
    api.validate_create_session_request(session)
    assert not session.recovering
    assert (
        session.recovery_attempts
        == session.recovery_deadline
        == session.recovery_next_attempt_at
        == 0
    )
    assert not session.recovery_pending_create
    assert session.reboot_deadline == 0


@pytest.fixture
def persisted_recovery(recovery):
    from ideadatamodel import (
        Project,
        VirtualDesktopSoftwareStack,
        VirtualDesktopSessionType,
    )

    session = recovery.store[0]
    session.type = VirtualDesktopSessionType.VIRTUAL
    session.software_stack = VirtualDesktopSoftwareStack(base_os='ubuntu2604')
    session.project = Project(project_id='project')
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._logger = Mock()
    db._record_run = Mock()
    db.trigger_update_event = Mock()
    db._server_db = Mock()
    db._server_db.convert_server_object_to_db_dict.return_value = {}
    db._server_db.convert_db_entry_to_server_object.return_value = session.server
    db._software_stack_db = Mock()
    db._software_stack_db.convert_software_stack_object_to_db_dict.return_value = {}
    db._software_stack_db.convert_db_dict_to_software_stack_object.return_value = (
        session.software_stack
    )
    db._schedule_db = Mock()
    db._schedule_db.convert_schedule_object_to_db_dict.return_value = {}
    db._schedule_db.convert_db_dict_to_schedule_object.return_value = None
    row = db.convert_session_object_to_db_dict(session)
    row['updated_on'] = 1792453594988
    recovery.now[0] = row['updated_on']
    table = Mock()
    table.get_item.side_effect = lambda **kwargs: {'Item': deepcopy(row)}

    def update_item(**request):
        values = request['ExpressionAttributeValues']
        if (
            request.get('ConditionExpression')
            and values[':expected_updated_on'] != row['updated_on']
        ):
            raise ClientError(
                {
                    'Error': {
                        'Code': 'ConditionalCheckFailedException',
                        'Message': f'stored {row["updated_on"]} != expected {values[":expected_updated_on"]}',
                    }
                },
                'UpdateItem',
            )
        old = deepcopy(row)
        row.update(
            {
                name: values[':' + name]
                for name in request['ExpressionAttributeNames'].values()
            }
        )
        return {'Attributes': old}

    table.update_item.side_effect = update_item
    db._table_obj = table
    recovery.handler.session_db = db
    recovery.db = db
    recovery.row = row
    recovery.check = lambda **kwargs: DcvSessionRecovery(recovery.handler).check(
        db.get_from_db('someuser', 'desktop'), **kwargs
    )
    return recovery


def test_millisecond_timestamp_survives_read_and_conditional_update(persisted_recovery):
    recovery = persisted_recovery
    session = recovery.db.get_from_db('someuser', 'desktop')
    worker = DcvSessionRecovery(recovery.handler)
    worker.key = 'dcv-session-desktop'
    worker.save(session)
    assert recovery.row['updated_on'] == 1792453594989
    worker.save(session)
    assert recovery.row['updated_on'] == 1792453594990


def test_dead_session_makes_first_attempt_with_stored_milliseconds(persisted_recovery):
    recovery = persisted_recovery
    recovery.check()
    assert recovery.row['state'] == 'INITIALIZING'
    recovery.now[0] += 60000
    recovery.check()
    assert recovery.row['recovery_attempts'] == 1
    assert recovery.row['dcv_session_id'] == 'new-dcv'
    recovery.broker.create_session.assert_called_once()


def test_planned_reboot_completes_through_normal_resume_and_validation(
    persisted_recovery,
):
    from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_reboot_complete_event_handler import (
        DCVHostRebootCompleteEventHandler,
    )
    from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_resume_session_command_progress_event_handler import (
        IDEAResumeSessionCommandProgressEventHandler,
    )

    recovery = persisted_recovery
    recovery.row['state'] = 'RESUMING'
    recovery.row['reboot_deadline'] = recovery.now[0] + REBOOT_SECONDS * 1000
    event = SimpleNamespace(
        detail={
            'idea_session_id': 'desktop',
            'idea_session_owner': 'someuser',
            'status': 'Success',
            'command_id': 'resume-command',
        }
    )
    handler = DCVHostRebootCompleteEventHandler.__new__(
        DCVHostRebootCompleteEventHandler
    )
    handler.context = recovery.handler.context
    handler.session_db = recovery.db
    handler.session_counter_db = Mock()
    handler.session_counter_db.get.return_value = SimpleNamespace(counter=0)
    handler.server_db = Mock()
    handler._logger = Mock()
    handler.get_dcv_instance_id_from_sender_id = lambda sender: 'i-test'
    recovery.broker.resume_session.side_effect = lambda session: session
    handler.handle_event('reboot', 'host', event)
    recovery.broker.resume_session.assert_called_once()
    assert recovery.row['state'] == 'RESUMING'
    assert not recovery.row['recovering']

    progress = IDEAResumeSessionCommandProgressEventHandler.__new__(
        IDEAResumeSessionCommandProgressEventHandler
    )
    progress.session_db = recovery.db
    progress.ssm_commands_db = Mock()
    progress.events_utils = Mock()
    progress._logger = Mock()
    progress.is_sender_controller_role = lambda sender: True
    progress.handle_event('resume', 'controller', event)
    assert recovery.row['state'] == 'INITIALIZING'
    assert recovery.row['reboot_deadline']
    assert not recovery.row['recovering']
    recovery.handler.session_counter_db = handler.session_counter_db
    recovery.broker.describe_sessions.return_value = {
        'sessions': {'old-dcv': {'state': 'READY'}}
    }
    recovery.handler.handle_event('validation', 'controller', event)
    assert recovery.row['state'] == 'READY'
    assert recovery.row['reboot_deadline'] == 0
    assert not recovery.row['recovering']
    recovery.broker.create_session.assert_not_called()
    recovery.broker.delete_sessions.assert_not_called()

    recovery.broker.describe_sessions.return_value = {'sessions': {}}
    recovery.check()
    assert recovery.row['recovering']
    assert recovery.row['recovery_next_attempt_at'] == recovery.now[0] + 60000


def test_reboot_deadline_survives_unrelated_writes_and_controller_restarts(
    persisted_recovery,
):
    recovery = persisted_recovery
    recovery.row['state'] = 'RESUMING'
    deadline = recovery.now[0] + REBOOT_SECONDS * 1000
    recovery.row['reboot_deadline'] = deadline
    recovery.now[0] = deadline - 1
    session = recovery.db.get_from_db('someuser', 'desktop')
    session.name = 'renamed'
    recovery.db.update(session)
    recovery.check()
    assert recovery.row['reboot_deadline'] == deadline
    assert not recovery.row['recovering']
    recovery.now[0] = deadline
    recovery.check()
    assert recovery.row['recovering']


@pytest.mark.parametrize('state', ['READY', 'ERROR', 'STOPPING', 'STOPPED', 'DELETING'])
def test_reboot_deadline_clears_when_the_reboot_ends(persisted_recovery, state):
    recovery = persisted_recovery
    recovery.row['reboot_deadline'] = recovery.now[0] + REBOOT_SECONDS * 1000
    session = recovery.db.get_from_db('someuser', 'desktop')
    session.state = state
    recovery.db.update(session)
    assert recovery.row['reboot_deadline'] == 0


@pytest.mark.parametrize('conflict', [None, 'start', 'ready'])
def test_reboot_complete_resumes_through_ready_with_stored_milliseconds(
    persisted_recovery, conflict
):
    from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_reboot_complete_event_handler import (
        DCVHostRebootCompleteEventHandler,
    )

    recovery = persisted_recovery
    recovery.row['state'] = 'RESUMING'
    handler = DCVHostRebootCompleteEventHandler.__new__(
        DCVHostRebootCompleteEventHandler
    )
    handler.context = recovery.handler.context
    handler.session_db = recovery.db
    handler._logger = Mock()
    handler.get_dcv_instance_id_from_sender_id = lambda sender: 'i-test'
    event = SimpleNamespace(
        detail={'idea_session_id': 'desktop', 'idea_session_owner': 'someuser'}
    )
    update = recovery.db._table_obj.update_item.side_effect

    def concurrent_write(**request):
        recovery.row['updated_on'] += 1
        recovery.db._table_obj.update_item.side_effect = update
        return update(**request)

    if conflict == 'start':
        recovery.db._table_obj.update_item.side_effect = concurrent_write
        with pytest.raises(SocaException) as error:
            handler.handle_event('message', 'host', event)
        assert error.value.error_code == errorcodes.DO_NOT_DELETE_MESSAGE
        assert recovery.row['state'] == 'RESUMING'
        recovery.db._logger.warning.assert_called_once()
        assert 'desktop' in recovery.db._logger.warning.call_args.args[0]
        recovery.handler.context.distributed_lock().release.assert_called_with(
            key='dcv-session-desktop'
        )
    handler.handle_event('message', 'host', event)
    assert recovery.row['state'] == 'INITIALIZING'
    recovery.now[0] += 60000
    recovery.handler.handle_event('validation', 'controller', event)
    assert recovery.row['recovery_attempts'] == 1
    recovery.broker.create_session.assert_called_once()
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'new-dcv', 'state': 'READY'}
    ]
    recovery.now[0] += 180000
    if conflict == 'ready':
        recovery.db._table_obj.update_item.side_effect = concurrent_write
        with pytest.raises(SocaException) as error:
            recovery.handler.handle_event('validation', 'controller', event)
        assert error.value.error_code == errorcodes.DO_NOT_DELETE_MESSAGE
        assert recovery.row['state'] == 'INITIALIZING'
        recovery.db._logger.warning.assert_called_once()
        recovery.handler.session_utils.refresh_bootstrap_on_ready.assert_not_called()
    recovery.handler.handle_event('validation', 'controller', event)
    assert recovery.row['state'] == 'READY'
    assert not recovery.row['recovering']
    assert recovery.row['failure_reason'] is None
    recovery.handler.session_utils.refresh_bootstrap_on_ready.assert_called_once()


@pytest.mark.parametrize('state', ['READY', 'STOPPING', 'DELETING'])
def test_recovery_conflict_retries_from_current_state(persisted_recovery, state):
    recovery = persisted_recovery
    update = recovery.db._table_obj.update_item.side_effect

    def concurrent_write(**request):
        recovery.row['state'] = state
        recovery.row['updated_on'] += 1
        recovery.db._table_obj.update_item.side_effect = update
        return update(**request)

    recovery.db._table_obj.update_item.side_effect = concurrent_write
    with pytest.raises(SocaException) as error:
        recovery.check()
    assert error.value.error_code == errorcodes.DO_NOT_DELETE_MESSAGE
    recovery.db._logger.warning.assert_called_once()
    assert 'desktop' in recovery.db._logger.warning.call_args.args[0]
    recovery.broker.create_session.assert_not_called()
    recovery.check()
    if state == 'READY':
        recovery.now[0] += 60000
        recovery.check()
        assert recovery.row['recovery_attempts'] == 1
        recovery.broker.create_session.assert_called_once()
    else:
        assert recovery.row['state'] == state
        recovery.broker.create_session.assert_not_called()


def test_stale_session_version_cannot_overwrite_a_newer_write(persisted_recovery):
    recovery = persisted_recovery
    stale = recovery.db.get_from_db('someuser', 'desktop')
    current = recovery.db.get_from_db('someuser', 'desktop')
    recovery.db.update(current)
    with pytest.raises(ClientError):
        recovery.db.update(stale, check_version=True)
    assert recovery.row['updated_on'] == 1792453594989
    recovery.db.trigger_update_event.assert_called_once()
    recovery.db._logger.warning.assert_called_once()


def test_session_version_is_private_and_required_for_conditional_writes(
    persisted_recovery,
):
    recovery = persisted_recovery
    session = recovery.db.get_from_db('someuser', 'desktop')
    assert session.model_copy(deep=True)._updated_on_ms == 1792453594988
    assert '_updated_on_ms' not in session.model_dump()
    supplied = VirtualDesktopSession(
        **session.model_dump(), _updated_on_ms=1792453594988
    )
    assert supplied._updated_on_ms is None
    with pytest.raises(ValueError, match='stored session version'):
        recovery.db.update(supplied, check_version=True)
    recovery.db._table_obj.update_item.assert_not_called()


def test_failed_resume_explains_the_error(persisted_recovery):
    from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_resume_session_command_progress_event_handler import (
        RESUME_FAILED_MESSAGE,
        IDEAResumeSessionCommandProgressEventHandler,
    )

    recovery = persisted_recovery
    recovery.row['state'] = 'RESUMING'
    recovery.row['reboot_deadline'] = recovery.now[0] + REBOOT_SECONDS * 1000
    event = SimpleNamespace(
        detail={
            'idea_session_id': 'desktop',
            'idea_session_owner': 'someuser',
            'status': 'Failed',
            'command_id': 'resume-command',
        }
    )
    progress = IDEAResumeSessionCommandProgressEventHandler.__new__(
        IDEAResumeSessionCommandProgressEventHandler
    )
    progress.session_db = recovery.db
    progress.ssm_commands_db = Mock()
    progress.events_utils = Mock()
    progress._logger = Mock()
    progress.is_sender_controller_role = lambda sender: True
    progress.handle_event('resume', 'controller', event)
    assert recovery.row['state'] == 'ERROR'
    assert recovery.row['failure_reason'] == RESUME_FAILED_MESSAGE
