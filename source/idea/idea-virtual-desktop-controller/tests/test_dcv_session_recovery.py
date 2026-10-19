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
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.sessions.dcv_session_recovery import (
    DcvSessionRecovery,
    RESTARTING_MESSAGE,
    RECOVERY_FAILED_MESSAGE,
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
    db = Mock()
    store = [row]
    db.get_from_db.side_effect = lambda *args, **kwargs: deepcopy(store[0])

    def update(session, **kwargs):
        if (
            kwargs.get('expected_updated_on', store[0].updated_on)
            != store[0].updated_on
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
            )
        store[0] = deepcopy(session)
        store[0].updated_on += timedelta(milliseconds=1)
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
    recovery.check()
    recovery.broker.create_session.assert_not_called()
    recovery.handler.context.distributed_lock().acquire.side_effect = RuntimeError(
        'held by another worker'
    )
    with pytest.raises(RuntimeError):
        recovery.check()
    recovery.broker.create_session.assert_not_called()


def test_existing_unready_session_is_deleted_before_create(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'old-dcv', 'state': 'UNKNOWN'}
    ]
    recovery.check()
    recovery.broker.delete_sessions.assert_called_once()
    recovery.broker.create_session.assert_not_called()


def test_resume_waits_for_live_broker_session(recovery):
    recovery.store[0].state = 'RESUMING'
    recovery.check(resume=True)
    assert recovery.store[0].state == 'INITIALIZING'
    assert recovery.store[0].recovering
    recovery.now[0] += 60000
    recovery.check()
    recovery.broker.create_session.assert_called_once()
    assert recovery.store[0].state != 'READY'


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
        ],
        cursor='next-page',
    )
    utils.check_dcv_session_health()
    assert (
        utils.events_utils.publish_validate_dcv_session_creation_event.call_count == 2
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
    db.update(row, expected_updated_on=row.updated_on)
    request = db._table_obj.update_item.call_args.kwargs
    assert request['ConditionExpression'] == '#updated_on = :expected_updated_on'
    assert request['ExpressionAttributeValues'][
        ':expected_updated_on'
    ] == Utils.to_milliseconds(row.updated_on)
    assert (
        request['ExpressionAttributeValues'][':updated_on']
        > request['ExpressionAttributeValues'][':expected_updated_on']
    )


def test_async_deletion_and_creation_share_one_attempt(recovery):
    recovery.check()
    recovery.now[0] += 60000
    recovery.broker.describe_sessions_for_host.return_value = [
        {'id': 'old-dcv', 'state': 'UNKNOWN'}
    ]
    recovery.check()
    assert recovery.store[0].recovery_pending_create
    recovery.now[0] += 60000
    recovery.check()
    recovery.broker.delete_sessions.assert_called_once()
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
    session.recovery_next_attempt_at = 2000000
    session.recovery_deadline = 3000000
    session.recovery_pending_create = True
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
