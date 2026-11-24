"""
Share expiry, the broker session id, the host a broker session runs on, the per-user desktop
limit, the rendered bootstrap package on shared storage and the terminate-sessions reads.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from ideadatamodel import (
    CreateSessionRequest,
    VirtualDesktopBaseOS,
    VirtualDesktopPermissionProfile,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionConnectionInfo,
    VirtualDesktopSessionPermission,
    VirtualDesktopSessionState,
    VirtualDesktopSessionType,
    VirtualDesktopSoftwareStack,
)
from ideadatamodel.exceptions import SocaException
from ideavirtualdesktopcontroller.app.api import (
    virtual_desktop_user_api as user_api_module,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client import (
    DCVBrokerClient,
)
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client_utils import (
    DCVBrokerClientUtils,
)
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
    VirtualDesktopEventType,
)
from ideavirtualdesktopcontroller.app.events.handlers.validate_dcv_session_event_handlers.validate_dcv_session_creation_event_handler import (
    ValidateDCVSessionCreationEventHandler,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
    VirtualDesktopSessionPermissionUtils,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)
from ideavirtualdesktopcontroller.cli import sessions as sessions_cli
from stored_session import read_back

PAST = datetime.now(timezone.utc) - timedelta(hours=1)
FUTURE = datetime.now(timezone.utc) + timedelta(hours=1)
PROFILE = VirtualDesktopPermissionProfile(profile_id='full')


# --- share expiry ---------------------------------------------------------------------------


def share(actor, expiry_date):
    return VirtualDesktopSessionPermission(
        idea_session_id='s-1',
        idea_session_owner='user-a',
        actor_name=actor,
        actor_type='USER',
        permission_profile=PROFILE,
        expiry_date=expiry_date,
    )


@pytest.mark.parametrize('expiry_date, allowed', [(PAST, False), (FUTURE, True)])
def test_connection_details_need_a_share_that_has_not_expired(expiry_date, allowed):
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api.context.projects_client.get_user_projects.return_value = [
        SimpleNamespace(project_id='p-1')
    ]
    api.session_permissions_db = Mock()
    api.session_permissions_db.get.return_value = share('user-b', expiry_date)
    api.session_permissions_db.get_for_session.return_value = []
    api.session_db = Mock()
    api.session_db.get_from_db.return_value = read_back(
        state='READY', project={'project_id': 'p-1', 'name': 'p'}
    )
    api._validate_user_and_project_membership = lambda username, project_id: (
        True,
        '',
    )
    request = VirtualDesktopSessionConnectionInfo(
        idea_session_id='s-1', idea_session_owner='user-a'
    )

    assert api._may_connect(request, 'user-b') is allowed


def test_expired_shares_are_left_out_of_the_host_permission_file():
    utils = VirtualDesktopSessionPermissionUtils.__new__(
        VirtualDesktopSessionPermissionUtils
    )
    utils.context = Mock()
    utils.context.config.return_value.get_string.return_value = 'full'
    utils._logger = Mock()
    utils.controller_utils = Mock()
    utils._session_permission_db = Mock()
    utils._session_permission_db.get_for_session.return_value = [
        share('user-live', FUTURE),
        share('user-gone', PAST),
    ]
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

    assert 'user-live' in content
    assert 'user-gone' not in content


def test_share_reads_go_through_the_real_table_conversion():
    # the test above mocks the db; this one runs the db's own read and convert path
    db = VirtualDesktopSessionPermissionDB.__new__(VirtualDesktopSessionPermissionDB)
    db._logger = Mock()
    db._ddb_client = Mock()
    row = {
        'idea_session_id': 's-1',
        'actor_name': 'user-a',
        'actor_type': 'USER',
        'idea_session_owner': 'owner-a',
        'idea_session_base_os': VirtualDesktopBaseOS.AMAZON_LINUX2023.value,
        'idea_session_state': VirtualDesktopSessionState.READY.value,
        'idea_session_type': VirtualDesktopSessionType.VIRTUAL.name,
        'idea_session_created_on': 1600000000000,
        'permission_profile_id': 'full',
    }
    db._table_obj = Mock()
    db._table_obj.get_item.return_value = {'Item': row}
    db._table_obj.query.return_value = {'Items': [row]}

    assert db.get('s-1', 'user-a').actor_name == 'user-a'
    assert [p.actor_name for p in db.get_for_session('s-1')] == ['user-a']


# --- the broker session id in the resume commands -------------------------------------------


def resume_commands(dcv_session_id, base_os=VirtualDesktopBaseOS.WINDOWS2022):
    utils = DCVBrokerClientUtils.__new__(DCVBrokerClientUtils)
    utils._logger = Mock()
    utils._session_permission_utils = Mock()
    utils._session_permission_utils.generate_permissions_for_session.return_value = (
        '[groups]\n'
    )
    return utils.get_commands_to_execute_for_resuming_session(
        VirtualDesktopSession(
            idea_session_id='s-1',
            name='desk',
            owner='user-a',
            dcv_session_id=dcv_session_id,
            type=VirtualDesktopSessionType.VIRTUAL,
            software_stack=VirtualDesktopSoftwareStack(base_os=base_os),
        )
    )


@pytest.mark.parametrize(
    'dcv_session_id', ['4f6c1f0e-1d2a-4c55-9e0b-0a1b2c3d4e5f', 'console']
)
def test_broker_issued_session_ids_are_resumed(dcv_session_id):
    commands = resume_commands(dcv_session_id)
    assert commands[-2].endswith(f"'{dcv_session_id}'")
    # WIN-24: the SSM result is dcv.exe's, not the last cmdlet's
    assert commands[-1] == 'exit $LASTEXITCODE'
    assert any('Remove-Item -LiteralPath' in command for command in commands)


@pytest.mark.parametrize(
    'dcv_session_id', ['', None, '..', 'a/b', 'a\\b', 'marker id', 'x' * 129]
)
@pytest.mark.parametrize(
    'base_os', [VirtualDesktopBaseOS.WINDOWS2022, VirtualDesktopBaseOS.AMAZON_LINUX2023]
)
def test_other_session_ids_build_no_commands(dcv_session_id, base_os):
    assert resume_commands(dcv_session_id, base_os) == []


def test_resume_with_no_commands_sends_nothing():
    client = DCVBrokerClient.__new__(DCVBrokerClient)
    client._logger = Mock()
    client._ssm_commands_utils = Mock()
    client._dcv_broker_client_utils = Mock()
    client._dcv_broker_client_utils.get_commands_to_execute_for_resuming_session.return_value = []

    session = client.resume_session(
        VirtualDesktopSession(
            idea_session_id='s-1',
            server=VirtualDesktopServer(instance_id='i-1'),
            software_stack=VirtualDesktopSoftwareStack(
                base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023
            ),
        )
    )

    assert session.failure_reason
    client._ssm_commands_utils.submit_ssm_command_to_resume_session.assert_not_called()


# --- the host a broker session runs on ------------------------------------------------------


def broker_entry(ip, state='READY'):
    return {
        'id': 'dcv-1',
        'state': state,
        'server': {'ip': ip, 'tags': [{'key': 'idea_session_id', 'value': 's-1'}]},
    }


def stored_desktop():
    return read_back()


def ec2_instance(*interfaces):
    """describe_instances for i-1; each argument is one interface's private addresses"""
    return {
        'Reservations': [
            {
                'Instances': [
                    {
                        'InstanceId': 'i-1',
                        'PrivateIpAddress': interfaces[0][0] if interfaces else None,
                        'NetworkInterfaces': [
                            {
                                'PrivateIpAddresses': [
                                    {'PrivateIpAddress': ip, 'Primary': n == 0}
                                    for n, ip in enumerate(ips)
                                ]
                            }
                            for ips in interfaces
                        ],
                    }
                ]
            }
        ]
        if interfaces
        else []
    }


