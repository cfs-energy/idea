from unittest.mock import Mock

import ldap
import pytest
from ideadatamodel import User, ListUsersResult, SocaPaginator, CognitoUser
from ideaclustermanager.app.accounts.account_reconciler import (
    AccountReconciler,
    ReconcileUsersRequest,
)
from ideaclustermanagertests.metrics_fakes import FakeContext

PREFIX = 'cluster-manager.accounts.reconcile.'


def build(values=None, records=None, users=None):
    context = FakeContext(
        {
            'metrics.provider': 'cloudwatch',
            'directoryservice.provider': 'activedirectory',
            PREFIX + 'okta.approved_origins': ['https://id.example.invalid'],
            **(values or {}),
        }
    )
    context._config.db.values.update(
        {key: value for key, value in (values or {}).items() if key.startswith(PREFIX)}
    )
    context._config.db.aws = Mock()
    context._config.db.aws.secretsmanager.return_value.get_secret_value.side_effect = (
        lambda SecretId: {'SecretString': context._config.secrets.get(SecretId)}
    )
    users = users or [User(username=f'user{index}', enabled=True) for index in range(4)]
    records = (
        records if records is not None else {'user0': {'user_account_control': 514}}
    )
    accounts = Mock()
    accounts.user_dao.get_user.return_value = {}
    accounts.ldap_client.ldap_root_username = 'bind-service'
    accounts.ldap_client.get_user.side_effect = lambda username, **kw: records.get(
        username, {'user_account_control': 512}
    )

    def directory_record(username, email, identity):
        record = accounts.ldap_client.get_user(username, trace=False)
        return (
            {**record, 'directory_identity': '00' * 16} if record is not None else None
        )

    accounts.ldap_client.get_reconcile_user.side_effect = directory_record
    accounts.is_cluster_administrator.side_effect = (
        lambda username: username == 'cluster-admin'
    )
    accounts.list_users.return_value = ListUsersResult(listing=users)
    context.accounts = accounts
    return AccountReconciler(context), context


@pytest.mark.parametrize('provider', ['activedirectory', 'aws_managed_activedirectory'])
@pytest.mark.parametrize(
    'record,missing', [({'user_account_control': 514}, 0), (None, 1)]
)
def test_directory_disabled_or_missing(provider, record, missing):
    service, context = build({'directoryservice.provider': provider}, {'user0': record})
    report = service.run_once(dry_run=False)
    assert report['disabled'] == 1
    assert report['missing'] == missing
    context.accounts.disable_user.assert_called_once_with(
        'user0', preserve_directory=True, reconcile_sources=['directory']
    )
    assert context.published('accounts.reconcile.disabled')[0]['Value'] == 1
    assert context._lock.held == []


def test_default_dry_run_and_api_default():
    service, context = build()
    report = service.run_once()
    assert ReconcileUsersRequest().dry_run is True
    assert report['changes'][0]['action'] == 'disable'
    assert report['disabled'] == 0
    context.accounts.disable_user.assert_not_called()
    with pytest.raises(ValueError):
        ReconcileUsersRequest(dry_run='false')


def test_cap_refuses_whole_run():
    service, context = build(records={'user0': None, 'user1': None})
    report = service.run_once(dry_run=False)
    assert report['refused'] == 1
    assert len(report['changes']) == 2
    context.accounts.disable_user.assert_not_called()
    assert context.published('accounts.reconcile.refused')[0]['Value'] == 1


@pytest.mark.parametrize('control', [None, 'invalid'])
def test_incomplete_read_at_cap_applies_successful_reads(control):
    service, context = build(
        records={'user0': None, 'user1': {'user_account_control': control}}
    )
    report = service.run_once(dry_run=False)
    assert report['refused'] == 0 and report['errors'] == 1
    assert report['disabled'] == 1
    assert any(row['action'] == 'error' for row in report['changes'])


