#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from ideadatamodel import (
    exceptions,
    errorcodes,
    SocaBaseModel,
    SocaPayload,
    SocaListingPayload,
)
from ideasdk.server import SocaServer, SocaServerOptions
from ideasdk.context import SocaContext, SocaContextOptions
from ideasdk.protocols import ApiInvokerProtocol, TokenServiceProtocol
from ideasdk.client import SocaClient, SocaClientOptions
from ideasdk.api import ApiInvocationContext, BaseAPI
from ideasdk.utils import Utils
from ideatestutils import MockConfig
from pydantic import Field
from typing import Optional, List

import http.client
import threading as _threading
import socket
import tempfile
import pytest


@pytest.fixture(scope='session')
def server_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


@pytest.fixture(scope='session')
def unix_socket_file():
    # Keep Unix paths short enough for macOS and unique across concurrent gate runs.
    with tempfile.TemporaryDirectory(prefix='idea-server-', dir='/tmp') as directory:
        yield f'{directory}/api.sock'


# Create sample Calculator API Data Model


class CalculateRequest(SocaPayload):
    num1: Optional[int] = Field(default=None)
    num2: Optional[int] = Field(default=None)


class CalculateResult(SocaPayload):
    result: Optional[int] = Field(default=None)


class CalculateHistoryEntry(SocaBaseModel):
    num1: Optional[int] = Field(default=None)
    num2: Optional[int] = Field(default=None)
    operation: Optional[str] = Field(default=None)
    result: Optional[int] = Field(default=None)


class ListHistoryRequest(SocaListingPayload):
    pass


class ListHistoryResult(SocaListingPayload):
    listing: Optional[List[CalculateHistoryEntry]] = Field(default=None)


# Create Calculator API


# Calculator.Block holds its worker until this is set
RELEASE_BLOCKED = _threading.Event()


class WhereFromResult(SocaPayload):
    address: Optional[str] = Field(default=None)


