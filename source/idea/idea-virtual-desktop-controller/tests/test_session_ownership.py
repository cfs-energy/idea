"""Session APIs bind client-supplied resource IDs to the authenticated owner."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
import boto3
from botocore.stub import Stubber

from ideadatamodel import (
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideasdk.api import ApiInvocationContext
from ideasdk.auth import ApiAuthorization, ApiAuthorizationType
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)


@pytest.fixture
def api():
    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = Mock()
    api._logger = Mock()
    rows = {
        (owner, f'session-{owner}'): VirtualDesktopSession(
            idea_session_id=f'session-{owner}',
            owner=owner,
            name='desktop',
            dcv_session_id=f'dcv-{owner}',
            server=VirtualDesktopServer(
                instance_id=f'i-{owner}', instance_type=instance_type
            ),
        )
        for owner, instance_type in [('a', 'm5.large'), ('b', 'm5.xlarge')]
    }
    api.session_db = Mock(spec=VirtualDesktopSessionDB)
    api.session_db.get_from_db.side_effect = (
        lambda idea_session_owner, idea_session_id: deepcopy(
            rows.get((idea_session_owner, idea_session_id))
        )
    )
    api.session_db.get_from_index.side_effect = lambda idea_session_id: deepcopy(
        next(row for row in rows.values() if row.idea_session_id == idea_session_id)
    )

    def save_session(session):
        rows[(session.owner, session.idea_session_id)] = deepcopy(session)
        return deepcopy(session)

    api.session_db.update.side_effect = save_session
    api.server_db = Mock(spec=VirtualDesktopServerDB)
    api.server_db.get.side_effect = lambda instance_id: deepcopy(
        next(
            (
                row.server
                for row in rows.values()
                if row.server.instance_id == instance_id
            ),
            None,
        )
    )
    api.controller_utils = Mock(spec=VirtualDesktopControllerUtils)
    api.controller_utils.get_instance_type_rejection_reason.return_value = None
    api.controller_utils.change_instance_type.return_value = (None, True)
    api.context.dcv_broker_client.get_session_screenshots.side_effect = (
        lambda screenshots: (screenshots, [])
    )
    return api


def invocation(api, payload):
    context = ApiInvocationContext(
        context=api.context,
        request={'header': {}, 'payload': payload},
        invocation_source='http',
        group_name_helper=Mock(),
        logger=api._logger,
        token_service=Mock(),
    )
    context._authorization = ApiAuthorization(
        type=ApiAuthorizationType.USER, username='b'
    )
    return context


@pytest.mark.parametrize('instance_type', ['m5.xlarge', 'm5.2xlarge', None])
def test_update_rejects_another_users_instance_before_any_write(api, instance_type):
    context = invocation(
        api,
        {
            'session': {
                'idea_session_id': 'session-b',
                'name': 'renamed',
                'server': {'instance_id': 'i-a', 'instance_type': instance_type},
            }
        },
    )
    api.update_session(context)
    assert not context.is_success()
    assert 'instance' in context.response['message'].lower()
    api.server_db.get.assert_not_called()
    api.server_db.update.assert_not_called()
    api.session_db.update.assert_not_called()
    api.controller_utils.change_instance_type.assert_not_called()
    api.controller_utils.create_tag.assert_not_called()


@pytest.mark.parametrize('instance_id', [None, 'i-b'])
def test_update_uses_stored_instance_when_id_is_omitted_or_matches(api, instance_id):
    context = invocation(
        api,
        {
            'session': {
                'idea_session_id': 'session-b',
                'server': {'instance_id': instance_id, 'instance_type': 'm5.2xlarge'},
            }
        },
    )
    api.update_session(context)
    assert context.is_success()
    api.server_db.get.assert_called_once_with(instance_id='i-b')
    api.controller_utils.change_instance_type.assert_called_once_with(
        instance_id='i-b', instance_type_name='m5.2xlarge'
    )
    assert context.response['payload']['session']['server']['instance_id'] == 'i-b'


def test_update_cannot_name_another_users_session(api):
    context = invocation(
        api,
        {
            'session': {
                'idea_session_id': 'session-a',
                'owner': 'a',
                'server': {'instance_id': 'i-a', 'instance_type': 'm5.2xlarge'},
            }
        },
    )
    api.update_session(context)
    assert not context.is_success()
    api.controller_utils.change_instance_type.assert_not_called()


@pytest.mark.parametrize(
    'session_id, allowed', [('session-a', False), ('session-b', True)]
)
def test_get_info_checks_stored_owner(api, session_id, allowed):
    context = invocation(api, {'session': {'idea_session_id': session_id}})
    api.get_session_info(context)
    assert context.is_success() == allowed
    if allowed:
        assert context.response['payload']['session']['owner'] == 'b'
    else:
        assert 'server' not in context.response['payload']['session']


@pytest.fixture
def capacity_session(api):
    session = api.session_db.get_from_db('b', 'session-b')
    session.state = VirtualDesktopSessionState.STOPPED
    session.failure_reason = (
        'InsufficientInstanceCapacity: Update session to choose another allowed size'
    )
    api.session_db.update(session)
    api.session_db.update.reset_mock()
    return session


def test_get_info_returns_owned_session_with_previous_failure(api, capacity_session):
    context = invocation(api, {'session': {'idea_session_id': 'session-b'}})
    api.get_session_info(context)
    assert context.is_success()
    assert (
        context.response['payload']['session']['failure_reason']
        == capacity_session.failure_reason
    )
    api.session_db.update.assert_not_called()


@pytest.mark.parametrize('resize_succeeds', [True, False])
def test_resize_reports_current_result_after_capacity_failure(
    api, capacity_session, resize_succeeds
):
    controller = VirtualDesktopControllerUtils.__new__(VirtualDesktopControllerUtils)
    controller._logger = api._logger
    controller.ec2_client = boto3.client(
        'ec2',
        region_name='us-east-1',
        aws_access_key_id='testing',
        aws_secret_access_key='testing',
    )
    api.controller_utils.change_instance_type = controller.change_instance_type
    context = invocation(
        api,
        {
            'session': {
                'idea_session_id': 'session-b',
                'server': {'instance_type': 'm5.2xlarge'},
            }
        },
    )
    params = {
        'InstanceId': 'i-b',
        'Attribute': 'instanceType',
        'Value': 'm5.2xlarge',
    }
    with Stubber(controller.ec2_client) as ec2:
        if resize_succeeds:
            ec2.add_response('modify_instance_attribute', {}, params)
        else:
            ec2.add_client_error(
                'modify_instance_attribute',
                service_error_code='UnauthorizedOperation',
                expected_params=params,
            )
        api.update_session(context)
        ec2.assert_no_pending_responses()

    stored = api.session_db.get_from_db('b', 'session-b')
    if resize_succeeds:
        assert context.is_success(), context.response
        assert not context.response['payload']['session'].get('failure_reason')
        assert stored.failure_reason is None
        assert stored.server.instance_type == 'm5.2xlarge'
        assert stored.state == VirtualDesktopSessionState.STOPPED
        api.session_db.update.assert_called_once()
        assert api.server_db.update.call_args.args[0].instance_type == 'm5.2xlarge'
    else:
        assert not context.is_success()
        assert context.response['error_code'] == 'UPDATE_SESSION_FAILED'
        assert 'UnauthorizedOperation' in context.response['message']
        assert stored.failure_reason == capacity_session.failure_reason
        assert stored.server.instance_type == 'm5.xlarge'
        api.session_db.update.assert_not_called()
        api.server_db.update.assert_not_called()


@pytest.mark.parametrize(
    'edit', [{'name': 'renamed'}, {'description': 'notes'}, {'idle_autostop_delay': 30}]
)
def test_edit_without_resize_keeps_stored_failure(api, edit):
    from ideavirtualdesktopcontroller.app.sessions.dcv_session_recovery import (
        INSTANCE_UNHEALTHY_MESSAGE,
    )

    session = api.session_db.get_from_db('b', 'session-b')
    session.state = VirtualDesktopSessionState.ERROR
    session.failure_reason = INSTANCE_UNHEALTHY_MESSAGE
    api.session_db.update(session)
    api.validate_idle_autostop_delay = Mock(return_value=None)
    context = invocation(api, {'session': {'idea_session_id': 'session-b', **edit}})
    api.update_session(context)
    assert context.is_success(), context.response
    assert not context.response['payload']['session'].get('failure_reason')
    stored = api.session_db.get_from_db('b', 'session-b')
    assert stored.failure_reason == INSTANCE_UNHEALTHY_MESSAGE
    assert stored.state == VirtualDesktopSessionState.ERROR


@pytest.mark.parametrize('session_id', [None, 'session-a'])
def test_screenshot_cannot_target_another_users_dcv_id(api, session_id):
    context = invocation(
        api,
        {
            'screenshots': [
                {
                    'idea_session_id': session_id,
                    'dcv_session_id': 'dcv-a',
                }
            ]
        },
    )
    api.get_session_screenshots(context)
    assert context.response['payload']['success'] == []
    assert len(context.response['payload']['failed']) == 1
    if session_id is None:
        api.context.dcv_broker_client.get_session_screenshots.assert_not_called()
    else:
        api.context.dcv_broker_client.get_session_screenshots.assert_called_once_with(
            []
        )


def test_ownership_api_changes_are_documented_for_upgrades():
    from pathlib import Path

    changelog = (Path(__file__).resolve().parents[4] / 'docs/changelog.md').read_text()
    release_notes = changelog.split('## [26.11.1]', 1)[1].split('\n## [', 1)[0]
    note = next(
        (
            line
            for line in release_notes.splitlines()
            if '**Desktop session ownership**' in line
        ),
        '',
    )
    assert 'UpdateSession' in note and 'stored session' in note
    assert 'rejects a mismatched `server.instance_id`' in note
    assert 'Screenshot requests require `idea_session_id`' in note
    assert "session info returns only the caller's stored desktop" in note


def test_screenshot_resolves_dcv_id_from_owned_session(api):
    context = invocation(
        api,
        {
            'screenshots': [
                {
                    'idea_session_id': 'session-b',
                    'dcv_session_id': 'dcv-a',
                }
            ]
        },
    )
    api.get_session_screenshots(context)
    assert context.response['payload']['success'][0]['dcv_session_id'] == 'dcv-b'
    assert context.response['payload']['failed'] == []