@pytest.mark.parametrize('reenable', [False, True])
def test_reenable(reenable):
    service, context = build(
        {PREFIX + 'reenable': reenable, PREFIX + 'check_cognito': True},
        records={},
        users=[User(username='user0', enabled=False)],
    )
    context.accounts.user_dao.get_user.return_value = {
        'reconcile_sources': ['directory']
    }
    report = service.run_once(dry_run=False)
    assert report['reenabled'] == int(reenable)
    assert context.accounts.enable_user.call_count == int(reenable)
    context.accounts.user_pool.admin_get_user.assert_not_called()


@pytest.mark.parametrize(
    'status', ['DEPROVISIONED', 'SUSPENDED', 'DEACTIVATED', 'ACTIVE', 'STAGED']
)
def test_okta(status, monkeypatch):
    service, context = build(
        {
            PREFIX + 'okta.org_url': 'https://id.example.invalid',
            PREFIX + 'okta.api_token_secret_arn': 'secret-ref',
        },
        records={},
    )
    context._config.secrets['secret-ref'] = 'test-token'
    response = Mock(status_code=200)
    response.json.return_value = {'status': status}
    get = Mock(return_value=response)
    monkeypatch.setattr(
        'ideaclustermanager.app.accounts.account_reconciler.requests.get', get
    )
    report = service.run_once()
    assert len(report['changes']) == (
        4 if status in ('DEPROVISIONED', 'SUSPENDED', 'DEACTIVATED') else 0
    )
    assert get.call_args.kwargs['allow_redirects'] is False
    assert get.call_args.kwargs['timeout'] == 15


@pytest.mark.parametrize(
    'status,missing,errors', [(404, 4, 0), (429, 0, 4), (403, 0, 4), (500, 0, 4)]
)
def test_okta_errors_are_not_missing(status, missing, errors, monkeypatch):
    service, context = build(
        {
            PREFIX + 'okta.org_url': 'https://id.example.invalid',
            PREFIX + 'okta.api_token_secret_arn': 'secret-ref',
        },
        records={},
    )
    context._config.secrets['secret-ref'] = 'test-token'
    monkeypatch.setattr(
        'ideaclustermanager.app.accounts.account_reconciler.requests.get',
        Mock(return_value=Mock(status_code=status)),
    )
    report = service.run_once(dry_run=False)
    assert report['missing'] == missing and report['errors'] == errors
    assert report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


def test_cognito_fresh_native_read():
    service, context = build(
        {'directoryservice.provider': 'openldap', PREFIX + 'check_cognito': True}
    )
    context.accounts.user_pool.admin_get_user.side_effect = [
        CognitoUser(Enabled=False)
    ] + [CognitoUser(Enabled=True)] * 3
    assert service.run_once(dry_run=False)['disabled'] == 1
    assert all(
        call.kwargs == {'use_cache': False}
        for call in context.accounts.user_pool.admin_get_user.call_args_list
    )


def test_protected_and_pagination():
    service, context = build()
    context.accounts.list_users.side_effect = [
        ListUsersResult(
            listing=[
                User(username='cluster-admin', enabled=True),
                User(username='bind-service', enabled=True),
                User(username='ideaserviceaccount', enabled=True),
            ],
            paginator=SocaPaginator(cursor='next'),
        ),
        ListUsersResult(
            listing=[User(username=f'user{i}', enabled=True) for i in range(4)]
        ),
    ]
    report = service.run_once()
    assert report['checked'] == 4
    assert len(report['skipped']) == 3
    assert context.accounts.list_users.call_args.args[0].cursor == 'next'


def test_periodic_checkpoint_is_read_consistently_under_lock():
    service, context = build({PREFIX + 'enabled': True})
    context._config.db.lock = context._lock
    service.run_once(periodic=True)
    assert service.run_once(periodic=True) == {'skipped': 'interval'}
    context.accounts.list_users.assert_called_once()
    assert all(read[1] for read in context._config.db.reads)
    assert context._lock.held == []


def test_api_requires_administrator():
    from ideaclustermanager.app.api.accounts_api import AccountsAPI

    service, context = build()
    context.accounts.reconciler = service
    api = AccountsAPI(context)
    invocation = Mock()
    invocation.is_administrator.return_value = False
    with pytest.raises(Exception):
        api.reconcile_users(invocation)
    invocation.success.assert_not_called()


