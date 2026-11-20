"""
Round-4 panel findings in the desktop controller, each through the real request parsing,
table conversion or handler. Ids refer to the round-4 triage (VDC-nn).
"""

from unittest.mock import Mock

from ideadatamodel import (
    CreateSessionRequest,
    DayOfWeek,
    UpdateSessionRequest,
    VirtualDesktopScheduleType,
    VirtualDesktopSessionScreenshot,
    VirtualDesktopWeekSchedule,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
    VirtualDesktopEventType,
)
from ideavirtualdesktopcontroller.app.events.handlers.scheduled_event_handler import (
    ScheduledEventHandler,
)
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_utils import (
    VirtualDesktopScheduleUtils,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)
from stored_session import read_back


def required_pwsh() -> str:
    """
    pwsh, as the Windows bootstrap tests require it: in CI, or with IDEA_REQUIRE_PWSH=1, a
    missing pwsh fails the test; elsewhere the test is skipped and says why
    """
    import os
    import shutil

    pwsh = shutil.which('pwsh')
    if pwsh is not None:
        return pwsh
    if os.environ.get('CI') == 'true' or os.environ.get('IDEA_REQUIRE_PWSH') == '1':
        pytest.fail(
            'pwsh is required here (CI=true or IDEA_REQUIRE_PWSH=1) and is not installed'
        )
    pytest.skip(
        'SKIPPED: pwsh is not installed, so the PowerShell parse check did not run'
    )


def user_api():
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api.context.config.return_value.get_string.return_value = 'marker-value'
    api.context.config.return_value.get_list.return_value = []
    api._logger = Mock()
    api.controller_utils = Mock()
    api.controller_utils.get_gpu_manufacturer.return_value = None
    api._resolve_instance_profile_arn = lambda session: 'marker-profile'
    api.DEFAULT_ROOT_VOL_IOPS = '100'
    return api


def stored_schedule(day, schedule_type, start=None, stop=None, schedule_id='sch-1'):
    db = VirtualDesktopScheduleDB.__new__(VirtualDesktopScheduleDB)
    return db.convert_db_dict_to_schedule_object(
        {
            'day_of_week': day.value,
            'schedule_id': schedule_id,
            'idea_session_id': 's-1',
            'idea_session_owner': 'user-a',
            'schedule_type': schedule_type.value,
            # the table's key names for the two times are each other's
            'shut_down_time': start,
            'start_up_time': stop,
        }
    )


def schedule_utils():
    utils = VirtualDesktopScheduleUtils.__new__(VirtualDesktopScheduleUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._schedule_db = Mock()
    utils._schedule_db.get_empty_schedule.side_effect = (
        VirtualDesktopScheduleDB.__new__(VirtualDesktopScheduleDB).get_empty_schedule
    )
    utils._schedule_db.create.side_effect = lambda schedule: schedule
    utils._events_utils = Mock()
    return utils


# --- VDC-01, VDC-04, VDC-27: a new desktop holds only what its creator may choose ------------

CREATE_PAYLOAD = {
    'session': {
        'name': 'desk',
        'owner': 'user-a',
        'hibernation_enabled': False,
        'software_stack': {'stack_id': 'ss-1', 'base_os': 'amazonlinux2023'},
        'project': {'project_id': 'p-1'},
        'server': {
            'instance_type': 'm5.large',
            'root_volume_size': {'value': 20, 'unit': 'gb'},
            'subnet_id': 'subnet-marker',
            'bootstrap_refresh_version': 99,
            'state': 'marker-state',
            'locked': True,
            'instance_id': 'i-marker',
        },
        'dcv_session_id': 'dcv-of-another-desktop',
        'schedule': {
            'monday': {
                'schedule_id': 'sch-of-another-desktop',
                'day_of_week': 'monday',
                'schedule_type': 'START_ALL_DAY',
            }
        },
        'locked': True,
        'stopped_on': '2020-01-01T00:00:00Z',
        'cleanup_exempt': True,
        'recovering': True,
        'reboot_deadline': 5,
    }
}


def created_session():
    request = CreateSessionRequest.model_validate(CREATE_PAYLOAD)
    context = Mock()
    context.get_username.return_value = 'user-a'
    return user_api().complete_create_session_request(request.session, context)


def test_a_new_desktop_keeps_only_the_fields_its_creator_may_choose():
    session = created_session()

    assert session.dcv_session_id is None
    assert session.schedule is None
    assert session.locked is None and session.stopped_on is None
    assert session.cleanup_exempt is None and session.recovering is False
    assert session.reboot_deadline == 0
    assert session.server.bootstrap_refresh_version is None
    assert session.server.state is None and session.server.locked is None
    assert session.server.instance_id is None
    # what the creator chose is kept
    assert session.name == 'desk'
    assert session.server.instance_type == 'm5.large'
    assert session.server.subnet_id == 'subnet-marker'
    assert session.software_stack.stack_id == 'ss-1'


def test_creating_a_desktop_removes_no_schedule_it_names():
    utils = schedule_utils()
    defaults = VirtualDesktopWeekSchedule(
        **{
            day.value: stored_schedule(
                day, VirtualDesktopScheduleType.NO_SCHEDULE, schedule_id=None
            )
            for day in DayOfWeek
        }
    )
    utils.update_schedule_for_session(defaults, created_session())
    utils._schedule_db.delete.assert_not_called()


def test_a_screenshot_needs_a_ready_desktop():
    api = user_api()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = read_back(
        state='PROVISIONING', dcv_session_id='dcv-of-another-desktop'
    )
    api.context.dcv_broker_client.get_session_screenshots.return_value = ([], [])

    _, failed = api._get_session_screenshots(
        [
            VirtualDesktopSessionScreenshot(
                idea_session_id='s-1', idea_session_owner='user-a'
            )
        ]
    )

    assert len(failed) == 1
    api.context.dcv_broker_client.get_session_screenshots.assert_called_once_with([])


# --- VDC-22: one bad schedule row does not stop the scheduled tick ---------------------------


def scheduled_handler(schedules, permissions):
    handler = ScheduledEventHandler.__new__(ScheduledEventHandler)
    handler.context = Mock()
    handler.context.cluster_timezone.return_value = 'UTC'
    handler.context.aws().dynamodb_table().Table().get_item.return_value = {
        'Item': {'idea_session_id': 's-1'}
    }
    handler._logger = Mock()
    handler.is_sender_scheduled_event_lambda_role = lambda sender: True
    handler.DAY_OF_WEEKS = list(DayOfWeek)
    handler.schedule_db = Mock()
    handler.schedule_db.get_schedules_for_day_of_week.return_value = schedules
    handler.session_db = Mock()
    handler.session_permissions_db = Mock()
    handler.session_permissions_db.list_all_from_db.return_value = (permissions, None)
    handler.events_utils = Mock()
    handler.session_utils = Mock()
    handler.session_utils.fail_stuck_provisioning_sessions.return_value = 0
    handler.session_utils.repair_project_instance_profiles.return_value = 0
    handler.session_utils.clean_up_stopped_sessions.return_value = {}
    handler.schedule_utils = schedule_utils()
    handler.schedule_utils.context.config.return_value.get_string.return_value = '09:00'
    return handler


def tick(handler):
    # 2026-11-16 is a Monday
    handler.handle_event(
        'm-1',
        'scheduler',
        VirtualDesktopEvent(
            event_group_id='scheduler',
            event_type=VirtualDesktopEventType.SCHEDULED_EVENT,
            detail={'time': '2026-11-16T10:00:00Z'},
        ),
    )


def test_a_custom_schedule_with_no_times_does_not_stop_the_tick():
    broken = stored_schedule(
        DayOfWeek.MONDAY, VirtualDesktopScheduleType.CUSTOM_SCHEDULE
    )
    good = stored_schedule(
        DayOfWeek.MONDAY, VirtualDesktopScheduleType.START_ALL_DAY, schedule_id='sch-2'
    )
    no_expiry = (
        VirtualDesktopSessionPermissionDB.convert_db_dict_to_session_permission_object(
            {
                'idea_session_id': 's-1',
                'actor_name': 'user-b',
                'actor_type': 'USER',
                'idea_session_base_os': 'amazonlinux2023',
                'idea_session_state': 'READY',
                'idea_session_type': 'VIRTUAL',
                'permission_profile_id': 'full',
            }
        )
    )
    handler = scheduled_handler([broken, good], [no_expiry])

    tick(handler)

    handler.schedule_utils._events_utils.publish_idea_session_scheduled_resume_event.assert_called_once()
    handler.session_utils.fail_stuck_provisioning_sessions.assert_called_once()
    handler.session_permissions_db.delete.assert_not_called()


def update_api(stored):
    api = user_api()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = stored
    api.session_db.update.side_effect = lambda session: session
    api.schedule_utils = Mock()
    api.server_db = Mock()
    return api


def test_a_custom_schedule_needs_its_times_on_write():
    request = UpdateSessionRequest.model_validate(
        {
            'session': {
                'idea_session_id': 's-1',
                'owner': 'user-a',
                'schedule': {
                    'monday': {
                        'day_of_week': 'monday',
                        'schedule_type': 'CUSTOM_SCHEDULE',
                        'start_up_time': '9am',
                        'shut_down_time': '17:00',
                    }
                },
            }
        }
    )
    api = update_api(read_back(state='READY'))

    result = api._update_session(request.session)

    assert 'HH:MM' in result.failure_reason
    api.schedule_utils.update_schedule_for_session.assert_not_called()


# --- VDC-23: changed custom hours are stored -----------------------------------------------


def test_changed_custom_hours_replace_the_stored_schedule():
    utils = schedule_utils()
    current = stored_schedule(
        DayOfWeek.MONDAY, VirtualDesktopScheduleType.CUSTOM_SCHEDULE, '09:00', '17:00'
    )
    requested = UpdateSessionRequest.model_validate(
        {
            'session': {
                'schedule': {
                    'monday': {
                        'schedule_type': 'CUSTOM_SCHEDULE',
                        'start_up_time': '10:00',
                        'shut_down_time': '18:00',
                    }
                }
            }
        }
    ).session.schedule.monday

    result = utils._create_or_update_schedule_for_session_for_day_of_week(
        DayOfWeek.MONDAY, current, requested, 's-1', 'user-a'
    )

    utils._schedule_db.delete.assert_called_once()
    created = utils._schedule_db.create.call_args.args[0]
    assert (created.start_up_time, created.shut_down_time) == ('10:00', '18:00')
    assert result is created


def test_resubmitted_custom_hours_leave_the_stored_schedule_alone():
    utils = schedule_utils()
    current = stored_schedule(
        DayOfWeek.MONDAY, VirtualDesktopScheduleType.CUSTOM_SCHEDULE, '09:00', '17:00'
    )
    requested = current.model_copy(update={'schedule_id': None})

    utils._create_or_update_schedule_for_session_for_day_of_week(
        DayOfWeek.MONDAY, current, requested, 's-1', 'user-a'
    )

    utils._schedule_db.delete.assert_not_called()
    utils._schedule_db.create.assert_not_called()


# --- shared fixtures for the share, broker and lifecycle findings --------------------------

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402
from ideadatamodel.exceptions import SocaException  # noqa: E402
from stored_session import FakeTable  # noqa: E402

SHARE_ROW = {
    'idea_session_id': 's-1',
    'actor_name': 'user-b',
    'actor_type': 'USER',
    'idea_session_owner': 'user-a',
    'idea_session_name': 'desk',
    'idea_session_instance_type': 'm5.large',
    'idea_session_base_os': 'amazonlinux2023',
    'idea_session_state': 'READY',
    'idea_session_type': 'VIRTUAL',
    'idea_session_created_on': 1600000000000,
    'permission_profile_id': 'full',
    'expiry_date': 4102444800000,
}


def share_db(rows=(SHARE_ROW,)):
    db = VirtualDesktopSessionPermissionDB.__new__(VirtualDesktopSessionPermissionDB)
    db._logger = Mock()
    db._ddb_client = Mock()
    db._table_obj = FakeTable('idea_session_id', 'actor_name', rows)
    db.trigger_update_event = Mock()
    return db


# --- VDC-03: the metadata worker never brings a revoked share back -------------------------


def test_a_share_revoked_during_the_metadata_update_stays_revoked():
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_permissions_event_handlers.idea_session_permissions_update_event_handler import (
        IDEASessionPermissionsUpdateEventHandler,
    )

    db = share_db()
    table = db._table_obj
    read = table.query

    def read_then_revoke(**kwargs):
        response = read(**kwargs)
        table.rows.clear()  # the owner revokes the share right after the read
        return response

    table.query = read_then_revoke
    handler = IDEASessionPermissionsUpdateEventHandler.__new__(
        IDEASessionPermissionsUpdateEventHandler
    )
    handler._logger = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_permissions_db = db

    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.IDEA_SESSION_PERMISSIONS_UPDATE_EVENT,
            detail={'idea_session_id': 's-1', 'new_state': 'STOPPED'},
        ),
    )

    assert table.rows == {}


def test_the_metadata_update_keeps_a_changed_expiry():
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_permissions_event_handlers.idea_session_permissions_update_event_handler import (
        IDEASessionPermissionsUpdateEventHandler,
    )

    db = share_db()
    table = db._table_obj
    read = table.query

    def read_then_shorten(**kwargs):
        response = read(**kwargs)
        table.rows[('s-1', 'user-b')]['expiry_date'] = 1700000000000
        return response

    table.query = read_then_shorten
    handler = IDEASessionPermissionsUpdateEventHandler.__new__(
        IDEASessionPermissionsUpdateEventHandler
    )
    handler._logger = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_permissions_db = db

    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.IDEA_SESSION_PERMISSIONS_UPDATE_EVENT,
            detail={'idea_session_id': 's-1', 'new_name': 'renamed'},
        ),
    )

    row = table.rows[('s-1', 'user-b')]
    assert row['expiry_date'] == 1700000000000
    assert row['idea_session_name'] == 'renamed'


# --- VDC-05 and VDC-37 (R4-05): connecting on a share ------------------------------------


def connecting_api(projects):
    api = user_api()
    api.session_permissions_db = share_db()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project={'project_id': 'p-1', 'name': 'p'}
    )
    api.VDI_GROUPS = ['vdi-users']
    api.context.accounts_client.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(additional_groups=['vdi-users'])
    )
    api.context.projects_client.get_user_projects.return_value = projects
    return api


def connection(owner='user-a'):
    from ideadatamodel import VirtualDesktopSessionConnectionInfo

    return VirtualDesktopSessionConnectionInfo(
        idea_session_id='s-1', idea_session_owner=owner
    )


def test_a_recipient_who_left_the_project_cannot_connect():
    api = connecting_api(projects=[])
    assert api._may_connect(connection(), 'user-b') is False


