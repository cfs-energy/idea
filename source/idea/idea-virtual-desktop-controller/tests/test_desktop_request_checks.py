"""
Desktop names, share entries, host events and share removal are checked where every caller
passes through, and the commands that recreate a desktop session pass every value as a literal.
"""

import base64
import shlex
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    CreateSessionRequest,
    ListSoftwareStackRequest,
    UpdateSessionPermissionRequest,
    UpdateSessionRequest,
    VirtualDesktopBaseOS,
    VirtualDesktopPermissionProfile,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionPermission,
    VirtualDesktopSessionState,
    VirtualDesktopSessionType,
    VirtualDesktopSoftwareStack,
    errorcodes,
)
from ideadatamodel.exceptions import SocaException
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client_utils import (
    DCVBrokerClientUtils,
)
from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_reboot_complete_event_handler import (
    DCVHostRebootCompleteEventHandler,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
    VirtualDesktopSessionPermissionUtils,
)
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
    VirtualDesktopEventType,
)
from ideavirtualdesktopcontroller.app.sessions.constants import SESSION_NAME_RULE
from ideavirtualdesktopcontroller.app.sessions.image_validation import (
    VALIDATION_NAME_PREFIX,
)

# a name the old forms allowed and the new rule refuses: it carries shell characters
LEGACY_NAME = 'desk"; $(x)'


def invocation(payload, username='user-a'):
    context = Mock()
    context.get_username.return_value = username
    context.get_request_payload_as.return_value = payload
    return context


def user_api():
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api.context.config.return_value.get_int.return_value = 10
    api._logger = Mock()
    api.session_db = Mock()
    api.session_db.get_session_count_for_user.return_value = 0
    api.server_db = Mock()
    api.controller_utils = Mock()
    api.schedule_utils = Mock()
    api.session_utils = Mock()
    api.session_permissions_utils = Mock()
    # every share names a profile that exists (an unknown one is refused, test_panel_findings)
    api.permission_profile_db = Mock()
    return api


# --- desktop names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'name',
    [
        'MyDesktop1',
        'build box 2',
        'team_a.desk-01',
        f'{VALIDATION_NAME_PREFIX}amazonlinux2023-x86_64-gpu-6789abcd',
        'a' * 64,
    ],
)
def test_names_the_product_uses_are_accepted(name):
    assert VirtualDesktopAPI.validate_session_name(name) is None


@pytest.mark.parametrize('name', [LEGACY_NAME, 'a' * 65, 'tab\there', 'line\n'])
def test_names_outside_the_rule_are_refused(name):
    assert VirtualDesktopAPI.validate_session_name(name) == SESSION_NAME_RULE


def test_create_refuses_a_name_outside_the_rule_before_anything_is_launched():
    api = user_api()
    context = invocation(
        CreateSessionRequest(session=VirtualDesktopSession(name=LEGACY_NAME))
    )

    api.create_session(context)

    context.fail.assert_called_once()
    assert context.fail.call_args.kwargs['error_code'] == errorcodes.INVALID_PARAMS
    assert context.fail.call_args.kwargs['message'] == SESSION_NAME_RULE
    api.session_utils.create_session.assert_not_called()


def stored_session(name):
    return VirtualDesktopSession(
        idea_session_id='s-1',
        owner='user-a',
        name=name,
        description='old',
        server=VirtualDesktopServer(instance_id='i-1', instance_type='m5.large'),
    )


def test_rename_to_a_name_outside_the_rule_is_refused():
    api = user_api()
    api.session_db.get_from_db.return_value = stored_session('MyDesktop1')
    context = invocation(
        UpdateSessionRequest(
            session=VirtualDesktopSession(idea_session_id='s-1', name=LEGACY_NAME)
        )
    )

    api.update_session(context)

    assert context.fail.call_args.kwargs['message'] == SESSION_NAME_RULE
    api.session_db.update.assert_not_called()


def test_a_desktop_with_an_older_name_can_still_be_edited_under_that_name():
    api = user_api()
    api.session_db.get_from_db.return_value = stored_session(LEGACY_NAME)
    api.session_db.update.side_effect = lambda session, **kwargs: session
    context = invocation(
        UpdateSessionRequest(
            session=VirtualDesktopSession(
                idea_session_id='s-1', name=LEGACY_NAME, description='new'
            )
        )
    )

    api.update_session(context)

    context.fail.assert_not_called()
    assert api.session_db.update.call_args.args[0].description == 'new'


# --- commands that recreate the DCV session on resume -----------------------------------------

PERMISSIONS = '[groups]\nuser-b allow full-allow\n'


