from unittest.mock import Mock

import pytest

from ideaclustermanager.app.accounts.helpers.preset_computer_helper import (
    PresetComputeHelper,
)
from ideaclustermanager.app.accounts.ldapclient import abstract_ldap_client
from ideaclustermanager.app.accounts.ldapclient.abstract_ldap_client import (
    AbstractLDAPClient,
)


def preset_helper(entries):
    helper = PresetComputeHelper.__new__(PresetComputeHelper)
    helper.logger = Mock()
    helper.nonce, helper.instance_id, helper.hostname = 'nonce-1', 'i-marker', 'IDEA-1'
    helper.ou = 'ou=marker'
    helper.ec2_instance = Mock()
    helper.ad_automation_dao = Mock()
    helper.ad_automation_dao.list_entries_by_instance_id.return_value = entries
    helper.get_any_domain_controller_ip = Mock(return_value='192.0.2.10')
    helper.is_existing_computer_account = Mock(return_value=True)
    helper.delete_computer = Mock()
    helper.preset_computer = Mock(return_value='otp-marker')
    return helper


def test_a_preset_delivered_again_after_it_succeeded_changes_nothing():
    helper = preset_helper([{'nonce': 'nonce-1', 'status': 'success'}])
    helper.invoke()
    helper.delete_computer.assert_not_called()
    helper.preset_computer.assert_not_called()


@pytest.mark.parametrize(
    'entries',
    [
        [],
        [{'nonce': 'nonce-0', 'status': 'success'}],
        [{'nonce': 'nonce-1', 'status': 'fail'}],
    ],
)
def test_a_new_preset_request_still_presets(entries):
    helper = preset_helper(entries)
    helper.invoke()
    helper.preset_computer.assert_called_once()


def test_stored_ldap_options_are_read_as_literals_never_run(monkeypatch):
    ran = []
    monkeypatch.setattr(abstract_ldap_client, 'ConnectionManager', Mock())
    monkeypatch.setattr(abstract_ldap_client.ldap, 'set_option', Mock())
    monkeypatch.setattr(abstract_ldap_client.ldap, 'get_option', Mock())
    monkeypatch.setattr(
        AbstractLDAPClient, 'refresh_root_username_password', lambda self: None
    )
    context = Mock()
    context.config().get_list.return_value = [
        "{'name': 'marker', 'code': 1, 'value': 0}"
    ]
    options = Mock(domain_name='example.invalid')
    AbstractLDAPClient(context, options, logger=Mock())
    abstract_ldap_client.ldap.set_option.assert_any_call(1, 0)
    context.config().get_list.return_value = [
        "dict(code=1, value=__import__('builtins').print('ran') or 0)"
    ]
    monkeypatch.setattr('builtins.print', lambda *a: ran.append(a))
    with pytest.raises(ValueError):
        AbstractLDAPClient(context, options, logger=Mock())
    assert ran == []


def test_a_self_managed_directory_root_password_is_never_reset(monkeypatch):
    from ideaclustermanager.app.accounts.ad_automation_agent import ADAutomationAgent
    from ideadatamodel import constants

    agent = ADAutomationAgent.__new__(ADAutomationAgent)
    agent.context = Mock()
    agent.context.config().get_bool.return_value = True
    agent.logger = Mock()
    agent._reset_disabled_logged = False
    agent.ldap_client = Mock(ds_provider=constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY)
    agent.is_password_expired = Mock(return_value=True)
    monkeypatch.setattr('time.sleep', lambda _: None)
    agent.check_and_reset_admin_password()
    agent.ldap_client.change_password.assert_not_called()
    agent.ldap_client.update_root_password.assert_not_called()


def test_a_group_without_a_title_can_be_given_one(context):
    from ideadatamodel import Group

    context.accounts.group_dao.create_group(
        {
            'group_name': 'notitle9',
            'gid': 7501,
            'group_type': 'project',
            'enabled': True,
        }
    )
    try:
        modified = context.accounts.modify_group(Group(name='notitle9', title='Marker'))
        assert modified.title == 'Marker'
    finally:
        context.accounts.group_dao.delete_group('notitle9')


