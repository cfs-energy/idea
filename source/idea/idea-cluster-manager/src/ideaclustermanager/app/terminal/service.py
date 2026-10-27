"""Authenticated, bounded WebSocket-to-SSH bridge. Audit metadata only."""

import asyncio
import hashlib
import json
import logging
import re
import time
import threading
import uuid
from urllib.parse import urlsplit

import asyncssh
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from sanic.response import json as json_response
from sanic.exceptions import WebsocketClosed
from websockets.exceptions import ConnectionClosed

from ideaclustermanager.app.accounts.auth_utils import AuthUtils
from ideadatamodel.exceptions import SocaException

from .store import (
    TerminalDenied,
    TerminalMisconfigured,
    TerminalUnavailable,
    TerminalStore,
    TicketRejected,
    aws_unavailable,
)

FRAME_LIMIT = 32768
CHUNK_SIZE = 16384
WATCHDOG_INTERVAL = 15
WRITE_TIMEOUT = 30
SHUTDOWN_TIMEOUT = 5
DRAIN_TIMEOUT = 3 * SHUTDOWN_TIMEOUT + 5
PRE_READY_TIMEOUT = 40
LINUX = {
    'amazonlinux2',
    'amazonlinux2023',
    'centos7',
    'rhel8',
    'rhel9',
    'rocky8',
    'rocky9',
    'rhel10',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
    'ubuntu2604',
}


def certificate(ca, username, session_id, now=None):
    now = int(time.time() if now is None else now)
    key = asyncssh.generate_private_key('ssh-ed25519')
    cert = ca.generate_user_certificate(
        key,
        f'web-terminal:{session_id}',
        principals=['idea:' + username],
        valid_after=now - 60,
        valid_before=now + 300,
        permit_pty=True,
        permit_agent_forwarding=False,
        permit_port_forwarding=False,
        permit_x11_forwarding=False,
        permit_user_rc=False,
    )
    return key, cert


def validate_desktop(row, username):
    if not row or row.get('owner') != username or row.get('base_os') not in LINUX:
        raise TerminalDenied('Desktop is not an owned, ready Linux session')
    if row.get('state') in {
        'PROVISIONING',
        'CREATING',
        'INITIALIZING',
        'RESUMING',
        'STOPPING',
        'STOPPED',
    }:
        raise TerminalUnavailable('Desktop is not ready')
    if row.get('state') != 'READY':
        raise TerminalDenied('Desktop is not an owned, ready Linux session')
    instance = row.get('server', {}).get('instance_id')
    if not isinstance(instance, str) or not re.fullmatch(r'i-[0-9a-f]+', instance):
        raise TerminalDenied('Desktop has no instance')
    return instance


def check_origin(origin, endpoint):
    try:
        portal, source = urlsplit(endpoint), urlsplit(origin or '')
        if (
            not origin
            or source.scheme != 'https'
            or portal.scheme != 'https'
            or not source.hostname
            or not portal.hostname
            or source.username is not None
            or source.password is not None
            or source.path
            or source.query
            or source.fragment
            or '?' in origin
            or '#' in origin
            or source.hostname.lower().rstrip('.')
            != portal.hostname.lower().rstrip('.')
            or (source.port if source.port is not None else 443)
            != (portal.port if portal.port is not None else 443)
        ):
            raise TerminalDenied('Origin rejected')
    except ValueError:
        raise TerminalDenied('Origin rejected') from None