def test_a_recipient_still_in_the_project_connects():
    api = connecting_api(projects=[SimpleNamespace(project_id='p-1')])
    assert api._may_connect(connection(), 'user-b') is True


def test_no_share_row_is_a_plain_refusal():
    api = connecting_api(projects=[SimpleNamespace(project_id='p-1')])
    api.session_permissions_db = share_db(rows=())
    assert api.session_permissions_db.get(idea_session_id='s-1', actor='user-c') is None
    assert api._may_connect(connection(), 'user-c') is False


# --- VDC-08: a permission update the broker refuses is retried -----------------------------


def test_a_refused_permission_update_is_not_reported_as_done():
    from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client import (
        DCVBrokerClient,
    )
    from ideavirtualdesktopcontroller.app.clients.dcvssmswaggerclient.models.update_session_permissions_response import (
        UpdateSessionPermissionsResponse,
    )
    from ideavirtualdesktopcontroller.app.clients.dcvssmswaggerclient.models.update_session_permissions_unsuccessful_response import (
        UpdateSessionPermissionsUnsuccessfulResponse,
    )

    client = DCVBrokerClient.__new__(DCVBrokerClient)
    client._logger = Mock()
    client._session_permission_utils = Mock()
    client._session_permission_utils.generate_permissions_for_session.return_value = (
        'marker-permissions'
    )
    api = Mock()
    api.update_session_permissions.return_value = UpdateSessionPermissionsResponse(
        unsuccessful_list=[
            UpdateSessionPermissionsUnsuccessfulResponse(
                session_id='dcv-1', failure_reason='marker refusal'
            )
        ]
    )
    client._get_session_permissions_api = lambda: api

    with pytest.raises(SocaException):
        client.enforce_session_permissions(read_back(state='READY'))


# --- VDC-15: an edit does not undo a stop or delete made meanwhile ------------------------


def test_an_edit_racing_a_stop_does_not_write_the_old_state_back():
    from stored_session import session_table_reader, stored_row
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import (
        SESSION_CHANGED_MESSAGE,
    )

    db = session_table_reader()
    db._table_obj = FakeTable('owner', 'idea_session_id', [stored_row(state='READY')])
    api = update_api(db.convert_db_dict_to_session_object(stored_row(state='READY')))
    api.session_db = db
    # the desktop is stopped between the edit's read and its write
    api.session_db.get_from_db = Mock(
        side_effect=lambda **kwargs: db.convert_db_dict_to_session_object(
            stored_row(state='READY')
        )
    )
    db._table_obj.rows[('user-a', 's-1')].update(
        state='STOPPING', updated_on=1700000000999
    )
    request = UpdateSessionRequest.model_validate(
        {'session': {'idea_session_id': 's-1', 'owner': 'user-a', 'name': 'renamed'}}
    )

    result = api._update_session(request.session)

    assert result.failure_reason == SESSION_CHANGED_MESSAGE
    assert db._table_obj.rows[('user-a', 's-1')]['state'] == 'STOPPING'


# --- VDC-16: a resume command's result only moves a resuming desktop ----------------------


def resume_progress(row, status, session_db=None):
    from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_resume_session_command_progress_event_handler import (
        IDEAResumeSessionCommandProgressEventHandler,
    )

    handler = IDEAResumeSessionCommandProgressEventHandler.__new__(
        IDEAResumeSessionCommandProgressEventHandler
    )
    handler._logger = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_db = session_db or Mock()
    if session_db is None:
        handler.session_db.get_from_db.return_value = row
    handler.events_utils = Mock()
    handler.ssm_commands_db = Mock()
    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.IDEA_SESSION_RESUME_SESSION_COMMAND_PROGRESS_EVENT,
            detail={
                'status': status,
                'idea_session_id': 's-1',
                'idea_session_owner': 'user-a',
                'command_id': 'c-1',
            },
        ),
    )
    return handler


def test_a_late_resume_success_does_not_undo_a_delete():
    handler = resume_progress(read_back(state='DELETING'), 'Success')
    handler.session_db.update.assert_not_called()
    handler.events_utils.publish_validate_dcv_session_creation_event.assert_not_called()


@pytest.mark.parametrize('status', ['TimedOut', 'Cancelled'])
def test_a_resume_that_never_ran_puts_the_desktop_in_error(status):
    handler = resume_progress(read_back(state='RESUMING'), status)
    written = handler.session_db.update.call_args.args[0]
    assert written.state == 'ERROR'


# --- VDC-21 part 1, R4-07: a host whose desktop cannot be recorded is released ------------


def test_a_launched_host_is_released_when_the_desktop_cannot_be_recorded():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    session = read_back(state='PROVISIONING')
    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._server_utils = Mock()
    utils._server_utils.provision_host_for_session.return_value = session.server
    utils._schedule_utils = Mock()
    utils._schedule_utils.update_schedule_for_session.side_effect = (
        lambda schedules, s: s
    )
    utils._session_db = Mock()
    utils._session_db.create.side_effect = ClientError(
        {'Error': {'Code': 'ProvisionedThroughputExceededException'}}, 'PutItem'
    )

    result = utils.create_session(session)

    utils._server_utils.terminate_dcv_hosts.assert_called_once_with(
        [session.server], force=True
    )
    assert result.failure_reason


# --- VDC-31: a disabled stack launches nothing ---------------------------------------------


def test_a_disabled_stack_is_refused_on_create():
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import (
        STACK_DISABLED_MESSAGE,
    )
    from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
        VirtualDesktopSoftwareStackDB,
    )
    from stored_session import stored_row

    stacks = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    stack = stacks.convert_db_dict_to_software_stack_object(
        {
            **stored_row()['software_stack'],
            'enabled': False,
            'projects': ['p-1'],
        }
    )
    api = user_api()
    api.context.config.return_value.get_bool.return_value = False
    api.context.projects_client.get_user_projects.return_value = [
        SimpleNamespace(project_id='p-1')
    ]
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = stack
    request = CreateSessionRequest.model_validate(CREATE_PAYLOAD)

    session, valid = api.validate_create_session_request(request.session)

    assert valid is False
    assert session.failure_reason == STACK_DISABLED_MESSAGE


# --- VDC-07: a profile change reaches every desktop that uses it --------------------------


def profile_change_handler(share_rows, sessions):
    from ideadatamodel import SocaListingPayload
    from ideavirtualdesktopcontroller.app.events.handlers.db_entry_event_handlers.db_entry_updated_event_handler import (
        DbEntryUpdatedEventHandler,
    )
    from ideavirtualdesktopcontroller.app.permission_profiles.virtual_desktop_permission_profile_db import (
        VirtualDesktopPermissionProfileDB,
    )

    class OneTypeProfiles(VirtualDesktopPermissionProfileDB):
        permission_types = [
            SimpleNamespace(key='clipboard_copy', name='Copy', description='')
        ]

    profiles = OneTypeProfiles.__new__(OneTypeProfiles)
    handler = DbEntryUpdatedEventHandler.__new__(DbEntryUpdatedEventHandler)
    handler.context = Mock()
    handler.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        'owner-default' if key.endswith('.owner') else 'admin-default'
    )
    handler._logger = Mock()
    handler.permission_profile_db = profiles
    handler.session_permissions_db = share_db(share_rows)
    handler.session_db = Mock()
    handler.session_db.list_all_from_db.return_value = SocaListingPayload(
        listing=sessions
    )
    handler.events_utils = Mock()
    return handler


def enforced(handler, profile_id):
    handler._handle_permission_profile_updated(
        '',
        '',
        {'profile_id': profile_id, 'clipboard_copy': True},
        {'profile_id': profile_id, 'clipboard_copy': False},
        '',
    )
    return sorted(
        c.kwargs['idea_session_id']
        for c in handler.events_utils.publish_enforce_session_permissions_event.call_args_list
    )


def test_a_profile_change_reaches_every_share_past_the_first_ten():
    rows = [
        {
            **SHARE_ROW,
            'idea_session_id': f's-{n:02d}',
            'permission_profile_id': 'shared',
        }
        for n in range(11)
    ]
    assert len(enforced(profile_change_handler(rows, []), 'shared')) == 11


def test_a_default_profile_change_reaches_ready_desktops_with_no_share():
    sessions = [
        read_back(session_id='s-ready', state='READY'),
        read_back(session_id='s-off', state='STOPPED'),
    ]
    assert enforced(profile_change_handler((), sessions), 'owner-default') == [
        's-ready'
    ]


# --- VDC-14: an exemption set during the cleanup keeps the desktop -------------------------


def test_an_exemption_set_during_the_cleanup_keeps_the_desktop():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    instance = {
        'InstanceId': 'i-1',
        'State': {'Name': 'stopped'},
        'StateTransitionReason': 'User initiated (2020-01-01 00:00:00 GMT)',
        'Tags': [],
    }
    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._controller_utils = Mock()
    utils._controller_utils.describe_instance.return_value = instance
    utils._session_db = Mock()
    utils._session_db.get_from_db.return_value = read_back(
        state='STOPPED', cleanup_exempt=True
    )
    utils._tear_down_session = Mock()

    outcome = utils.clean_up_stopped_session(
        read_back(state='STOPPED'), 30, 0, {'idea:keep'}, False
    )

    assert outcome == 'kept'
    utils._tear_down_session.assert_not_called()


def test_a_keep_tag_set_during_the_cleanup_keeps_the_desktop():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    first = {
        'InstanceId': 'i-1',
        'State': {'Name': 'stopped'},
        'StateTransitionReason': 'User initiated (2020-01-01 00:00:00 GMT)',
        'Tags': [],
    }
    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._controller_utils = Mock()
    utils._controller_utils.describe_instance.side_effect = [
        first,
        {**first, 'Tags': [{'Key': 'idea:keep', 'Value': 'yes'}]},
    ]
    utils._session_db = Mock()
    utils._session_db.get_from_db.return_value = read_back(state='STOPPED')
    utils._tear_down_session = Mock()

    outcome = utils.clean_up_stopped_session(
        read_back(state='STOPPED'), 30, 0, {'idea:keep'}, False
    )

    assert outcome == 'kept'
    utils._tear_down_session.assert_not_called()


# --- VDC-32: the base refresh keeps a promotion made meanwhile ----------------------------


def test_the_base_refresh_keeps_a_promotion_made_after_its_read():
    import test_refresh_base_stack_amis as refresh_tests
    from ideadatamodel import SocaListingPayload
    from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
        VirtualDesktopSoftwareStackDB,
    )

    stack_id = 'ss-base-amazonlinux2023-x86-64-base'
    row = {
        'base_os': 'amazonlinux2023',
        'stack_id': stack_id,
        'ami_id': 'ami-oldoldoldoldold01',
        'min_storage_value': 10,
        'min_storage_unit': 'gb',
        'min_ram_value': 4,
        'min_ram_unit': 'gb',
        'architecture': 'x86_64',
        'gpu': 'NO_GPU',
    }
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db._logger = Mock()
    db._table_obj = FakeTable('base_os', 'stack_id', [row])
    db.trigger_update_event = Mock()
    listed = db.convert_db_dict_to_software_stack_object(row)
    # the image pipeline promotes a validated image after the refresh read the stack
    db._table_obj.rows[('amazonlinux2023', stack_id)]['ami_id'] = (
        'ami-promotedpromoted1'
    )
    db.list_all_from_db = lambda request: SocaListingPayload(listing=[listed])
    utils = refresh_tests.build_utils(
        db,
        refresh_tests.FakeEc2(
            'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
        ),
    )

    results = utils.refresh_base_software_stack_amis()

    stored = db._table_obj.rows[('amazonlinux2023', stack_id)]
    assert [r.status for r in results] == ['base_updated']
    assert stored['ami_id'] == 'ami-promotedpromoted1'
    assert stored['base_ami_id'] == 'ami-newnewnewnewnew01'


# --- WIN-18: the Windows image check fits the cluster's directory -------------------------


def test_a_windows_image_check_outside_a_domain_checks_the_local_owner():
    from ideavirtualdesktopcontroller.app.sessions.image_validation import (
        windows_host_script,
    )

    script = windows_host_script(
        'dcv-1', [], 'cpu', user='marker-user', active_directory=False
    )
    assert 'Test-ComputerSecureChannel' not in script
    assert "Get-LocalUser -Name 'marker-user'" in script
    assert 'Test-ComputerSecureChannel' in windows_host_script('dcv-1', [], 'cpu')


# --- WIN-22: the capture cleanup restores the agent and always unlocks ---------------------


def test_the_capture_cleanup_command_binds_its_confirm_switch():
    import subprocess

    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        VirtualDesktopSSMCommandsUtils,
    )

    utils = VirtualDesktopSSMCommandsUtils.__new__(VirtualDesktopSSMCommandsUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._ssm_client = Mock()
    utils._ssm_client.send_command.return_value = {'Command': {'CommandId': 'c-1'}}
    utils._ssm_commands_db = Mock()
    utils.submit_ssm_command_to_disable_userdata_execution_on_windows(
        'i-1', 's-1', 'user-a'
    )
    (command,) = utils._ssm_client.send_command.call_args.kwargs['Parameters'][
        'commands'
    ]
    # the PowerShell parser names each parameter of the command as written
    parsed = subprocess.run(
        [
            required_pwsh(),
            '-NoProfile',
            '-Command',
            '$ast = [System.Management.Automation.Language.Parser]::ParseInput($env:MARKER_COMMAND, [ref]$null, [ref]$null); '
            '$ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandParameterAst] }, $true) | ForEach-Object { $_.ParameterName }',
        ],
        env={**__import__('os').environ, 'MARKER_COMMAND': command},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert 'Confirm' in parsed and 'Confirm$False' not in parsed


@pytest.mark.parametrize('status', ['Failed', 'TimedOut', 'Cancelled'])
@pytest.mark.parametrize('step', ['disable', 'enable'])
def test_a_capture_step_that_did_not_finish_unlocks_the_desktop(step, status):
    import importlib

    module = importlib.import_module(
        'ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.'
        f'{step}_userdata_windows_command_progress_event_handler'
    )
    cls = next(
        getattr(module, name)
        for name in dir(module)
        if name.endswith('ProgressEventListener') and name.lower().startswith(step)
    )
    handler = cls.__new__(cls)
    handler._logger = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_db = Mock()
    handler.session_db.get_from_db.return_value = read_back(state='READY', locked=True)
    handler.server_db = Mock()
    handler.ssm_commands_db = Mock()
    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.DISABLE_USERDATA_WINDOWS_COMMAND_PROGRESS_EVENT,
            detail={
                'status': status,
                'idea_session_id': 's-1',
                'idea_session_owner': 'user-a',
                'command_id': 'c-1',
            },
        ),
    )
    written = handler.session_db.update.call_args.args[0]
    assert written.locked is False and written.server.locked is False


# --- VDC-21 part 3, R4-10: the per-user limit holds across controller processes ------------