def resume_commands(base_os, name=LEGACY_NAME, owner='user-a'):
    permission_utils = Mock()
    permission_utils.generate_permissions_for_session.return_value = PERMISSIONS
    utils = DCVBrokerClientUtils.__new__(DCVBrokerClientUtils)
    utils._session_permission_utils = permission_utils
    return utils.get_commands_to_execute_for_resuming_session(
        VirtualDesktopSession(
            name=name,
            owner=owner,
            dcv_session_id='d-1',
            type=VirtualDesktopSessionType.VIRTUAL,
            software_stack=VirtualDesktopSoftwareStack(base_os=base_os),
        )
    )


def test_linux_resume_passes_each_value_as_one_word_and_writes_the_file_from_base64():
    commands = resume_commands(VirtualDesktopBaseOS.AMAZON_LINUX2023)

    write = shlex.split(commands[1])
    assert write[:2] == ['echo', write[1]]
    assert base64.b64decode(write[1]).decode() == PERMISSIONS
    assert write[2:] == ['|', 'base64', '-d', '>', '/etc/dcv/d-1/idea.perm']

    argv = shlex.split(commands[-1])
    assert argv[argv.index('--name') + 1] == 'desk__ __x_'
    assert argv[argv.index('--user') + 1] == 'user-a'
    assert argv[-1] == 'd-1'
    for command in commands:
        assert '$(' not in command and '"' not in command


def test_windows_resume_passes_values_as_single_quoted_literals():
    commands = resume_commands(VirtualDesktopBaseOS.WINDOWS2022, owner="o'neil")

    dcv = next(c for c in commands if 'create-session' in c)
    assert 'cmd.exe' not in dcv
    assert "--name 'desk__ __x_'" in dcv
    assert "--user 'o''neil'" in dcv
    assert DCVBrokerClientUtils._powershell_literal('a\u2019b') == "'a\u2019\u2019b'"
    write = next(c for c in commands if 'FromBase64String' in c)
    encoded = base64.b64encode(PERMISSIONS.encode()).decode()
    assert f"FromBase64String('{encoded}')" in write
    for command in commands:
        assert '"' not in command and '`' not in command


def test_share_entries_stored_before_names_were_checked_are_left_out_of_the_file():
    profile = VirtualDesktopPermissionProfile(profile_id='full')
    good = VirtualDesktopSessionPermission(
        actor_name='user-b', actor_type='USER', permission_profile=profile
    )
    bad = VirtualDesktopSessionPermission(
        actor_name='user-c\nuser-d', actor_type='USER', permission_profile=profile
    )
    utils = VirtualDesktopSessionPermissionUtils.__new__(
        VirtualDesktopSessionPermissionUtils
    )
    utils.context = Mock()
    utils.context.config.return_value.get_string.return_value = 'full'
    utils._logger = Mock()
    utils.controller_utils = Mock()
    utils._session_permission_db = Mock()
    utils._session_permission_db.get_for_session.return_value = [good, bad]
    utils._retrieve_permission_profile_values = lambda profile_id: (['display'], [])

    content = utils.generate_permissions_for_session(
        VirtualDesktopSession(
            idea_session_id='s-1',
            software_stack=VirtualDesktopSoftwareStack(
                base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023
            ),
        ),
        for_broker=False,
    )

    assert 'user-b allow full-allow' in content
    assert 'user-c' not in content and 'user-d' not in content


# --- host events -------------------------------------------------------------------------------


def reboot_handler(session):
    handler = DCVHostRebootCompleteEventHandler.__new__(
        DCVHostRebootCompleteEventHandler
    )
    handler.context = Mock()
    handler.context.config.return_value.get_string.return_value = 'AROACONTROLLER'
    handler._logger = Mock()
    handler.session_db = Mock()
    handler.session_db.get_from_db.return_value = session
    return handler


def reboot_event():
    return VirtualDesktopEvent(
        event_group_id='s-1',
        event_type=VirtualDesktopEventType.DCV_HOST_REBOOT_COMPLETE_EVENT,
        detail={'idea_session_id': 's-1', 'idea_session_owner': 'user-a'},
    )


def owned_session():
    # a state the handler does not act on, so an accepted event stops right after the check
    session = stored_session('MyDesktop1')
    session.state = VirtualDesktopSessionState.PROVISIONING
    return session


def test_a_host_cannot_report_a_reboot_for_another_desktop():
    handler = reboot_handler(owned_session())
    with pytest.raises(SocaException) as raised:
        handler.handle_event('m-1', 'AROAHOST:i-2', reboot_event())
    assert raised.value.error_code == errorcodes.MESSAGE_SOURCE_VALIDATION_FAILED


@pytest.mark.parametrize('sender', ['AROAHOST:i-1', 'AROACONTROLLER:task-1'])
def test_the_desktops_own_host_and_the_controller_are_accepted(sender):
    handler = reboot_handler(owned_session())
    handler.handle_event('m-1', sender, reboot_event())