class TerminalService:
    def __init__(self, context):
        self.context = context
        self.logger = context.logger('web-terminal')
        # Protocol debug logs can contain packet bytes; audit only our fixed metadata.
        asyncssh.set_log_level(logging.WARNING)
        self.table = (
            context.aws()
            .dynamodb_table()
            .Table(f'{context.cluster_name()}.cluster-manager.terminal')
        )
        self.store = TerminalStore(self.table)
        self.initialized = False
        self.stopping = False
        self._handlers = set()
        self._closing_handlers = set()
        self._initialization_lock = threading.Lock()
        self._next_initialization = 0.0

    def enabled(self):
        return self.context.config().get_bool(
            'cluster-manager.web_terminal.enabled', True
        )

    def initialize(self):
        with self._initialization_lock:
            if self.initialized or time.monotonic() < self._next_initialization:
                return
            self._next_initialization = time.monotonic() + 30
            lock = self.context.distributed_lock()
            try:
                lock.acquire(key='initialize-terminal')
                name = self.table.name
                if not self.context.aws_util().dynamodb_check_table_exists(name, True):
                    self.context.aws_util().dynamodb_create_table(
                        create_table_request={
                            'TableName': name,
                            'AttributeDefinitions': [
                                {'AttributeName': 'pk', 'AttributeType': 'S'}
                            ],
                            'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}],
                            'BillingMode': 'PAY_PER_REQUEST',
                        },
                        wait=True,
                    )
                client = self.context.aws().dynamodb()
                ttl = client.describe_time_to_live(TableName=name)[
                    'TimeToLiveDescription'
                ]
                status = ttl.get('TimeToLiveStatus')
                if status == 'DISABLED':
                    client.update_time_to_live(
                        TableName=name,
                        TimeToLiveSpecification={
                            'Enabled': True,
                            'AttributeName': 'expires_at',
                        },
                    )
                elif (
                    status not in {'ENABLED', 'ENABLING'}
                    or ttl.get('AttributeName') != 'expires_at'
                ):
                    raise TerminalUnavailable(
                        'Terminal TTL is not configured for expires_at'
                    )
                self.load_ca(publish=True)
                self.initialized = True
            except Exception as error:
                self.initialized = False
                code = (
                    error.response['Error']['Code']
                    if isinstance(error, ClientError)
                    else str(error)
                    if isinstance(error, TerminalDenied)
                    else type(error).__name__
                )
                self.logger.error(
                    'terminal initialization failed code=%s; terminal unavailable until successful initialization',
                    code,
                )
            finally:
                try:
                    lock.release(key='initialize-terminal')
                except Exception as error:
                    self.initialized = False
                    self.logger.error(
                        'terminal initialization lock release failed code=%s',
                        type(error).__name__,
                    )

    def load_ca(self, publish=False):
        # The empty secret is provisioned by CDK. An immutable version arbitrates first boot.
        secret = f'{self.context.cluster_name()}-{self.context.module_id()}-terminal-ca'
        client = self.context.aws().secretsmanager()
        value = client.get_secret_value(SecretId=secret)['SecretString']
        if value == '{"schema":1}':
            version = hashlib.sha256(f'{secret}:v1'.encode()).hexdigest()
            private = (
                asyncssh.generate_private_key('ssh-ed25519')
                .export_private_key()
                .decode()
            )
            try:
                client.put_secret_value(
                    SecretId=secret, ClientRequestToken=version, SecretString=private
                )
            except ClientError as error:
                if error.response['Error']['Code'] != 'ResourceExistsException':
                    raise
            except (EndpointConnectionError, ReadTimeoutError):
                # Resolve an ambiguous transport failure by reading the current version.
                pass
            # Only AWSCURRENT may sign: an old immutable version may have been rotated away.
            value = client.get_secret_value(SecretId=secret)['SecretString']
            if value == '{"schema":1}':
                raise TerminalDenied(
                    'Terminal CA reset to seed; restore the current CA'
                )
        ca = asyncssh.import_private_key(value)
        if ca.get_algorithm() != 'ssh-ed25519':
            raise TerminalDenied('Invalid terminal CA')
        if not publish:
            return ca
        public = ca.export_public_key().decode().strip()
        settings = (
            self.context.aws()
            .dynamodb_table()
            .Table(f'{self.context.cluster_name()}.cluster-settings')
        )
        settings.update_item(
            Key={'key': 'cluster-manager.web_terminal.ca_public_key'},
            UpdateExpression='SET #v = :v, #s = :s ADD #n :n',
            ExpressionAttributeNames={'#v': 'value', '#s': 'source', '#n': 'version'},
            ExpressionAttributeValues={':v': public, ':s': 'web-terminal', ':n': 1},
        )
        return ca

    def user(self, username):
        if not self.enabled():
            raise TerminalDenied('Terminal is disabled')
        if not self.initialized:
            self.initialize()
        if not self.initialized:
            raise TerminalUnavailable('Terminal initialization is incomplete')
        try:
            user = self.context.accounts.get_user(username)
        except SocaException:
            raise TerminalDenied('Account is disabled or invalid') from None
        if (
            not user
            or not user.enabled
            or user.username != username
            or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}', username)
        ):
            raise TerminalDenied('Account is disabled or invalid')
        try:
            AuthUtils.check_allowed_username(username)
        except SocaException:
            raise TerminalDenied('Account conflicts with a system user') from None
        return user

    def desktops(self):
        if not self.context.config().is_module_enabled('virtual-desktop-controller'):
            raise TerminalDenied('Desktops are unavailable')
        module = self.context.config().get_module_id('virtual-desktop-controller')
        return (
            self.context.aws()
            .dynamodb_table()
            .Table(f'{self.context.cluster_name()}.{module}.controller.user-sessions')
        )

    def targets(self, username):
        self.user(username)
        targets = (
            [{'id': 'bastion', 'label': 'Bastion'}]
            if self.context.config().is_module_enabled('bastion-host')
            else []
        )
        if not self.context.config().is_module_enabled('virtual-desktop-controller'):
            return targets
        args = {
            'KeyConditionExpression': Key('owner').eq(username),
            'ConsistentRead': True,
        }
        candidates = []
        while True:
            response = self.desktops().query(**args)
            for row in response.get('Items', []):
                try:
                    instance_id = validate_desktop(row, username)
                    candidates.append(
                        {
                            'host': 'host:' + self.instance_arn(instance_id),
                            'id': 'desktop:' + row['idea_session_id'],
                            'label': row.get('name') or 'My desktop',
                        }
                    )
                except TerminalDenied:
                    continue
            if 'LastEvaluatedKey' not in response:
                break
            args['ExclusiveStartKey'] = response['LastEvaluatedKey']
        pins = set()
        keys = list(dict.fromkeys(row['host'] for row in candidates))
        for offset in range(0, len(keys), 100):
            response = (
                self.context.aws()
                .dynamodb_table()
                .batch_get_item(
                    RequestItems={
                        self.table.name: {
                            'Keys': [
                                {'pk': key} for key in keys[offset : offset + 100]
                            ],
                            'ConsistentRead': True,
                        }
                    }
                )
            )
            pins.update(
                row['pk']
                for row in response.get('Responses', {}).get(self.table.name, [])
                if row.get('public_key')
            )
        # Unprocessed/missing keys are unavailable; never advertise an unverified host.
        targets.extend(
            {'id': row['id'], 'label': row['label']}
            for row in candidates
            if row['host'] in pins
        )
        return targets

    def instance_arn(self, instance_id):
        aws = self.context.aws()
        return f'arn:{aws.aws_partition()}:ec2:{aws.aws_region()}:{aws.aws_account_id()}:instance/{instance_id}'

    def target(self, username, target):
        self.user(username)
        if target == 'bastion':
            if not self.context.config().is_module_enabled('bastion-host'):
                raise TerminalDenied('Bastion is not configured')
            host_id = 'bastion'
            cluster = self.context.config().get_string(
                'ecs.cluster_name', required=True
            )
            service = self.context.config().get_string(
                'bastion-host.service_name', required=True
            )
            ecs = self.context.aws().get_client('ecs')
            tasks = ecs.list_tasks(
                cluster=cluster, serviceName=service, desiredStatus='RUNNING'
            )['taskArns']
            if not tasks:
                raise TerminalUnavailable('Bastion is unavailable')
            running = [
                task
                for task in ecs.describe_tasks(cluster=cluster, tasks=tasks)['tasks']
                if task.get('lastStatus') == 'RUNNING'
                and task.get('healthStatus') == 'HEALTHY'
            ]
            if not running:
                raise TerminalUnavailable('Bastion is unavailable')
            for task in sorted(running, key=lambda item: item['taskArn']):
                addresses = [
                    d['value']
                    for a in task.get('attachments', [])
                    for d in a.get('details', [])
                    if d['name'] == 'privateIPv4Address'
                ]
                if len(addresses) == 1:
                    host = addresses[0]
                    break
            else:
                raise TerminalUnavailable('Bastion network is unavailable')
        elif target.startswith('desktop:') and re.fullmatch(
            r'[a-zA-Z0-9_-]{1,128}', target[8:]
        ):
            row = (
                self.desktops()
                .get_item(
                    Key={'owner': username, 'idea_session_id': target[8:]},
                    ConsistentRead=True,
                )
                .get('Item')
            )
            instance_id = validate_desktop(row, username)
            try:
                response = (
                    self.context.aws()
                    .ec2()
                    .describe_instances(InstanceIds=[instance_id])
                )
            except ClientError as error:
                if error.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
                    raise TerminalDenied('Desktop instance is unavailable') from None
                raise
            instances = [
                instance
                for reservation in response.get('Reservations', [])
                for instance in reservation.get('Instances', [])
            ]
            if not instances:
                raise TerminalDenied('Desktop instance is unavailable')
            instance = instances[0]
            tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
            if tags.get('idea:ClusterName') != self.context.cluster_name():
                raise TerminalDenied('Desktop instance is unavailable')
            if instance['State']['Name'] in {'terminated', 'shutting-down'}:
                raise TerminalDenied('Desktop instance is unavailable')
            if instance['State']['Name'] != 'running' or not instance.get(
                'PrivateIpAddress'
            ):
                raise TerminalUnavailable('Desktop instance is not running')
            host = instance['PrivateIpAddress']
            host_id = self.instance_arn(instance_id)
        else:
            raise TerminalDenied('Invalid target')
        pin = (
            self.table.get_item(Key={'pk': 'host:' + host_id}, ConsistentRead=True)
            .get('Item', {})
            .get('public_key')
        )
        if not pin:
            raise TerminalUnavailable('Target SSH identity is not registered')
        try:
            return host, asyncssh.import_public_key(pin)
        except asyncssh.KeyImportError:
            raise TerminalMisconfigured('Target SSH identity is invalid') from None

    def create_ticket(self, username, target, client):
        if not isinstance(client, str) or not re.fullmatch(r'[a-f0-9]{64}', client):
            raise TerminalDenied('Invalid client binding')
        self.target(username, target)
        return {
            'ticket': self.store.issue(username, target, client),
            'username': username,
            'expires_in': 30,
        }

    def register(self, server):
        if not server.options.enable_http:
            return
        server.options.options.enable_web_sockets = True
        server.http_app.config.WEBSOCKET_MAX_SIZE = FRAME_LIMIT
        server.http_app.config.WEBSOCKET_PING_INTERVAL = 20
        server.http_app.config.WEBSOCKET_PING_TIMEOUT = 20
        for prefix in server.options.api_path_prefixes:
            path = prefix + '/terminal'

            async def origin_guard(request, route=path):
                if request.path.rstrip('/') == route:
                    try:
                        check_origin(
                            request.headers.get('origin'),
                            self.context.config().get_cluster_external_endpoint(),
                        )
                        if not self.enabled():
                            raise TerminalDenied('Disabled')
                    except TerminalDenied as error:
                        self.logger.warning(
                            'terminal request rejected reason=access-denied error_type=%s',
                            type(error).__name__,
                        )
                        return json_response(
                            {'error': 'Terminal unavailable'}, status=403
                        )

            server.http_app.register_middleware(origin_guard, 'request')
            server.http_app.add_websocket_route(
                self.websocket, path, name='terminal-' + prefix
            )
        # Stop listeners run in reverse order: drain before Sanic cancels WebSockets.
        server.http_app.register_listener(self.shutdown, 'before_server_stop')

    async def shutdown(self, *_):
        self.stopping = True
        handlers = list(self._handlers)
        for task in handlers:
            # Cancelling a handler inside its finally block would skip audit/close.
            if task not in self._closing_handlers:
                task.cancel()
        if handlers:
            # SSH close, lease release and WebSocket close each have a bounded deadline.
            _, pending = await asyncio.wait(handlers, timeout=DRAIN_TIMEOUT)
            if pending:
                self.logger.warning(
                    'terminal drain deadline reached pending=%d', len(pending)
                )

    async def websocket(self, request, ws):
        if self.stopping:
            await ws.close(code=1012, reason='server-shutdown')
            return
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            await self._websocket(request, ws)
        finally:
            self._handlers.discard(task)
            self._closing_handlers.discard(task)

    async def _websocket(self, request, ws):
        session_id = uuid.uuid4().hex
        slot = None
        username = target = ''
        started = time.monotonic()
        reason = 'connection-failed'
        error_type = None
        tasks = []
        conn = process = None
        audited = False
        authenticated = False
        try:
            async with asyncio.timeout(PRE_READY_TIMEOUT):
                check_origin(
                    request.headers.get('origin'),
                    self.context.config().get_cluster_external_endpoint(),
                )
                raw = await asyncio.wait_for(ws.recv(), 5)
                if not isinstance(raw, str) or len(raw.encode()) > 4096:
                    raise TerminalDenied('Invalid handshake')
                hello = json.loads(raw)
                if (
                    not isinstance(hello, dict)
                    or set(hello) != {'ticket', 'username', 'target', 'client'}
                    or any(
                        not isinstance(v, str) or len(v) > 256 for v in hello.values()
                    )
                ):
                    raise TerminalDenied('Invalid handshake')
                username, target = hello['username'], hello['target']
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.store.consume,
                        hello['ticket'],
                        username,
                        target,
                        hello['client'],
                    ),
                    10,
                )
                authenticated = True
                host, pin = await asyncio.wait_for(
                    asyncio.to_thread(self.target, username, target), 10
                )
                # shortcut: a timed-out worker can still acquire a slot; its lease expires
                # 60 seconds after its write. Revisit if DynamoDB latency regularly reaches this deadline.
                slot = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.store.acquire,
                        username,
                        session_id,
                        self.context.config().get_int(
                            'cluster-manager.web_terminal.max_sessions_per_user', 3
                        ),
                    ),
                    10,
                )
                ca = await asyncio.wait_for(asyncio.to_thread(self.load_ca), 10)
                key, cert = certificate(ca, username, session_id)
                conn = await asyncssh.connect(
                    host,
                    username=username,
                    client_keys=[(key, cert)],
                    known_hosts=([pin], [], []),
                    agent_path=None,
                    config=None,
                    preferred_auth='publickey',
                    login_timeout=15,
                    connect_timeout=20,
                    keepalive_interval=15,
                    keepalive_count_max=2,
                )
                # Login can consume most of the initial lease. Never open a shell
                # unless this session still owns its slot.
                await asyncio.wait_for(
                    asyncio.to_thread(self.store.renew, slot, session_id), 10
                )
                process = await asyncio.wait_for(
                    conn.create_process(
                        'tmux new -A -s web',
                        term_type='xterm-256color',
                        term_size=(80, 24),
                        encoding=None,
                        window=65536,
                        max_pktsize=CHUNK_SIZE,
                    ),
                    10,
                )
                self.logger.info(
                    'terminal start user=%s target=%s session=%s',
                    username,
                    target,
                    session_id,
                )
                audited = True
                await ws.send(json.dumps({'type': 'ready'}))
            activity = [time.monotonic()]
            ack = asyncio.Queue(maxsize=1)
            pending = [False]

            async def output():
                while True:
                    data = await process.stdout.read(CHUNK_SIZE)
                    if not data:
                        return 'shell-exited'
                    pending[0] = True
                    await asyncio.wait_for(ws.send(data), WRITE_TIMEOUT)
                    try:
                        await asyncio.wait_for(ack.get(), WRITE_TIMEOUT)
                    except TimeoutError:
                        return 'ack-timeout'
                    pending[0] = False

            async def input_frames():
                while True:
                    data = await ws.recv()
                    if (
                        not isinstance(data, str)
                        or len(data.encode('utf-8', 'replace')) > FRAME_LIMIT
                    ):
                        raise TerminalDenied('Invalid frame')
                    frame = json.loads(data)
                    if not isinstance(frame, dict):
                        raise TerminalDenied('Invalid frame')
                    if (
                        set(frame) == {'type'}
                        and frame.get('type') == 'ack'
                        and pending[0]
                        and ack.empty()
                    ):
                        pending[0] = False
                        ack.put_nowait(True)
                    elif (
                        frame.get('type') == 'input'
                        and isinstance(frame.get('data'), str)
                        and len(frame['data'].encode('utf-8', 'replace')) <= CHUNK_SIZE
                    ):
                        activity[0] = time.monotonic()
                        process.stdin.write(frame['data'].encode('utf-8', 'replace'))
                        await asyncio.wait_for(process.stdin.drain(), WRITE_TIMEOUT)
                    elif frame.get('type') == 'resize' and all(
                        type(frame.get(k)) is int and 1 <= frame[k] <= 500
                        for k in ('cols', 'rows')
                    ):
                        process.change_terminal_size(frame['cols'], frame['rows'])
                    else:
                        raise TerminalDenied('Invalid frame')

            async def watchdog():
                idle = max(
                    1,
                    self.context.config().get_int(
                        'cluster-manager.web_terminal.idle_timeout_seconds',
                        1800,
                    ),
                )
                lifetime = max(
                    1,
                    self.context.config().get_int(
                        'cluster-manager.web_terminal.max_duration_seconds',
                        43200,
                    ),
                )
                while True:
                    await asyncio.sleep(WATCHDOG_INTERVAL)
                    now = time.monotonic()
                    if now - started >= lifetime:
                        return 'max-duration'
                    if now - activity[0] >= idle:
                        return 'idle-timeout'
                    # A lease must be renewed before it can be reassigned to another replica.
                    await asyncio.wait_for(
                        asyncio.to_thread(self.store.renew, slot, session_id),
                        10,
                    )
                    await asyncio.wait_for(asyncio.to_thread(self.user, username), 10)

            tasks = [asyncio.create_task(f()) for f in (output, input_frames, watchdog)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finished = next(iter(done))
            reason = 'disconnected' if finished.cancelled() else finished.result()
        except TicketRejected as error:
            error_type = type(error).__name__
            reason = 'ticket-rejected'
        except TerminalUnavailable as error:
            error_type = type(error).__name__
            reason = 'unavailable'
        except (
            TerminalMisconfigured,
            asyncssh.HostKeyNotVerifiable,
            asyncssh.PermissionDenied,
        ) as error:
            error_type = type(error).__name__
            reason = 'target-misconfigured'
        except (TerminalDenied, json.JSONDecodeError, UnicodeError) as error:
            error_type = type(error).__name__
            reason = 'access-denied'
        except ClientError as error:
            error_type = type(error).__name__
            reason = 'unavailable' if aws_unavailable(error) else 'target-misconfigured'
        except (ConnectionClosed, WebsocketClosed):
            reason = 'disconnected'
        except asyncio.CancelledError:
            if self.stopping:
                reason = 'server-shutdown'
                raise
            reason = 'disconnected'
        except Exception as error:
            error_type = type(error).__name__
            reason = 'connection-failed'
        finally:
            self._closing_handlers.add(asyncio.current_task())
            protocol = getattr(ws, 'ws_proto', None)
            if (
                protocol is not None
                and protocol.close_sent is not None
                and protocol.close_sent.code == 1009
            ):
                reason = 'access-denied'
            if error_type:
                # Exception messages can contain terminal data; log only the class.
                self.logger.warning(
                    'terminal failure %s=%r %s=%r peer=%r session=%s reason=%s error_type=%s',
                    'user' if authenticated else 'claimed_user',
                    username,
                    'target' if authenticated else 'claimed_target',
                    target,
                    (request.headers.get('x-forwarded-for') or '')
                    .split(',')[-1]
                    .strip()
                    or getattr(request, 'ip', ''),
                    session_id,
                    reason,
                    error_type,
                )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A channel close can wait forever for buffered stdin. Close the transport
            # independently and abort it before releasing the session's lease.
            if conn is not None:
                try:
                    if process is not None:
                        process.close()
                    conn.close()
                    await asyncio.wait_for(conn.wait_closed(), SHUTDOWN_TIMEOUT)
                except Exception:
                    conn.abort()
            if slot:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self.store.release, slot, session_id), 5
                    )
                except Exception:
                    pass  # The lease expires even if the store is unavailable.
            if audited:
                self.logger.info(
                    'terminal stop user=%s target=%s session=%s duration=%d reason=%s',
                    username,
                    target,
                    session_id,
                    int(time.monotonic() - started),
                    reason,
                )
            code = (
                4003
                if reason == 'access-denied'
                else 4000
                if reason in {'idle-timeout', 'max-duration', 'shell-exited'}
                else 4001
                if reason == 'ticket-rejected'
                else 4002
                if reason == 'unavailable'
                else 4004
                if reason == 'target-misconfigured'
                else 4005
                if reason == 'ack-timeout'
                else 1000
                if reason == 'disconnected'
                else 1012
                if reason == 'server-shutdown'
                else 1011
            )
            try:
                await asyncio.wait_for(
                    ws.close(code=code, reason=reason), SHUTDOWN_TIMEOUT
                )
            except Exception:
                pass