def creates_across_processes(monkeypatch, distributed):
    import threading
    import time

    import ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api as module

    class EachRequestItsOwnProcess(dict):
        # every request gets a fresh in-process lock, as in separate controller tasks
        def setdefault(self, key, default=None):
            return default

        def __delitem__(self, key):
            pass

    monkeypatch.setattr(module, '_CREATE_LOCKS', EachRequestItsOwnProcess())
    rows = []

    def launch(session):
        time.sleep(0.05)
        rows.append(session)
        return session

    def a_controller():
        api = user_api()
        api.context.config.return_value.get_int.return_value = 1
        api.context.distributed_lock.return_value = distributed
        api.session_db = Mock()
        api.session_db.get_session_count_for_user.side_effect = lambda username: len(
            rows
        )
        api.validate_create_session_request = lambda session: (
            (session, True) if len(rows) < 1 else (session, False)
        )
        api.complete_create_session_request = lambda session, context: session
        api.session_utils = Mock()
        api.session_utils.create_session.side_effect = launch
        return api

    def request():
        context = Mock()
        context.get_username.return_value = 'user-a'
        context.get_request_payload_as.return_value = (
            CreateSessionRequest.model_validate(CREATE_PAYLOAD)
        )
        return context

    threads = [
        threading.Thread(target=a_controller().create_session, args=(request(),))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(rows)


def test_the_limit_holds_across_two_controller_processes(monkeypatch):
    import threading

    class LeaseStore:
        def __init__(self):
            self.lock = threading.Lock()

        def acquire(self, key):
            self.lock.acquire()

        def release(self, key):
            self.lock.release()

    class NoLease:
        def acquire(self, key):
            pass

        def release(self, key):
            pass

    assert creates_across_processes(monkeypatch, LeaseStore()) == 1
    # the same requests without the shared lease both launch: the test can tell
    assert creates_across_processes(monkeypatch, NoLease()) == 2


# --- VDI-07: enabling or disabling a stack changes only its status -----------------------


def stack_table(**fields):
    from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
        VirtualDesktopSoftwareStackDB,
    )

    row = {
        'base_os': 'amazonlinux2023',
        'stack_id': 'ss-1',
        'name': 'stack',
        'ami_id': 'ami-newer0000000000001',
        'projects': ['p-new'],
        'enabled': True,
        'min_storage_value': 10,
        'min_storage_unit': 'gb',
        'min_ram_value': 4,
        'min_ram_unit': 'gb',
        'architecture': 'x86_64',
        'gpu': 'NO_GPU',
        **fields,
    }
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db._logger = Mock()
    db.context = Mock()
    db._table_obj = FakeTable('base_os', 'stack_id', [row])
    db.trigger_update_event = Mock()
    return db


def test_a_stack_status_change_keeps_the_stacks_other_fields():
    from ideadatamodel import UpdateSoftwareStackRequest
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        VirtualDesktopAdminAPI,
    )

    api = VirtualDesktopAdminAPI.__new__(VirtualDesktopAdminAPI)
    api._logger = Mock()
    api.software_stack_db = stack_table()
    context = Mock()
    context.get_request_payload_as.side_effect = lambda cls: cls.model_validate(
        {
            'software_stack': {
                'stack_id': 'ss-1',
                'base_os': 'amazonlinux2023',
                'enabled': False,
            }
        }
    )

    api.update_software_stack(context)

    stored = api.software_stack_db._table_obj.rows[('amazonlinux2023', 'ss-1')]
    assert stored['enabled'] is False
    assert stored['ami_id'] == 'ami-newer0000000000001'
    assert stored['projects'] == ['p-new']
    context.success.assert_called_once()
    assert UpdateSoftwareStackRequest  # the request shape the portal sends


# --- VDI-18: a resize is checked against the stack as it is now --------------------------


def test_a_resize_uses_the_stacks_current_allowed_sizes():
    api = user_api()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = read_back(state='STOPPED')
    live = stack_table(
        allowed_instance_types=['m5.large']
    ).convert_db_dict_to_software_stack_object(
        stack_table(allowed_instance_types=['m5.large'])._table_obj.rows[
            ('amazonlinux2023', 'ss-1')
        ]
    )
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = live
    request = UpdateSessionRequest.model_validate(
        {
            'session': {
                'idea_session_id': 's-1',
                'owner': 'user-a',
                'server': {'instance_type': 'm5.xlarge'},
            }
        }
    )
    context = Mock()
    context.get_username.return_value = 'user-a'
    api._validate_owner = lambda owner, context: ('', True)

    api._validate_update_session_request(request.session, context)

    checked = api.controller_utils.get_instance_type_rejection_reason.call_args.kwargs[
        'software_stack'
    ]
    assert checked.allowed_instance_types == ['m5.large']


# --- VDI-06: an administrator's custom size passes every other create check --------------


def test_a_custom_size_create_still_needs_the_owner_in_the_project():
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        VirtualDesktopAdminAPI,
    )

    api = VirtualDesktopAdminAPI.__new__(VirtualDesktopAdminAPI)
    api.context = Mock()
    api.context.config.return_value.get_string.return_value = 'marker-admin'
    api._logger = Mock()
    api.VDI_GROUPS = ['vdi-users']
    api.context.accounts_client.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(additional_groups=['vdi-users'])
    )
    api.context.projects_client.get_user_projects.return_value = []
    api.session_utils = Mock()
    context = Mock()
    context.get_request_payload_as.return_value = CreateSessionRequest.model_validate(
        {**CREATE_PAYLOAD, 'admin_custom_instance_type': True}
    )

    api.create_session(context)

    context.fail.assert_called_once()
    assert 'member of this project' in context.fail.call_args.kwargs['message']
    api.session_utils.create_session.assert_not_called()


# --- R4-21: broker tables created after start are settled by the scheduled tick ----------


def test_broker_tables_created_after_start_get_their_settings_on_a_later_tick():
    from ideavirtualdesktopcontroller.app.events.handlers.dcv_broker_userdata_execution_complete_event_handler import (
        DCVBrokerUserdataExecutionCompleteEventHandler,
    )

    tables = []
    settler = DCVBrokerUserdataExecutionCompleteEventHandler.__new__(
        DCVBrokerUserdataExecutionCompleteEventHandler
    )
    settler.context = Mock()
    settler.context.config.return_value.get_bool.return_value = True
    settler._logger = Mock()
    settler.dynamodb_client = Mock()
    settler.dynamodb_client.describe_table.return_value = {
        'Table': {'BillingModeSummary': {'BillingMode': 'PROVISIONED'}}
    }
    settler.dcv_broker_client_utils = Mock()
    settler.dcv_broker_client_utils.get_broker_dynamodb_table_names.side_effect = (
        lambda: list(tables)
    )
    settler._add_tags_to_table = Mock()

    handler = scheduled_handler([], [])
    ScheduledEventHandler._broker_tables = settler
    ScheduledEventHandler._broker_tables_settled = False
    try:
        tick(handler)
        settler.dynamodb_client.update_table.assert_not_called()
        tables.append('c.vdc.dcv-broker.sessions')  # the broker creates its table
        tick(handler)
        settler.dynamodb_client.update_table.assert_called_once_with(
            TableName='c.vdc.dcv-broker.sessions', BillingMode='PAY_PER_REQUEST'
        )
        settler._add_tags_to_table.assert_called_once()
        assert ScheduledEventHandler._broker_tables_settled is True
    finally:
        ScheduledEventHandler._broker_tables = None
        ScheduledEventHandler._broker_tables_settled = False


# --- U11: a desktop whose SSH rule failed is not recorded as refreshed ------------------


def test_a_refresh_with_a_failed_ssh_rule_exits_non_zero(tmp_path):
    import subprocess

    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        BOOTSTRAP_REFRESH_EPILOGUE,
    )

    script = tmp_path / 'tail.sh'
    script.write_text(BOOTSTRAP_REFRESH_EPILOGUE)
    assert subprocess.run(['bash', str(script)]).returncode == 0
    failed = subprocess.run(
        ['bash', str(script)],
        env={'IDEA_SSH_ACCESS_FAILED': '1', 'PATH': '/usr/bin:/bin'},
    )
    assert failed.returncode == 1


# --- A06: a traced connection response carries no broker token ---------------------------


def test_a_traced_connection_response_masks_the_broker_token():
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_api_invoker import (
        VirtualDesktopApiInvoker,
    )

    invoker = VirtualDesktopApiInvoker.__new__(VirtualDesktopApiInvoker)
    context = Mock()
    context.namespace = 'VirtualDesktop.GetSessionConnectionInfo'
    context.get_response.return_value = {
        'payload': {
            'connection_info': {
                'access_token': 'marker-token',
                'dcv_session_id': 'dcv-1',
            }
        }
    }

    logged = invoker.get_response_logging_payload(context)

    assert 'marker-token' not in str(logged)
    assert logged['payload']['connection_info']['dcv_session_id'] == 'dcv-1'


# --- IN-14 / LIN-28, controller half: the broker is verified once its CA is delivered -----


def test_the_broker_is_verified_when_the_internal_ca_is_delivered(
    tmp_path, monkeypatch
):
    from ideavirtualdesktopcontroller.app.clients.dcv_broker_client import (
        dcv_broker_client as module,
    )

    client = module.DCVBrokerClient.__new__(module.DCVBrokerClient)
    client._DCVBrokerClient__INTERNAL_ALB_ENDPOINT = 'https://internal.marker'
    client._DCVBrokerClient__BROKER_CLIENT_COMMUNICATION_PORT = 8443
    monkeypatch.delenv(module.INTERNAL_CA_FILE_ENV, raising=False)
    assert client._get_client_configuration().verify_ssl is False

    ca = tmp_path / 'ca.pem'
    ca.write_text('marker')
    monkeypatch.setenv(module.INTERNAL_CA_FILE_ENV, str(ca))
    configuration = client._get_client_configuration()
    assert configuration.verify_ssl is True
    assert configuration.ssl_ca_cert == str(ca)


# --- low items -------------------------------------------------------------------------


def test_vdc_09_a_guest_profile_deny_list_is_applied():
    from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
        VirtualDesktopSessionPermissionUtils,
    )

    utils = VirtualDesktopSessionPermissionUtils.__new__(
        VirtualDesktopSessionPermissionUtils
    )
    utils.context = Mock()
    utils.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        'admin-profile' if key.endswith('.admin') else 'owner-profile'
    )
    utils._logger = Mock()
    utils.controller_utils = Mock()
    utils._session_permission_db = share_db(
        [{**SHARE_ROW, 'permission_profile_id': 'guest-profile'}]
    )
    lists = {
        'admin-profile': (['display'], []),
        'owner-profile': (['display'], []),
        'guest-profile': (['display'], ['clipboard-copy']),
    }
    utils._retrieve_permission_profile_values = lambda profile_id: lists[profile_id]

    content = utils.generate_permissions_for_session(
        read_back(state='READY'), for_broker=False
    )

    assert 'user-b deny guest-profile-deny' in content


def test_vdc_10_a_share_naming_an_unknown_profile_is_refused():
    from ideadatamodel import VirtualDesktopSessionPermission

    api = user_api()
    api.permission_profile_db = Mock()
    api.permission_profile_db.get.return_value = None
    permission = (
        VirtualDesktopSessionPermissionDB.convert_db_dict_to_session_permission_object(
            {**SHARE_ROW, 'permission_profile_id': 'no-such-profile'}
        )
    )
    assert VirtualDesktopSessionPermission

    permission, valid = api._validate_session_permission_create_request(permission)

    assert valid is False and 'no-such-profile' in permission.failure_reason


def test_vdc_20_the_per_user_count_reads_every_page():
    from stored_session import session_table_reader

    db = session_table_reader()
    db._table_obj = Mock()
    db._table_obj.query.side_effect = [
        {'Count': 3, 'LastEvaluatedKey': {'owner': 'user-a'}},
        {'Count': 2},
    ]
    assert db.get_session_count_for_user('user-a') == 5


def test_vdc_24_the_schedules_for_a_day_read_every_page():
    db = VirtualDesktopScheduleDB.__new__(VirtualDesktopScheduleDB)
    db._table_obj = Mock()
    row = {
        'day_of_week': 'monday',
        'schedule_id': 'sch-1',
        'idea_session_id': 's-1',
        'idea_session_owner': 'user-a',
        'schedule_type': 'START_ALL_DAY',
    }
    db._table_obj.query.side_effect = [
        {'Items': [row], 'LastEvaluatedKey': {'schedule_id': 'sch-1'}},
        {'Items': [{**row, 'schedule_id': 'sch-2'}]},
    ]
    assert [
        s.schedule_id for s in db.get_schedules_for_day_of_week(DayOfWeek.MONDAY)
    ] == [
        'sch-1',
        'sch-2',
    ]


def test_vdc_24_the_shares_of_a_desktop_read_every_page():
    db = share_db()
    db._table_obj = Mock()
    db._table_obj.query.side_effect = [
        {'Items': [SHARE_ROW], 'LastEvaluatedKey': {'actor_name': 'user-b'}},
        {'Items': [{**SHARE_ROW, 'actor_name': 'user-c'}]},
    ]
    assert [p.actor_name for p in db.get_for_session('s-1')] == ['user-b', 'user-c']


def test_vdc_19_a_failed_row_cleanup_after_termination_is_retried():
    from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_deletion_event_handler import (
        ValidateDCVSessionDeletionEventHandler,
    )
    import inspect

    source = inspect.getsource(ValidateDCVSessionDeletionEventHandler)
    # the cleanup's except branch raises instead of logging and deleting the message
    branch = source.split('self.session_db.delete(session)', 1)[1].split('def ', 1)[0]
    assert 'raise exceptions.general_exception' in branch


def test_vdc_28_a_failed_desktop_render_leaves_no_directory(tmp_path, monkeypatch):
    import os

    import ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils as module
    from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
        VirtualDesktopControllerUtils,
    )

    class FailingBuilder:
        def __init__(self, tmp_dir, target_package_basename, **kwargs):
            self.tmp_dir = tmp_dir

        def build(self):
            os.makedirs(os.path.join(self.tmp_dir, 'partial'))
            raise RuntimeError('marker render failure')

    monkeypatch.setattr(module, 'BootstrapPackageBuilder', FailingBuilder)
    utils = VirtualDesktopControllerUtils.__new__(VirtualDesktopControllerUtils)
    utils.context = Mock()
    utils.context.cluster_name.return_value = 'c'
    utils.context.module_id.return_value = 'vdc'
    utils.context.config.return_value.get_string.return_value = str(tmp_path)
    utils._logger = Mock()

    with pytest.raises(RuntimeError):
        utils._package_and_upload(
            read_back(state='PROVISIONING'), Mock(), 'virtual-desktop-host-linux'
        )
    assert not os.path.exists(
        os.path.join(
            str(tmp_path), 'c', 'vdc', 'dcv-host-bootstrap', 'user-a', 'desk-s-1'
        )
    )


