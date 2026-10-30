import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import asyncssh
import boto3
import pytest
from cryptography.hazmat.primitives.serialization import load_ssh_public_identity
from ideaclustermanager.app.api.api_invoker import ClusterManagerApiInvoker
from ideaclustermanager.app.api.terminal_api import TerminalAPI
from ideaclustermanager.app.terminal import service as module
from ideaclustermanager.app.terminal.service import (
    TerminalService,
    certificate,
    check_origin,
    validate_desktop,
)
from ideaclustermanager.app.terminal.store import TerminalDenied, TerminalStore
from moto import mock_aws
from ideasdk.server.soca_server import ServerOptionsProvider, SocaServerOptions


ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def store():
    with mock_aws():
        table = boto3.resource('dynamodb', region_name='us-east-1').create_table(
            TableName='terminal',
            AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}],
            BillingMode='PAY_PER_REQUEST',
        )
        yield TerminalStore(table, clock=lambda: 1000)


def test_ticket_single_use_and_exact_expiry(store):
    ticket = store.issue('alice', 'bastion', 'client')
    store.consume(ticket, 'alice', 'bastion', 'client')
    with pytest.raises(TerminalDenied):
        store.consume(ticket, 'alice', 'bastion', 'client')
    ticket = store.issue('alice', 'bastion', 'client')
    store.clock = lambda: 1030
    with pytest.raises(TerminalDenied):
        store.consume(ticket, 'alice', 'bastion', 'client')


@pytest.mark.parametrize(
    'binding',
    [
        ('bob', 'bastion', 'client'),
        ('alice', 'desktop:one', 'client'),
        ('alice', 'bastion', 'stolen'),
    ],
)
def test_ticket_binding(store, binding):
    ticket = store.issue('alice', 'bastion', 'client')
    with pytest.raises(TerminalDenied):
        store.consume(ticket, *binding)
    # A failed redemption also consumes it.
    with pytest.raises(TerminalDenied):
        store.consume(ticket, 'alice', 'bastion', 'client')


def test_slots_shared_across_replicas_and_lost_lease(store):
    other = TerminalStore(store.table, clock=store.clock)
    slots = [store.acquire('alice', str(i)) for i in range(3)]
    with pytest.raises(TerminalDenied):
        other.acquire('alice', 'four')
    other.acquire('bob', 'separate')
    store.release(slots[0], 'wrong-owner')
    with pytest.raises(TerminalDenied):
        other.acquire('alice', 'four')
    store.clock = other.clock = lambda: 1061
    assert other.acquire('alice', 'replacement') == slots[0]
    with pytest.raises(Exception):
        store.renew(slots[0], '0')
    store.release(slots[0], '0')
    other.renew(slots[0], 'replacement')


@pytest.mark.parametrize(
    'origin',
    [
        None,
        'null',
        'http://portal.example',
        'https://evil.example',
        'https://portal.example.evil',
        'https://portal.example/',
    ],
)
def test_origin_rejected(origin):
    with pytest.raises(TerminalDenied):
        check_origin(origin, 'https://portal.example')


def test_origin_exact():
    check_origin('https://portal.example', 'https://portal.example/portal')


def test_certificate_limits():
    ca = asyncssh.generate_private_key('ssh-ed25519')
    key, cert = certificate(ca, 'alice', 'session-one', now=1000)
    assert key.get_algorithm() == 'ssh-ed25519'
    assert cert.principals == ['idea:alice']
    parsed = load_ssh_public_identity(cert.export_certificate())
    parsed.verify_cert_signature()
    assert parsed.valid_after == 940 and parsed.valid_before == 1300
    assert parsed.key_id == b'web-terminal:session-one'
    assert parsed.extensions == {b'permit-pty': b''}
    assert parsed.critical_options == {}
    assert certificate(ca, 'alice', 'session-two', now=1000)[0] != key


@pytest.mark.parametrize(
    'change',
    [
        {'owner': 'bob'},
        {'state': 'ERROR'},
        {'state': 'DELETING'},
        {'state': 'DELETED'},
        {'base_os': 'windows2025'},
        {'server': {}},
    ],
)
def test_desktop_boundary(change):
    row = {
        'owner': 'alice',
        'state': 'READY',
        'base_os': 'ubuntu2404',
        'server': {'instance_id': 'i-abc'},
    }
    assert validate_desktop(row, 'alice') == 'i-abc'
    with pytest.raises(TerminalDenied):
        validate_desktop({**row, **change}, 'alice')


def test_enabled_account_and_no_client_hosts():
    service = TerminalService(MagicMock())
    service.initialized = True
    service.context = MagicMock()
    service.context.config().get_bool.return_value = True
    service.context.accounts.get_user.return_value = SimpleNamespace(
        username='alice', enabled=False
    )
    with pytest.raises(TerminalDenied):
        service.target('alice', 'bastion')
    service.context.accounts.get_user.return_value.enabled = True
    with pytest.raises(TerminalDenied):
        service.target('alice', 'ssh://example.com')
    service.context.config().get_bool.return_value = False
    with pytest.raises(TerminalDenied):
        service.target('alice', 'bastion')


def test_api_auth_and_redaction():
    context = MagicMock()
    invocation = MagicMock(
        namespace='Terminal.CreateTicket',
        request_payload={'target': 'bastion', 'client': 'a' * 64},
    )
    invocation.is_unix_domain_socket_invocation.return_value = False
    invocation.get_username.return_value = 'alice'
    TerminalAPI(context).invoke(invocation)
    context.terminal.create_ticket.assert_called_once_with('alice', 'bastion', 'a' * 64)
    invocation.has_access_token.return_value = False
    with pytest.raises(Exception):
        TerminalAPI(context).invoke(invocation)
    invoker = object.__new__(ClusterManagerApiInvoker)
    invocation.get_request.return_value = {'payload': {'client': 'secret'}}
    invocation.get_response.return_value = {'payload': {'ticket': 'secret'}}
    assert invoker.get_request_logging_payload(invocation)['payload'] == {
        'redacted': True
    }
    assert invoker.get_response_logging_payload(invocation)['payload'] == {
        'redacted': True
    }


class Socket:
    def __init__(self, hello):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps(hello))
        self.sent = []
        self.closed = None

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes):
            self.incoming.put_nowait(json.dumps({'type': 'ack'}))
            self.incoming.put_nowait(
                json.dumps({'type': 'input', 'data': 'private-keystrokes\n'})
            )

    async def close(self, **kwargs):
        self.closed = kwargs