def host_utils(instance):
    utils = DCVBrokerClientUtils.__new__(DCVBrokerClientUtils)
    utils._logger = Mock()
    utils.context = Mock()
    utils.context.aws().ec2().describe_instances.return_value = instance
    return utils


def test_the_stored_desktop_carries_no_address():
    # why the address is read from EC2: the session table does not keep it
    assert stored_desktop().server.private_ip is None


def test_sessions_for_a_host_must_be_on_that_hosts_address():
    client = DCVBrokerClient.__new__(DCVBrokerClient)
    client._dcv_broker_client_utils = host_utils(ec2_instance(['10.0.0.5']))
    client._describe_sessions = lambda **kwargs: {
        'sessions': [broker_entry('10.0.0.9'), broker_entry('10.0.0.5')]
    }

    entries = client.describe_sessions_for_host(stored_desktop())

    assert [entry['server']['ip'] for entry in entries] == ['10.0.0.5']


def validation_handler(entry, instance):
    handler = ValidateDCVSessionCreationEventHandler.__new__(
        ValidateDCVSessionCreationEventHandler
    )
    handler.context = Mock()
    handler._logger = Mock()
    handler.is_sender_controller_role = lambda sender: True
    handler.session_db = Mock()
    handler.session_db.get_from_db.return_value = stored_desktop()
    handler.session_utils = Mock()
    handler.session_counter_db = Mock()
    handler.session_counter_db.get.return_value = SimpleNamespace(counter=0)
    handler.context.dcv_broker_client.describe_sessions.return_value = {
        'sessions': {'dcv-1': entry}
    }
    utils = host_utils(instance)
    handler.context.dcv_broker_client.desktop_host_addresses = (
        utils.desktop_host_addresses
    )
    handler.mark_ready = Mock()
    return handler, utils


