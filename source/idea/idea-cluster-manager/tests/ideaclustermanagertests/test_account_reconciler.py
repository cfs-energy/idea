from unittest.mock import Mock

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
            **(values or {}),
        }
    )
    users = users or [User(username=f'user{index}', enabled=True) for index in range(4)]
    records = (
        records if records is not None else {'user0': {'user_account_control': 514}}
    )
    accounts = Mock()
    accounts.ldap_client.ldap_root_username = 'bind-service'
    accounts.ldap_client.get_user.side_effect = lambda username, **kw: records.get(
        username, {'user_account_control': 512}
    )
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
    context.accounts.disable_user.assert_called_once_with('user0')
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
def test_incomplete_read_refuses_whole_run(control):
    service, context = build(
        records={'user0': None, 'user1': {'user_account_control': control}}
    )
    report = service.run_once(dry_run=False)
    assert report['refused'] == 1 and report['errors'] == 1
    context.accounts.disable_user.assert_not_called()


@pytest.mark.parametrize('reenable', [False, True])
def test_reenable(reenable):
    service, context = build(
        {PREFIX + 'reenable': reenable, PREFIX + 'check_cognito': True},
        records={},
        users=[User(username='user0', enabled=False)],
    )
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
    service, context = build()
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


def test_reenable_defaults_to_directory_state():
    service, context = build(records={}, users=[User(username='user0', enabled=False)])
    assert service.run_once(dry_run=False)['reenabled'] == 1
    context.accounts.enable_user.assert_called_once_with('user0')


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
        records={'user0': None, 'user1': {'user_account_control': None}}
    )
    report = service.run_once(dry_run=False, override_max_disable_fraction=True)
    assert report['refused'] == 1
    context.accounts.disable_user.assert_not_called()


def test_periodic_never_overrides():
    service, context = build(
        {PREFIX + 'dry_run': False}, records={'user0': None, 'user1': None}
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