@pytest.mark.parametrize(
    'mode',
    [
        'shell-exited',
        'idle-timeout',
        'max-duration',
        'bad-frame',
        'extra-input-field',
        'extra-resize-field',
        'surrogate-input',
        'wrong-host-key',
        'trust-not-ready',
        'disconnected',
        'socket-closed',
        'ack-timeout',
        'server-shutdown',
        'stalled-stdin',
        'lease-login',
        'lease-watchdog',
        'account-deleted',
    ],
)
def test_real_ssh_bridge_and_audit_without_content(store, monkeypatch, caplog, mode):
    async def run():
        ca = asyncssh.generate_private_key('ssh-ed25519')
        host_key = asyncssh.generate_private_key('ssh-ed25519')

        async def shell(process):
            assert process.command == 'tmux new -A -s web'
            assert process.get_terminal_type() == 'xterm-256color'
            process.stdout.write(b'private-terminal-output')
            if mode == 'shell-exited':
                assert await process.stdin.readline() == b'private-keystrokes\n'
                process.exit(0)
            elif mode == 'surrogate-input':
                assert await process.stdin.readexactly(1) == b'?'
                process.exit(0)
            elif mode == 'stalled-stdin':
                process.channel.pause_reading()
                await asyncio.Event().wait()
            else:
                await process.stdin.read()

        trusted_ca = (
            asyncssh.generate_private_key('ssh-ed25519')
            if mode == 'trust-not-ready'
            else ca
        )
        authorized = asyncssh.import_authorized_keys(
            'cert-authority,principals="idea:alice" '
            + trusted_ca.export_public_key().decode()
        )
        server = await asyncssh.listen(
            '127.0.0.1',
            0,
            server_host_keys=[host_key],
            authorized_client_keys=authorized,
            process_factory=shell,
            encoding=None,
        )
        real_connect = asyncssh.connect
        connections = []
        from asyncssh.misc import async_context_manager

        @async_context_manager
        async def connect(host, **kwargs):
            assert kwargs['known_hosts'][0] == [pin]
            assert kwargs['agent_path'] is None and kwargs['config'] is None
            conn = await real_connect(host, port=server.get_port(), **kwargs)
            connections.append(conn)
            if mode == 'lease-login':
                store.clock = lambda: 1061
            if mode == 'stalled-stdin':
                conn.close = lambda: None
            return conn

        monkeypatch.setattr(module.asyncssh, 'connect', connect)
        monkeypatch.setattr(module, 'WATCHDOG_INTERVAL', 0.05)
        monkeypatch.setattr(module, 'WRITE_TIMEOUT', 0.2, raising=False)
        monkeypatch.setattr(module, 'SHUTDOWN_TIMEOUT', 0.1, raising=False)
        service = TerminalService(MagicMock())
        service.initialized = True
        service.context = MagicMock()
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        service.context.config().get_int.side_effect = lambda name, default: (
            1
            if (mode in {'idle-timeout', 'stalled-stdin'} and 'idle' in name)
            or (mode == 'max-duration' and 'duration' in name)
            else default
        )
        service.logger = logging.getLogger('terminal-test')
        service.store = store
        pin = (
            asyncssh.generate_private_key('ssh-ed25519').convert_to_public()
            if mode == 'wrong-host-key'
            else host_key.convert_to_public()
        )
        service.target = lambda *_: ('127.0.0.1', pin)
        service.load_ca = lambda: ca
        service.user = lambda *_: True
        if mode == 'account-deleted':
            from ideaclustermanager.app.accounts.accounts_service import AccountsService

            accounts = object.__new__(AccountsService)
            accounts.user_dao = MagicMock()
            accounts.user_dao.get_user.return_value = None
            service.context.accounts = accounts
            service.user = TerminalService.user.__get__(service)
        ticket = store.issue('alice', 'bastion', 'client')
        ws = Socket(
            dict(ticket=ticket, username='alice', target='bastion', client='client')
        )
        if mode != 'shell-exited':

            async def send(value):
                ws.sent.append(value)
                if mode == 'lease-watchdog' and value == json.dumps({'type': 'ready'}):
                    store.clock = lambda: 1061
                if isinstance(value, bytes) and mode in {
                    'disconnected',
                    'socket-closed',
                }:
                    from sanic.exceptions import WebsocketClosed

                    ws.incoming.put_nowait(
                        asyncio.CancelledError()
                        if mode == 'disconnected'
                        else WebsocketClosed('client closed')
                    )
                elif isinstance(value, bytes) and mode == 'server-shutdown':
                    service.stopping = True
                    handler.cancel()
                elif isinstance(value, bytes) and mode == 'stalled-stdin':
                    ws.incoming.put_nowait(json.dumps({'type': 'ack'}))
                    for _ in range(300):
                        ws.incoming.put_nowait(
                            json.dumps(
                                {'type': 'input', 'data': 'x' * module.CHUNK_SIZE}
                            )
                        )
                elif isinstance(value, bytes) and mode in {
                    'idle-timeout',
                    'max-duration',
                }:
                    ws.incoming.put_nowait(json.dumps({'type': 'ack'}))
                if mode == 'surrogate-input' and isinstance(value, bytes):
                    ws.incoming.put_nowait(json.dumps({'type': 'ack'}))
                    ws.incoming.put_nowait(
                        json.dumps({'type': 'input', 'data': '\ud800'})
                    )
                if isinstance(value, bytes) and mode in {
                    'extra-input-field',
                    'extra-resize-field',
                }:
                    frame = (
                        {'type': 'input', 'data': 'x', 'seq': 1}
                        if mode == 'extra-input-field'
                        else {'type': 'resize', 'cols': 90, 'rows': 30, 'seq': 1}
                    )
                    ws.incoming.put_nowait(json.dumps(frame))
                if mode == 'bad-frame' and isinstance(value, bytes):
                    ws.incoming.put_nowait('x' * (module.FRAME_LIMIT + 1))

            ws.send = send
        handler = asyncio.create_task(
            service.websocket(
                SimpleNamespace(
                    headers={'origin': 'https://portal.example'},
                    conn_info=SimpleNamespace(lost=False),
                ),
                ws,
            )
        )
        try:
            done, _ = await asyncio.wait([handler], timeout=4)
            assert done, 'bridge teardown stalled with buffered SSH input'
            if mode == 'server-shutdown':
                with pytest.raises(asyncio.CancelledError):
                    handler.result()
            else:
                handler.result()
            if mode == 'stalled-stdin':
                assert connections and connections[0].is_closed()
        finally:
            for conn in connections:
                conn.abort()
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            server.close()
            await server.wait_closed()
        if mode not in {
            'wrong-host-key',
            'lease-login',
            'trust-not-ready',
        }:
            assert b'private-terminal-output' in ws.sent
        assert ws.closed['reason'] == {
            'bad-frame': 'access-denied',
            'extra-input-field': 'access-denied',
            'extra-resize-field': 'access-denied',
            'surrogate-input': 'shell-exited',
            'wrong-host-key': 'target-misconfigured',
            'socket-closed': 'disconnected',
            'stalled-stdin': 'connection-failed',
            'lease-login': 'unavailable',
            'lease-watchdog': 'unavailable',
            'account-deleted': 'access-denied',
        }.get(mode, mode)
        if mode in {'disconnected', 'socket-closed'}:
            assert ws.closed['code'] == 1000
        if mode in {'lease-login', 'lease-watchdog', 'trust-not-ready'}:
            assert ws.closed['code'] == 4002
        if mode == 'account-deleted':
            assert ws.closed['code'] == 4003
        if mode == 'ack-timeout':
            assert ws.closed['code'] == 4005
        assert store.table.scan()['Count'] == 0

    caplog.set_level(logging.INFO, logger='terminal-test')
    asyncio.run(run())
    if mode == 'wrong-host-key':
        assert (
            'reason=target-misconfigured error_type=HostKeyNotVerifiable' in caplog.text
        )
    started = mode not in {
        'wrong-host-key',
        'lease-login',
        'trust-not-ready',
    }
    assert ('terminal start user=alice' in caplog.text) == started
    assert ('terminal stop user=alice' in caplog.text) == started
    assert (
        'private-terminal-output' not in caplog.text
        and 'private-keystrokes' not in caplog.text
    )


def test_ca_first_boot_is_atomic_and_private_material_stays_out_of_settings(store):
    service = TerminalService(MagicMock())
    service.initialized = True
    service.context = MagicMock()
    service.context.cluster_name.return_value = 'example'
    service.context.module_id.return_value = 'cluster-manager'
    settings = MagicMock()
    service.context.aws().dynamodb_table().Table.return_value = settings
    secrets = service.context.aws().secretsmanager()
    winner = asyncssh.generate_private_key('ssh-ed25519')
    secrets.get_secret_value.side_effect = [
        {'SecretString': '{"schema":1}'},
        {'SecretString': winner.export_private_key().decode()},
    ]
    from botocore.exceptions import ClientError

    secrets.put_secret_value.side_effect = ClientError(
        {'Error': {'Code': 'ResourceExistsException'}}, 'PutSecretValue'
    )
    service.load_ca(publish=True)
    row = settings.update_item.call_args.kwargs['ExpressionAttributeValues']
    assert row[':v'] == winner.export_public_key().decode().strip()
    assert 'PRIVATE' not in str(row)
    assert secrets.get_secret_value.call_args.kwargs == {
        'SecretId': 'example-cluster-manager-terminal-ca'
    }


