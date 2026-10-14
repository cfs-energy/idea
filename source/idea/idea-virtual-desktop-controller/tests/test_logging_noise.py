from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    DayOfWeek,
    VirtualDesktopSessionState,
    VirtualDesktopSchedule,
    VirtualDesktopScheduleType,
)
from ideasdk.utils import DateTimeUtils
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.events.handlers.scheduled_event_handler import (
    ScheduledEventHandler,
)
from ideavirtualdesktopcontroller.app.events.handlers.idea_session_state_event_handlers.idea_session_scheduled_stop_event_handler import (
    IDEASessionScheduledStopEventHandler,
)
from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_resume_session_command_progress_event_handler import (
    IDEAResumeSessionCommandProgressEventHandler,
)
from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_session_cpu_utilization_command_progress_event_handler import (
    IDEASessionCPUUtilizationCommandProgressEventHandler,
)


def handler(cls):
    obj = object.__new__(cls)
    obj._logger = Mock()
    obj.context = Mock()
    obj.session_db = Mock(table_name='test.vdc.controller.sessions')
    obj.schedule_db = Mock()
    obj.schedule_utils = Mock()
    obj.session_utils = Mock()
    obj.events_utils = Mock()
    obj.ssm_commands_utils = Mock()
    obj.is_sender_controller_role = Mock(return_value=True)
    obj.is_sender_scheduled_event_lambda_role = Mock(return_value=True)
    return obj


def event(**detail):
    return SimpleNamespace(
        detail={'idea_session_id': 'deleted', 'idea_session_owner': 'owner', **detail}
    )


def scheduled_handler():
    obj = handler(ScheduledEventHandler)
    obj.DAY_OF_WEEKS = list(DayOfWeek)
    obj.context.cluster_timezone.return_value = 'UTC'
    obj.session_permissions_db = Mock()
    obj.session_permissions_db.list_all_from_db.return_value = ([], None)
    obj.session_utils.fail_stuck_provisioning_sessions.return_value = 0
    obj.session_utils.repair_project_instance_profiles.return_value = 0
    obj.session_utils.clean_up_stopped_sessions.return_value = {}
    schedule = SimpleNamespace(
        schedule_id='stale',
        idea_session_id='deleted',
        idea_session_owner='owner',
        updated_on=None,
    )
    schedules = [schedule]
    obj.schedule_db.get_schedules_for_day_of_week.side_effect = lambda day: list(
        schedules
    )
    obj.schedule_db.delete.side_effect = schedules.remove
    return obj, schedule


def test_deleted_session_schedule_is_removed_before_stop_can_recur():
    obj, schedule = scheduled_handler()
    table = obj.context.aws().dynamodb_table().Table.return_value
    table.get_item.return_value = {}
    for hour in (10, 11):
        obj.handle_event(
            'message', 'scheduler', event(time=f'2026-10-12T{hour}:00:00Z')
        )
    obj.schedule_db.delete.assert_called_once_with(schedule)
    obj.schedule_utils.trigger_schedule.assert_not_called()
    table.get_item.assert_called_once_with(
        Key={'owner': 'owner', 'idea_session_id': 'deleted'},
        ConsistentRead=True,
        ProjectionExpression='idea_session_id',
    )


def test_existing_session_keeps_its_schedule_and_still_triggers():
    obj, schedule = scheduled_handler()
    obj.context.aws().dynamodb_table().Table.return_value.get_item.return_value = {
        'Item': {'idea_session_id': 'deleted'}
    }
    obj.handle_event('message', 'scheduler', event(time='2026-10-12T10:00:00Z'))
    obj.schedule_db.delete.assert_not_called()
    obj.schedule_utils.trigger_schedule.assert_called_once_with(
        datetime(2026, 10, 12, 10).time(), schedule
    )


def test_read_failure_skips_one_schedule_and_continues_hourly_work():
    obj, failed_schedule = scheduled_handler()
    live_schedule = SimpleNamespace(
        schedule_id='live',
        idea_session_id='live',
        idea_session_owner='owner',
        updated_on=None,
    )
    obj.schedule_db.get_schedules_for_day_of_week.side_effect = None
    obj.schedule_db.get_schedules_for_day_of_week.return_value = [
        failed_schedule,
        live_schedule,
    ]
    obj.context.aws().dynamodb_table().Table.return_value.get_item.side_effect = [
        ClientError({'Error': {'Code': 'AccessDeniedException'}}, 'GetItem'),
        {'Item': {'idea_session_id': 'live'}},
    ]
    expired_permission = SimpleNamespace(
        idea_session_id='live',
        idea_session_owner='owner',
        expiry_date=datetime(2026, 10, 12, 9, tzinfo=timezone.utc),
    )
    obj.session_permissions_db.list_all_from_db.return_value = (
        [expired_permission],
        None,
    )
    obj.handle_event('message', 'scheduler', event(time='2026-10-12T10:00:00Z'))
    obj.schedule_db.delete.assert_not_called()
    obj.schedule_utils.trigger_schedule.assert_called_once_with(
        datetime(2026, 10, 12, 10).time(), live_schedule
    )
    obj._logger.exception.assert_called_once()
    assert 'AccessDeniedException' in obj._logger.exception.call_args.args[0]
    obj.session_permissions_db.delete.assert_called_once_with(expired_permission)
    obj.events_utils.publish_enforce_session_permissions_event.assert_called_once_with(
        idea_session_id='live', idea_session_owner='owner'
    )
    obj.session_utils.fail_stuck_provisioning_sessions.assert_called_once()
    obj.session_utils.repair_project_instance_profiles.assert_called_once()
    obj.session_utils.clean_up_stopped_sessions.assert_called_once()