# --- removing a share entry ----------------------------------------------------------------------


def delete_request(session_id):
    return UpdateSessionPermissionRequest(
        create=[],
        update=[],
        delete=[
            VirtualDesktopSessionPermission(
                idea_session_id=session_id,
                idea_session_owner='user-a',
                actor_name='user-b',
            )
        ],
    )


def test_removing_a_share_entry_needs_the_caller_to_own_that_desktop():
    api = user_api()
    api.session_db.get_from_db.return_value = None  # s-other is not user-a's desktop
    context = invocation(delete_request('s-other'))

    api.update_session_permission(context)

    context.fail.assert_called_once()
    api.session_permissions_utils.update_permission_for_sessions.assert_not_called()


def test_the_owner_can_remove_a_share_entry():
    api = user_api()
    api.session_db.get_from_db.return_value = stored_session('MyDesktop1')
    context = invocation(delete_request('s-1'))

    api.update_session_permission(context)

    context.fail.assert_not_called()
    api.session_permissions_utils.update_permission_for_sessions.assert_called_once()


@pytest.mark.parametrize('actor', ['user-b\nuser-c allow', 'a b', '"x"'])
def test_share_entries_must_name_a_plain_user_or_group(actor):
    assert VirtualDesktopAPI.validate_actor_name(actor) is not None


# --- what a share says about the desktop ---------------------------------------------------------


def test_a_share_describes_the_stored_desktop_not_the_request():
    api = user_api()
    stored = stored_session('MyDesktop1')
    stored.base_os = VirtualDesktopBaseOS.AMAZON_LINUX2023
    stored.type = VirtualDesktopSessionType.VIRTUAL
    stored.state = VirtualDesktopSessionState.READY
    api.session_db.get_from_db.return_value = stored
    share = VirtualDesktopSessionPermission(
        idea_session_id='s-1',
        idea_session_owner='user-a',
        idea_session_name='Something else entirely',
        idea_session_instance_type='p5.48xlarge',
        idea_session_state=VirtualDesktopSessionState.READY,
        idea_session_base_os=VirtualDesktopBaseOS.WINDOWS2022,
        idea_session_created_on='2026-01-01T00:00:00Z',
        idea_session_type=VirtualDesktopSessionType.CONSOLE,
        permission_profile=VirtualDesktopPermissionProfile(profile_id='view'),
        actor_type='USER',
        actor_name='user-b',
        expiry_date='2027-01-01T00:00:00Z',
    )
    api._validate_user_and_project_membership = lambda username, session_project_id: (
        True,
        '',
    )
    context = invocation(
        UpdateSessionPermissionRequest(create=[share], update=[], delete=[])
    )

    api.update_session_permission(context)

    context.fail.assert_not_called()
    sent = api.session_permissions_utils.update_permission_for_sessions.call_args.args[
        0
    ].create[0]
    assert sent.idea_session_name == 'MyDesktop1'
    assert sent.idea_session_instance_type == 'm5.large'
    assert sent.idea_session_base_os == VirtualDesktopBaseOS.AMAZON_LINUX2023
    assert sent.idea_session_type == VirtualDesktopSessionType.VIRTUAL


# --- software stacks offered for a project -------------------------------------------------------


def stacks_api(member_of):
    api = user_api()
    api.context.projects_client.get_user_projects.return_value = [
        Mock(project_id=project_id) for project_id in member_of
    ]
    api._list_software_stacks = Mock(return_value=Mock(listing=[], paginator=None))
    return api


def test_stacks_of_a_project_the_caller_is_not_in_are_not_listed():
    api = stacks_api(member_of=['p-mine'])
    context = invocation(ListSoftwareStackRequest(project_id='p-other'))
    context.is_administrator.return_value = False
    context.is_manager.return_value = False

    api.list_software_stacks(context)

    assert context.fail.call_args.kwargs['error_code'] == errorcodes.INVALID_PARAMS
    api._list_software_stacks.assert_not_called()


def test_stacks_of_the_callers_own_project_are_listed():
    api = stacks_api(member_of=['p-mine'])
    context = invocation(ListSoftwareStackRequest(project_id='p-mine'))
    context.is_administrator.return_value = False
    context.is_manager.return_value = False

    api.list_software_stacks(context)

    context.fail.assert_not_called()
    context.success.assert_called_once()


def test_an_administrator_can_list_stacks_for_the_person_they_create_a_desktop_for():
    api = stacks_api(member_of=[])
    context = invocation(ListSoftwareStackRequest(project_id='p-theirs'))
    context.is_administrator.return_value = True

    api.list_software_stacks(context)

    context.fail.assert_not_called()