def test_origin_middleware_runs_before_the_websocket_upgrade(caplog):
    service = TerminalService(MagicMock())
    service.initialized = True
    service.context = MagicMock()
    service.context.config().get_cluster_external_endpoint.return_value = (
        'https://portal.example'
    )
    service.enabled = lambda: True
    service.logger = logging.getLogger('terminal-origin-test')
    caplog.set_level(logging.WARNING, logger='terminal-origin-test')
    server = SimpleNamespace(
        options=ServerOptionsProvider(
            service.context,
            SocaServerOptions(
                enable_http=True,
                enable_web_sockets=False,
                api_path_prefixes=['/cluster-manager'],
            ),
        ),
        http_app=MagicMock(),
    )
    service.register(server)
    assert server.options.enable_web_sockets is True
    assert server.http_app.add_websocket_route.call_count == 1
    assert (
        server.http_app.add_websocket_route.call_args.args[1]
        == '/cluster-manager/terminal'
    )
    middleware = server.http_app.register_middleware.call_args.args[0]
    request = SimpleNamespace(
        path='/cluster-manager/terminal', headers={'origin': 'https://evil.example'}
    )
    assert asyncio.run(middleware(request)).status == 403
    assert (
        'terminal request rejected reason=access-denied error_type=TerminalDenied'
        in caplog.text
    )
    assert 'evil.example' not in caplog.text
    request.headers['origin'] = 'https://portal.example'
    assert asyncio.run(middleware(request)) is None


def test_host_bootstrap_only_distributes_public_trust(tmp_path):
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    path = (
        Path(__file__).resolve().parents[5]
        / 'source/idea/idea-bootstrap/common/terminal_host.py'
    )
    spec = importlib.util.spec_from_file_location('terminal_host_test', path)
    helper = importlib.util.module_from_spec(spec)
    # Import without leaving bytecode in the bootstrap archive source tree.
    exec(compile(path.read_text(), str(path), 'exec'), helper.__dict__)
    public = (
        asyncssh.generate_private_key('ssh-ed25519')
        .export_public_key()
        .decode()
        .strip()
    )
    ssh = tmp_path / 'etc/ssh'
    ssh.mkdir(parents=True)
    (ssh / 'sshd_config').write_text(
        'PasswordAuthentication no\nUsePAM yes\nMatch User guest\n  X11Forwarding no\n'
    )
    (ssh / 'ssh_host_ed25519_key.pub').write_text(public)
    with (
        patch.object(helper.subprocess, 'run') as validate,
        patch.object(
            helper, 'dynamodb', return_value={'Item': {'value': {'S': public}}}
        ) as ddb,
    ):
        helper.configure('example', 'bastion', root=tmp_path)
        helper.configure('example', 'bastion', root=tmp_path)
    config = (ssh / 'sshd_config').read_text()
    assert 'TrustedUserCAKeys /etc/ssh/idea_terminal_ca.pub\n' in config
    assert (
        'AuthorizedPrincipalsCommand /etc/ssh/idea_terminal_principals %u %K' in config
    )
    assert config.count('TrustedUserCAKeys') == 1
    assert 'PasswordAuthentication no\nUsePAM yes' in config
    assert ddb.call_args.kwargs['Item'] == {
        'pk': {'S': 'host:bastion'},
        'public_key': {'S': public},
    }
    assert any(
        call.args[0][:2] == ['ssh-keygen', '-l'] for call in validate.call_args_list
    )
    assert not list(ssh.glob('*private*'))


@pytest.mark.parametrize(
    'code', ['AccessDeniedException', 'KMSInvalidStateException', 'ThrottlingException']
)
def test_ca_write_preserves_cause(code):
    from botocore.exceptions import ClientError

    service = TerminalService(MagicMock())
    service.initialized = True
    service.context = MagicMock()
    secrets = service.context.aws().secretsmanager()
    secrets.get_secret_value.return_value = {'SecretString': '{"schema":1}'}
    error = ClientError(
        {'Error': {'Code': code, 'Message': 'write failed'}}, 'PutSecretValue'
    )
    secrets.put_secret_value.side_effect = error
    with pytest.raises(ClientError) as caught:
        service.load_ca()
    assert caught.value is error
    assert secrets.get_secret_value.call_count == 1