@pytest.mark.parametrize(
    'age_minutes,deleted', [(0, False), (9, False), (10, True), (11, True)]
)
def test_missing_session_schedule_has_a_ten_minute_grace_period(
    monkeypatch, age_minutes, deleted
):
    obj, schedule = scheduled_handler()
    now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(DateTimeUtils, 'current_datetime', lambda: now)
    schedule.updated_on = now - timedelta(minutes=age_minutes)
    obj.context.aws().dynamodb_table().Table.return_value.get_item.return_value = {}
    obj.handle_event('message', 'scheduler', event(time='2026-10-12T10:00:00Z'))
    assert obj.schedule_db.delete.called is deleted
    obj.schedule_utils.trigger_schedule.assert_not_called()
    obj.session_utils.fail_stuck_provisioning_sessions.assert_called_once()


def test_schedule_creation_timestamp_survives_storage_and_refreshes_on_update(
    monkeypatch,
):
    obj = object.__new__(VirtualDesktopScheduleDB)
    obj.context = Mock()
    obj._table_obj = Mock()
    obj._table_name = 'test.schedules'
    obj._table_obj.update_item.return_value = {'Attributes': {}}
    now_ms = 1791799200123
    monkeypatch.setattr(DateTimeUtils, 'current_time_millis', lambda: now_ms)
    schedule = VirtualDesktopSchedule(
        day_of_week=DayOfWeek.MONDAY,
        idea_session_id='new',
        idea_session_owner='owner',
        schedule_type=VirtualDesktopScheduleType.CUSTOM_SCHEDULE,
    )
    created = obj.create(schedule)
    stored = obj._table_obj.put_item.call_args.kwargs['Item']
    assert stored['updated_on'] == now_ms
    assert created.updated_on == DateTimeUtils.to_datetime(now_ms)
    now_ms += 60000
    updated = obj.update(created)
    assert (
        obj._table_obj.update_item.call_args.kwargs['ExpressionAttributeValues'][
            ':updated_on'
        ]
        == now_ms
    )
    assert updated.updated_on == DateTimeUtils.to_datetime(now_ms)


def test_live_new_schedule_still_triggers():
    obj, schedule = scheduled_handler()
    schedule.updated_on = DateTimeUtils.current_datetime()
    obj.context.aws().dynamodb_table().Table.return_value.get_item.return_value = {
        'Item': {'idea_session_id': 'deleted'}
    }
    obj.handle_event('message', 'scheduler', event(time='2026-10-12T10:00:00Z'))
    obj.schedule_db.delete.assert_not_called()
    obj.schedule_utils.trigger_schedule.assert_called_once()


def test_deleted_session_stop_is_debug_only():
    obj = handler(IDEASessionScheduledStopEventHandler)
    obj.session_db.get_from_db.return_value = None
    obj.handle_event('message', 'controller', event())
    obj._logger.debug.assert_called_once()
    obj._logger.info.assert_not_called()
    obj._logger.error.assert_not_called()
    obj.ssm_commands_utils.submit_ssm_command_to_get_cpu_utilization.assert_not_called()
    obj.session_utils.stop_sessions.assert_not_called()


def test_ready_session_stop_still_checks_cpu():
    obj = handler(IDEASessionScheduledStopEventHandler)
    obj.session_db.get_from_db.return_value = SimpleNamespace(
        state=VirtualDesktopSessionState.READY,
        server=SimpleNamespace(instance_id='i-test'),
        idea_session_id='live',
        owner='owner',
        base_os='linux',
    )
    obj.handle_event('message', 'controller', event(idea_session_id='live'))
    obj.ssm_commands_utils.submit_ssm_command_to_get_cpu_utilization.assert_called_once()


@pytest.mark.parametrize(
    'cls',
    [
        IDEAResumeSessionCommandProgressEventHandler,
        IDEASessionCPUUtilizationCommandProgressEventHandler,
    ],
)
def test_in_progress_is_debug(cls):
    obj = handler(cls)
    obj.handle_event('message', 'controller', event(status='InProgress'))
    obj._logger.debug.assert_called_once()
    obj._logger.error.assert_not_called()


@pytest.mark.parametrize(
    'cls',
    [
        IDEAResumeSessionCommandProgressEventHandler,
        IDEASessionCPUUtilizationCommandProgressEventHandler,
    ],
)
def test_unexpected_progress_state_stays_error(cls):
    obj = handler(cls)
    obj.handle_event('message', 'controller', event(status='Unexpected'))
    obj._logger.error.assert_called_once()


@pytest.mark.parametrize('name', ['enforce', 'update'])
def test_missing_permissions_are_debug(name):
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_permissions_event_handlers.idea_session_permissions_enforce_event_handler import (
        IDEASessionPermissionsEnforceEventHandler,
    )
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_permissions_event_handlers.idea_session_permissions_update_event_handler import (
        IDEASessionPermissionsUpdateEventHandler,
    )

    cls = (
        IDEASessionPermissionsEnforceEventHandler
        if name == 'enforce'
        else IDEASessionPermissionsUpdateEventHandler
    )
    obj = handler(cls)
    obj.session_db.get_from_db.return_value = None
    obj.session_permissions_db = Mock()
    obj.session_permissions_db.get_for_session.return_value = []
    obj.handle_event('message', 'controller', event())
    obj._logger.debug.assert_called_once()
    obj._logger.error.assert_not_called()
    obj._logger.info.assert_not_called()