@pytest.mark.parametrize('provider', ['cloudwatch', 'dogstatsd'])
def test_metrics_have_all_families_on_both_providers(provider):
    service, context = build({'metrics.provider': provider})
    service.run_once()
    assert {entry['MetricName'] for entry in context.published()} == {
        f'accounts.reconcile.{name}'
        for name in ('checked', 'disabled', 'reenabled', 'missing', 'errors', 'refused')
    }


def test_default_reenable_preserves_administrator_disable():
    service, context = build(records={}, users=[User(username='user0', enabled=False)])
    assert service.run_once(dry_run=False)['reenabled'] == 0
    context.accounts.enable_user.assert_not_called()


@pytest.mark.parametrize('dry_run', [True, False])
def test_cap_override(dry_run):
    service, context = build(records={'user0': None, 'user1': None})
    report = service.run_once(dry_run=dry_run, override_max_disable_fraction=True)
    assert report['refused'] == 0
    assert report['would_disable'] == 2
    assert report['eligible_enabled'] == 4
    assert report['disabled'] == (0 if dry_run else 2)


def test_override_does_not_bypass_read_errors():
    service, context = build(
        records={
            'user0': None,
            'user1': {'user_account_control': None},
            'user2': {'user_account_control': None},
        }
    )
    report = service.run_once(dry_run=False, override_max_disable_fraction=True)
    assert report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


def test_periodic_never_overrides():
    service, context = build(
        {PREFIX + 'enabled': True, PREFIX + 'dry_run': False},
        records={'user0': None, 'user1': None},
    )
    report = service.run_once(periodic=True, override_max_disable_fraction=True)
    assert report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


@pytest.mark.parametrize('admin', [True, False])
def test_api_override_requires_admin_and_logs_actor(admin):
    from ideaclustermanager.app.api.accounts_api import AccountsAPI

    service, context = build(records={'user0': None, 'user1': None})
    context.accounts.reconciler = service
    context.logger = Mock(return_value=Mock())
    invocation = Mock()
    invocation.is_administrator.return_value = admin
    invocation.get_username.return_value = 'cluster-admin'
    invocation.get_request_payload_as.return_value = ReconcileUsersRequest(
        dry_run=False, override_max_disable_fraction=True
    )
    if admin:
        AccountsAPI(context).reconcile_users(invocation)
        assert invocation.success.call_args.args[0]['disabled'] == 2
        assert 'cluster-admin' in context.logger.return_value.warning.call_args.args[0]
    else:
        with pytest.raises(Exception):
            AccountsAPI(context).reconcile_users(invocation)
        context.accounts.disable_user.assert_not_called()


def test_override_is_strict_and_defaults_off():
    assert ReconcileUsersRequest().override_max_disable_fraction is False
    with pytest.raises(ValueError):
        ReconcileUsersRequest(override_max_disable_fraction='true')


def test_worker_starts_when_disabled_so_portal_can_enable_it():
    service, context = build()
    service._thread = Mock()
    service.start()
    service._thread.start.assert_called_once()


@pytest.mark.parametrize(
    'minutes,seconds', [(0, 60), (1, 60), (60, 3600), (1440, 86400), (1441, 86400)]
)
def test_interval_bounds(minutes, seconds):
    service, context = build({PREFIX + 'interval_minutes': minutes})
    assert service.interval_seconds() == seconds


def test_unapproved_origin_never_receives_token(monkeypatch):
    service, context = build(
        {
            PREFIX + 'okta.org_url': 'https://unapproved.example.invalid',
            PREFIX + 'okta.api_token_secret_arn': 'secret-ref',
        }
    )
    context._config.secrets['secret-ref'] = 'test-token'
    get = Mock()
    monkeypatch.setattr(
        'ideaclustermanager.app.accounts.account_reconciler.requests.get', get
    )
    assert service.run_once()['errors'] == 4
    get.assert_not_called()
    assert 'test-token' not in str(context._logger.lines)