def test_initialization_failure_retries_through_user_with_backoff(monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    service = TerminalService(MagicMock())
    service.context.accounts.get_user.return_value = SimpleNamespace(
        username='alice', enabled=True
    )
    service.logger = logging.getLogger('terminal-init-test')
    service.context.aws().dynamodb().describe_time_to_live.return_value = {
        'TimeToLiveDescription': {
            'TimeToLiveStatus': 'ENABLED',
            'AttributeName': 'expires_at',
        }
    }
    service.load_ca = MagicMock(side_effect=RuntimeError('provisioning failure'))
    for _ in range(2):
        with pytest.raises(module.TerminalUnavailable):
            service.user('alice')
    assert service.load_ca.call_count == 1
    assert 'RuntimeError' in caplog.text
    now[0] += 30
    with pytest.raises(module.TerminalUnavailable):
        service.user('alice')
    assert service.load_ca.call_count == 2
    assert len([r for r in caplog.records if 'initialization failed' in r.message]) == 2
    service.load_ca.side_effect = None
    now[0] += 30
    assert service.user('alice').username == 'alice'
    assert service.initialized
    assert service.context.distributed_lock().release.call_count == 3


def test_initialize_even_when_terminal_disabled():
    import ast
    import inspect
    import textwrap
    from ideaclustermanager.app.cluster_manager_app import ClusterManagerApp

    # Initialization must be unconditional in the actual startup path.
    body = (
        ast.parse(textwrap.dedent(inspect.getsource(ClusterManagerApp.app_initialize)))
        .body[0]
        .body
    )
    assert any(
        isinstance(node, ast.Expr)
        and ast.unparse(node) == 'self.context.terminal.initialize()'
        for node in body
    )
    context = MagicMock()
    context.config().get_bool.return_value = False
    service = TerminalService(context)
    service.context.aws().dynamodb().describe_time_to_live.return_value = {
        'TimeToLiveDescription': {
            'TimeToLiveStatus': 'ENABLED',
            'AttributeName': 'expires_at',
        }
    }
    service.load_ca = MagicMock()
    service.initialize()
    assert service.initialized
    service.load_ca.assert_called_once_with(publish=True)
    assert not service.enabled()


@pytest.mark.parametrize(
    'endpoint',
    [
        'https://Portal.Example',
        'https://portal.example:443',
        'https://portal.example./portal',
    ],
)
def test_origin_normalizes_dns_and_default_port(endpoint):
    check_origin('https://portal.example', endpoint)


@pytest.mark.parametrize(
    'origin',
    [
        'https://user@portal.example',
        'https://portal.example?x',
        'https://portal.example#x',
        'https://portal.example:444',
        'https://portal.example:bad',
        'https://portal.example:0',
    ],
)
def test_origin_rejects_non_origin_components(origin):
    with pytest.raises(TerminalDenied):
        check_origin(origin, 'https://portal.example')


def target_service(store):
    service = TerminalService(MagicMock())
    service.initialized = True
    service.user = lambda *_: True
    service.table = store.table
    service.context.cluster_name.return_value = 'example'
    aws = service.context.aws()
    aws.aws_partition.return_value = 'aws-us-gov'
    aws.aws_region.return_value = 'us-gov-west-1'
    aws.aws_account_id.return_value = '123456789012'
    return service


def test_targets_omit_unregistered_desktops(store):
    service = target_service(store)
    rows = [
        {
            'owner': 'alice',
            'state': 'READY',
            'base_os': 'ubuntu2404',
            'idea_session_id': str(i),
            'server': {'instance_id': f'i-{i:03x}'},
        }
        for i in range(102)
    ]
    service.desktops = MagicMock()
    service.desktops().query.return_value = {'Items': rows}
    key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    aws = service.context.aws()
    calls = []

    def batch(**kwargs):
        request = kwargs['RequestItems'][store.table.name]
        assert request['ConsistentRead'] is True
        assert len(request['Keys']) <= 100
        calls.extend(request['Keys'])
        return {
            'Responses': {
                store.table.name: [
                    {
                        'pk': 'host:arn:aws-us-gov:ec2:us-gov-west-1:123456789012:instance/i-001',
                        'public_key': key,
                    }
                ]
            }
        }

    aws.dynamodb_table().batch_get_item.side_effect = batch
    assert service.targets('alice') == [
        {'id': 'bastion', 'label': 'Bastion'},
        {'id': 'desktop:1', 'label': 'My desktop'},
    ]
    assert len(calls) == 102


@pytest.mark.parametrize(
    'response', [{}, {'Reservations': []}, {'Reservations': [{'Instances': []}]}]
)
def test_missing_desktop_instance_is_permanent(store, response):
    service = target_service(store)
    service.desktops = MagicMock()
    service.desktops().get_item.return_value = {
        'Item': {
            'owner': 'alice',
            'state': 'READY',
            'base_os': 'ubuntu2404',
            'server': {'instance_id': 'i-abc'},
        }
    }
    service.context.aws().ec2().describe_instances.return_value = response
    with pytest.raises(TerminalDenied, match='Desktop instance is unavailable'):
        service.target('alice', 'desktop:one')


def test_purged_desktop_instance_is_permanent(store):
    from botocore.exceptions import ClientError

    service = target_service(store)
    service.desktops = MagicMock()
    service.desktops().get_item.return_value = {
        'Item': {
            'owner': 'alice',
            'state': 'READY',
            'base_os': 'ubuntu2404',
            'server': {'instance_id': 'i-abc'},
        }
    }
    service.context.aws().ec2().describe_instances.side_effect = ClientError(
        {'Error': {'Code': 'InvalidInstanceID.NotFound'}}, 'DescribeInstances'
    )
    with pytest.raises(TerminalDenied, match='Desktop instance is unavailable'):
        service.target('alice', 'desktop:one')


@pytest.mark.parametrize('limit, expected', [(0, 1), (2, 2), (50, 10)])
def test_configurable_session_limit(store, limit, expected):
    for i in range(expected):
        store.acquire('alice', str(i), limit)
    with pytest.raises(module.TerminalUnavailable, match=str(expected)):
        store.acquire('alice', 'overflow', limit)


@pytest.mark.parametrize(
    'error, code',
    [
        (module.TerminalUnavailable('Bastion is unavailable'), 'TERMINAL_UNAVAILABLE'),
        (module.TerminalTargetStopped('Desktop is stopped'), 'TERMINAL_TARGET_STOPPED'),
        (module.TerminalTargetError('Desktop is in an error'), 'TERMINAL_TARGET_ERROR'),
        (TerminalDenied('Account is disabled or invalid'), 'UNAUTHORIZED_ACCESS'),
    ],
)
def test_api_preserves_error_category_and_logs_fixed_reason(error, code, caplog):
    from ideadatamodel.exceptions import SocaException

    context = MagicMock()
    context.logger.return_value = logging.getLogger('terminal-api-test')
    context.terminal.create_ticket.side_effect = error
    invocation = MagicMock(
        namespace='Terminal.CreateTicket',
        request_payload={'target': 'bastion', 'client': 'secret-binding'},
    )
    invocation.get_username.return_value = 'alice'
    invocation.is_unix_domain_socket_invocation.return_value = False
    caplog.set_level(logging.INFO, logger='terminal-api-test')
    with pytest.raises(SocaException) as caught:
        TerminalAPI(context).invoke(invocation)
    assert caught.value.error_code == code
    assert str(error) in caplog.text
    assert 'secret-binding' not in caplog.text


def test_bastion_roll_is_retryable(store):
    service = target_service(store)
    service.context.aws().get_client().list_tasks.return_value = {'taskArns': []}
    with pytest.raises(module.TerminalUnavailable, match='Bastion is unavailable'):
        service.target('alice', 'bastion')


@pytest.mark.parametrize(
    'username',
    [
        'root',
        'rocky',
        'ubuntu',
        'almalinux',
        'debian',
        'fedora',
        'cloud-user',
        'ec2-user',
        'ssm-user',
    ],
)
def test_terminal_denies_local_system_names(username):
    from ideaclustermanager.app.accounts.auth_utils import AuthUtils

    service = TerminalService(MagicMock())
    service.initialized = True
    service.context.accounts.get_user.return_value = SimpleNamespace(
        username=username, enabled=True
    )
    with pytest.raises(TerminalDenied):
        service.user(username)
    with pytest.raises(Exception):
        AuthUtils.check_allowed_username(username)


def test_initialization_lock_failure_does_not_stop_manager(caplog):
    service = TerminalService(MagicMock())
    service.logger = logging.getLogger('terminal-init-test')
    service.context.distributed_lock().acquire.side_effect = RuntimeError(
        'lock unavailable'
    )
    service.context.distributed_lock().release.side_effect = RuntimeError(
        'lock unavailable'
    )
    service.initialize()
    assert not service.initialized
    assert 'RuntimeError' in caplog.text


@pytest.mark.parametrize(
    'error, expected',
    [
        (module.TerminalUnavailable('slots full'), 4002),
        (module.TerminalTargetStopped('Desktop is stopped'), 4006),
        (module.TerminalTargetError('Desktop is in an error state'), 4006),
        (module.TerminalMisconfigured('bad pin'), 4004),
        (TerminalDenied('not owner'), 4003),
    ],
)
def test_websocket_error_contract(store, error, expected):
    async def run():
        service = target_service(store)
        service.store = store
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        service.target = MagicMock(side_effect=error)
        ticket = store.issue('alice', 'bastion', 'client')
        socket = Socket(
            dict(ticket=ticket, username='alice', target='bastion', client='client')
        )
        await service.websocket(
            SimpleNamespace(
                headers={'origin': 'https://portal.example'},
                conn_info=SimpleNamespace(lost=False),
            ),
            socket,
        )
        assert socket.closed['code'] == expected

    asyncio.run(run())


@pytest.mark.parametrize('enabled', [True, False])
def test_desktop_module_lookup_uses_real_cluster_config(enabled):
    from unittest.mock import patch
    from ideasdk.config.cluster_config import ClusterConfig

    with patch('ideasdk.config.cluster_config.ClusterConfigDB') as database:
        database.return_value.build_config_from_db.return_value.as_dict.return_value = {}
        config = ClusterConfig('example', 'us-east-1')
    if enabled:
        config.put(
            'global-settings.module_sets.default.virtual-desktop-controller.module_id',
            'vdc',
        )
    service = TerminalService(MagicMock())
    service.context.config.return_value = config
    service.context.cluster_name.return_value = 'example'
    if enabled:
        service.desktops()
        service.context.aws().dynamodb_table().Table.assert_called_with(
            'example.vdc.controller.user-sessions'
        )
    else:
        with pytest.raises(TerminalDenied, match='Desktops are unavailable'):
            service.desktops()


@pytest.mark.parametrize(
    'unready', ['PENDING', 'PROVISIONING', 'UNHEALTHY', 'UNKNOWN', 'no-address']
)
def test_bastion_picker_skips_unready_tasks(store, unready):
    service = target_service(store)
    ecs = service.context.aws().get_client()
    task = {
        'taskArn': 'b-ready',
        'lastStatus': 'RUNNING',
        'healthStatus': 'HEALTHY',
        'attachments': [
            {'details': [{'name': 'privateIPv4Address', 'value': '10.0.0.2'}]}
        ],
    }
    other = {
        **task,
        'taskArn': 'a-unready',
        'attachments': [
            {'details': [{'name': 'privateIPv4Address', 'value': '10.0.0.3'}]}
        ],
    }
    if unready == 'no-address':
        other['attachments'] = []
    elif unready in {'UNHEALTHY', 'UNKNOWN'}:
        other['healthStatus'] = unready
    else:
        other['lastStatus'] = unready
    ecs.list_tasks.return_value = {'taskArns': [other['taskArn'], task['taskArn']]}
    ecs.describe_tasks.return_value = {'tasks': [other, task]}
    key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    store.table.put_item(Item={'pk': 'host:bastion', 'public_key': key})
    host, _ = service.target('alice', 'bastion')
    assert host == '10.0.0.2'
    assert ecs.describe_tasks.call_args.kwargs['tasks'] == ['a-unready', 'b-ready']


@pytest.mark.parametrize(
    'error, reason',
    [
        (module.TicketRejected('secret-ticket'), 'ticket-rejected'),
        (TerminalDenied('private-terminal-output'), 'access-denied'),
        (module.TerminalUnavailable('secret-binding'), 'unavailable'),
        (module.TerminalMisconfigured('private-host-pin'), 'target-misconfigured'),
        (RuntimeError('private-keystrokes'), 'connection-failed'),
    ],
)
def test_terminal_failure_diagnostic_before_slot_is_redacted(
    store, caplog, error, reason
):
    async def run():
        service = target_service(store)
        service.logger = logging.getLogger('terminal-diagnostic-test')
        service.store = store
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        service.target = MagicMock(side_effect=error)
        ticket = store.issue('alice', 'bastion', 'client')
        socket = Socket(
            dict(ticket=ticket, username='alice', target='bastion', client='client')
        )
        await service.websocket(
            SimpleNamespace(
                headers={'origin': 'https://portal.example'},
                conn_info=SimpleNamespace(lost=False),
            ),
            socket,
        )
        assert socket.closed['reason'] == reason

    caplog.set_level(logging.INFO, logger='terminal-diagnostic-test')
    asyncio.run(run())
    assert "user='alice' target='bastion'" in caplog.text
    assert f'reason={reason} error_type={type(error).__name__}' in caplog.text
    assert str(error) not in caplog.text
    assert 'terminal start' not in caplog.text and 'terminal stop' not in caplog.text


def test_absent_bastion_with_real_cluster_config(store):
    from unittest.mock import patch
    from ideasdk.config.cluster_config import ClusterConfig

    with patch('ideasdk.config.cluster_config.ClusterConfigDB') as database:
        database.return_value.build_config_from_db.return_value.as_dict.return_value = {}
        config = ClusterConfig('example', 'us-east-1')
    service = target_service(store)
    service.context.config.return_value = config
    assert service.targets('alice') == []
    with pytest.raises(TerminalDenied, match='Bastion is not configured'):
        service.target('alice', 'bastion')


@pytest.mark.parametrize('mode', ['close', 'abort'])
def test_real_sanic_disconnect_audit(store, monkeypatch, caplog, mode):
    from unittest.mock import AsyncMock
    from sanic import Sanic
    from websockets.asyncio.client import connect

    async def run():
        service = target_service(store)
        service.store = store
        service.logger = logging.getLogger('terminal-sanic-test')
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        key = asyncssh.generate_private_key('ssh-ed25519')
        service.target = lambda *_: ('127.0.0.1', key.convert_to_public())
        service.load_ca = lambda: key
        process = MagicMock()

        async def read(*_):
            await asyncio.Event().wait()

        process.stdout.read = read
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        conn.wait_closed = AsyncMock()
        monkeypatch.setattr(module.asyncssh, 'connect', AsyncMock(return_value=conn))
        service.context.config().get_int.side_effect = lambda _, default: default
        app = Sanic('terminal_' + mode)
        app.config.TOUCHUP = False
        completed = asyncio.Event()

        async def handler(request, ws):
            try:
                await service.websocket(request, ws)
            finally:
                completed.set()

        app.add_websocket_route(handler, '/terminal')
        import socket

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        server = await app.create_server(
            sock=listener, return_asyncio_server=True, access_log=False
        )
        await server.startup()
        await server.start_serving()
        port = server.server.sockets[0].getsockname()[1]
        try:
            async with connect(
                f'ws://127.0.0.1:{port}/terminal', origin='https://portal.example'
            ) as client:
                ticket = store.issue('alice', 'bastion', 'client')
                await client.send(
                    json.dumps(
                        dict(
                            ticket=ticket,
                            username='alice',
                            target='bastion',
                            client='client',
                        )
                    )
                )
                assert json.loads(await client.recv()) == {'type': 'ready'}
                if mode == 'close':
                    await client.close()
                elif mode == 'abort':
                    client.transport.abort()
                await asyncio.wait_for(completed.wait(), 5)
            assert store.table.scan()['Count'] == 0
        finally:
            await server.close()
            Sanic.unregister_app(app)

    caplog.set_level(logging.INFO, logger='terminal-sanic-test')
    asyncio.run(run())
    stops = [
        record.message
        for record in caplog.records
        if 'terminal stop ' in record.message
    ]
    assert len(stops) == 1
    expected = 'server-shutdown' if mode == 'shutdown' else 'disconnected'
    assert stops[0].endswith('reason=' + expected)


def test_closed_tab_frees_its_slot_for_the_next_terminal(store, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    from sanic import Sanic
    from websockets.asyncio.client import connect

    async def run():
        service = target_service(store)
        service.store = store
        service.logger = logging.getLogger('terminal-limit-test')
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        key = asyncssh.generate_private_key('ssh-ed25519')
        service.target = lambda *_: ('127.0.0.1', key.convert_to_public())
        service.load_ca = lambda: key

        async def read(*_):
            await asyncio.Event().wait()

        def process():
            started = MagicMock()
            started.stdout.read = read
            return started

        conn = MagicMock()
        conn.create_process = AsyncMock(side_effect=lambda *_, **__: process())
        conn.wait_closed = AsyncMock()
        monkeypatch.setattr(module.asyncssh, 'connect', AsyncMock(return_value=conn))
        service.context.config().get_int.side_effect = lambda _, default: default
        app = Sanic('terminal_limit')
        app.config.TOUCHUP = False

        async def handler(request, ws):
            await service.websocket(request, ws)

        app.add_websocket_route(handler, '/terminal')
        import socket

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        server = await app.create_server(
            sock=listener, return_asyncio_server=True, access_log=False
        )
        await server.startup()
        await server.start_serving()
        port = server.server.sockets[0].getsockname()[1]

        async def open_tab():
            client = await connect(
                f'ws://127.0.0.1:{port}/terminal', origin='https://portal.example'
            )
            await client.send(
                json.dumps(
                    dict(
                        ticket=store.issue('alice', 'bastion', 'client'),
                        username='alice',
                        target='bastion',
                        client='client',
                    )
                )
            )
            return client

        tabs = []
        try:
            for _ in range(3):
                tabs.append(await open_tab())
                assert json.loads(await tabs[-1].recv()) == {'type': 'ready'}
            refused = await open_tab()
            with pytest.raises(Exception):
                await refused.recv()
            assert refused.close_code == 4002
            # The server releases the slot before it answers the close.
            await tabs[0].close(code=1000)
            assert tabs[0].close_code == 1000
            tabs[0] = await open_tab()
            assert json.loads(await asyncio.wait_for(tabs[0].recv(), 5)) == {
                'type': 'ready'
            }
        finally:
            for tab in tabs:
                await tab.close()
            await server.close()
            Sanic.unregister_app(app)

    caplog.set_level(logging.INFO, logger='terminal-limit-test')
    asyncio.run(run())
    stops = [r.message for r in caplog.records if 'terminal stop ' in r.message]
    assert stops[0].endswith('reason=disconnected')


@pytest.mark.parametrize(
    'code,status,retryable',
    [
        ('ServerException', 500, True),
        ('InternalServiceError', 500, True),
        ('InternalError', 500, True),
        ('Unavailable', 503, True),
        ('ServiceUnavailable', 503, True),
        ('NewServiceFailure', 502, True),
        ('Throttling', 400, True),
        ('RequestThrottledException', 400, True),
        ('TooManyRequestsException', 429, True),
        ('RequestLimitExceeded', 400, True),
        ('ProvisionedThroughputExceededException', 400, True),
        ('ResourceNotFoundException', 400, True),
        ('AccessDeniedException', 403, False),
        ('KMSInvalidStateException', 400, False),
    ],
)
def test_aws_error_contract_shared_by_api_and_websocket(store, code, status, retryable):
    from botocore.exceptions import ClientError

    error = ClientError(
        {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}},
        'Test',
    )
    context = MagicMock()
    context.terminal.create_ticket.side_effect = error
    invocation = MagicMock(
        namespace='Terminal.CreateTicket',
        request_payload={'target': 'bastion', 'client': 'a' * 64},
    )
    invocation.is_unix_domain_socket_invocation.return_value = False
    with pytest.raises(module.SocaException) as caught:
        TerminalAPI(context).invoke(invocation)
    assert caught.value.error_code == (
        'TERMINAL_UNAVAILABLE' if retryable else 'TERMINAL_TARGET_MISCONFIGURED'
    )
    test_websocket_error_contract(store, error, 4002 if retryable else 4004)


def test_expired_lease_is_retryable(store):
    slot = store.acquire('alice', 'one')
    store.clock = lambda: 1061
    with pytest.raises(module.TerminalUnavailable, match='lease'):
        store.renew(slot, 'one')


def test_rejection_logs_escape_client_target(caplog):
    context = MagicMock()
    context.logger.return_value = logging.getLogger('terminal-api-injection-test')
    context.terminal = TerminalService(context)
    invocation = MagicMock(
        namespace='Terminal.CreateTicket',
        request_payload={
            'target': 'bad\nterminal start user=admin',
            'client': 'invalid',
        },
    )
    invocation.get_username.return_value = 'alice'
    invocation.is_unix_domain_socket_invocation.return_value = False
    caplog.set_level(logging.INFO, logger='terminal-api-injection-test')
    with pytest.raises(module.SocaException):
        TerminalAPI(context).invoke(invocation)
    records = [
        r.message for r in caplog.records if r.name == 'terminal-api-injection-test'
    ]
    assert len(records) == 1
    assert '\n' not in records[0]
    assert repr(invocation.request_payload['target']) in records[0]


def test_deleted_account_uses_real_accounts_service():
    from ideaclustermanager.app.accounts.accounts_service import AccountsService

    accounts = object.__new__(AccountsService)
    accounts.user_dao = MagicMock()
    accounts.user_dao.get_user.return_value = None
    service = TerminalService(MagicMock())
    service.initialized = True
    service.context.accounts = accounts
    with pytest.raises(TerminalDenied, match='Account is disabled or invalid'):
        service.user('alice')


@pytest.mark.parametrize('field', ['ticket', 'client'])
def test_surrogate_handshake_is_permanent(store, field):
    async def run():
        service = target_service(store)
        service.store = store
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        hello = dict(
            ticket=store.issue('alice', 'bastion', 'client'),
            username='alice',
            target='bastion',
            client='client',
        )
        hello[field] = '\ud800'
        socket = Socket(hello)
        await service.websocket(
            SimpleNamespace(headers={'origin': 'https://portal.example'}), socket
        )
        assert socket.closed['code'] == 4003

    asyncio.run(run())


def test_whole_handshake_has_a_deadline(store, monkeypatch):
    async def run():
        service = target_service(store)
        service.store = store
        service.context.config().get_cluster_external_endpoint.return_value = (
            'https://portal.example'
        )
        service.target = MagicMock(side_effect=TerminalDenied('must not reach lookup'))
        socket = Socket(
            dict(
                ticket=store.issue('alice', 'bastion', 'client'),
                username='alice',
                target='bastion',
                client='client',
            )
        )
        recv = socket.recv

        async def delayed_recv():
            await asyncio.sleep(0.05)
            return await recv()

        socket.recv = delayed_recv
        monkeypatch.setattr(module, 'PRE_READY_TIMEOUT', 0.01, raising=False)
        await service.websocket(
            SimpleNamespace(headers={'origin': 'https://portal.example'}), socket
        )
        assert socket.closed['code'] == 1011
        service.target.assert_not_called()
        assert not any(
            row['pk'].startswith('slot:') for row in store.table.scan()['Items']
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    'mode',
    [
        'shutdown',
        'oversized',
        'shutdown-during-release',
        'shutdown-hook-error',
        'slash-origin',
        'slash-disabled',
    ],
)
@pytest.mark.parametrize('unix_socket', [False, True])
def test_real_soca_server_stop_drains_terminal(
    store, monkeypatch, caplog, mode, unix_socket
):
    from unittest.mock import AsyncMock
    from sanic import Sanic
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from ideasdk.server.soca_server import SocaServer
    from tempfile import TemporaryDirectory
    from threading import Event

    socket_directory = TemporaryDirectory(prefix='terminal-', dir='/tmp')

    service = TerminalService(MagicMock())
    service.store = store
    completed = Event()
    released = Event()
    resume_release = Event()
    websocket = service.websocket
    shutdown = service.shutdown
    release = store.release

    async def observed_websocket(request, ws):
        try:
            await websocket(request, ws)
        finally:
            completed.set()

    def held_release(*args):
        release(*args)
        released.set()
        assert resume_release.wait(10)

    async def observed_shutdown(*args):
        # Resume the worker only once shutdown can cancel its awaiting handler.
        resume_release.set()
        await shutdown(*args)

    monkeypatch.setattr(service, 'websocket', observed_websocket)
    if mode == 'shutdown-during-release':
        monkeypatch.setattr(store, 'release', held_release)
        monkeypatch.setattr(service, 'shutdown', observed_shutdown)
    service.logger = logging.getLogger('terminal-soca-stop-test')
    service.context.config().get_cluster_external_endpoint.return_value = (
        'https://portal.example'
    )
    service.context.config().get_int.side_effect = lambda _, default: default
    key = asyncssh.generate_private_key('ssh-ed25519')
    service.target = lambda *_: ('127.0.0.1', key.convert_to_public())
    service.load_ca = lambda: key
    process = MagicMock()

    async def read(*_):
        await asyncio.Event().wait()

    process.stdout.read = read
    conn = MagicMock()
    conn.create_process = AsyncMock(return_value=process)
    conn.wait_closed = AsyncMock()
    monkeypatch.setattr(module.asyncssh, 'connect', AsyncMock(return_value=conn))
    import socket

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    server = SocaServer(
        service.context,
        MagicMock(),
        SocaServerOptions(
            enable_http=True,
            hostname='127.0.0.1',
            port=port,
            enable_unix_socket=unix_socket,
            unix_socket_file=socket_directory.name + '/api.sock',
            enable_tls=False,
            enable_metrics=False,
            enable_openapi_spec=False,
            enable_http_file_upload=False,
            enable_audit_logs=False,
            api_path_prefixes=['/test'],
            graceful_shutdown_timeout=0.1,
            max_workers=2,
        ),
    )
    server.initialize()
    server.http_app.config.TOUCHUP = False
    service.register(server)
    caplog.set_level(logging.INFO, logger='terminal-soca-stop-test')
    mainloop = asyncio.new_event_loop()
    asyncio.set_event_loop(mainloop)
    try:
        server.start()
        server_type = type(server._http_server)
        original_before_stop = server_type.before_stop
        if mode == 'shutdown-hook-error':

            async def failed_hook(listener):
                await original_before_stop(listener)
                raise TimeoutError('drain deadline expired')

            monkeypatch.setattr(server_type, 'before_stop', failed_hook)
        if mode.startswith('slash-'):
            service.enabled = lambda: mode != 'slash-disabled'
            with pytest.raises(InvalidStatus) as caught:
                with connect(
                    f'ws://127.0.0.1:{port}/test/terminal/',
                    origin='https://evil.example'
                    if mode == 'slash-origin'
                    else 'https://portal.example',
                ):
                    pass
            assert caught.value.response.status_code == 403
            return
        with connect(
            f'ws://127.0.0.1:{port}/test/terminal', origin='https://portal.example'
        ) as client:
            client.send(
                json.dumps(
                    dict(
                        ticket=store.issue('alice', 'bastion', 'client'),
                        username='alice',
                        target='bastion',
                        client='client',
                    )
                )
            )
            assert json.loads(client.recv(timeout=5)) == {'type': 'ready'}
            if mode in {'shutdown', 'shutdown-hook-error'}:
                server.stop()
                assert not server._server_thread.is_alive()
            else:
                client.send('x' * (module.FRAME_LIMIT + 1))
            with pytest.raises(ConnectionClosed):
                client.recv(timeout=5)
            assert client.close_code == (
                1012 if mode in {'shutdown', 'shutdown-hook-error'} else 1009
            )
            if mode == 'shutdown-during-release':
                assert released.wait(10)
                assert store.table.scan()['Count'] == 0
                assert not completed.is_set()
                server.stop()
            assert completed.wait(10)
        assert store.table.scan()['Count'] == 0
        stops = [r.message for r in caplog.records if 'terminal stop ' in r.message]
        assert len(stops) == 1
        assert stops[0].endswith(
            'reason='
            + (
                'server-shutdown'
                if mode in {'shutdown', 'shutdown-hook-error'}
                else 'access-denied'
            )
        )
    finally:
        resume_release.set()
        if mode == 'shutdown-hook-error' and server._server_thread.is_alive():
            monkeypatch.setattr(server_type, 'before_stop', original_before_stop)
            server._is_running.set()
        if server.is_running():
            server.stop()
        Sanic.unregister_app(server.http_app)
        if unix_socket:
            Sanic.unregister_app(server.unix_app)
        mainloop.close()
        server._server_loop.close()
        socket_directory.cleanup()


def test_seed_reset_never_reuses_rotated_ca(store):
    from botocore.stub import Stubber, ANY
    import hashlib

    service = target_service(store)
    service.context.module_id.return_value = 'cluster-manager'
    secret = 'example-cluster-manager-terminal-ca'
    version = hashlib.sha256(f'{secret}:v1'.encode()).hexdigest()
    client = boto3.client('secretsmanager', region_name='us-east-1')
    service.context.aws().secretsmanager.return_value = client
    with Stubber(client) as stub:
        stub.add_response(
            'get_secret_value', {'SecretString': '{"schema":1}'}, {'SecretId': secret}
        )
        stub.add_client_error(
            'put_secret_value',
            'ResourceExistsException',
            expected_params={
                'SecretId': secret,
                'ClientRequestToken': version,
                'SecretString': ANY,
            },
        )
        stub.add_response(
            'get_secret_value', {'SecretString': '{"schema":1}'}, {'SecretId': secret}
        )
        with pytest.raises(TerminalDenied, match='seed'):
            service.load_ca(publish=True)
        stub.assert_no_pending_responses()
    service.context.aws().dynamodb_table().Table().update_item.assert_not_called()


def test_ttl_failure_recovers_with_real_table_helpers(monkeypatch):
    from ideasdk.aws.aws_util import AWSUtil
    from botocore.stub import Stubber

    with mock_aws():
        context = MagicMock()
        context.cluster_name.return_value = 'example'
        context.module_name.return_value = 'cluster-manager'
        context.module_id.return_value = 'cluster-manager'
        context.config().get_string.return_value = None
        context.config().get_list.return_value = []
        client = boto3.client('dynamodb', region_name='us-east-1')
        context.aws().dynamodb.return_value = client
        context.aws().dynamodb_table.return_value = boto3.resource(
            'dynamodb', region_name='us-east-1'
        )
        context.aws_util.return_value = AWSUtil(context)
        service = TerminalService(context)
        service.load_ca = MagicMock()
        update = client.update_time_to_live
        calls = []

        def fail_once(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                with Stubber(client) as stub:
                    stub.add_client_error(
                        'update_time_to_live',
                        'InternalServerError',
                        http_status_code=500,
                        expected_params=kwargs,
                    )
                    return update(**kwargs)
            return update(**kwargs)

        monkeypatch.setattr(client, 'update_time_to_live', fail_once)
        service.initialize()
        assert not service.initialized
        assert (
            client.describe_time_to_live(TableName=service.table.name)[
                'TimeToLiveDescription'
            ]['TimeToLiveStatus']
            == 'DISABLED'
        )
        service._next_initialization = 0
        service.initialize()
        assert service.initialized
        assert client.describe_time_to_live(TableName=service.table.name)[
            'TimeToLiveDescription'
        ] == {'TimeToLiveStatus': 'ENABLED', 'AttributeName': 'expires_at'}
        restarted = TerminalService(context)
        restarted.load_ca = MagicMock()
        restarted.initialize()
        assert restarted.initialized
        assert len(calls) == 2


@pytest.mark.parametrize(
    'forwarded, peer',
    [(None, '192.0.2.5'), ('', '192.0.2.5'), ('spoofed, 198.51.100.7', '198.51.100.7')],
)
def test_rejected_ticket_logs_claims_and_peer_not_authenticated_user(
    store, caplog, forwarded, peer
):
    service = target_service(store)
    service.store = store
    service.logger = logging.getLogger('terminal-claims-test')
    service.context.config().get_cluster_external_endpoint.return_value = (
        'https://portal.example'
    )
    caplog.set_level(logging.WARNING, logger='terminal-claims-test')
    asyncio.run(
        service.websocket(
            SimpleNamespace(
                headers={
                    'origin': 'https://portal.example',
                    'x-forwarded-for': forwarded,
                },
                ip='192.0.2.5',
            ),
            Socket(
                dict(
                    ticket='invalid',
                    username='alice',
                    target='bastion',
                    client='untrusted',
                )
            ),
        )
    )
    assert "claimed_user='alice' claimed_target='bastion'" in caplog.text
    assert f'peer={peer!r}' in caplog.text
    assert ' user=' not in caplog.text
    assert 'untrusted' not in caplog.text


@pytest.mark.parametrize(
    'state',
    ['PROVISIONING', 'CREATING', 'INITIALIZING', 'RESUMING', 'STOPPING'],
)
def test_owned_desktop_transitions_are_retryable(state):
    row = {'owner': 'alice', 'base_os': 'ubuntu2404', 'state': state}
    with pytest.raises(module.TerminalUnavailable):
        validate_desktop(row, 'alice')
    with pytest.raises(TerminalDenied) as denied:
        validate_desktop(row, 'bob')
    assert not isinstance(denied.value, module.TerminalUnavailable)


@pytest.mark.parametrize(
    'error',
    [
        TerminalDenied('Terminal CA reset to seed; restore the current CA'),
        TerminalDenied('Invalid terminal CA'),
        module.TerminalUnavailable('Terminal TTL is not configured for expires_at'),
        module.TerminalMisconfigured('Target SSH identity is invalid'),
        RuntimeError('private exception data'),
    ],
)
def test_initialization_logs_only_safe_causes(error, caplog):
    service = TerminalService(MagicMock())
    service.logger = logging.getLogger('terminal-init-cause-test')
    service.context.distributed_lock().acquire.side_effect = error
    service.initialize()
    assert not service.initialized
    if isinstance(error, TerminalDenied):
        assert str(error) in caplog.text
    else:
        assert str(error) not in caplog.text
        assert type(error).__name__ in caplog.text


def test_shutdown_deadline_does_not_recancel_cleanup(monkeypatch):
    async def run():
        service = TerminalService(MagicMock())
        entered = asyncio.Event()
        release = asyncio.Event()
        completed = []

        async def handler():
            try:
                await asyncio.Event().wait()
            finally:
                service._closing_handlers.add(asyncio.current_task())
                entered.set()
                await release.wait()
                completed.append('audit-and-close')

        task = asyncio.create_task(handler())
        service._handlers.add(task)
        await asyncio.sleep(0)
        task.cancel()
        await entered.wait()
        monkeypatch.setattr(module, 'DRAIN_TIMEOUT', 0.02, raising=False)
        try:
            await service.shutdown()
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert completed == ['audit-and-close']

    asyncio.run(run())


@pytest.mark.parametrize('state', ['ERROR', 'DELETING', 'DELETED'])
def test_error_desktop_points_its_owner_at_the_desktop(state):
    row = {'owner': 'alice', 'base_os': 'ubuntu2404', 'state': state}
    with pytest.raises(module.TerminalTargetError):
        validate_desktop(row, 'alice')
    with pytest.raises(TerminalDenied) as denied:
        validate_desktop(row, 'bob')
    assert type(denied.value) is TerminalDenied


@pytest.mark.parametrize('payload', [['target', 'client'], 'target', 7])
def test_api_denies_a_non_object_payload(payload):
    from ideadatamodel.exceptions import SocaException

    context = MagicMock()
    invocation = MagicMock(namespace='Terminal.CreateTicket', request_payload=payload)
    invocation.is_unix_domain_socket_invocation.return_value = False
    with pytest.raises(SocaException) as caught:
        TerminalAPI(context).invoke(invocation)
    assert caught.value.error_code == 'UNAUTHORIZED_ACCESS'
    context.terminal.create_ticket.assert_not_called()


def test_api_doc_lists_every_terminal_error_code():
    source = (
        ROOT
        / 'source/idea/idea-cluster-manager/src/ideaclustermanager/app/api/terminal_api.py'
    ).read_text()
    doc = (
        ROOT / 'source/idea/idea-cluster-manager/resources/api/api_doc.yml'
    ).read_text()
    codes = set(re.findall(r"'(TERMINAL_[A-Z_]+)'", source))
    assert {'TERMINAL_TARGET_STOPPED', 'TERMINAL_TARGET_ERROR'} <= codes
    assert not {code for code in codes if code not in doc}


def test_targets_retry_unprocessed_host_pins(store, monkeypatch):
    service = target_service(store)
    service.desktops = MagicMock()
    service.desktops().query.return_value = {
        'Items': [
            {
                'owner': 'alice',
                'state': 'READY',
                'base_os': 'ubuntu2404',
                'idea_session_id': 'one',
                'server': {'instance_id': 'i-001'},
            }
        ]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    pk = 'host:arn:aws-us-gov:ec2:us-gov-west-1:123456789012:instance/i-001'
    requests = []

    def batch(**kwargs):
        requests.append(kwargs['RequestItems'])
        if len(requests) == 1:
            return {
                'Responses': {store.table.name: []},
                'UnprocessedKeys': kwargs['RequestItems'],
            }
        return {'Responses': {store.table.name: [{'pk': pk, 'public_key': key}]}}

    service.context.aws().dynamodb_table().batch_get_item.side_effect = batch
    assert {'id': 'desktop:one', 'label': 'My desktop'} in service.targets('alice')
    assert requests[1] == requests[0]


def test_targets_give_up_on_keys_that_stay_unprocessed(store, monkeypatch):
    service = target_service(store)
    service.desktops = MagicMock()
    service.desktops().query.return_value = {
        'Items': [
            {
                'owner': 'alice',
                'state': 'READY',
                'base_os': 'ubuntu2404',
                'idea_session_id': 'one',
                'server': {'instance_id': 'i-001'},
            }
        ]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    batch = service.context.aws().dynamodb_table().batch_get_item
    batch.side_effect = lambda **kwargs: {'UnprocessedKeys': kwargs['RequestItems']}
    assert service.targets('alice') == [{'id': 'bastion', 'label': 'Bastion'}]
    assert batch.call_count == 3


def refusing_target(store, monkeypatch, target):
    import datetime

    day_old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    service = target_service(store)
    service.store = store
    service.logger = logging.getLogger('terminal-refusal-test')
    service.context.config().get_cluster_external_endpoint.return_value = (
        'https://portal.example'
    )
    service.context.config().get_int.side_effect = lambda _, default: default
    key = asyncssh.generate_private_key('ssh-ed25519')
    service.load_ca = lambda: key
    pin = key.export_public_key().decode()
    if target == 'bastion':
        ecs = service.context.aws().get_client()
        ecs.list_tasks.return_value = {'taskArns': ['task']}
        ecs.describe_tasks.return_value = {
            'tasks': [
                {
                    'taskArn': 'task',
                    'lastStatus': 'RUNNING',
                    'healthStatus': 'HEALTHY',
                    'startedAt': day_old,
                    'attachments': [
                        {
                            'details': [
                                {'name': 'privateIPv4Address', 'value': '10.0.0.2'}
                            ]
                        }
                    ],
                }
            ]
        }
        store.table.put_item(Item={'pk': 'host:bastion', 'public_key': pin})
    else:
        service.desktops = MagicMock()
        service.desktops().get_item.return_value = {
            'Item': {
                'owner': 'alice',
                'state': 'READY',
                'base_os': 'ubuntu2404',
                'server': {'instance_id': 'i-abc'},
            }
        }
        # An OS reboot leaves LaunchTime unchanged while /run, and trust readiness, reset.
        service.context.aws().ec2().describe_instances.return_value = {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'State': {'Name': 'running'},
                            'PrivateIpAddress': '10.0.0.9',
                            'LaunchTime': day_old,
                            'Tags': [{'Key': 'idea:ClusterName', 'Value': 'example'}],
                        }
                    ]
                }
            ]
        }
        store.table.put_item(
            Item={
                'pk': 'host:arn:aws-us-gov:ec2:us-gov-west-1:123456789012:instance/i-abc',
                'public_key': pin,
            }
        )

    async def refuse(*_, **__):
        raise asyncssh.PermissionDenied('refused')

    monkeypatch.setattr(module.asyncssh, 'connect', refuse)
    return service