def test_vdc_35_terminate_sessions_force_reaches_the_delete_and_failures_exit_non_zero(
    monkeypatch,
):
    import test_desktop_access_checks as cli_tests
    from ideadatamodel import DeleteSessionResponse, VirtualDesktopSession
    from ideavirtualdesktopcontroller.cli import sessions as sessions_cli

    sent = {}

    def invoke_alt(namespace, payload, result_as):
        sent['sessions'] = payload.sessions
        return DeleteSessionResponse(
            success=[],
            failed=[
                VirtualDesktopSession(
                    idea_session_id='marker-one',
                    owner='user-a',
                    failure_reason='marker',
                )
            ],
        )

    monkeypatch.setattr(
        sessions_cli,
        'build_cli_context',
        lambda **kwargs: SimpleNamespace(
            unix_socket_client=SimpleNamespace(invoke_alt=invoke_alt)
        ),
    )
    import boto3
    from ideasdk.utils import EnvironmentUtils
    from click.testing import CliRunner

    table = cli_tests.PagedTable(cli_tests.ROWS)
    monkeypatch.setattr(
        boto3, 'resource', lambda *a, **k: SimpleNamespace(Table=lambda name: table)
    )
    monkeypatch.setattr(EnvironmentUtils, 'aws_default_region', lambda **k: 'r')
    monkeypatch.setattr(EnvironmentUtils, 'idea_cluster_name', lambda **k: 'c')
    monkeypatch.setattr(EnvironmentUtils, 'idea_module_id', lambda **k: 'vdc')

    result = CliRunner().invoke(
        sessions_cli.terminate_sessions, ['--username', 'user-a', '--force']
    )

    assert result.exit_code == 1
    assert all(session.force for session in sent['sessions'])


def test_vdc_38_a_share_update_with_one_list_is_read():
    from ideadatamodel import UpdateSessionPermissionRequest

    api = user_api()
    api.session_permission_utils = Mock()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = None
    context = Mock()
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = (
        UpdateSessionPermissionRequest.model_validate(
            {
                'delete': [
                    {
                        'idea_session_id': 's-1',
                        'idea_session_owner': 'user-a',
                        'actor_name': 'user-b',
                    }
                ]
            }
        )
    )
    api.update_session_permission(context)
    request = context.get_request_payload_as.return_value
    assert request.create == [] and request.update == []


# --- final list: VBE-11, VDC-13, VDC-11 (api), VDC-29 and the trivial lows ---------------


def test_vbe_11_desktops_reach_error_before_the_queue_moves_their_message():
    import re
    from pathlib import Path

    from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_ready_event_handler import (
        DCVHostReadyEventHandler,
    )
    from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_creation_event_handler import (
        ValidateDCVSessionCreationEventHandler,
    )

    stack = (
        Path(__file__).resolve().parents[2]
        / 'ideactl'
        / 'src'
        / 'cdk'
        / 'stacks'
        / 'vdc.ts'
    ).read_text()
    redrive = int(re.search(r'SQS_MAX_RECEIVE_COUNT_DEFAULT = (\d+);', stack).group(1))
    assert DCVHostReadyEventHandler.SESSION_CREATION_THRESHOLD_COUNT < redrive
    assert ValidateDCVSessionCreationEventHandler.VALIDATION_REQUEST_THRESHOLD < redrive


@pytest.mark.parametrize(
    'line, busy',
    [
        (
            '%Cpu(s):100.0 us,  0.0 sy,  0.0 ni,  0.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st',
            100.0,
        ),
        (
            '%Cpu(s):  1.0 us,  0.5 sy, 95.0 ni,  3.5 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st',
            96.5,
        ),
        (
            '%Cpu(s): 97.0 us,  3.0 sy,  0.0 ni,  0.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st',
            100.0,
        ),
        (
            '%Cpu(s):  2.0 us,  1.0 sy,  0.0 ni, 97.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st',
            3.0,
        ),
    ],
)
def test_vdc_13_the_idle_cpu_probe_reads_every_sample_by_label(line, busy):
    import subprocess

    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        CPU_BUSY_PIPELINE,
    )

    awk = CPU_BUSY_PIPELINE.split('tail -n 1 | ', 1)[1]
    out = subprocess.run(
        ['sh', '-c', awk], input=line + '\n', capture_output=True, text=True, check=True
    ).stdout
    assert float(out) == pytest.approx(busy)


def test_vdc_11_a_refused_edit_does_not_resize_the_instance():
    api = update_api(read_back(state='STOPPED'))
    # the per-desktop idle delay is off by default: any supplied delay is refused
    api.context.config.return_value.get_int.return_value = 0
    request = UpdateSessionRequest.model_validate(
        {
            'session': {
                'idea_session_id': 's-1',
                'owner': 'user-a',
                'server': {'instance_type': 'm5.xlarge'},
                'idle_autostop_delay': 30,
            }
        }
    )

    result = api._update_session(request.session)

    assert result.failure_reason
    api.controller_utils.change_instance_type.assert_not_called()


def test_vbe_08_an_expired_share_is_enforced_before_its_row_goes():
    order = []
    expired = (
        VirtualDesktopSessionPermissionDB.convert_db_dict_to_session_permission_object(
            {**SHARE_ROW, 'expiry_date': 1600000000000}
        )
    )
    handler = scheduled_handler([], [expired])
    handler.events_utils.publish_enforce_session_permissions_event.side_effect = (
        lambda **kwargs: order.append('enforce')
    )
    handler.session_permissions_db.delete.side_effect = lambda permission: order.append(
        'delete'
    )

    tick(handler)

    assert order == ['enforce', 'delete']


def test_vds_22_the_deletion_timeout_setting_is_read():
    from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_deletion_event_handler import (
        ValidateDCVSessionDeletionEventHandler,
    )

    context = Mock()
    context.config.return_value.get_int.side_effect = lambda key, default=None: (
        7
        if key == 'virtual-desktop-controller.dcv_session.validation_timeout_minutes'
        else default
    )
    handler = ValidateDCVSessionDeletionEventHandler.__new__(
        ValidateDCVSessionDeletionEventHandler
    )
    import ideavirtualdesktopcontroller.app.events.handlers.base_event_handler as base

    original = base.BaseVirtualDesktopControllerEventHandler.__init__
    base.BaseVirtualDesktopControllerEventHandler.__init__ = lambda self, *a, **k: None
    try:
        ValidateDCVSessionDeletionEventHandler.__init__(handler, context)
    finally:
        base.BaseVirtualDesktopControllerEventHandler.__init__ = original
    assert handler.dcv_validation_timeout_minutes == 7


def test_vdc_16_a_scheduled_start_leaves_a_failed_desktop_alone():
    from ideavirtualdesktopcontroller.app.events.handlers.idea_session_state_event_handlers.idea_session_scheduled_resume_event_handler import (
        IDEASessionScheduledResumeEventHandler,
    )

    handler = IDEASessionScheduledResumeEventHandler.__new__(
        IDEASessionScheduledResumeEventHandler
    )
    handler._logger = Mock()
    handler.context = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_db = Mock()
    handler.session_db.get_from_db.return_value = read_back(state='ERROR')
    handler.session_utils = Mock()

    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.IDEA_SESSION_SCHEDULED_RESUME_EVENT,
            detail={'idea_session_id': 's-1', 'idea_session_owner': 'user-a'},
        ),
    )

    handler.session_utils.resume_sessions.assert_not_called()


def test_vds_24_a_server_row_keeps_its_state_through_a_read():
    from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
        VirtualDesktopServerDB,
    )

    server = VirtualDesktopServerDB.convert_db_entry_to_server_object(
        {'instance_id': 'i-1', 'state': 'CREATED'}
    )
    assert server.state == 'CREATED'


def test_vbe_25_a_share_without_an_expiry_still_notifies():
    from ideavirtualdesktopcontroller.app.events.handlers.db_entry_event_handlers.db_entry_created_event_handler import (
        DbEntryCreatedEventHandler,
    )

    handler = DbEntryCreatedEventHandler.__new__(DbEntryCreatedEventHandler)
    handler.context = Mock()
    handler.context.config.return_value.get_bool.return_value = True
    handler.context.config.return_value.get_string.return_value = 'marker-template'
    handler._logger = Mock()
    handler.session_permissions_db = share_db()
    handler.session_permission_utils = Mock()
    handler.session_permission_db = handler.session_permissions_db
    row = {k: v for k, v in SHARE_ROW.items() if k != 'expiry_date'}
    name = next(
        n for n in dir(handler) if n.startswith('_handle_session_permission_created')
    )
    getattr(handler, name)('', '', row, '')
    handler.context.notification_async_client.send_notification.assert_called_once()


def schedule_table_with_another_desktops_row():
    table = FakeTable(
        'day_of_week',
        'schedule_id',
        [
            {
                'day_of_week': 'monday',
                'schedule_id': 'sch-other',
                'idea_session_id': 's-other',
                'idea_session_owner': 'user-z',
                'schedule_type': 'START_ALL_DAY',
            }
        ],
    )

    def delete_item(
        Key,
        ReturnValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        row = table.rows.get((Key['day_of_week'], Key['schedule_id']))
        if ConditionExpression and (
            row is None or row['idea_session_id'] != ExpressionAttributeValues[':sid']
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'DeleteItem'
            )
        table.rows.pop((Key['day_of_week'], Key['schedule_id']), None)
        return {'Attributes': row or {}}

    table.delete_item = delete_item
    db = VirtualDesktopScheduleDB.__new__(VirtualDesktopScheduleDB)
    db._logger = Mock()
    db._table_obj = table
    return table, db


def test_vdc_03_residual_a_schedule_reference_to_another_desktop_is_not_deleted():
    table, db = schedule_table_with_another_desktops_row()
    reference = stored_schedule(
        DayOfWeek.MONDAY,
        VirtualDesktopScheduleType.START_ALL_DAY,
        schedule_id='sch-other',
    )

    db.delete(reference)

    assert ('monday', 'sch-other') in table.rows


def test_vdc_35_api_a_batch_create_is_recorded_as_an_administrators():
    from ideadatamodel import BatchCreateSessionRequest
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        VirtualDesktopAdminAPI,
    )

    api = VirtualDesktopAdminAPI.__new__(VirtualDesktopAdminAPI)
    api._logger = Mock()
    api._validate_create_session_request = lambda session: (session, True)
    api.complete_create_session_request = lambda session, context: session
    api.session_utils = Mock()
    api.session_utils.create_session.side_effect = lambda session: session
    context = Mock()
    context.get_request_payload_as.return_value = (
        BatchCreateSessionRequest.model_validate(
            {'sessions': [CREATE_PAYLOAD['session']]}
        )
    )

    api.batch_create_sessions(context)

    assert (
        api.session_utils.create_session.call_args.args[0].is_launched_by_admin is True
    )


def test_vds_06_the_reaper_deletes_only_test_launches():
    import types
    from datetime import datetime, timedelta, timezone

    from ideadatamodel import ImagePipelineSettings
    from ideavirtualdesktopcontroller.app.sessions import image_validation as module
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
        VirtualDesktopSessionDB,
    )
    from stored_session import session_table_reader, stored_row

    old = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
    reader = session_table_reader()
    reader._table_obj = Mock()
    reader._table_obj.query.return_value = {
        'Items': [
            {**stored_row(session_id='own'), 'name': 'my-desktop', 'created_on': old},
            {
                **stored_row(session_id='test'),
                'name': 'validate-rocky9-x86_64',
                'created_on': old,
            },
        ]
    }
    tester = module.ImageTestLauncher.__new__(module.ImageTestLauncher)
    tester._logger = Mock()
    tester.api = SimpleNamespace(
        session_db=SimpleNamespace(
            list_for_owner=types.MethodType(
                VirtualDesktopSessionDB.list_for_owner, reader
            )
        ),
        session_utils=Mock(),
        software_stack_db=Mock(),
    )
    tester._reap_stacks = Mock()

    reaped = tester.reap(ImagePipelineSettings(), timedelta(hours=2))

    assert reaped == ['test']


def test_vdc_18_a_new_attempt_forgets_the_previous_image_name():
    from ideavirtualdesktopcontroller.app.software_stacks import (
        image_pipeline as module,
    )

    class Saved(Exception):
        pass

    seen = {}

    def save(record):
        seen['ami_name'] = record.ami_name
        raise Saved()

    runner = next(
        cls
        for name, cls in vars(module).items()
        if isinstance(cls, type) and hasattr(cls, '_resolve')
    )
    pipeline = runner.__new__(runner)
    pipeline._save = save
    record = SimpleNamespace(
        status=None,
        checks=['old'],
        image_id='ami-old',
        ami_name='marker-old-name',
        instance_id='i-old',
        validated_on='x',
        error='x',
        attempt_started_on=None,
    )
    with pytest.raises(Saved):
        pipeline._resolve(record)
    assert seen['ami_name'] is None


def test_vdc_33_the_cli_does_not_overwrite_a_stack_created_meanwhile():
    from ideavirtualdesktopcontroller.cli.software_stacks import create_software_stack
    from stored_session import stored_row
    from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
        VirtualDesktopSoftwareStackDB,
    )

    stack = VirtualDesktopSoftwareStackDB.__new__(
        VirtualDesktopSoftwareStackDB
    ).convert_db_dict_to_software_stack_object(stored_row()['software_stack'])
    table = Mock()
    table.put_item.side_effect = ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
    )

    assert create_software_stack(table, stack, Mock()) is None
    assert (
        'attribute_not_exists' in table.put_item.call_args.kwargs['ConditionExpression']
    )


def test_vdc_24_api_the_shares_of_a_desktop_are_read_consistently():
    db = share_db()
    db._table_obj = Mock()
    db._table_obj.query.return_value = {'Items': [SHARE_ROW]}
    db.get_for_session('s-1')
    assert db._table_obj.query.call_args.kwargs['ConsistentRead'] is True


# --- VDC-05 (owner decision): an owner who left the project, and moving a desktop ---------


PROJECT_P1 = {'project_id': 'p-1', 'name': 'project-one'}


def resuming_utils(stored, member_of):
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils.context.config.return_value.get_string.return_value = 'marker-admin'
    utils.context.accounts_client.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(enabled=True)
    )
    utils.context.projects_client.get_user_projects.return_value = [
        SimpleNamespace(project_id=p) for p in member_of
    ]
    utils._logger = Mock()
    utils._session_db = Mock()
    utils._session_db.get_from_db.return_value = stored
    utils._session_db.update.side_effect = lambda session, **kwargs: session
    utils._server_utils = Mock()
    utils._server_utils.start_dcv_hosts.return_value = {}
    return utils