def use_real_account_transitions(context):
    from ideaclustermanager.app.accounts.accounts_service import AccountsService
    from ideaclustermanager.app.accounts.account_tasks import (
        SyncUserInDirectoryServiceTask,
    )
    from ideaclustermanager.app.accounts.db.user_dao import UserDAO

    accounts = context.accounts
    stored = {
        user.username: {
            'username': user.username,
            'enabled': user.enabled,
            'email': user.email,
            'group_name': user.username,
        }
        for user in accounts.list_users.return_value.listing
    }
    accounts.user_dao.get_user.side_effect = lambda username: dict(stored[username])

    def update(values):
        stored[values['username']].update(values)
        return dict(stored[values['username']])

    accounts.user_dao.update_user.side_effect = update
    accounts.list_users.side_effect = lambda request: ListUsersResult(
        listing=[
            UserDAO.convert_from_db(accounts.user_dao, row) for row in stored.values()
        ]
    )
    accounts.disable_user.side_effect = (
        lambda *args, **kwargs: AccountsService.disable_user(accounts, *args, **kwargs)
    )
    accounts.enable_user.side_effect = (
        lambda *args, **kwargs: AccountsService.enable_user(accounts, *args, **kwargs)
    )
    context.ldap_client = accounts.ldap_client
    context.ldap_client.is_readonly.return_value = False
    accounts.task_manager.send.side_effect = (
        lambda **kwargs: SyncUserInDirectoryServiceTask(context).invoke(
            kwargs['payload']
        )
    )
    return stored


def test_managed_ad_disable_keeps_authoritative_objects():
    service, context = build(
        {'directoryservice.provider': 'aws_managed_activedirectory'}
    )
    stored = use_real_account_transitions(context)
    assert service.run_once(dry_run=False)['disabled'] == 1
    assert stored['user0']['enabled'] is False
    context.accounts.user_pool.admin_disable_user.assert_called_once_with('user0')
    context.accounts.evdi_client.publish_user_disabled_event.assert_called_once_with(
        username='user0'
    )
    context.accounts.task_manager.send.assert_called_once()
    context.ldap_client.delete_user.assert_not_called()
    context.ldap_client.delete_group.assert_not_called()


def test_cognito_revocation_is_not_reversed_on_next_run():
    service, context = build({PREFIX + 'check_cognito': True}, records={})
    stored = use_real_account_transitions(context)
    context.accounts.task_manager.send.side_effect = None
    context.accounts.user_pool.admin_get_user.side_effect = (
        lambda username, **kwargs: CognitoUser(Enabled=username != 'user0')
    )
    assert service.run_once(dry_run=False)['disabled'] == 1
    assert service.run_once(dry_run=False)['reenabled'] == 0
    assert stored['user0']['enabled'] is False
    context.accounts.user_pool.admin_enable_user.assert_not_called()


def test_only_external_disable_is_restored_by_default():
    records = {'user0': {'user_account_control': 514}}
    service, context = build({PREFIX + 'check_cognito': True}, records=records)
    stored = use_real_account_transitions(context)
    context.accounts.user_pool.admin_get_user.return_value = CognitoUser(Enabled=True)
    assert service.run_once(dry_run=False)['disabled'] == 1
    context.accounts.user_pool.admin_get_user.side_effect = (
        lambda username, **kwargs: CognitoUser(Enabled=username != 'user0')
    )
    assert service.run_once(dry_run=False)['reenabled'] == 0
    records['user0'] = {'user_account_control': 512}
    assert service.run_once(dry_run=False)['reenabled'] == 1
    assert stored['user0']['reconcile_sources'] == []
    context.accounts.disable_user('user0')
    assert service.run_once(dry_run=False)['reenabled'] == 0


def test_administrator_can_retain_an_already_reconciled_disable():
    service, context = build()
    stored = use_real_account_transitions(context)
    service.run_once(dry_run=False)
    context.accounts.disable_user('user0')
    assert stored['user0']['reconcile_sources'] == []


def real_directory_reader(context, results):
    from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
        ActiveDirectoryClient,
    )

    client = Mock()
    client.ldap_user_base = 'ou=users,dc=example,dc=invalid'
    client.ldap_user_filterstr = '(objectClass=user)'
    client.search_s.return_value = results
    client.is_activedirectory.return_value = True
    client.password_max_age = None
    client.convert_ldap_user.side_effect = lambda attrs: (
        ActiveDirectoryClient.convert_ldap_user(client, attrs)
    )
    context.accounts.ldap_client.get_reconcile_user.side_effect = (
        lambda *args: ActiveDirectoryClient.get_reconcile_user(client, *args)
    )
    return client