def validate(handler):
    handler.handle_event(
        'm-1',
        'controller',
        VirtualDesktopEvent(
            event_group_id='s-1',
            event_type=VirtualDesktopEventType.VALIDATE_DCV_SESSION_CREATION_EVENT,
            detail={'idea_session_id': 's-1', 'idea_session_owner': 'user-a'},
        ),
    )


def test_a_ready_session_on_another_address_is_not_marked_ready():
    handler, _ = validation_handler(
        broker_entry('10.0.0.9'), ec2_instance(['10.0.0.5'])
    )
    with pytest.raises(SocaException):
        validate(handler)
    handler.mark_ready.assert_not_called()


def test_a_ready_session_on_the_primary_address_is_marked_ready():
    handler, _ = validation_handler(
        broker_entry('10.0.0.5'), ec2_instance(['10.0.0.5'])
    )
    validate(handler)
    handler.mark_ready.assert_called_once()


@pytest.mark.parametrize('reported', ['10.0.0.5', '10.0.0.6', '10.0.1.7'])
def test_any_address_of_a_host_with_several_interfaces_is_its_own(reported):
    handler, _ = validation_handler(
        broker_entry(reported),
        ec2_instance(['10.0.0.5', '10.0.0.6'], ['10.0.1.7']),
    )
    validate(handler)
    handler.mark_ready.assert_called_once()


def test_an_instance_with_no_address_marks_nothing_ready_and_says_so():
    handler, utils = validation_handler(broker_entry('10.0.0.5'), ec2_instance())
    with pytest.raises(SocaException):
        validate(handler)
    handler.mark_ready.assert_not_called()
    assert 'no private address' in utils._logger.error.call_args.args[0]


# --- the per-user desktop limit -------------------------------------------------------------