def test_vdc_05_an_owner_who_left_the_project_cannot_start_the_desktop():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        OWNER_LEFT_PROJECT_MESSAGE,
    )

    utils = resuming_utils(read_back(state='STOPPED', project=PROJECT_P1), member_of=[])
    _, failed = utils.resume_sessions([read_back(state='STOPPED')])
    assert failed[0].failure_reason == OWNER_LEFT_PROJECT_MESSAGE
    utils._server_utils.start_dcv_hosts.assert_not_called()


def test_vdc_05_an_owner_still_in_the_project_starts_the_desktop():
    utils = resuming_utils(
        read_back(state='STOPPED', project=PROJECT_P1), member_of=['p-1']
    )
    utils.resume_sessions([read_back(state='STOPPED')])
    utils._server_utils.start_dcv_hosts.assert_called_once()


def test_vdc_05_nobody_starts_a_connection_to_a_desktop_whose_owner_left():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        OWNER_LEFT_PROJECT_MESSAGE,
    )

    api = connecting_api(projects=[])
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project=PROJECT_P1
    )
    for caller in ('user-a', 'user-b'):
        request = connection()
        assert api._may_connect(request, caller) is False
        assert request.failure_reason == OWNER_LEFT_PROJECT_MESSAGE
    # nothing here stops the desktop or ends a connection already open
    api.context.dcv_broker_client.delete_sessions.assert_not_called()
    api.session_utils = getattr(api, 'session_utils', Mock())


class PolicyEc2:
    """
    EC2 as the controller role allows the move on the merged tree: the statements are those of
    fix20-iam-cdk 29f4f358 (vdc.ts desktopMovePolicy; virtual-desktop-controller.yml instance
    statement and IdentityTagsSetOnlyAtLaunch). A request is refused whole when any resource or
    key fails a condition, as EC2 does. Any call that is not on the move's list fails the test.
    fail_at injects one failure (any exception) at the named call.
    """

    MOVE_CALLS = {
        'describe_instances',
        'describe_volumes',
        'describe_iam_instance_profile_associations',
        'associate_iam_instance_profile',
        'replace_iam_instance_profile_association',
        'disassociate_iam_instance_profile',
        'create_tags',
        'delete_tags',
    }
    INSTANCE_IDENTITY_KEYS = {'idea:ClusterName', 'idea:NodeType', 'idea:ModuleId'}
    VOLUME_EXCLUDED_KEYS = {
        'idea:ClusterName',
        'idea:NodeType',
        'idea:ModuleId',
        'idea:BackupPlan',
    }

    def __init__(
        self,
        volume_grant=True,
        profile_grant=True,
        passable=('arn:profile-p1', 'arn:profile-p2', 'arn:shared'),
        profile_arn='arn:profile-p1',
        state='stopped',
        volume_cluster_tag=True,
        fail_at=None,
    ):
        self.volume_grant = volume_grant
        self.profile_grant = profile_grant
        self.passable = set(passable)
        self.state = state
        self.fail_at = dict(fail_at or {})
        self.calls = []
        self.tags = {
            'i-1': {
                'idea:ClusterName': 'c',
                'idea:ModuleId': 'vdc',
                'idea:NodeType': 'virtual-desktop-dcv-host',
                'idea:Project': 'project-one',
                'old-only': 'x',
                'cost-centre': 'old-cc',
                'shared-custom': 'p1-value',
            },
            'vol-1': {
                'idea:Project': 'project-one',
                'old-only': 'x',
                'cost-centre': 'old-cc',
                'shared-custom': 'p1-value',
            },
        }
        if volume_cluster_tag:
            self.tags['vol-1']['idea:ClusterName'] = 'c'
        self.association = (
            {
                'AssociationId': 'assoc-1',
                'InstanceId': 'i-1',
                'IamInstanceProfile': {'Arn': profile_arn},
            }
            if profile_arn
            else None
        )

    def __getattr__(self, name):
        raise AssertionError(f'the move made a call that is not on its list: {name}')

    @staticmethod
    def _deny(action):
        return ClientError(
            {'Error': {'Code': 'UnauthorizedOperation', 'Message': action}}, action
        )

    def _enter(self, name):
        self.calls.append(name)
        failure = self.fail_at.pop(name, None)
        if failure is not None:
            raise failure

    def _authorize_tags(self, action, resources, keys):
        for resource in resources:
            tags = self.tags[resource]
            if resource.startswith('i-'):
                allowed = (
                    tags.get('idea:ClusterName') == 'c'
                    and tags.get('idea:ModuleId') == 'vdc'
                )
                if not allowed or keys & self.INSTANCE_IDENTITY_KEYS:
                    raise self._deny(action)
            else:
                if not self.volume_grant or tags.get('idea:ClusterName') != 'c':
                    raise self._deny(action)
                if keys & self.VOLUME_EXCLUDED_KEYS:
                    raise self._deny(action)

    def _authorize_profile(self, action, arn):
        tags = self.tags['i-1']
        if (
            not self.profile_grant
            or tags.get('idea:ClusterName') != 'c'
            or tags.get('idea:NodeType') != 'virtual-desktop-dcv-host'
        ):
            raise self._deny(action)
        if arn is not None and arn not in self.passable:
            raise self._deny('iam:PassRole')

    def describe_instances(self, InstanceIds):
        self._enter('describe_instances')
        return {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'InstanceId': 'i-1',
                            'State': {'Name': self.state},
                            **(
                                {
                                    'IamInstanceProfile': self.association[
                                        'IamInstanceProfile'
                                    ]
                                }
                                if self.association
                                else {}
                            ),
                            'Tags': [
                                {'Key': k, 'Value': v}
                                for k, v in self.tags['i-1'].items()
                            ],
                            'BlockDeviceMappings': [{'Ebs': {'VolumeId': 'vol-1'}}],
                        }
                    ]
                }
            ]
        }

    def describe_volumes(self, VolumeIds):
        self._enter('describe_volumes')
        return {
            'Volumes': [
                {
                    'VolumeId': v,
                    'Tags': [{'Key': k, 'Value': x} for k, x in self.tags[v].items()],
                }
                for v in VolumeIds
            ]
        }

    def describe_iam_instance_profile_associations(self, Filters):
        self._enter('describe_iam_instance_profile_associations')
        if not self.profile_grant:
            raise self._deny('DescribeIamInstanceProfileAssociations')
        return {
            'IamInstanceProfileAssociations': [dict(self.association)]
            if self.association
            else []
        }

    def replace_iam_instance_profile_association(
        self, IamInstanceProfile, AssociationId
    ):
        self._enter('replace_iam_instance_profile_association')
        self._authorize_profile(
            'ReplaceIamInstanceProfileAssociation', IamInstanceProfile['Arn']
        )
        assert AssociationId == self.association['AssociationId']
        self.association = {
            'AssociationId': AssociationId + '+',
            'InstanceId': 'i-1',
            'IamInstanceProfile': IamInstanceProfile,
        }

    def associate_iam_instance_profile(self, IamInstanceProfile, InstanceId):
        self._enter('associate_iam_instance_profile')
        self._authorize_profile(
            'AssociateIamInstanceProfile', IamInstanceProfile['Arn']
        )
        self.association = {
            'AssociationId': 'assoc-new',
            'InstanceId': InstanceId,
            'IamInstanceProfile': IamInstanceProfile,
        }

    def disassociate_iam_instance_profile(self, AssociationId):
        self._enter('disassociate_iam_instance_profile')
        self._authorize_profile('DisassociateIamInstanceProfile', None)
        self.association = None

    def create_tags(self, Resources, Tags):
        self._enter('create_tags')
        for tag in Tags:
            if not isinstance(tag.get('Value'), str):
                # what botocore says before sending a tag with no value
                from botocore.exceptions import ParamValidationError

                raise ParamValidationError(
                    report=f'Invalid type for parameter Tags, value: {tag}'
                )
        self._authorize_tags('CreateTags', Resources, {t['Key'] for t in Tags})
        for r in Resources:
            self.tags[r].update({t['Key']: t['Value'] for t in Tags})

    def delete_tags(self, Resources, Tags):
        self._enter('delete_tags')
        self._authorize_tags('DeleteTags', Resources, {t['Key'] for t in Tags})
        for r in Resources:
            for t in Tags:
                self.tags[r].pop(t['Key'], None)


def mover(
    monkeypatch,
    ec2,
    stored='STOPPED',
    owner_projects=('p-2',),
    target_enabled=True,
    stack_projects=('p-1', 'p-2'),
    profiles=None,
):
    from stored_session import FakeTable, session_table_reader, stored_row
    from ideadatamodel import Project
    from ideavirtualdesktopcontroller.app.api import virtual_desktop_api
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        VirtualDesktopAdminAPI,
    )
    from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
        VirtualDesktopControllerUtils,
    )

    profiles = (
        {'p-1': 'arn:profile-p1', 'p-2': 'arn:profile-p2'}
        if profiles is None
        else profiles
    )
    monkeypatch.setattr(
        virtual_desktop_api,
        'resolve_project_instance_profile_arn',
        lambda context, logger, session: profiles.get(session.project.project_id),
    )
    db = session_table_reader()
    row = stored_row(
        state=stored,
        project={
            **PROJECT_P1,
            'tags': [
                {'key': 'cost-centre', 'value': 'old-cc'},
                {'key': 'old-only', 'value': 'x'},
                {'key': 'shared-custom', 'value': 'p1-value'},
            ],
        },
    )
    row['server']['instance_profile_arn'] = profiles.get('p-1', 'arn:shared')
    db._table_obj = FakeTable('owner', 'idea_session_id', [row])
    db.trigger_update_event = Mock()
    api = VirtualDesktopAdminAPI.__new__(VirtualDesktopAdminAPI)
    api.context = Mock()
    api.context.cluster_name.return_value = 'c'
    config = {
        'cluster.administrator_username': 'marker-admin',
        'virtual-desktop-controller.dcv_host_instance_profile_arn': 'arn:shared',
    }
    api.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        config.get(key)
    )
    api.context.config.return_value.get_list.side_effect = lambda key, **kwargs: (
        ['Key=shared-custom,Value=cluster-value']
        if key == 'global-settings.custom_tags'
        else []
    )
    api._logger = Mock()
    api.session_db = db
    api.server_db = Mock()
    projects = {
        'p-1': Project(
            project_id='p-1',
            name='project-one',
            enabled=True,
            tags=[
                {'key': 'cost-centre', 'value': 'old-cc'},
                {'key': 'old-only', 'value': 'x'},
                {'key': 'shared-custom', 'value': 'p1-value'},
            ],
        ),
        'p-2': Project(
            project_id='p-2',
            name='project-two',
            enabled=target_enabled,
            tags=[{'key': 'cost-centre', 'value': 'new-cc'}],
        ),
    }
    api.context.projects_client.get_project.side_effect = lambda request: (
        SimpleNamespace(project=projects.get(request.project_id))
    )
    api.context.projects_client.get_user_projects.return_value = [
        SimpleNamespace(project_id=p) for p in owner_projects
    ]
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = SimpleNamespace(
        name='stack', projects=[SimpleNamespace(project_id=p) for p in stack_projects]
    )
    api.controller_utils = VirtualDesktopControllerUtils.__new__(
        VirtualDesktopControllerUtils
    )
    api.controller_utils._logger = Mock()
    api.controller_utils.ec2_client = ec2
    context = Mock()
    context.get_username.return_value = 'admin-a'
    context.get_request_payload_as.return_value = UpdateSessionRequest.model_validate(
        {
            'session': {
                'idea_session_id': 's-1',
                'owner': 'user-a',
                'project': {'project_id': 'p-2'},
            }
        }
    )
    return api, context, db._table_obj


def stored_project(table):
    project = table.rows[('user-a', 's-1')]['project']
    return project.get('project_id') if isinstance(project, dict) else project


def assert_unmoved(ec2, table, before):
    assert stored_project(table) == 'p-1'
    if ec2.association is not None:
        assert ec2.association['IamInstanceProfile']['Arn'] == 'arn:profile-p1'
    assert ec2.tags == before


def test_vdc_05_a_move_the_role_cannot_tag_the_volumes_of_changes_nothing(monkeypatch):
    # B1: the role as deployed before the move grant: the volume call is refused after the
    # row, the instance role and the instance tags changed; all three are put back
    ec2 = PolicyEc2(volume_grant=False)
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    message = context.fail.call_args.kwargs['message']
    assert "Tagging the desktop's volumes failed, and nothing was changed." in message
    assert_unmoved(ec2, table, before)


def test_vdc_05_a_move_the_role_may_not_pass_the_new_role_for_changes_nothing(
    monkeypatch,
):
    ec2 = PolicyEc2(passable=('arn:profile-p1', 'arn:shared'))
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    assert (
        "Changing the desktop's instance role failed, and nothing was changed."
        in (context.fail.call_args.kwargs['message'])
    )
    assert_unmoved(ec2, table, before)


@pytest.mark.parametrize(
    'call',
    [
        'disassociate_iam_instance_profile',
        'associate_iam_instance_profile',
        'create_tags',
        'delete_tags',
    ],
)
@pytest.mark.parametrize('failure', ['timeout', 'not sent'])
def test_vdc_05_a_move_that_fails_any_way_at_any_step_changes_nothing(
    monkeypatch, call, failure
):
    from botocore.exceptions import ParamValidationError, ReadTimeoutError

    error = (
        ReadTimeoutError(endpoint_url='https://ec2.marker')
        if failure == 'timeout'
        else ParamValidationError(report='marker')
    )
    ec2 = PolicyEc2(fail_at={call: error})
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    assert 'nothing was changed' in context.fail.call_args.kwargs['message']
    assert_unmoved(ec2, table, before)
    # and the same move then goes through
    api.move_session_to_project(context)
    context.success.assert_called_once()
    assert stored_project(table) == 'p-2'


def test_vdc_05_a_move_whose_row_write_times_out_after_landing_is_put_back(monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    ec2 = PolicyEc2()
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)
    write = api.session_db.update

    def lands_then_times_out(session, **kwargs):
        if session.project.project_id == 'p-2' and not getattr(
            lands_then_times_out, 'done', False
        ):
            lands_then_times_out.done = True
            write(session, **kwargs)
            raise ReadTimeoutError(endpoint_url='https://dynamodb.marker')
        return write(session, **kwargs)

    api.session_db.update = lands_then_times_out

    api.move_session_to_project(context)

    assert 'nothing was changed' in context.fail.call_args.kwargs['message']
    assert_unmoved(ec2, table, before)