def test_different_directory_username_maps_by_unique_email_and_persists_guid():
    service, context = build(
        users=[User(username='local-user', email='user@example.invalid', enabled=True)]
    )
    stored = use_real_account_transitions(context)
    attrs = {
        'objectGUID': [bytes(range(16))],
        'userAccountControl': [b'512'],
        'sAMAccountName': [b'directory-user'],
    }
    client = real_directory_reader(context, [('dn', attrs)])
    assert service.run_once(dry_run=False)['errors'] == 0
    assert (
        '(mail=user@example.invalid)' in client.search_s.call_args.kwargs['filterstr']
    )
    assert stored['local-user']['directory_identity'] == bytes(range(16)).hex()
    context.accounts.disable_user.assert_not_called()
    service.run_once()
    assert 'objectGUID=' in client.search_s.call_args.kwargs['filterstr']
    client.search_s.return_value = []
    report = service.run_once()
    assert report['missing'] == 1 and report['would_disable'] == 1


def test_ambiguous_directory_identity_is_error():
    service, context = build(
        users=[User(username='local-user', email='user@example.invalid', enabled=True)]
    )
    real_directory_reader(context, [('dn', {})] * 2)
    report = service.run_once(dry_run=False)
    assert report['errors'] == 1 and report['missing'] == 0 and report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


def test_identity_mapping_dry_run_does_not_write():
    service, context = build()
    service.run_once()
    context.accounts.user_dao.update_user.assert_not_called()


@pytest.mark.parametrize('failed_effect', ['cognito', 'group', 'queue', 'event'])
def test_disable_cleanup_is_retried_after_persisted_revocation(failed_effect):
    service, context = build({PREFIX + 'reenable': False})
    stored = use_real_account_transitions(context)
    accounts = context.accounts
    target = {
        'cognito': accounts.user_pool.admin_disable_user,
        'group': accounts.group_dao.update_group,
        'queue': accounts.task_manager.send,
        'event': accounts.evdi_client.publish_user_disabled_event,
    }[failed_effect]
    effect = target.side_effect
    target.side_effect = RuntimeError('transient effect failure')
    assert service.run_once(dry_run=False)['errors'] == 1
    assert stored['user0']['enabled'] is False
    assert stored['user0']['disable_pending'] is True
    target.side_effect = effect
    assert service.run_once(dry_run=False)['errors'] == 0
    assert stored['user0']['disable_pending'] is False
    accounts.evdi_client.publish_user_disabled_event.assert_called_with(
        username='user0'
    )
    count = accounts.evdi_client.publish_user_disabled_event.call_count
    service.run_once(dry_run=False)
    assert accounts.evdi_client.publish_user_disabled_event.call_count == count


@pytest.mark.parametrize('control', [544, 514])
@pytest.mark.parametrize('pwd_last_set', [b'0', b'134341266774992927'])
def test_proof_record_uses_real_conversion_and_persisted_identity(
    control, pwd_last_set
):
    service, context = build(
        users=[
            User(username='proofuser', email='proofuser@example.invalid', enabled=True)
        ]
    )
    stored = use_real_account_transitions(context)
    attrs = {
        'objectClass': [b'top', b'person', b'organizationalPerson', b'user'],
        'cn': [b'proofuser'],
        'sn': [b'proofuser'],
        'sAMAccountName': [b'proofuser'],
        'mail': [b'proofuser@example.invalid'],
        'objectGUID': [bytes(range(16))],
        'userAccountControl': [str(control).encode()],
        'uidNumber': [b'5001'],
        'gidNumber': [b'5001'],
        'unixHomeDirectory': [b'/home/proofuser'],
        'loginShell': [b'/bin/bash'],
        'pwdLastSet': [pwd_last_set],
    }
    client = real_directory_reader(context, [('cn=proofuser', attrs)])
    first = service.run_once()
    assert first['errors'] == 0
    assert first['would_disable'] == int(control == 514)
    context.accounts.user_dao.update_user.assert_not_called()
    stored['proofuser']['directory_identity'] = bytes(range(16)).hex()
    second = service.run_once()
    assert second['errors'] == 0
    assert second['would_disable'] == int(control == 514)
    assert 'objectGUID=' in client.search_s.call_args.kwargs['filterstr']
    stored['proofuser'].pop('directory_identity')
    conflict = {**attrs, 'objectGUID': [bytes(reversed(range(16)))]}
    client.search_s.return_value.append(('cn=proofuser\\0ACNF:conflict', conflict))
    report = service.run_once(dry_run=False)
    assert report['errors'] == 1 and report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