def test_parallel_creates_stop_at_the_per_user_limit():
    rows = []
    distributed = threading.Lock()
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api.context.config.return_value.get_int.return_value = 1
    api.context.distributed_lock.return_value.acquire.side_effect = lambda key: (
        distributed.acquire()
    )
    api.context.distributed_lock.return_value.release.side_effect = lambda key: (
        distributed.release()
    )
    api._logger = Mock()
    api.session_db = Mock()
    api.session_db.get_session_count_for_user.side_effect = lambda username: len(rows)
    api.validate_create_session_request = lambda session: (session, True)
    api.complete_create_session_request = lambda session, context: session
    api.session_utils = Mock()

    def launch(session):
        # the row is written only once the host has launched
        time.sleep(0.05)
        rows.append(session)
        return session

    api.session_utils.create_session.side_effect = launch
    contexts = []
    for _ in range(4):
        context = Mock()
        context.get_username.return_value = 'user-a'
        context.get_request_payload_as.return_value = CreateSessionRequest(
            session=VirtualDesktopSession(name='desk', owner='user-a')
        )
        contexts.append(context)
    threads = [
        threading.Thread(target=api.create_session, args=(context,))
        for context in contexts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(rows) == 1
    assert sum(context.success.called for context in contexts) == 1


def create_api(acquire=lambda key: None, launch=lambda session: session):
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api.context.config.return_value.get_int.return_value = 5
    api.context.distributed_lock.return_value.acquire.side_effect = acquire
    api._logger = Mock()
    api.session_db = Mock()
    api.session_db.get_session_count_for_user.return_value = 0
    api.validate_create_session_request = lambda session: (session, True)
    api.complete_create_session_request = lambda session, context: session
    api.session_utils = Mock()
    api.session_utils.create_session.side_effect = launch
    return api


def create_context():
    context = Mock()
    context.get_username.return_value = 'user-a'
    context.get_request_payload_as.return_value = CreateSessionRequest(
        session=VirtualDesktopSession(name='desk', owner='user-a')
    )
    return context


def test_a_create_waiting_on_another_controller_gets_a_plain_answer():
    from python_dynamodb_lock.python_dynamodb_lock import DynamoDBLockError

    def held_elsewhere(key):
        raise DynamoDBLockError(DynamoDBLockError.ACQUIRE_TIMEOUT, 'timed out')

    api = create_api(acquire=held_elsewhere)
    context = create_context()

    api.create_session(context)

    assert (
        context.fail.call_args.kwargs['message'] == user_api_module.CREATE_BUSY_MESSAGE
    )
    api.session_utils.create_session.assert_not_called()


def test_a_create_waiting_in_this_process_is_bounded(monkeypatch):
    monkeypatch.setattr(user_api_module, 'CREATE_LOCK_WAIT_SECONDS', 0.05)
    started = threading.Event()

    def slow_launch(session):
        started.set()
        time.sleep(0.5)
        return session

    api = create_api(launch=slow_launch)
    first, second = create_context(), create_context()
    thread = threading.Thread(target=api.create_session, args=(first,))
    thread.start()
    started.wait(2)

    api.create_session(second)
    thread.join()

    assert (
        second.fail.call_args.kwargs['message'] == user_api_module.CREATE_BUSY_MESSAGE
    )
    first.success.assert_called_once()


def test_the_per_user_create_locks_do_not_accumulate():
    api = create_api()
    for name in ('user-a', 'user-b'):
        context = create_context()
        context.get_username.return_value = name
        api.create_session(context)
    assert 'user-a' not in user_api_module._CREATE_LOCKS
    assert 'user-b' not in user_api_module._CREATE_LOCKS


def test_the_per_user_count_reads_rows_just_written(monkeypatch):
    from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
        VirtualDesktopSessionDB,
    )

    table = Mock()
    table.query.return_value = {'Count': 2}
    monkeypatch.setattr(VirtualDesktopSessionDB, '_table', property(lambda self: table))
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)

    assert db.get_session_count_for_user('user-a') == 2
    assert table.query.call_args.kwargs['ConsistentRead'] is True


# --- the rendered bootstrap package ---------------------------------------------------------


def test_the_rendered_package_is_removed_once_uploaded(tmp_path, monkeypatch):
    import ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils as module

    seen = {}

    class Builder:
        def __init__(self, tmp_dir, target_package_basename, **kwargs):
            self.tmp_dir = tmp_dir
            self.name = target_package_basename

        def build(self):
            os.makedirs(os.path.join(self.tmp_dir, self.name))
            archive = os.path.join(self.tmp_dir, f'{self.name}.tar.gz')
            open(archive, 'w').close()
            return archive

    monkeypatch.setattr(module, 'BootstrapPackageBuilder', Builder)
    utils = VirtualDesktopControllerUtils.__new__(VirtualDesktopControllerUtils)
    utils.context = Mock()
    utils.context.cluster_name.return_value = 'c'
    utils.context.module_id.return_value = 'vdc'
    utils.context.config.return_value.get_string.side_effect = lambda key, **kwargs: (
        str(tmp_path) if key == 'shared-storage.apps.mount_dir' else 'bucket'
    )
    utils._logger = Mock()
    utils.s3_client = Mock()
    utils.s3_client.upload_file.side_effect = lambda **kwargs: seen.update(
        uploaded_exists=os.path.exists(kwargs['Filename'])
    )
    session = VirtualDesktopSession(
        idea_session_id='s-1',
        name='desk',
        owner='user-a',
        software_stack=VirtualDesktopSoftwareStack(
            base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023
        ),
    )

    uri = utils._package_and_upload(session, Mock(), 'virtual-desktop-host-linux')

    assert uri.startswith('s3://bucket/')
    assert seen['uploaded_exists'] is True
    assert not os.path.exists(
        os.path.join(
            str(tmp_path), 'c', 'vdc', 'dcv-host-bootstrap', 'user-a', 'desk-s-1'
        )
    )


# --- terminate-sessions reads ---------------------------------------------------------------


class PagedTable:
    """two pages, the way DynamoDB returns a read past 1 MB"""

    def __init__(self, items):
        self.pages = [items[:1], items[1:]]

    def _read(self, **kwargs):
        page = 1 if 'ExclusiveStartKey' in kwargs else 0
        response = {'Items': self.pages[page]}
        if page == 0:
            response['LastEvaluatedKey'] = {'owner': 'next'}
        return response

    scan = _read
    query = _read