@pytest.mark.parametrize('target', ['desktop:one', 'bastion'])
def test_refusal_long_after_launch_is_retryable_trust_not_ready(
    store, monkeypatch, target
):
    async def run():
        service = refusing_target(store, monkeypatch, target)
        ticket = store.issue('alice', target, 'client')
        socket = Socket(
            dict(ticket=ticket, username='alice', target=target, client='client')
        )
        await service.websocket(
            SimpleNamespace(
                headers={'origin': 'https://portal.example'},
                conn_info=SimpleNamespace(lost=False),
            ),
            socket,
        )
        # The client's eight-attempt budget, not a server clock, escalates persistence.
        assert socket.closed == {'code': 4002, 'reason': 'trust-not-ready'}

    asyncio.run(run())


def test_stopped_desktop_has_actionable_final_reason():
    row = {'owner': 'alice', 'base_os': 'ubuntu2404', 'state': 'STOPPED'}
    with pytest.raises(TerminalDenied) as stopped:
        validate_desktop(row, 'alice')
    assert type(stopped.value).__name__ == 'TerminalTargetStopped'
    with pytest.raises(TerminalDenied) as denied:
        validate_desktop(row, 'bob')
    assert type(denied.value) is TerminalDenied


@pytest.mark.parametrize('state', ['stopped', 'stopping', 'pending'])
def test_instance_state_uses_real_ec2_response(store, state):
    from botocore.stub import Stubber

    service = target_service(store)
    service.desktops = MagicMock()
    service.desktops().get_item.return_value = {
        'Item': {
            'owner': 'alice',
            'state': 'READY',
            'base_os': 'ubuntu2404',
            'server': {'instance_id': 'i-abc'},
        }
    }
    ec2 = boto3.client(
        'ec2',
        region_name='us-gov-west-1',
        aws_access_key_id='test',
        aws_secret_access_key='test',
    )
    service.context.aws().ec2.return_value = ec2
    with Stubber(ec2) as stubber:
        stubber.add_response(
            'describe_instances',
            {
                'Reservations': [
                    {
                        'Instances': [
                            {
                                'InstanceId': 'i-abc',
                                'State': {'Name': state},
                                'Tags': [
                                    {'Key': 'idea:ClusterName', 'Value': 'example'}
                                ],
                            }
                        ]
                    }
                ]
            },
            {'InstanceIds': ['i-abc']},
        )
        with pytest.raises(TerminalDenied) as caught:
            service.target('alice', 'desktop:one')
        # My desktops still shows READY, with no Start action, until VDC reconciles.
        assert type(caught.value).__name__ == 'TerminalUnavailable'
        stubber.assert_no_pending_responses()