@pytest.mark.parametrize(
    'error',
    [
        ValueError('Directory identity must resolve uniquely'),
        ldap.INVALID_CREDENTIALS({'result': 49, 'desc': 'Invalid credentials'}),
        ldap.SERVER_DOWN({'desc': 'Directory unavailable'}),
    ],
)
def test_upstream_warning_reports_exception_class(error):
    service, context = build()
    context.accounts.ldap_client.get_reconcile_user.side_effect = error
    report = service.run_once(dry_run=False)
    assert report['errors'] == 4 and report['refused'] == 1
    assert type(error).__name__ in str(context._logger.lines)
    assert report['changes'][0]['error'] == type(error).__name__
    context.accounts.disable_user.assert_not_called()


def test_upstream_warning_redacts_http_urls():
    service, context = build()
    context.accounts.ldap_client.get_reconcile_user.side_effect = ValueError(
        'Lookup failed at https://token@example.invalid/private'
    )
    service.run_once()
    logs = str(context._logger.lines)
    assert 'ValueError' in logs
    assert 'token@' not in logs


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('inventory_enabled', [False, True])
def test_disabled_directory_dry_run_then_apply(bound, inventory_enabled):
    from copy import deepcopy
    from contextlib import nullcontext

    from ideaclustermanager.app.accounts.ldapclient.abstract_ldap_client import (
        AbstractLDAPClient,
    )

    service, context = build(
        users=[User(username='proofuser', email='proof@example.invalid', enabled=True)],
        values={PREFIX + 'max_disable_fraction': '1'},
    )
    stored = use_real_account_transitions(context)
    guid = bytes.fromhex('0123456789abcdef00282a295cff807f')
    if bound:
        stored['proofuser']['directory_identity'] = guid.hex()
    original = deepcopy(stored)
    # Model a DAO returning shared metadata, and a stale eventually consistent scan.
    context.accounts.user_dao.get_user.side_effect = lambda username: stored[username]
    context.accounts.list_users.side_effect = [
        ListUsersResult(
            listing=[
                User(
                    username='proofuser', email='proof@example.invalid', enabled=enabled
                )
            ]
        )
        for enabled in (True, inventory_enabled)
    ]
    client = real_directory_reader(context, [])
    connection = Mock()
    client.get_ldap_root_connection.return_value = nullcontext(connection)
    client.search_s.side_effect = lambda **kwargs: AbstractLDAPClient.search_s(
        client, **kwargs
    )
    selectors = []

    def search(base, scope, filterstr, attrlist, attrsonly):
        selectors.append(filterstr)
        expected = (
            '(objectGUID=\\01\\23\\45\\67\\89\\ab\\cd\\ef\\00\\28\\2a\\29\\5c\\ff\\80\\7f)'
            if bound
            else '(mail=proof@example.invalid)'
        )
        assert filterstr == f'(&(objectClass=user){expected})'
        return [
            (
                'cn=proofuser',
                {
                    'objectGUID': [guid],
                    'sAMAccountName': [b'proofuser'],
                    'userAccountControl': [b'514'],
                },
            )
        ]

    connection.search_s.side_effect = search
    preview = service.run_once()
    assert preview['would_disable'] == 1
    assert preview['errors'] == 0
    assert stored == original
    context.accounts.user_dao.update_user.assert_not_called()
    context.accounts.disable_user.assert_not_called()
    applied = service.run_once(dry_run=False)
    assert applied['errors'] == 0 and applied['refused'] == 0
    assert applied['eligible_enabled'] == 1
    assert applied['disabled'] == 1
    assert stored['proofuser']['enabled'] is False
    assert stored['proofuser']['directory_identity'] == guid.hex()
    assert stored['proofuser']['reconcile_sources'] == ['directory']
    context.accounts.user_pool.admin_disable_user.assert_called_once_with('proofuser')
    context.accounts.evdi_client.publish_user_disabled_event.assert_called_once_with(
        username='proofuser'
    )
    assert len(selectors) == 2
    assert context._lock.held == []