def test_vdc_05_an_administrator_moves_a_stopped_desktop_and_its_owner_can_start_it(
    monkeypatch,
):
    ec2 = PolicyEc2()
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    context.success.assert_called_once()
    assert stored_project(table) == 'p-2'
    assert ec2.association['IamInstanceProfile']['Arn'] == 'arn:profile-p2'
    for resource in ('i-1', 'vol-1'):
        tags = ec2.tags[resource]
        assert tags['idea:Project'] == 'project-two' and tags['cost-centre'] == 'new-cc'
        assert 'old-only' not in tags
        assert tags['shared-custom'] == 'cluster-value'
        assert tags['idea:ProjectMovedBy'] == 'admin-a'
    assert ec2.tags['i-1']['idea:ClusterName'] == 'c'

    moved = api.session_db.convert_db_dict_to_session_object(
        table.rows[('user-a', 's-1')]
    )
    utils = resuming_utils(moved, member_of=['p-2'])
    utils.resume_sessions([moved])
    utils._server_utils.start_dcv_hosts.assert_called_once()


def test_vdc_05_without_project_profiles_the_move_reads_and_touches_no_role(
    monkeypatch,
):
    ec2 = PolicyEc2(profile_grant=False, profile_arn='arn:shared')
    api, context, table = mover(monkeypatch, ec2, profiles={})

    api.move_session_to_project(context)

    context.success.assert_called_once()
    assert not any('profile' in call for call in ec2.calls)
    assert stored_project(table) == 'p-2'


def test_vdc_05_undoing_a_move_of_a_host_that_had_no_role_takes_the_new_role_off(
    monkeypatch,
):
    ec2 = PolicyEc2(profile_arn=None, volume_grant=False)
    api, context, table = mover(monkeypatch, ec2, profiles={'p-2': 'arn:profile-p2'})
    table.rows[('user-a', 's-1')]['server'].pop('instance_profile_arn', None)

    api.move_session_to_project(context)

    assert 'disassociate_iam_instance_profile' in ec2.calls
    assert ec2.association is None
    assert stored_project(table) == 'p-1'


def test_vdc_05_a_volume_without_the_cluster_tag_is_named_before_anything_changes(
    monkeypatch,
):
    ec2 = PolicyEc2(volume_cluster_tag=False)
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    message = context.fail.call_args.kwargs['message']
    assert 'vol-1' in message and 'idea:ClusterName=c' in message
    assert stored_project(table) == 'p-1'
    assert not {'create_tags', 'replace_iam_instance_profile_association'} & set(
        ec2.calls
    )


@pytest.mark.parametrize(
    'tag, refused',
    [
        ({'key': 'blank', 'value': ''}, False),
        ({'key': 'idea:ModuleId', 'value': 'x'}, True),
        ({'key': 'aws:marker', 'value': 'x'}, True),
        ({'key': 'k' * 129, 'value': 'x'}, True),
    ],
)
def test_vdc_05_a_project_tag_that_cannot_go_on_a_desktop_is_checked_first(
    monkeypatch, tag, refused
):
    from ideadatamodel import Project

    ec2 = PolicyEc2()
    api, context, table = mover(monkeypatch, ec2)
    target = Project(project_id='p-2', name='project-two', enabled=True, tags=[tag])
    api.context.projects_client.get_project.side_effect = lambda request: (
        SimpleNamespace(
            project=target
            if request.project_id == 'p-2'
            else Project(project_id='p-1', name='project-one', enabled=True, tags=[])
        )
    )

    api.move_session_to_project(context)

    if refused:
        assert 'cannot be put on a desktop' in context.fail.call_args.kwargs['message']
        assert 'create_tags' not in ec2.calls and stored_project(table) == 'p-1'
    else:
        # an empty value is left out, as at launch
        context.success.assert_called_once()
        assert 'blank' not in ec2.tags['i-1']


def test_vdc_05_a_move_racing_another_change_changes_nothing(monkeypatch):
    ec2 = PolicyEc2()
    api, context, table = mover(monkeypatch, ec2)
    table.rows[('user-a', 's-1')]['updated_on'] = 1700000000999
    api.session_db.get_from_db = Mock(
        return_value=api.session_db.convert_db_dict_to_session_object(
            {**table.rows[('user-a', 's-1')], 'updated_on': 1700000000000}
        )
    )

    api.move_session_to_project(context)

    assert (
        'changed while it was being edited' in context.fail.call_args.kwargs['message']
    )
    assert 'create_tags' not in ec2.calls
    assert 'replace_iam_instance_profile_association' not in ec2.calls


@pytest.mark.parametrize(
    'state, ec2_state, owner_projects, enabled, stack_projects, reason',
    [
        ('READY', 'running', ['p-2'], True, ('p-2',), 'Stop the desktop'),
        ('STOPPED', 'running', ['p-2'], True, ('p-2',), 'Stop the desktop'),
        ('STOPPED', 'stopped', ['p-1'], True, ('p-2',), 'not a member'),
        ('STOPPED', 'stopped', ['p-2'], False, ('p-2',), 'disabled'),
        ('STOPPED', 'stopped', ['p-2'], True, ('p-1',), 'not available in project'),
    ],
)
def test_vdc_05_a_move_the_rules_do_not_allow_is_refused(
    monkeypatch, state, ec2_state, owner_projects, enabled, stack_projects, reason
):
    ec2 = PolicyEc2(state=ec2_state)
    api, context, table = mover(
        monkeypatch,
        ec2,
        stored=state,
        owner_projects=owner_projects,
        target_enabled=enabled,
        stack_projects=stack_projects,
    )

    api.move_session_to_project(context)

    assert reason in context.fail.call_args.kwargs['message']
    assert stored_project(table) == 'p-1'
    assert 'create_tags' not in ec2.calls


def test_vdc_05_only_an_administrator_can_move_a_desktop(monkeypatch):
    # elevated access lets managers in; the owner's decision gives the move to administrators
    api, context, table = mover(monkeypatch, PolicyEc2())
    context.is_administrator.return_value = False
    context.is_manager.return_value = True

    with pytest.raises(SocaException):
        api.move_session_to_project(context)
    assert stored_project(table) == 'p-1'


def test_win_12_the_windows_image_check_quotes_typographic_apostrophes():
    import subprocess

    from ideavirtualdesktopcontroller.app.sessions.image_validation import (
        windows_host_script,
    )

    script = windows_host_script(
        'dcv-1', [('marker\u2019share', '\\\\server\\marker\u2019share')], 'cpu'
    )
    pwsh = required_pwsh()
    errors = subprocess.run(
        [
            pwsh,
            '-NoProfile',
            '-Command',
            '$e = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($env:MARKER_SCRIPT, [ref]$null, [ref]$e); $e.Count',
        ],
        env={**__import__('os').environ, 'MARKER_SCRIPT': script},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert errors == '0'


def test_win_24_the_capture_preparation_returns_the_agent_exit_code():
    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        VirtualDesktopSSMCommandsUtils,
    )

    utils = VirtualDesktopSSMCommandsUtils.__new__(VirtualDesktopSSMCommandsUtils)
    utils.context = Mock()
    utils._logger = Mock()
    utils._ssm_client = Mock()
    utils._ssm_client.send_command.return_value = {'Command': {'CommandId': 'c-1'}}
    utils._ssm_commands_db = Mock()
    utils.submit_ssm_command_to_enable_userdata_execution_on_windows(
        'i-1', 's-1', 'user-a', 'ss-1'
    )
    commands = utils._ssm_client.send_command.call_args.kwargs['Parameters']['commands']
    assert '    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }' in commands
    assert '    exit 1' in commands


def test_vdc_33_unverifiable_the_idle_probe_calls_dcv_directly():
    from ideavirtualdesktopcontroller.app.ssm_commands import (
        virtual_desktop_ssm_commands_utils as module,
    )
    import inspect

    assert 'Invoke-Expression' not in inspect.getsource(module)


# --- R4-03 / VDC-06: group shares ----------------------------------------------------------


def test_r4_03_a_stored_group_row_does_not_admit_a_user_of_the_same_name():
    # a row written before group shares were refused, keyed by the bare group name
    api = connecting_api(projects=[SimpleNamespace(project_id='p-1')])
    api.session_permissions_db = share_db(
        [{**SHARE_ROW, 'actor_name': 'team-a', 'actor_type': 'GROUP'}]
    )
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project=PROJECT_P1
    )
    assert api._may_connect(connection(), 'team-a') is False


GROUP_ROW = {**SHARE_ROW, 'actor_name': 'group:eng', 'actor_type': 'GROUP'}


def members_of(groups):
    """the accounts service: user -> groups, and group -> members"""

    def get_user(request):
        return SimpleNamespace(
            user=SimpleNamespace(
                additional_groups=groups.get(request.username, []) + ['vdi-users']
            )
        )

    def list_users_in_group(request):
        (group,) = request.group_names
        return SimpleNamespace(
            listing=[
                SimpleNamespace(username=user)
                for user, user_groups in groups.items()
                if group in user_groups
            ],
            paginator=None,
        )

    return get_user, list_users_in_group


def group_connecting_api(groups, rows=(GROUP_ROW,)):
    api = connecting_api(projects=[SimpleNamespace(project_id='p-1')])
    api.session_permissions_db = share_db(rows)
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project=PROJECT_P1
    )
    get_user, list_users = members_of(groups)
    api.context.accounts_client.get_user.side_effect = get_user
    api.context.accounts_client.list_users_in_group.side_effect = list_users
    return api


def test_group_share_a_member_connects():
    api = group_connecting_api({'alice': ['eng']})
    assert api._may_connect(connection(), 'alice') is True


def test_group_share_a_non_member_is_refused():
    api = group_connecting_api({'alice': ['eng'], 'bob': ['ops']})
    assert api._may_connect(connection(), 'bob') is False


def test_group_share_a_user_named_like_the_group_is_not_admitted():
    api = group_connecting_api({'alice': ['eng'], 'eng': []})
    assert api._may_connect(connection(), 'eng') is False


def test_group_share_a_removed_member_loses_access_at_the_next_connection():
    groups = {'alice': ['eng']}
    api = group_connecting_api(groups)
    assert api._may_connect(connection(), 'alice') is True
    groups['alice'] = []  # removed from the group in the directory
    assert api._may_connect(connection(), 'alice') is False


def test_group_share_an_expired_one_admits_nobody():
    api = group_connecting_api(
        {'alice': ['eng']}, rows=({**GROUP_ROW, 'expiry_date': 1600000000000},)
    )
    assert api._may_connect(connection(), 'alice') is False


def share_request_api(groups, group_size=None):
    api = user_api()
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project=PROJECT_P1
    )
    api.permission_profile_db = Mock()
    api.context.accounts_client.get_group.return_value = SimpleNamespace(
        group=SimpleNamespace(name='eng', enabled=True)
    )
    get_user, list_users = members_of(groups)
    api.context.accounts_client.get_user.side_effect = get_user
    api.context.accounts_client.list_users_in_group.side_effect = list_users
    api.controller_utils.is_active_directory.return_value = True
    api.session_permissions_utils = Mock()
    api.session_permissions_utils.update_permission_for_sessions.side_effect = (
        lambda request: request
    )
    return api


def share_request(actor_name, actor_type, which='create'):
    from ideadatamodel import UpdateSessionPermissionRequest

    return UpdateSessionPermissionRequest.model_validate(
        {
            which: [
                {
                    **{
                        k: v
                        for k, v in SHARE_ROW.items()
                        if k
                        not in (
                            'actor_name',
                            'permission_profile_id',
                            'expiry_date',
                            'idea_session_created_on',
                        )
                    },
                    'idea_session_created_on': '2026-01-01T00:00:00Z',
                    'actor_name': actor_name,
                    'actor_type': actor_type,
                    'permission_profile': {'profile_id': 'full'},
                    'expiry_date': '2099-01-01T00:00:00Z',
                }
            ]
        }
    )


def test_group_share_created_through_the_api_is_stored_under_its_group_key():
    api = share_request_api({'alice': ['eng'], 'bob': ['eng']})
    context = Mock()
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = share_request('eng', 'GROUP')

    api.update_session_permission(context)

    context.fail.assert_not_called()
    (sent,) = (
        api.session_permissions_utils.update_permission_for_sessions.call_args.args
    )
    assert sent.create[0].actor_name == 'group:eng'
    assert sent.create[0].actor_type == 'GROUP'


def test_group_share_of_a_group_too_large_is_refused():
    from ideavirtualdesktopcontroller.app.session_permissions.constants import (
        MAX_GROUP_SHARE_MEMBERS,
    )

    api = share_request_api(
        {f'user-{n}': ['eng'] for n in range(MAX_GROUP_SHARE_MEMBERS + 1)}
    )
    context = Mock()
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = share_request('eng', 'GROUP')

    api.update_session_permission(context)

    context.fail.assert_called_once()
    api.session_permissions_utils.update_permission_for_sessions.assert_not_called()


@pytest.mark.parametrize(
    'name, actor_type',
    [('group:eng', 'GROUP'), ('eng', 'GROUP'), ('group:eng', 'USER')],
)
def test_group_share_is_deleted_by_its_group_key_however_it_is_named(name, actor_type):
    api = share_request_api({'alice': ['eng']})
    context = Mock()
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = share_request(
        name, actor_type, 'delete'
    )

    api.update_session_permission(context)

    (sent,) = (
        api.session_permissions_utils.update_permission_for_sessions.call_args.args
    )
    assert sent.delete[0].actor_name == 'group:eng'
    assert sent.delete[0].actor_type == 'GROUP'