class CalculatorAPI(BaseAPI):
    def __init__(self):
        self._history = []

    def add_to_history(self, entry: CalculateHistoryEntry):
        if len(self._history) > 100:
            self._history.pop(0)
        self._history.append(entry)

    def add(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(CalculateRequest)
        num1 = Utils.get_as_int(payload.num1, 0)
        num2 = Utils.get_as_int(payload.num2, 0)
        result = num1 + num2
        self._history.append(CalculateHistoryEntry(num1=num1, num2=num2, result=result))
        context.success(CalculateResult(result=result))

    def multiply(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(CalculateRequest)
        num1 = Utils.get_as_int(payload.num1, 0)
        num2 = Utils.get_as_int(payload.num2, 0)
        result = num1 * num2
        self._history.append(CalculateHistoryEntry(num1=num1, num2=num2, result=result))
        context.success(CalculateResult(result=result))

    def divide(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(CalculateRequest)
        num1 = Utils.get_as_int(payload.num1, 0)
        num2 = Utils.get_as_int(payload.num2, 0)

        if num2 == 0:
            raise exceptions.invalid_params('num2 cannot be zero')

        result = int(num1 / num2)

        self._history.append(CalculateHistoryEntry(num1=num1, num2=num2, result=result))
        context.success(CalculateResult(result=result))

    def subtract(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(CalculateRequest)
        num1 = Utils.get_as_int(payload.num1, 0)
        num2 = Utils.get_as_int(payload.num2, 0)
        result = num1 - num2
        self._history.append(CalculateHistoryEntry(num1=num1, num2=num2, result=result))
        context.success(CalculateResult(result=result))

    def list_history(self, context: ApiInvocationContext):
        context.success(ListHistoryResult(listing=self._history))

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace
        if namespace == 'Calculator.Add':
            self.add(context)
        elif namespace == 'Calculator.Multiply':
            self.multiply(context)
        elif namespace == 'Calculator.Divide':
            self.divide(context)
        elif namespace == 'Calculator.Subtract':
            self.subtract(context)
        elif namespace == 'Calculator.ListHistory':
            self.list_history(context)
        elif namespace == 'Calculator.Block':
            RELEASE_BLOCKED.wait(30)
            context.success(WhereFromResult(address='released'))
        elif namespace == 'Calculator.WhereFrom':
            context.success(WhereFromResult(address=context.client_address))
        elif namespace == 'Calculator.Whoami':
            context.success(WhereFromResult(address=context.get_username()))
        elif namespace == 'Calculator.Unexpected':
            raise RuntimeError('internal-detail-marker')
        elif namespace == 'Calculator.Refuse':
            raise exceptions.soca_exception(
                error_code=errorcodes.INVALID_PARAMS,
                message='refused',
                ref=RuntimeError('internal-detail-marker'),
            )


# Create Calculator API Invoker
class ApiInvoker(ApiInvokerProtocol):
    def __init__(self):
        self.calculator_api = CalculatorAPI()

    def get_token_service(self) -> Optional[TokenServiceProtocol]:
        return None

    def get_namespaces(self):
        return {
            'Calculator.Add',
            'Calculator.Multiply',
            'Calculator.Divide',
            'Calculator.Subtract',
            'Calculator.ListHistory',
            'Calculator.WhereFrom',
            'Calculator.Whoami',
            'Calculator.Block',
            'Calculator.Unexpected',
            'Calculator.Refuse',
        }

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace
        if namespace.startswith('Calculator.'):
            self.calculator_api.invoke(context)


@pytest.fixture(scope='session')
def context():
    mock_config = MockConfig()
    return SocaContext(
        options=SocaContextOptions(
            module_id='test-server',
            module_name='test-server',
            config=mock_config.get_config(),
        )
    )


@pytest.fixture(scope='session', autouse=True)
def server(context, server_port, unix_socket_file):
    # Initialize SocaServer: HTTP + UNIX Socket
    server = SocaServer(
        context=context,
        api_invoker=ApiInvoker(),
        options=SocaServerOptions(
            enable_http=True,
            hostname='localhost',
            port=server_port,
            enable_unix_socket=True,
            unix_socket_file=unix_socket_file,
            graceful_shutdown_timeout=1,
            enable_openapi_spec=False,
            enable_http_file_upload=True,
            api_path_prefixes=['/test-server'],
        ),
    )
    server.initialize()
    server.start()

    yield server

    server.stop()
    assert Utils.is_socket(unix_socket_file) is False


@pytest.fixture(scope='session')
def http_client(context, server):
    return SocaClient(
        context=context,
        options=SocaClientOptions(
            enable_logging=True,
            endpoint=f'http://localhost:{server.options.port}/test-server/api/v1',
        ),
    )


@pytest.fixture(scope='session')
def unix_client(context, server):
    return SocaClient(
        context=context,
        options=SocaClientOptions(
            enable_logging=True,
            endpoint='http://localhost/test-server/api/v1',
            unix_socket=server.options.unix_socket_file,
        ),
    )


@pytest.fixture(scope='session')
def logger(context):
    return context.logger()


def invoke(
    http_client,
    unix_client,
    logger,
    namespace: str,
    num1: int,
    num2: int,
    assert_result: int,
):
    logger.info('http')
    result = http_client.invoke_alt(
        namespace,
        payload=CalculateRequest(num1=num1, num2=num2),
        result_as=CalculateResult,
    )
    assert result.result == assert_result

    logger.info('unix socket')
    result = unix_client.invoke_alt(
        namespace,
        payload=CalculateRequest(num1=num1, num2=num2),
        result_as=CalculateResult,
    )
    assert result.result == assert_result


def invoke_listing(http_client, unix_client, logger, namespace: str, result_count: int):
    logger.info('http')
    result = http_client.invoke_alt(
        namespace, payload=ListHistoryRequest(), result_as=ListHistoryResult
    )
    assert len(result.listing) == result_count

    logger.info('unix socket')
    result = unix_client.invoke_alt(
        namespace, payload=ListHistoryRequest(), result_as=ListHistoryResult
    )
    assert len(result.listing) == result_count

    # verify deserialization for listing results
    first = result.listing[0]
    assert first.num1 is not None
    assert first.num2 is not None
    assert first.result is not None


def test_server(monkeypatch, http_client, unix_client, logger, server):
    invoke(http_client, unix_client, logger, 'Calculator.Add', 10, 20, 30)
    invoke(http_client, unix_client, logger, 'Calculator.Subtract', 20, 10, 10)
    invoke(http_client, unix_client, logger, 'Calculator.Multiply', 20, 10, 200)
    invoke(http_client, unix_client, logger, 'Calculator.Divide', 20, 10, 2)
    invoke_listing(http_client, unix_client, logger, 'Calculator.ListHistory', 8)

    # test any payload (with SimpleNamespace)
    result = http_client.invoke_alt(
        namespace='Calculator.Add', payload=CalculateRequest(num1=10, num2=20)
    )
    assert result.result == 30

    # test exception
    with pytest.raises(exceptions.SocaException) as divide_by_zero:
        http_client.invoke_alt(
            namespace='Calculator.Divide', payload=CalculateRequest(num1=10, num2=0)
        )
    assert divide_by_zero.value.error_code == errorcodes.INVALID_PARAMS

    assert Utils.is_socket(server.options.unix_socket_file) is True


def test_cors_preflight(logger, server):
    # OPTIONS routes are added after the app is finalized; this asserts they go through the
    # same registration path as ordinary routes, so request handling finds the middleware it expects.
    logger.info('cors preflight')
    connection = http.client.HTTPConnection(
        'localhost', server.options.port, timeout=10
    )
    try:
        connection.request(
            'OPTIONS',
            '/test-server/api/v1',
            headers={
                'Origin': 'https://example.invalid',
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'authorization,content-type',
            },
        )
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()

    assert response.status == 204
    assert response.getheader('Access-Control-Allow-Origin') == '*'
    allow_methods = response.getheader('Access-Control-Allow-Methods')
    assert allow_methods is not None
    assert 'OPTIONS' in allow_methods


def test_an_api_body_over_the_limit_is_refused_before_it_is_read(server):
    import socket

    from ideasdk.server.sanic_config import REQUEST_MAX_SIZE

    with socket.create_connection(('localhost', server.options.port), timeout=10) as s:
        s.sendall(
            b'POST /test-server/api/v1 HTTP/1.1\r\nHost: localhost\r\n'
            b'Content-Type: application/json\r\n'
            + f'Content-Length: {REQUEST_MAX_SIZE + 1}\r\n\r\n'.encode()
            + b'{' * 1024
        )
        assert s.recv(64).startswith(b'HTTP/1.1 413')


def test_a_signed_in_upload_still_streams_past_the_api_limit(server, monkeypatch):
    from ideasdk.filesystem.filesystem_helper import FileSystemHelper
    from ideasdk.server.sanic_config import REQUEST_MAX_SIZE

    async def count(http_request, helper, cwd, username):
        received = 0
        while (chunk := await http_request.stream.read()) is not None:
            received += len(chunk)
        return {'success': True, 'payload': {'received': received}}

    monkeypatch.setattr(
        server._api_invocation_handler, 'get_file_username', lambda request: 'alice'
    )
    monkeypatch.setattr(FileSystemHelper, 'check_access', lambda *a, **k: None)
    monkeypatch.setattr(server, '_process_streaming_upload', count)
    size = REQUEST_MAX_SIZE + 1024 * 1024
    connection = http.client.HTTPConnection(
        'localhost', server.options.port, timeout=30
    )
    try:
        connection.request(
            'PUT', '/test-server/api/v1/upload?cwd=/data/home/alice', body=b'x' * size
        )
        response = connection.getresponse()
        assert response.status == 200
        assert Utils.from_json(response.read())['payload']['received'] == size
    finally:
        connection.close()


def test_an_unexpected_failure_reaches_the_caller_without_its_detail(http_client):
    from ideasdk.server.soca_server import UNEXPECTED_ERROR_MESSAGE

    with pytest.raises(exceptions.SocaException) as unexpected:
        http_client.invoke_alt(
            namespace='Calculator.Unexpected', payload=CalculateRequest(num1=1, num2=1)
        )
    assert unexpected.value.error_code == errorcodes.GENERAL_ERROR
    assert unexpected.value.message == UNEXPECTED_ERROR_MESSAGE

    # a refusal keeps its own code and message, and what caused it stays in the log
    with pytest.raises(exceptions.SocaException) as refused:
        http_client.invoke_alt(
            namespace='Calculator.Refuse', payload=CalculateRequest(num1=1, num2=1)
        )
    assert refused.value.error_code == errorcodes.INVALID_PARAMS
    assert refused.value.message == 'refused'


def test_a_request_that_fails_validation_names_the_field_not_the_value(http_client):
    with pytest.raises(exceptions.SocaException) as invalid:
        http_client.invoke_alt(
            namespace='Calculator.Add',
            payload={'num1': 'submitted-value-marker', 'num2': 1},
        )
    assert invalid.value.error_code == errorcodes.INVALID_PARAMS
    assert 'num1' in invalid.value.message
    assert 'submitted-value-marker' not in invalid.value.message


def test_only_a_namespace_no_handler_is_registered_for_is_one_metric_dimension(
    context, monkeypatch
):
    from types import SimpleNamespace
    from ideasdk.server import soca_server

    recorded = []

    class Metrics:
        def __init__(self, context):
            self.dimensions = {}

        def with_required_dimension(self, name, value):
            self.dimensions[name] = value
            return self

        def invocation(self, **_):
            recorded.append(self.dimensions['api'])

        def count(self, **_):
            recorded.append(self.dimensions['api'])

    monkeypatch.setattr(soca_server, 'BaseMetrics', Metrics)
    monkeypatch.setattr(
        context.config(), 'get_string', lambda key, *a, **k: 'cloudwatch'
    )
    handler = soca_server.ApiInvocationHandler(
        context=context,
        server=SimpleNamespace(options=SimpleNamespace(enable_metrics=True)),
        api_invoker=ApiInvoker(),
        group_name_helper=None,
    )

    def call(namespace, success):
        return SimpleNamespace(
            namespace=namespace,
            is_success=lambda: success,
            get_total_time_ms=lambda: 1,
            error_code=None if success else errorcodes.UNAUTHORIZED_ACCESS,
        )

    handler.publish_metrics(call('Marker.One', False))
    handler.publish_metrics(call('Marker.Two', True))
    # failing from the start of the task, a registered API still has its own series
    handler.publish_metrics(call('Calculator.Add', False))
    handler.publish_metrics(call('Calculator.Add', True))

    assert recorded == [
        'unknown',
        'unknown',
        'unknown',
        'Calculator.Add',
        'Calculator.Add',
        'Calculator.Add',
    ]


def test_the_sdk_handlers_register_every_namespace_they_dispatch():
    import inspect
    import re
    from ideasdk.app import SocaAppAPI
    from ideasdk.filesystem.filebrowser_api import FileBrowserAPI

    for api in (FileBrowserAPI, SocaAppAPI):
        dispatched = set(
            re.findall(r"namespace == '([^']+)'", inspect.getsource(api.invoke))
        )
        assert dispatched and dispatched == set(api.NAMESPACES)


def post_json(server, body: bytes, headers=None, timeout=10):
    import json

    connection = http.client.HTTPConnection(
        'localhost', server.options.port, timeout=timeout
    )
    try:
        connection.request(
            'POST',
            '/test-server/api/v1',
            body=body,
            headers={'Content-Type': 'application/json', **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_a_long_namespace_is_refused_at_once_and_the_server_keeps_answering(server):
    import json
    import threading
    import time

    # a long run of letters and one character outside them: the old pattern retried
    # every split point, holding the interpreter for minutes at this length
    body = json.dumps(
        {
            'header': {'namespace': 'a' * 200_000 + '!', 'request_id': 'r1'},
            'payload': {},
        }
    ).encode()
    answered = {}

    def send():
        answered['result'] = post_json(server, body, timeout=30)

    sender = threading.Thread(target=send)
    started = time.monotonic()
    sender.start()
    # meanwhile the same task answers others
    status = None
    connection = http.client.HTTPConnection('localhost', server.options.port, timeout=5)
    try:
        connection.request('GET', '/healthcheck')
        status = connection.getresponse().status
    finally:
        connection.close()
    sender.join(30)
    elapsed = time.monotonic() - started

    assert not sender.is_alive() and elapsed < 2
    assert status == 200
    _, answer = answered['result']
    assert answer['error_code'] == errorcodes.INVALID_PARAMS
    # and a real name still passes
    ok, answer = post_json(
        server,
        json.dumps(
            {
                'header': {'namespace': 'Calculator.Add'},
                'payload': {'num1': 1, 'num2': 2},
            }
        ).encode(),
    )
    assert answer['success'] and answer['payload']['result'] == 3


def test_the_namespace_check_is_linear_and_whole():
    from ideasdk.api.api_invocation_context import ApiInvocationContext
    import time

    def check(namespace):
        context = object.__new__(ApiInvocationContext)
        context._request = {'header': {'namespace': namespace}}
        return context.validate_request()

    started = time.monotonic()
    with pytest.raises(exceptions.SocaException) as refused:
        check('a' * 200_000 + '!')
    assert refused.value.error_code == errorcodes.INVALID_PARAMS
    assert time.monotonic() - started < 0.5
    for bad in ('Auth..X', 'Auth', 'Auth.Init iate', 'A' * 65 + '.B'):
        with pytest.raises(exceptions.SocaException):
            check(bad)
    check('Auth.InitiateAuth')


class ExpiredTokens:
    """a token service whose every token has expired"""

    def decode_token(self, token):
        raise exceptions.SocaException(
            error_code=errorcodes.AUTH_TOKEN_EXPIRED, message='Token Expired'
        )

    def get_authorization(self, decoded):
        raise AssertionError('not reached')

    def get_username(self, token):
        return None


def handler_with(server, token_service):
    """the running server's own handler code, with a token service of the test's"""
    from ideasdk.server import soca_server
    from ideasdk.utils import GroupNameHelper

    class Invoker(ApiInvoker):
        def get_token_service(self):
            return token_service

    return soca_server.ApiInvocationHandler(
        context=server._context,
        server=server,
        api_invoker=Invoker(),
        group_name_helper=GroupNameHelper(server._context),
    )


def http_request(body, token='expired-token'):
    from types import SimpleNamespace
    from sanic.compat import Header

    return SimpleNamespace(
        json=body,
        socket=('192.0.2.10', 443),
        app=None,
        headers=Header({'authorization': f'Bearer {token}'} if token else {}),
        ip='192.0.2.10',
    )


def test_an_expired_token_is_told_so_with_the_request_kept(server):
    handler = handler_with(server, ExpiredTokens())
    response = handler.invoke(
        http_request(
            {'header': {'namespace': 'Calculator.Whoami', 'request_id': 'req-1'}}
        )
    )
    assert response['header']['namespace'] == 'Calculator.Whoami'
    assert response['header']['request_id'] == 'req-1'
    assert response['error_code'] == errorcodes.AUTH_TOKEN_EXPIRED


def test_a_refused_request_is_never_logged_whole(server, context, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        context.config(),
        'get_bool',
        lambda key, *a, **k: (
            key == 'cluster.logging.audit_logs.enable_payload_tracing'
            or k.get('default', False)
        ),
    )
    handler = handler_with(server, ExpiredTokens())
    handler.logger = MagicMock()
    secret = 'password-marker-value'
    # a malformed request id: refused before anything in the body is masked
    handler.invoke(
        http_request(
            {
                'header': {'namespace': 'Auth.InitiateAuth', 'request_id': 'bad id!'},
                'payload': {'password': secret, 'refresh_token': secret},
            },
            token=None,
        )
    )
    logged = '\n'.join(str(call) for call in handler.logger.mock_calls)
    assert 'Auth.InitiateAuth (refused)' in logged
    assert secret not in logged


def test_the_client_address_is_the_socket_unless_the_load_balancer_sent_it(
    server, unix_client, monkeypatch
):
    import ipaddress
    import json

    body = json.dumps(
        {'header': {'namespace': 'Calculator.WhereFrom'}, 'payload': {}}
    ).encode()

    # straight to the server, as anything in the VPC can: a forwarded-for header is not taken
    monkeypatch.setattr(server, '_load_balancer_networks', lambda: [])
    _, answer = post_json(server, body, headers={'X-Forwarded-For': '198.51.100.7'})
    socket_address = answer['payload']['address']
    assert socket_address in ('127.0.0.1', '::1')

    # through the load balancer: its own last entry, never one the caller put before it
    monkeypatch.setattr(
        server,
        '_load_balancer_networks',
        lambda: [ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128')],
    )
    _, answer = post_json(
        server, body, headers={'X-Forwarded-For': '198.51.100.7, 203.0.113.9'}
    )
    assert answer['payload']['address'] == '203.0.113.9'
    # from the load balancer with no address appended: unknown, never the node's own
    _, answer = post_json(server, body)
    assert answer['payload'].get('address') is None
    # subnets not known: a node cannot be told from a client, so no address either
    monkeypatch.setattr(server, '_load_balancer_networks', lambda: None)
    _, answer = post_json(server, body, headers={'X-Forwarded-For': '203.0.113.9'})
    assert answer['payload'].get('address') is None

    # the local socket has no address
    result = unix_client.invoke_alt('Calculator.WhereFrom', payload={})
    assert getattr(result, 'address', None) is None


class Refused(Exception):
    """what the EC2 client raises without ec2:DescribeSubnets"""


def subnets_reader(settings, ec2):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    server = object.__new__(SocaServer)
    server._logger = MagicMock()
    config = SimpleNamespace(
        get_list=lambda key, default=None: settings.get(key, default),
        get_bool=lambda key, default=None: settings.get(key, default),
    )
    server._context = SimpleNamespace(
        config=lambda: config, aws=lambda: SimpleNamespace(ec2=lambda: ec2)
    )
    return server


def test_the_load_balancer_subnets_are_the_public_ones_of_a_public_alb():
    import ipaddress
    from unittest.mock import MagicMock

    settings = {
        'cluster.network.public_subnets': ['subnet-a'],
        'cluster.load_balancers.external_alb.public': True,
    }
    ec2 = MagicMock()
    ec2.describe_subnets.return_value = {'Subnets': [{'CidrBlock': '10.0.0.0/24'}]}
    server = subnets_reader(settings, ec2)
    assert server._load_balancer_networks() is None  # nothing read yet
    assert server._refresh_load_balancer_networks()
    assert server._load_balancer_networks() == [ipaddress.ip_network('10.0.0.0/24')]
    ec2.describe_subnets.assert_called_once_with(SubnetIds=['subnet-a'])

    # an internal external ALB shares the private subnets: nothing can be trusted
    settings['cluster.load_balancers.external_alb.public'] = False
    assert server._refresh_load_balancer_networks()
    assert server._load_balancer_networks() is None


def test_a_refused_subnet_read_says_so_once_and_records_no_address(server, monkeypatch):
    import ipaddress
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    # the running server's own lookup, with its EC2 client refusing as without the grant
    ec2 = MagicMock()
    ec2.describe_subnets.side_effect = Refused(
        'An error occurred (UnauthorizedOperation) when calling the DescribeSubnets '
        'operation: You are not authorized to perform this operation.'
    )
    config = server._context.config()
    monkeypatch.setattr(
        server._context, 'aws', lambda: SimpleNamespace(ec2=lambda: ec2)
    )
    monkeypatch.setattr(
        config,
        'get_list',
        lambda key, *a, **k: (
            ['subnet-a']
            if key == 'cluster.network.public_subnets'
            else k.get('default')
        ),
    )
    monkeypatch.setattr(server, '_load_balancer_networks_value', None, raising=False)
    monkeypatch.setattr(server, '_load_balancer_networks_failing', False, raising=False)
    logger = MagicMock()
    monkeypatch.setattr(server, '_logger', logger)

    assert not server._refresh_load_balancer_networks()
    assert not server._refresh_load_balancer_networks()
    errors = [str(call.args[0]) for call in logger.error.call_args_list]
    assert len(errors) == 1 and 'ec2:DescribeSubnets' in errors[0]

    body = json.dumps(
        {'header': {'namespace': 'Calculator.WhereFrom'}, 'payload': {}}
    ).encode()
    _, answer = post_json(server, body, headers={'X-Forwarded-For': '203.0.113.9'})
    assert answer['payload'].get('address') is None

    # once the grant is in place, the next read trusts the load balancer again
    ec2.describe_subnets.side_effect = None
    ec2.describe_subnets.return_value = {
        'Subnets': [{'CidrBlock': '127.0.0.0/8'}, {'CidrBlock': '10.9.0.0/16'}]
    }
    assert server._refresh_load_balancer_networks()
    assert ipaddress.ip_network('127.0.0.0/8') in server._load_balancer_networks()
    logger.info.assert_called()


def test_a_slow_subnet_read_holds_no_request(server, monkeypatch):
    import json
    import threading
    import time
    from types import SimpleNamespace

    released = threading.Event()

    class Slow:
        def describe_subnets(self, **_):
            released.wait(10)
            return {'Subnets': []}

    monkeypatch.setattr(
        server._context, 'aws', lambda: SimpleNamespace(ec2=lambda: Slow())
    )
    config = server._context.config()
    monkeypatch.setattr(
        config,
        'get_list',
        lambda key, *a, **k: (
            ['subnet-a']
            if key == 'cluster.network.public_subnets'
            else k.get('default')
        ),
    )
    reading = threading.Thread(target=server._refresh_load_balancer_networks)
    reading.start()
    try:
        body = json.dumps(
            {'header': {'namespace': 'Calculator.WhereFrom'}, 'payload': {}}
        ).encode()
        started = time.monotonic()
        post_json(server, body)
        assert time.monotonic() - started < 2
    finally:
        released.set()
        reading.join(10)


def test_the_local_socket_answers_while_every_network_worker_is_held(
    server, unix_client
):
    """
    Job submissions hold network workers while the batch system's admission hook calls
    back on the local socket: that call must have workers of its own.
    """
    import json
    import time
    from ideasdk.server.soca_server import DEFAULT_MAX_WORKERS

    body = json.dumps(
        {'header': {'namespace': 'Calculator.Block'}, 'payload': {}}
    ).encode()
    RELEASE_BLOCKED.clear()
    holders = [
        _threading.Thread(target=post_json, args=(server, body), kwargs={'timeout': 40})
        for _ in range(DEFAULT_MAX_WORKERS + 2)
    ]
    try:
        for holder in holders:
            holder.start()
        # every network worker is waiting now
        deadline = time.monotonic() + 10
        while server._executor._work_queue.qsize() < 2:
            assert time.monotonic() < deadline, 'the network pool did not fill'
            time.sleep(0.05)

        started = time.monotonic()
        result = unix_client.invoke_alt(
            'Calculator.Add', payload=CalculateRequest(num1=2, num2=3)
        )
        assert result.result == 5
        assert time.monotonic() - started < 5
        connection = http.client.HTTPConnection(
            'localhost', server.options.port, timeout=5
        )
        try:
            connection.request('GET', '/healthcheck')
            assert connection.getresponse().status == 200
        finally:
            connection.close()
    finally:
        RELEASE_BLOCKED.set()
        for holder in holders:
            holder.join(40)


def test_a_refused_namespace_never_writes_its_text_into_the_log(server):
    from unittest.mock import MagicMock

    handler = handler_with(server, ExpiredTokens())
    handler.logger = MagicMock()
    supplied = 'Auth.X\n(res) [actor:admin] Accounts.DeleteUser forged-marker'
    handler.invoke(
        http_request(
            {'header': {'namespace': supplied, 'request_id': 'r1'}}, token=None
        )
    )
    lines = [str(call.args[0]) for call in handler.logger.mock_calls if call.args]
    assert lines
    assert all('\n' not in line for line in lines)
    assert all('forged-marker' not in line for line in lines)
    assert any('(invalid namespace)' in line for line in lines)


def test_starting_the_server_reads_the_load_balancer_subnets():
    import ipaddress
    import time
    from concurrent.futures import Future
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    ec2 = MagicMock()
    ec2.describe_subnets.return_value = {'Subnets': [{'CidrBlock': '10.20.0.0/24'}]}
    settings = {
        'cluster.network.public_subnets': ['subnet-a'],
        'cluster.load_balancers.external_alb.public': True,
    }
    config = SimpleNamespace(
        get_list=lambda key, default=None: settings.get(key, default),
        get_bool=lambda key, default=None: settings.get(key, default),
    )
    # SocaServer.start() as it is; only serving is left out (Sanic starts one app of a
    # name per process, and the session's server is that one)
    server = object.__new__(SocaServer)
    server._context = SimpleNamespace(
        module_id=lambda: 'started',
        config=lambda: config,
        aws=lambda: SimpleNamespace(ec2=lambda: ec2),
    )
    server._logger = MagicMock()
    server.options = SimpleNamespace(enable_http=True)
    server._is_running = Event()
    server._is_started_future = Future()
    server._load_balancer_networks_stop = Event()
    server._run_server = lambda: server._is_started_future.set_result((True, None))
    server.start()
    try:
        # nothing here asks for a read: start() must have set the reader going
        deadline = time.monotonic() + 10
        while server._load_balancer_networks() is None:
            assert time.monotonic() < deadline, 'start() read no subnets'
            time.sleep(0.05)
        assert server._load_balancer_networks() == [
            ipaddress.ip_network('10.20.0.0/24')
        ]
    finally:
        server._load_balancer_networks_stop.set()