def test_current_administrator_disable_overrides_stale_enabled_inventory():
    service, context = build()
    context.accounts.user_dao.get_user.side_effect = lambda username: {
        'enabled': False,
        'reconcile_sources': [],
    }
    report = service.run_once(dry_run=False)
    assert report['eligible_enabled'] == 0
    assert report['changes'] == []
    context.accounts.ldap_client.get_reconcile_user.assert_not_called()
    context.accounts.disable_user.assert_not_called()
    context.accounts.enable_user.assert_not_called()


def test_deleted_before_first_run_is_missing_and_proposes_disable():
    service, context = build(
        users=[
            User(username='deleted-user', email='deleted@example.invalid', enabled=True)
        ]
    )
    client = real_directory_reader(context, [])
    report = service.run_once()
    assert report['errors'] == 0 and report['missing'] == 1
    assert report['would_disable'] == 1
    assert (
        '(mail=deleted@example.invalid)'
        in (client.search_s.call_args_list[0].kwargs['filterstr'])
    )
    assert (
        '(sAMAccountName=deleted-user)'
        in (client.search_s.call_args_list[1].kwargs['filterstr'])
    )
    context.accounts.disable_user.assert_not_called()


def test_first_lookup_falls_back_to_escaped_username():
    service, context = build(
        users=[User(username='local*(user)', email='old@example.invalid', enabled=True)]
    )
    client = real_directory_reader(context, [])
    attrs = {'objectGUID': [bytes(range(16))], 'userAccountControl': [b'512']}
    client.search_s.side_effect = [[], [('dn', attrs)]]
    assert service.run_once()['errors'] == 0
    assert (
        r'(sAMAccountName=local\2a\28user\29)'
        in (client.search_s.call_args.kwargs['filterstr'])
    )


def test_ambiguous_email_does_not_block_other_offboards_at_cap():
    service, context = build(
        users=[
            User(username=f'user{i}', email=f'user{i}@example.invalid', enabled=True)
            for i in range(4)
        ]
    )
    client = real_directory_reader(context, [])
    attrs = {'objectGUID': [bytes(range(16))], 'userAccountControl': [b'512']}

    def search(**kwargs):
        selector = kwargs['filterstr']
        if 'user0' in selector:
            return []
        if 'user1' in selector:
            assert 'mail=' in selector
            return [('dn', attrs), ('other', attrs)]
        return [('dn', attrs)]

    client.search_s.side_effect = search
    report = service.run_once(dry_run=False)
    assert report['errors'] == 1 and report['refused'] == 0
    assert report['disabled'] == 1
    context.accounts.disable_user.assert_called_once_with(
        'user0', preserve_directory=True, reconcile_sources=['directory']
    )
    assert report['changes'][1]['action'] == 'error'
    assert report['changes'][1]['error'] == 'ValueError'


@pytest.mark.parametrize('override', [False, True])
def test_error_fraction_above_cap_refuses(override):
    service, context = build(records={'user0': None, 'user1': {}, 'user2': {}})
    report = service.run_once(dry_run=False, override_max_disable_fraction=override)
    assert report['errors'] == 2 and report['refused'] == 1
    assert report['reason'] == 'upstream error fraction exceeded'
    context.accounts.disable_user.assert_not_called()


@pytest.mark.parametrize(
    'error',
    [
        ldap.SERVER_DOWN,
        ldap.TIMEOUT,
        ldap.CONNECT_ERROR,
        ldap.UNAVAILABLE,
        ldap.INVALID_CREDENTIALS,
    ],
)
def test_directory_unreachable_refuses_even_with_one_error_and_override(error):
    service, context = build()
    enabled = {'directory_identity': '00' * 16, 'user_account_control': 512}
    context.accounts.ldap_client.get_reconcile_user.side_effect = [
        None,
        error('Directory unavailable'),
        enabled,
        enabled,
    ]
    report = service.run_once(dry_run=False, override_max_disable_fraction=True)
    assert report['errors'] == 1 and report['reason'] == 'directory unreachable'
    context.accounts.disable_user.assert_not_called()