def test_the_join_entry_key_is_in_no_log_and_no_stored_message():
    from ideadatamodel import errorcodes, exceptions

    lines = []
    helper = preset_helper([])
    helper.nonce = 'nonce-marker-9'
    for level in ('debug', 'info', 'warning', 'error', 'exception'):
        setattr(helper.logger, level, lambda m, *a, **k: lines.append(str(m)))
    helper.get_any_domain_controller_ip.side_effect = exceptions.soca_exception(
        error_code=errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED,
        message=f'preset failed {helper.log_tag}',
    )
    with pytest.raises(exceptions.SocaException):
        helper.invoke()
    stored = helper.ad_automation_dao.create_ad_automation_entry.call_args.kwargs[
        'entry'
    ]
    assert 'nonce-marker-9' not in ' '.join(lines)
    assert 'nonce-marker-9' not in stored['message']
    # the row itself is still keyed by it
    assert stored['nonce'] == 'nonce-marker-9'


def test_the_sudoers_group_dn_is_escaped_in_the_sudoer_filter():
    from unittest.mock import Mock
    from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
        ActiveDirectoryClient,
    )

    client = object.__new__(ActiveDirectoryClient)
    values = {
        'directoryservice.sudoers.group_name': 'marker',
        'directoryservice.sudoers.ou': 'OU=Admins (marker)*,DC=example,DC=invalid',
    }
    client.context = Mock()
    client.context.config().get_string.side_effect = lambda key, *_, **__: values.get(
        key
    )
    filterstr = client.build_sudoer_filterstr('user')
    assert '(marker)*' not in filterstr
    assert 'Admins \\28marker\\29\\2a' in filterstr
    assert filterstr.endswith('(cn=user))')


def test_a_group_is_read_as_it_now_is():
    from ideaclustermanager.app.accounts.db.group_dao import GroupDAO

    dao = GroupDAO.__new__(GroupDAO)
    dao.table = Mock()
    dao.table.get_item.return_value = {'Item': {'group_name': 'g', 'enabled': False}}
    assert dao.get_group('g')['enabled'] is False
    assert dao.table.get_item.call_args.kwargs['ConsistentRead'] is True


def test_preset_success_records_are_read_consistently():
    from ideaclustermanager.app.accounts.db.ad_automation_dao import ADAutomationDAO

    dao = ADAutomationDAO.__new__(ADAutomationDAO)
    dao.table = Mock()
    dao.table.query.return_value = {'Items': []}
    dao.list_entries_by_instance_id('i-marker')
    assert dao.table.query.call_args.kwargs['ConsistentRead'] is True


def test_a_first_sign_in_keeps_its_password_when_marking_the_email_fails():
    from ideaclustermanager.app.accounts.cognito_user_pool import (
        CognitoUserPool,
        CognitoUserPoolOptions,
    )
    from ideadatamodel.auth import RespondToAuthChallengeRequest

    context = Mock()
    pool = CognitoUserPool(
        context,
        CognitoUserPoolOptions(
            user_pool_id='pool',
            admin_group_name='admins',
            client_id='client',
            client_secret='secret-marker',
        ),
    )
    context.aws().cognito_idp().admin_respond_to_auth_challenge.return_value = {
        'AuthenticationResult': {
            'AccessToken': 'a',
            'IdToken': 'i',
            'RefreshToken': 'r',
            'ExpiresIn': 3600,
            'TokenType': 'Bearer',
        }
    }
    pool.admin_set_email_verified = Mock(side_effect=RuntimeError('marker'))
    pool.password_updated = Mock()
    result = pool.respond_to_auth_challenge(
        RespondToAuthChallengeRequest(
            username='user',
            challenge_name='NEW_PASSWORD_REQUIRED',
            session='s',
            new_password='p',
        )
    )
    assert result.auth is not None
    pool.password_updated.assert_called_once_with('user')
