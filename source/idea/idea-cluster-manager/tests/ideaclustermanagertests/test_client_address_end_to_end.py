"""
PROVEN ONLY ON THE MERGED TREE (fix20-cm-server + fix20-cm-accounts).

A sign-in page request goes through the real SDK server, which records the client address
(fix20-cm-server: SocaServer.client_address), to the real Auth API and accounts service,
which count refused codes per (username, address) (fix20-cm-accounts:
AccountsService._password_refused). On either branch alone one half is missing and the
test skips; the merge worker runs it with the cluster-manager suite.
"""

import http.client
import ipaddress
import json
import socket
import time
from unittest.mock import Mock

import pytest

from ideaclustermanager import AppContext
from ideaclustermanager.app.accounts.accounts_service import AccountsService
from ideaclustermanager.app.api.auth_api import AuthAPI
from ideaclustermanagertests.fake_secrets_manager import client_error
from ideasdk.protocols import ApiInvokerProtocol
from ideasdk.server import SocaServer, SocaServerOptions

pytestmark = pytest.mark.skipif(
    not hasattr(SocaServer, 'client_address')
    or not hasattr(AccountsService, 'PASSWORD_REFUSALS_PER_USERNAME'),
    reason='needs fix20-cm-server and fix20-cm-accounts together: run on the merged tree',
)


class SignInPageInvoker(ApiInvokerProtocol):
    def __init__(self, context):
        self.auth_api = AuthAPI(context)

    def get_token_service(self):
        return None

    def get_namespaces(self):
        return {'Auth.ConfirmForgotPassword'}

    def invoke(self, context):
        self.auth_api.invoke(context)


@pytest.fixture(scope='module')
def running_server(context: AppContext):
    # one per module: Sanic allows one app of each name in a process
    with socket.socket() as probe:
        probe.bind(('localhost', 0))
        port = probe.getsockname()[1]
    server = SocaServer(
        context=context,
        api_invoker=SignInPageInvoker(context),
        options=SocaServerOptions(
            enable_http=True,
            hostname='localhost',
            port=port,
            enable_unix_socket=False,
            enable_openapi_spec=False,
            enable_metrics=False,
            graceful_shutdown_timeout=1,
            api_path_prefixes=['/cluster-manager'],
        ),
    )
    server.initialize()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def sign_in_server(context: AppContext, running_server, monkeypatch, tmp_path):
    from ideadatamodel import CognitoUserPoolPasswordPolicy

    # the accounts tests' sign-in page setup: a known user and a user pool refusing the code
    monkeypatch.setattr(
        context.accounts,
        '_password_policy',
        (CognitoUserPoolPasswordPolicy(minimum_length=1), time.monotonic() + 1e9),
        raising=False,
    )
    monkeypatch.setattr(context.accounts, 'ds_automation_dir', str(tmp_path))
    monkeypatch.setattr(context.accounts, '_next_password_secret_sweep', 0.0)
    fake = context.aws().secretsmanager()
    for name, value in (
        ('secrets', {}),
        ('created', []),
        ('deleted', []),
        ('list_calls', []),
        ('failing', {}),
        ('undeletable', set()),
    ):
        monkeypatch.setattr(fake, name, value)
    user_pool = Mock()
    monkeypatch.setattr(context.accounts, 'user_pool', user_pool)
    monkeypatch.setattr(context.accounts.task_manager, 'send', Mock())
    monkeypatch.setattr(context.accounts, '_password_sets_paused_until', 0.0)
    context.accounts._password_refusals.clear()
    users = context.accounts.user_dao.table
    users.put_item(Item={'username': 'user', 'enabled': True})
    user_pool.confirm_forgot_password.side_effect = client_error(
        'CodeMismatchException', 'ConfirmForgotPassword'
    )
    try:
        yield running_server, user_pool
    finally:
        users.delete_item(Key={'username': 'user'})
        context.accounts._password_refusals.clear()


def confirm(server, forwarded_for=None):
    body = json.dumps(
        {
            'header': {'namespace': 'Auth.ConfirmForgotPassword'},
            'payload': {
                'username': 'user',
                'password': 'secret-value',
                'confirmation_code': '123456',
            },
        }
    ).encode()
    headers = {'Content-Type': 'application/json'}
    if forwarded_for:
        headers['X-Forwarded-For'] = forwarded_for
    connection = http.client.HTTPConnection(
        'localhost', server.options.port, timeout=30
    )
    try:
        connection.request(
            'POST', '/cluster-manager/api/v1', body=body, headers=headers
        )
        return json.loads(connection.getresponse().read())
    finally:
        connection.close()


def test_refused_codes_through_the_load_balancer_count_per_client_address(
    context: AppContext, sign_in_server, monkeypatch
):
    server, user_pool = sign_in_server
    allowed = context.accounts.PASSWORD_REFUSALS_ALLOWED
    # this test's connections stand in for the public load balancer's
    monkeypatch.setattr(
        server,
        '_load_balancer_networks',
        lambda: [ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128')],
    )

    # someone else's attempts, from their own address: an earlier entry they wrote changes nothing
    for _ in range(allowed + 3):
        answer = confirm(server, '198.51.100.1, 192.0.2.1')
        assert answer['success'] is False
    assert user_pool.confirm_forgot_password.call_count == allowed

    # the account's owner, from theirs, still reaches the user pool, and the right code works
    user_pool.confirm_forgot_password.side_effect = None
    answer = confirm(server, '203.0.113.7')
    assert answer['success'] is True
    assert user_pool.confirm_forgot_password.call_count == allowed + 1


def test_a_forged_forwarded_for_straight_to_the_server_counts_against_the_socket(
    context: AppContext, sign_in_server, monkeypatch
):
    server, user_pool = sign_in_server
    allowed = context.accounts.PASSWORD_REFUSALS_ALLOWED
    # straight to port 8443 from inside the VPC: no load balancer subnet
    monkeypatch.setattr(server, '_load_balancer_networks', lambda: [])

    for attempt in range(allowed + 3):
        # a new made-up address each time does not buy new attempts
        confirm(server, f'192.0.2.{attempt}')
    assert user_pool.confirm_forgot_password.call_count == allowed


def test_with_no_trusted_load_balancer_only_the_per_username_bound_applies(
    context: AppContext, sign_in_server, monkeypatch
):
    server, user_pool = sign_in_server
    allowed = context.accounts.PASSWORD_REFUSALS_ALLOWED
    ceiling = context.accounts.PASSWORD_REFUSALS_PER_USERNAME
    # subnets not readable, or the external load balancer internal: the server records
    # no address (None), and the accounts service applies only the per-username bound,
    # so attempts through one load balancer node do not share the low per-address one
    monkeypatch.setattr(server, '_load_balancer_networks', lambda: None)

    for _ in range(allowed + 3):
        confirm(server, '192.0.2.1')
    assert user_pool.confirm_forgot_password.call_count == allowed + 3

    for _ in range(ceiling):
        confirm(server, '192.0.2.1')
    assert user_pool.confirm_forgot_password.call_count == ceiling