def run_terminate(monkeypatch, table, *args):
    import boto3
    from ideasdk.utils import EnvironmentUtils

    monkeypatch.setattr(
        boto3, 'resource', lambda *a, **k: SimpleNamespace(Table=lambda name: table)
    )
    monkeypatch.setattr(EnvironmentUtils, 'aws_default_region', lambda **k: 'r')
    monkeypatch.setattr(EnvironmentUtils, 'idea_cluster_name', lambda **k: 'c')
    monkeypatch.setattr(EnvironmentUtils, 'idea_module_id', lambda **k: 'vdc')
    return (
        CliRunner().invoke(sessions_cli.terminate_sessions, [*args, '--dry-run']).output
    )


ROWS = [
    {
        'idea_session_id': 'marker-one',
        'owner': 'user-a',
        'state': 'READY',
        'created_on': 1600000000000,
    },
    {
        'idea_session_id': 'marker-two',
        'owner': 'user-a',
        'state': 'READY',
        'created_on': 1600000000000,
    },
]


@pytest.mark.parametrize(
    'args',
    [
        ['--username', 'user-a'],
        ['--username', 'user-a', '--created-before', '2030-01-01'],
        ['--created-before', '2030-01-01'],
    ],
)
def test_terminate_sessions_reads_every_page(monkeypatch, args):
    output = run_terminate(monkeypatch, PagedTable(ROWS), *args)
    assert 'marker-one' in output and 'marker-two' in output


def test_terminate_sessions_finds_a_session_on_a_later_page(monkeypatch):
    output = run_terminate(
        monkeypatch, PagedTable([{}, ROWS[1]]), '--session-id', 'marker-two'
    )
    assert 'Session not found' not in output
    assert 'marker-two' in output


def test_a_row_with_no_creation_time_is_never_selected_by_date(monkeypatch):
    rows = [
        {**ROWS[0], 'created_on': 0},
        {**ROWS[1], 'idea_session_id': 'marker-none', 'created_on': None},
        ROWS[1],
    ]
    output = run_terminate(
        monkeypatch, PagedTable(rows), '--created-before', '2030-01-01'
    )
    assert 'marker-one' not in output and 'marker-none' not in output
    assert 'marker-two' in output


def test_epoch_creation_times_are_read_in_host_local_time():
    # the same reading as before paging was fixed, so a dated command selects the same rows
    assert sessions_cli._created_on(1600000000000) == datetime.fromtimestamp(1600000000)
    assert sessions_cli._created_on('not a date') is None


def test_the_image_builder_keeps_its_package_for_reuse(tmp_path, monkeypatch):
    import ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder as module

    seen = {}

    class Builder:
        def __init__(self, tmp_dir, target_package_basename, **kwargs):
            self.tmp_dir = tmp_dir
            self.name = target_package_basename

        def build(self):
            os.makedirs(os.path.join(self.tmp_dir, self.name))
            archive = os.path.join(self.tmp_dir, f'{self.name}.tar.gz')
            open(archive, 'w').close()
            return archive

    monkeypatch.setattr(module, 'BootstrapPackageBuilder', Builder)
    monkeypatch.setattr(module, 'BootstrapContext', lambda **kwargs: Mock())
    monkeypatch.setattr(module, 'BootstrapUtils', Mock())
    monkeypatch.setattr(module, 'BootstrapUserDataBuilder', Mock())
    builder = module.DcvHostImageBuilder.__new__(module.DcvHostImageBuilder)
    builder.context = Mock()
    builder.context.aws().s3().upload_file.side_effect = lambda **kwargs: seen.update(
        uploaded_exists=os.path.exists(kwargs['Filename'])
    )
    builder.base_os = 'amazonlinux2023'
    builder.instance_type = 'm5.large'
    builder.overwrite = False
    monkeypatch.setattr(module.DcvHostImageBuilder, 'report', Mock())
    builder.get_ami_dir = lambda: str(tmp_path / 'ami')
    builder.get_ami_full_name = lambda: 'marker-image'
    builder.get_bootstrap_dir = lambda: str(tmp_path / 'src')

    builder.build_userdata()

    assert seen['uploaded_exists'] is True
    # without --overwrite the next build reuses this directory, customisations included
    assert (tmp_path / 'ami' / 'bootstrap').exists()