class IndexSearch:
    """the share index: terms filters on the .raw fields, a sort, from and size, a total"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def search(self, term_filters, sort_filter, start_from, size, **kwargs):
        self.calls += 1
        hits = [
            r
            for r in self.rows
            if all(r.get(f.key[: -len('.raw')]) in f.value for f in term_filters)
        ]
        hits.sort(key=lambda r: (r.get(sort_filter.key) or 0, r['idea_session_id']))
        return {
            'hits': {
                'total': {'value': len(hits)},
                'hits': [{'_source': r} for r in hits[start_from : start_from + size]],
            }
        }


def shared_listing_api(groups, rows):
    from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
        VirtualDesktopSessionPermissionDB as RealDB,
    )

    api = group_connecting_api(groups)
    db = RealDB.__new__(RealDB)
    db._logger = Mock()
    db._term_filter_map = {
        'actor_name': 'actor_name.raw',
        'idea_session_base_os': 'idea_session_base_os.raw',
    }
    db._date_range_filter_map = None
    db._free_text_search_support = False
    db._default_page_size = RealDB.DEFAULT_PAGE_SIZE
    db._os_client = IndexSearch(rows)
    db.get_index_name = lambda: 'shares'
    api.session_permissions_db = db
    return api


def test_group_share_is_listed_for_its_members():
    from ideadatamodel import ListPermissionsRequest

    rows = [GROUP_ROW, {**GROUP_ROW, 'actor_name': 'alice', 'idea_session_id': 's-9'}]
    api = shared_listing_api({'alice': ['eng']}, rows)

    response = api._list_shared_permissions('alice', ListPermissionsRequest())

    # the bare-named group row is nobody's
    assert [p.actor_name for p in response.listing] == ['group:eng']


def test_shared_with_me_pages_user_and_group_shares_as_one_listing():
    from ideadatamodel import ListPermissionsRequest, SocaFilter, SocaPaginator

    rows = (
        [
            {**GROUP_ROW, 'idea_session_id': f'e-{i:02}', 'idea_session_base_os': os}
            for i, os in enumerate(['amazonlinux2023'] * 11 + ['windows2022'])
        ]
        + [
            {**GROUP_ROW, 'actor_name': 'group:ops', 'idea_session_id': f'o-{i}'}
            for i in range(2)
        ]
        + [
            {**SHARE_ROW, 'actor_name': 'alice', 'idea_session_id': f'u-{i}'}
            for i in range(2)
        ]
        # not alice's: another user's share, and a group she is not in
        + [{**SHARE_ROW, 'idea_session_id': 'x-1'}]
        + [{**GROUP_ROW, 'actor_name': 'group:hr', 'idea_session_id': 'x-2'}]
    )
    api = shared_listing_api({'alice': ['eng', 'ops']}, rows)

    def page(start, *filters):
        return api._list_shared_permissions(
            'alice',
            ListPermissionsRequest(
                filters=list(filters),
                paginator=SocaPaginator(start=start, page_size=10),
            ),
        )

    first, second = page(0), page(10)
    ids = [p.idea_session_id for p in first.listing + second.listing]
    assert first.paginator.total == 16 and second.paginator.total == 16
    assert len(first.listing) == 10 and len(second.listing) == 6
    assert sorted(ids) == sorted(
        [f'e-{i:02}' for i in range(12)] + ['o-0', 'o-1', 'u-0', 'u-1']
    )
    # a filter applies to the group shares too, and a caller cannot widen the actor filter
    windows = page(
        0,
        SocaFilter(key='idea_session_base_os', value='windows2022'),
        SocaFilter(key='actor_name', value='user-b'),
    )
    assert [p.idea_session_id for p in windows.listing] == ['e-11']
    assert windows.paginator.total == 1


@pytest.mark.parametrize('base_os', ['amazonlinux2023', 'windows2022'])
def test_group_share_writes_one_line_per_member_in_the_permissions_file(base_os):
    from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
        VirtualDesktopSessionPermissionUtils,
    )

    utils = VirtualDesktopSessionPermissionUtils.__new__(
        VirtualDesktopSessionPermissionUtils
    )
    utils.context = Mock()
    utils.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        'admin-profile' if key.endswith('.admin') else 'owner-profile'
    )
    get_user, list_users = members_of(
        {'alice': ['eng'], 'bob': ['eng'], 'carol': ['ops']}
    )
    utils.context.accounts_client.list_users_in_group.side_effect = list_users
    utils._logger = Mock()
    utils.controller_utils = Mock()
    utils._session_permission_db = share_db(
        [
            GROUP_ROW,
            {**GROUP_ROW, 'actor_name': 'eng'},
        ]  # the second is a legacy bare row
    )
    utils._retrieve_permission_profile_values = lambda profile_id: (['display'], [])

    content = utils.generate_permissions_for_session(
        read_back(state='READY', base_os=base_os), for_broker=False
    )

    lines = content.replace('\r\n', '\n').split('\n')
    assert 'alice allow full-allow' in lines and 'bob allow full-allow' in lines
    assert not any(line.startswith(('carol ', 'eng ', 'group:eng ')) for line in lines)


def test_group_share_desktops_are_refreshed_on_every_tick():
    live = (
        VirtualDesktopSessionPermissionDB.convert_db_dict_to_session_permission_object(
            GROUP_ROW
        )
    )
    handler = scheduled_handler([], [live])

    tick(handler)

    handler.events_utils.publish_enforce_session_permissions_event.assert_called_once_with(
        idea_session_id='s-1', idea_session_owner='user-a'
    )


def rendered_refresh(base_os):
    from pathlib import Path

    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        render_bootstrap_refresh,
    )

    return render_bootstrap_refresh(
        str(Path(__file__).resolve().parents[2] / 'idea-bootstrap'),
        base_os,
        {
            'pbs_server_name': 'scheduler',
            'private_zone': 'marker.internal',
            'bootstrap_refresh': True,
            'ssh_allowed_users': ['user-a'],
        },
    )


@pytest.mark.parametrize(
    'base_os', ['ubuntu2204', 'ubuntu2404', 'rocky9', 'amazonlinux2023']
)
def test_vdc_29_the_whole_refresh_parses_under_dash(base_os):
    # the SSM document runs the script with sh, dash on Ubuntu. it is parsed whole, not run:
    # run, it would change this machine's packages, services and /etc
    import shutil
    import subprocess

    dash = shutil.which('dash')
    if dash is None:
        pytest.skip('dash is not installed')
    subprocess.run([dash, '-n'], input=rendered_refresh(base_os), text=True, check=True)


def test_vdc_29_the_refresh_prelude_and_epilogue_run_under_dash():
    import shutil
    import subprocess

    from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
        BOOTSTRAP_REFRESH_EPILOGUE,
        BOOTSTRAP_REFRESH_PRELUDE,
    )

    dash = shutil.which('dash')
    if dash is None:
        pytest.skip('dash is not installed')
    script = BOOTSTRAP_REFRESH_PRELUDE + 'log_info a\nlog_warning b\nlog_error c\n'
    ok = subprocess.run(
        [dash, '-c', script + BOOTSTRAP_REFRESH_EPILOGUE],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0 and ok.stderr.strip() == 'c'
    failed = subprocess.run(
        [
            dash,
            '-c',
            'IDEA_SSH_ACCESS_FAILED=1\n' + script + BOOTSTRAP_REFRESH_EPILOGUE,
        ],
        capture_output=True,
    )
    assert failed.returncode == 1


def test_group_share_of_a_disabled_group_admits_nobody():
    api = group_connecting_api({'alice': ['eng']})
    api.context.accounts_client.get_group.return_value = SimpleNamespace(
        group=SimpleNamespace(enabled=False)
    )
    assert api._may_connect(connection(), 'alice') is False


def test_group_share_a_new_member_is_written_to_the_host_when_they_connect():
    api = group_connecting_api({'alice': ['eng']})
    assert api._may_connect(connection(), 'alice') is True
    api.context.dcv_broker_client.enforce_session_permissions.assert_called_once()
    assert (
        api.context.dcv_broker_client.enforce_session_permissions.call_args.args[
            0
        ].idea_session_id
        == 's-1'
    )


def test_a_user_share_connection_does_not_rewrite_the_host_file():
    api = connecting_api(projects=[SimpleNamespace(project_id='p-1')])
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project=PROJECT_P1
    )
    assert api._may_connect(connection(), 'user-b') is True
    api.context.dcv_broker_client.enforce_session_permissions.assert_not_called()


# --- re-review S5, S6 and S1 ----------------------------------------------------------------


def test_s6_one_membership_read_serves_a_batch_of_screenshots():
    api = user_api()
    api.context.projects_client.get_user_projects.return_value = [
        SimpleNamespace(project_id='p-1')
    ]
    api.session_db = Mock()
    api.session_db.get_from_db.side_effect = lambda **kwargs: read_back(
        session_id=kwargs['idea_session_id'], state='READY', project=PROJECT_P1
    )
    api._validate_owner = lambda owner, context: ('', True)
    context = Mock()
    context.get_username.return_value = 'user-a'
    for n in range(3):
        api._validate_get_session_screenshot_request(
            VirtualDesktopSessionScreenshot(
                idea_session_id=f's-{n}', idea_session_owner='user-a'
            ),
            context,
        )
    assert api.context.projects_client.get_user_projects.call_count == 1


def test_s6_a_disabled_project_is_named_as_disabled():
    from ideadatamodel import Project
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        PROJECT_DISABLED_MESSAGE,
        owner_project_refusal,
    )

    context = Mock()
    context.config.return_value.get_string.return_value = 'marker-admin'
    context.projects_client.get_user_projects.return_value = []  # disabled ones are left out
    context.projects_client.get_project.return_value = SimpleNamespace(
        project=Project(project_id='p-1', enabled=False)
    )
    assert (
        owner_project_refusal(context, read_back(state='STOPPED', project=PROJECT_P1))
        == PROJECT_DISABLED_MESSAGE
    )


def test_s6_an_administrator_starting_a_held_desktop_reads_an_administrators_message():
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        ADMIN_OWNER_LEFT_PROJECT_MESSAGE,
    )

    utils = resuming_utils(read_back(state='STOPPED', project=PROJECT_P1), member_of=[])
    _, failed = utils.resume_sessions([read_back(state='STOPPED')], by_admin=True)
    assert failed[0].failure_reason == ADMIN_OWNER_LEFT_PROJECT_MESSAGE


def test_s5_a_group_share_sends_no_share_email():
    from ideavirtualdesktopcontroller.app.events.handlers.db_entry_event_handlers.db_entry_created_event_handler import (
        DbEntryCreatedEventHandler,
    )

    handler = DbEntryCreatedEventHandler.__new__(DbEntryCreatedEventHandler)
    handler.context = Mock()
    handler.context.config.return_value.get_bool.return_value = True
    handler._logger = Mock()
    handler.session_permissions_db = share_db()
    handler.session_permission_utils = Mock()
    name = next(
        n for n in dir(handler) if n.startswith('_handle_session_permission_created')
    )
    getattr(handler, name)('', '', GROUP_ROW, '')
    handler.context.notification_async_client.send_notification.assert_not_called()


def test_s1_the_move_is_registered_for_elevated_users_only():
    from ideavirtualdesktopcontroller.app.api import virtual_desktop_admin_api as module

    # the server's namespace table (test_api_namespaces.py) must carry it with no scope
    source = __import__('inspect').getsource(module.VirtualDesktopAdminAPI.__init__)
    assert (
        "'VirtualDesktopAdmin.MoveSessionToProject': self.move_session_to_project"
        in source
    )
    assert 'VirtualDesktopAdmin.MoveSessionToProject' not in str(
        getattr(module, 'APP_TOKEN_NAMESPACE_ACCESS', {})
    )


def test_every_bootstrap_status_a_desktop_host_can_write_has_a_message():
    import re
    from pathlib import Path

    from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
        BOOTSTRAP_STATUS_MESSAGES,
    )

    source = Path(__file__).resolve().parents[2]
    bootstrap = source / 'idea-bootstrap'
    # the files a Linux desktop runs, and the user data Linux and Windows desktops start with
    templates = [
        *(bootstrap / '_templates' / 'linux').glob('*.jinja2'),
        *(bootstrap / 'common').glob('*.sh'),
        *(bootstrap / 'virtual-desktop-host-linux').glob('*'),
    ]
    written = set()
    for path in templates:
        written |= set(
            re.findall(
                r'\b(?:fail_[a-z_]+|efs_utils_unavailable)\s+"?([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"?',
                path.read_text(),
            )
        )
    userdata = (
        source
        / 'idea-sdk'
        / 'src'
        / 'ideasdk'
        / 'bootstrap'
        / 'bootstrap_userdata_builder.py'
    ).read_text()
    # the user data tags "<step>-failed" for each fail_userdata <step>
    written |= {
        f'{step}-failed'
        for step in re.findall(r'fail_userdata ([a-z][a-z-]*)', userdata)
    }
    # and the Windows user data names its status directly
    written |= set(re.findall(r"'idea:BootstrapStatus', '([a-z][a-z-]+)'", userdata))
    assert {'aws-cli-failed', 'bootstrap-download-failed'} <= written, (
        'the scan no longer sees the user data statuses'
    )
    # these arrive with the bootstrap-linux area's site hook, download and EFS checks
    arriving = {
        'site-hook-mismatch',
        'download-failed',
        'download-unverified',
        'efs-utils-build-failed',
    }
    assert sorted((written | arriving) - set(BOOTSTRAP_STATUS_MESSAGES)) == []
    assert (
        'dcv_host_site_hook_sha256'
        not in BOOTSTRAP_STATUS_MESSAGES['site-hook-mismatch']
    )


@pytest.mark.parametrize(
    'env, outcome',
    [({'CI': 'true'}, 'fail'), ({'IDEA_REQUIRE_PWSH': '1'}, 'fail'), ({}, 'skip')],
)
def test_a_missing_pwsh_fails_where_it_is_required_and_skips_elsewhere(
    monkeypatch, env, outcome
):
    import shutil

    monkeypatch.setattr(shutil, 'which', lambda name: None)
    for key in ('CI', 'IDEA_REQUIRE_PWSH'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    expected = pytest.fail.Exception if outcome == 'fail' else pytest.skip.Exception
    with pytest.raises(expected):
        required_pwsh()


def test_s2_the_site_hook_hint_names_the_key_under_the_real_module_id():
    from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
        bootstrap_status_admin_hint,
    )

    context = Mock()
    context.module_id.return_value = 'vdc'
    hint = bootstrap_status_admin_hint(context, 'site-hook-mismatch')
    assert 'vdc.dcv_host_site_hook_sha256' in hint
    assert 'virtual-desktop-controller.dcv_host_site_hook_sha256' not in hint
    assert bootstrap_status_admin_hint(context, 'reboot-failed') is None


# --- post-merge D1: the group check goes through the real accounts client ------------------


class AccountsServer:
    """
    the cluster manager as the accounts client reaches it: keyed by the namespaces it serves
    (accounts_api.py), refusing any other as the server does
    """

    def __init__(self, groups, users, disabled=()):
        self.groups = groups
        self.users = users
        self.disabled = set(disabled)
        self.namespaces = []

    def invoke_alt(self, namespace, payload, result_as, access_token=None):
        from ideadatamodel import errorcodes
        from ideadatamodel.exceptions import SocaException as Error

        self.namespaces.append(namespace)
        if namespace == 'Accounts.GetGroup':
            if payload.group_name not in self.groups:
                raise Error(
                    error_code=errorcodes.AUTH_GROUP_NOT_FOUND, message='not found'
                )
            return result_as(
                group={
                    'name': payload.group_name,
                    'enabled': payload.group_name not in self.disabled,
                }
            )
        if namespace == 'Accounts.GetUser':
            return result_as(
                user={
                    'username': payload.username,
                    'additional_groups': self.users.get(payload.username, [])
                    + ['vdi-users'],
                }
            )
        if namespace == 'Accounts.ListUsersInGroup':
            (group,) = payload.group_names
            return result_as(
                listing=[{'username': u} for u, g in self.users.items() if group in g]
            )
        raise Error(
            error_code='UNAUTHORIZED_ACCESS', message=f'{namespace} is not served'
        )


def real_accounts_client(server):
    from ideasdk.client.accounts_client import AccountsClient

    client = AccountsClient.__new__(AccountsClient)
    client.context = Mock()
    client.logger = Mock()
    client.client = server
    client.token_service = None
    client.get_access_token = lambda: 'marker-token'
    return client


def test_d1_a_group_member_is_admitted_through_the_real_accounts_client():
    server = AccountsServer(groups={'eng'}, users={'alice': ['eng']})
    api = group_connecting_api({})
    api.context.accounts_client = real_accounts_client(server)
    assert api._may_connect(connection(), 'alice') is True
    assert 'Accounts.GetGroup' in server.namespaces


def test_d1_an_unreadable_group_is_refused_with_its_own_reason():
    from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
        GROUP_UNREADABLE_MESSAGE,
    )

    server = AccountsServer(groups={'eng'}, users={'alice': ['eng']})
    failing = Mock(side_effect=ConnectionError('marker transport failure'))
    client = real_accounts_client(server)
    client.get_group = failing
    api = group_connecting_api({})
    api.context.accounts_client = client
    request = connection()
    assert api._may_connect(request, 'alice') is False
    assert request.failure_reason == GROUP_UNREADABLE_MESSAGE


def test_d1_creating_a_share_tells_a_missing_group_from_an_unreadable_one():
    api = share_request_api({})
    api.context.accounts_client = real_accounts_client(
        AccountsServer(groups=set(), users={})
    )
    assert 'was not found' in api._group_share_refusal('eng')
    client = real_accounts_client(AccountsServer(groups={'eng'}, users={}))
    client.get_group = Mock(side_effect=TimeoutError('marker timeout'))
    api.context.accounts_client = client
    assert 'could not be checked' in api._group_share_refusal('eng')


# --- sign-off B1: a share never writes a line for the desktop's owner ----------------------


def rendered_permissions(rows, profiles, groups):
    from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
        VirtualDesktopSessionPermissionUtils,
    )

    utils = VirtualDesktopSessionPermissionUtils.__new__(
        VirtualDesktopSessionPermissionUtils
    )
    utils.context = Mock()
    utils.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        'admin-profile' if key.endswith('.admin') else 'owner-profile'
    )
    get_user, list_users = members_of(groups)
    utils.context.accounts_client.list_users_in_group.side_effect = list_users
    utils._logger = Mock()
    utils.controller_utils = Mock()
    utils._session_permission_db = share_db(rows)
    utils._retrieve_permission_profile_values = lambda profile_id: profiles[profile_id]
    content = utils.generate_permissions_for_session(
        read_back(state='READY'), for_broker=False
    )
    return content.replace('\r\n', '\n').split('\n')


SHARE_PROFILES = {
    'view': (['display'], ['keyboard', 'mouse']),
    'collaborate': (
        ['display', 'keyboard', 'mouse'],
        ['clipboard-copy', 'file-download'],
    ),
}


@pytest.mark.parametrize('profile_id', sorted(SHARE_PROFILES))
def test_an_owner_in_a_shared_group_keeps_the_owner_permissions(profile_id):
    profiles = {
        **SHARE_PROFILES,
        'admin-profile': (['builtin'], []),
        'owner-profile': (['builtin'], []),
    }
    lines = rendered_permissions(
        [{**GROUP_ROW, 'permission_profile_id': profile_id}],
        profiles,
        {'user-a': ['eng'], 'alice': ['eng']},  # user-a owns the desktop
    )
    assert not any(line.startswith('user-a ') for line in lines)
    assert '%owner% allow owner-profile-allow' in lines
    # a member who is not the owner gets the share's profile
    assert f'alice allow {profile_id}-allow' in lines
    assert f'alice deny {profile_id}-deny' in lines


def test_a_direct_share_naming_the_owner_writes_nothing_for_them():
    profiles = {
        **SHARE_PROFILES,
        'admin-profile': (['builtin'], []),
        'owner-profile': (['builtin'], []),
    }
    lines = rendered_permissions(
        [{**SHARE_ROW, 'actor_name': 'user-a', 'permission_profile_id': 'view'}],
        profiles,
        {},
    )
    assert not any(line.startswith('user-a ') for line in lines)


# --- sign-off B1: the move decides the role from what the host carries ----------------------


def test_a_move_between_projects_that_resolve_to_shared_takes_off_a_third_role(
    monkeypatch,
):
    # the host still carries project one's role, though neither project resolves to one now
    ec2 = PolicyEc2(profile_arn='arn:profile-p1')
    api, context, table = mover(monkeypatch, ec2, profiles={})

    api.move_session_to_project(context)

    context.success.assert_called_once()
    assert ec2.association['IamInstanceProfile']['Arn'] == 'arn:shared'
    # the host is stopped: its role is taken off and the new one associated
    assert 'replace_iam_instance_profile_association' not in ec2.calls
    assert ec2.calls.count('disassociate_iam_instance_profile') == 1
    assert table.rows[('user-a', 's-1')]['server']['instance_profile_arn'] == (
        'arn:shared'
    )
    assert api.server_db.update.call_args.args[0].instance_profile_arn == 'arn:shared'


def test_a_move_whose_new_role_is_refused_on_the_stopped_host_changes_nothing(
    monkeypatch,
):
    from botocore.exceptions import ClientError

    refused = ClientError(
        {'Error': {'Code': 'IncorrectInstanceState', 'Message': 'marker'}},
        'AssociateIamInstanceProfile',
    )
    ec2 = PolicyEc2(fail_at={'associate_iam_instance_profile': refused})
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)

    api.move_session_to_project(context)

    assert 'nothing was changed' in context.fail.call_args.kwargs['message']
    assert ec2.association['IamInstanceProfile']['Arn'] == 'arn:profile-p1'
    assert_unmoved(ec2, table, before)


def test_a_move_whose_row_lands_but_its_event_fails_is_put_back(monkeypatch):
    from botocore.exceptions import ClientError

    ec2 = PolicyEc2()
    before = {r: dict(t) for r, t in ec2.tags.items()}
    api, context, table = mover(monkeypatch, ec2)
    failures = [ClientError({'Error': {'Code': 'ThrottlingException'}}, 'SendMessage')]

    def event(*args, **kwargs):
        if failures:
            raise failures.pop()

    api.session_db.trigger_update_event.side_effect = event

    api.move_session_to_project(context)

    assert 'nothing was changed' in context.fail.call_args.kwargs['message']
    assert_unmoved(ec2, table, before)


# --- sign-off B5: the cleanup warning writes only its own fields ------------------------------


def test_a_cleanup_warning_written_after_a_move_keeps_the_new_project(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    ec2 = PolicyEc2()
    api, context, table = mover(monkeypatch, ec2)
    # the cleanup pass listed the desktop before the move
    listed = api.session_db.get_from_db(
        idea_session_owner='user-a', idea_session_id='s-1'
    )

    api.move_session_to_project(context)
    context.success.assert_called_once()

    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils.context.config.return_value.get_bool.return_value = False
    utils._logger = Mock()
    utils._session_db = api.session_db
    stopped_at = datetime.now(timezone.utc) - timedelta(days=24)
    outcome = utils._warn_owner(
        listed, stopped_at, timedelta(days=24), stopped_at + timedelta(days=30), False
    )

    assert outcome == 'warned'
    row = table.rows[('user-a', 's-1')]
    assert stored_project(table) == 'p-2'
    assert row['server']['instance_profile_arn'] == 'arn:profile-p2'
    assert row['cleanup_warning_stop_time'] is not None


def test_a_cleanup_warning_for_a_desktop_resumed_meanwhile_is_not_written(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
        VirtualDesktopSessionUtils,
    )

    api, context, table = mover(monkeypatch, PolicyEc2())
    listed = api.session_db.get_from_db(
        idea_session_owner='user-a', idea_session_id='s-1'
    )
    table.rows[('user-a', 's-1')]['state'] = 'RESUMING'

    utils = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils.context.config.return_value.get_bool.return_value = False
    utils._logger = Mock()
    utils._session_db = api.session_db
    stopped_at = datetime.now(timezone.utc) - timedelta(days=24)
    outcome = utils._warn_owner(
        listed, stopped_at, timedelta(days=24), stopped_at + timedelta(days=30), False
    )

    assert outcome == 'raced'
    row = table.rows[('user-a', 's-1')]
    assert row['state'] == 'RESUMING'
    assert row.get('cleanup_warning_stop_time') is None


# --- signoff desktops 5: a share table read that fails is not taken as the whole table ------


def test_a_share_table_page_that_cannot_be_read_raises():
    db = share_db([SHARE_ROW])

    def scan(**kwargs):
        raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceeded'}}, 'Scan')

    db._table_obj.scan = scan
    with pytest.raises(ClientError):
        db.list_all_from_db(None)


def test_a_share_table_read_that_fails_does_not_stop_the_rest_of_the_tick():
    handler = scheduled_handler([], [])
    handler.session_permissions_db.list_all_from_db.side_effect = ClientError(
        {'Error': {'Code': 'ProvisionedThroughputExceeded'}}, 'Scan'
    )

    tick(handler)

    handler.session_utils.clean_up_stopped_sessions.assert_called_once()


@pytest.mark.parametrize('embedded', [None, 's-other'])
def test_a_desktops_schedule_delete_is_held_to_that_desktop_whatever_its_reference_says(
    embedded,
):
    from ideadatamodel import VirtualDesktopSession, VirtualDesktopWeekSchedule

    table, db = schedule_table_with_another_desktops_row()
    reference = stored_schedule(
        DayOfWeek.MONDAY,
        VirtualDesktopScheduleType.START_ALL_DAY,
        schedule_id='sch-other',
    )
    reference.idea_session_id = embedded
    utils = schedule_utils()
    utils._schedule_db = db
    session = VirtualDesktopSession(
        idea_session_id='s-1',
        owner='user-a',
        schedule=VirtualDesktopWeekSchedule(monday=reference),
    )

    utils.delete_schedules_for_session(session)

    assert ('monday', 'sch-other') in table.rows


# --- signoff desktops 8: a resume result lands only on a desktop still resuming --------------


@pytest.mark.parametrize('status', ['Success', 'Failed'])
def test_a_resume_result_after_a_delete_since_the_read_changes_nothing(status):
    from stored_session import FakeTable, session_table_reader, stored_row

    db = session_table_reader()
    db._table_obj = FakeTable(
        'owner', 'idea_session_id', [stored_row(state='RESUMING')]
    )
    db.trigger_update_event = Mock()
    resuming = db.get_from_db(idea_session_owner='user-a', idea_session_id='s-1')
    # the desktop is deleted between the handler's read and its write
    db._table_obj.rows[('user-a', 's-1')]['state'] = 'DELETING'
    db.get_from_db = lambda **kwargs: resuming
    handler = resume_progress(resuming, status, session_db=db)

    assert db._table_obj.rows[('user-a', 's-1')]['state'] == 'DELETING'
    handler.events_utils.publish_validate_dcv_session_creation_event.assert_not_called()


# --- signoff desktops 11: a missing sessions table is not "every session gone" -------------


def test_orphan_schedule_cleanup_stops_when_the_sessions_table_is_missing(monkeypatch):
    import boto3
    from click.testing import CliRunner
    from ideasdk.utils import EnvironmentUtils
    from ideavirtualdesktopcontroller.cli.sessions import cleanup_orphaned_schedules

    schedules = Mock()
    schedules.query.return_value = {
        'Items': [
            {
                'day_of_week': 'monday',
                'schedule_id': 'sch-1',
                'idea_session_id': 's-1',
                'idea_session_owner': 'user-a',
            }
        ]
    }
    sessions = Mock()
    sessions.get_item.side_effect = ClientError(
        {'Error': {'Code': 'ResourceNotFoundException'}}, 'GetItem'
    )
    resource = Mock()
    resource.Table.side_effect = lambda name: (
        schedules if name.endswith('.schedules') else sessions
    )
    monkeypatch.setattr(boto3, 'resource', lambda *args, **kwargs: resource)
    for name in ('aws_default_region', 'idea_cluster_name', 'idea_module_id'):
        monkeypatch.setattr(EnvironmentUtils, name, lambda **kwargs: 'x')

    result = CliRunner().invoke(cleanup_orphaned_schedules, ['--force'])

    assert 'does not exist' in result.output
    schedules.delete_item.assert_not_called()


# --- signoff2 B1: a write after a delete creates nothing; one bad row stops no sweep ------


def test_a_session_write_landing_after_the_row_was_deleted_creates_nothing():
    from ideadatamodel import VirtualDesktopSessionState
    from stored_session import FakeTable, session_table_reader, stored_row

    db = session_table_reader()
    db._table_obj = FakeTable(
        'owner', 'idea_session_id', [stored_row(state='CREATING')]
    )
    db.trigger_update_event = Mock()
    session = db.get_from_db(idea_session_owner='user-a', idea_session_id='s-1')
    db._table_obj.rows.clear()  # the desktop is deleted meanwhile

    session.state = VirtualDesktopSessionState.INITIALIZING
    db.update(session)

    assert db._table_obj.rows == {}
    db.trigger_update_event.assert_not_called()


def test_a_partial_session_row_does_not_stop_a_sweep_of_its_page():
    from ideadatamodel import ListSessionsRequest
    from stored_session import FakeTable, session_table_reader, stored_row

    db = session_table_reader()
    db._table_obj = FakeTable(
        'owner',
        'idea_session_id',
        [
            {'owner': 'user-a', 'idea_session_id': 's-0', 'state': 'READY'},
            stored_row(state='READY'),
        ],
    )

    listed = db.list_all_from_db(ListSessionsRequest())
    assert [s.idea_session_id for s in listed.listing] == ['s-1']
    assert [s.idea_session_id for s in db.list_for_owner('user-a')] == ['s-1']


# --- signoff2 4: a resume whose validation is not queued is handled again ------------------


def test_a_resume_whose_validation_cannot_be_queued_is_handled_again_on_redelivery():
    from stored_session import FakeTable, session_table_reader, stored_row

    db = session_table_reader()
    db._table_obj = FakeTable(
        'owner', 'idea_session_id', [stored_row(state='RESUMING')]
    )
    db.trigger_update_event = Mock()
    db._history_db = Mock()
    handler = resume_progress(None, 'InProgress', session_db=db)
    publish = handler.events_utils.publish_validate_dcv_session_creation_event
    publish.side_effect = [
        ClientError({'Error': {'Code': 'Throttling'}}, 'SendMessage'),
        None,
    ]
    event = VirtualDesktopEvent(
        event_group_id='s-1',
        event_type=VirtualDesktopEventType.IDEA_SESSION_RESUME_SESSION_COMMAND_PROGRESS_EVENT,
        detail={
            'status': 'Success',
            'idea_session_id': 's-1',
            'idea_session_owner': 'user-a',
            'command_id': 'c-1',
        },
    )

    with pytest.raises(ClientError):
        handler.handle_event('m-1', 'controller', event)
    assert db._table_obj.rows[('user-a', 's-1')]['state'] == 'RESUMING'

    handler.handle_event('m-1', 'controller', event)
    assert publish.call_count == 2
    assert db._table_obj.rows[('user-a', 's-1')]['state'] == 'INITIALIZING'