def test_settings_write_wakes_worker_and_shorter_interval_is_used_immediately():
    import arrow

    service, context = build(
        {PREFIX + 'enabled': True, PREFIX + 'interval_minutes': 60}
    )
    context._config.db.values[PREFIX + 'last_completed'] = (
        arrow.utcnow().timestamp() - 120
    )
    assert service.run_once(periodic=True) == {'skipped': 'interval'}
    context._config.db.values[PREFIX + 'interval_minutes'] = 1
    context._config.db.values[PREFIX + 'dry_run'] = False
    service.settings_changed()
    assert service._wake.is_set()
    assert service.run_once(periodic=True)['disabled'] == 1
    context._config.db.values[PREFIX + 'enabled'] = False
    assert service.run_once(periodic=True) == {'skipped': 'disabled'}


def test_manual_run_reads_current_settings_and_saves_report_without_delaying_periodic_run():
    service, context = build(
        {PREFIX + 'max_disable_fraction': 0}, records={'user0': None}
    )
    context._config.db.values[PREFIX + 'max_disable_fraction'] = 1
    report = service.run_once(dry_run=False)
    assert report['disabled'] == 1
    assert context._config.db.values[PREFIX + 'last_run']['report']['disabled'] == 1
    assert PREFIX + 'last_completed' not in context._config.db.values


@pytest.mark.parametrize('dry_run', [True, False])
def test_manual_modes_run_with_scheduling_disabled_and_preserve_checkpoint(dry_run):
    service, context = build(
        {
            PREFIX + 'enabled': False,
            PREFIX + 'max_disable_fraction': 1,
            PREFIX + 'last_completed': 123,
        },
        records={'user0': None},
        users=[
            User(username='cluster-admin', enabled=True),
            User(username='user0', enabled=True),
        ],
    )

    report = service.run_once(dry_run=dry_run)

    assert report['dry_run'] is dry_run
    assert report['would_disable'] == 1
    assert report['disabled'] == int(not dry_run)
    assert context.accounts.disable_user.call_count == int(not dry_run)
    assert context._config.db.values[PREFIX + 'last_completed'] == 123
    assert context._config.db.values[PREFIX + 'last_run']['report'] == {
        **report,
        'truncated': False,
    }
    assert all(row.get('username') != 'cluster-admin' for row in report['changes'])


def test_worker_wait_is_interrupted_by_save_and_stop():
    from threading import Event

    service, context = build()
    first_run = Event()
    second_run = Event()

    def run(**kwargs):
        if first_run.is_set():
            second_run.set()
        else:
            first_run.set()

    service.run_once = Mock(side_effect=run)
    service.start()
    try:
        assert first_run.wait(5)
        service.settings_changed()
        assert second_run.wait(5)
    finally:
        service.stop()
    assert not service._thread.is_alive()


def test_invalid_header_never_discloses_credentials():
    import requests

    service, context = build()
    context.accounts.ldap_client.get_reconcile_user.side_effect = (
        requests.exceptions.InvalidHeader(
            'Invalid header value: Authorization: SSWS TOKEN\nSECOND_LINE'
        )
    )
    report = service.run_once(dry_run=False)
    assert report['changes'][0]['error'] == 'InvalidHeader'
    for value in (
        str(report),
        str(context._logger.lines),
        str(context.config().db.values),
    ):
        assert 'TOKEN' not in value
        assert 'SECOND_LINE' not in value


def test_http_failure_reports_status_without_message():
    import requests

    service, context = build()
    response = requests.Response()
    response.status_code = 403
    context.accounts.ldap_client.get_reconcile_user.side_effect = requests.HTTPError(
        'credential', response=response
    )
    assert service.run_once()['changes'][0]['error'] == 'HTTPError (HTTP 403)'
