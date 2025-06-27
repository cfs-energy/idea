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
from pydantic import Field

from ideadatamodel import constants

from ideasdk.protocols import SocaContextProtocol, ApiInvokerProtocol
from ideasdk.service import SocaService
from ideasdk.metrics import BaseMetrics
from ideadatamodel import exceptions, errorcodes, SocaBaseModel
from ideasdk.utils import Utils, EnvironmentUtils, GroupNameHelper, Jinja2Utils
from ideasdk.api import ApiInvocationContext

from ideasdk.server.sanic_config import SANIC_LOGGING_CONFIG, SANIC_APP_CONFIG
from ideasdk.server.cors import add_cors_headers
from ideasdk.server.options import setup_options
from ideasdk.filesystem.filesystem_helper import FileSystemHelper

from typing import Optional, Dict, List, Iterable
import asyncio
from concurrent.futures import ThreadPoolExecutor, Future
from threading import Thread, Event
import pathlib
import os
import ssl
import sanic
import logging
from sanic.server import AsyncioServer, serve as create_server, HttpProtocol
from sanic.server.protocols.websocket_protocol import WebSocketProtocol
from sanic.views import stream as stream_decorator
import sanic.config
import sanic.signals
import sanic.router
from sanic.models.handler_types import RouteHandler
import socket
from prometheus_client import generate_latest
import time
import jwt

DEFAULT_ENABLE_HTTP = True
DEFAULT_ENABLE_HTTP_FILE_UPLOAD = False
DEFAULT_ENABLE_OPENAPI_SPEC = True
DEFAULT_ENABLE_TLS = False
DEFAULT_ENABLE_WEBSOCKETS = False
DEFAULT_HOSTNAME = 'localhost'
DEFAULT_HTTP_PORT = 8080
DEFAULT_HTTPS_PORT = 8443
DEFAULT_METRICS_PORT = 8081
DEFAULT_UNIX_SOCKET_FILE = '/run/idea.sock'
DEFAULT_MAX_WORKERS = 16
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 10
DEFAULT_ENABLE_AUDIT_LOGS = True


# these are never used to serve content but used to serve http content over Unix Domain Sockets
DUMMY_HOSTNAME = 'localhost'
DUMMY_PORT = 9999


class SocaServerOptions(SocaBaseModel):
    enable_http: Optional[bool] = Field(default=None)
    hostname: Optional[str] = Field(default=None)
    port: Optional[int] = Field(default=None)
    enable_unix_socket: Optional[bool] = Field(default=None)
    unix_socket_file: Optional[str] = Field(default=None)
    max_workers: Optional[int] = Field(default=None)
    enable_metrics: Optional[bool] = Field(default=None)
    enable_tls: Optional[bool] = Field(default=None)
    tls_certificate_file: Optional[str] = Field(default=None)
    tls_key_file: Optional[str] = Field(default=None)
    graceful_shutdown_timeout: Optional[float] = Field(default=None)
    enable_web_sockets: Optional[bool] = Field(default=None)
    api_path_prefixes: Optional[List[str]] = Field(default=None)
    enable_http_file_upload: Optional[bool] = Field(default=None)
    enable_openapi_spec: Optional[bool] = Field(default=None)
    openapi_spec_file: Optional[str] = Field(default=None)
    enable_audit_logs: Optional[bool] = Field(default=None)

    @staticmethod
    def default() -> 'SocaServerOptions':
        return SocaServerOptions(
            enable_http=None,
            hostname=None,
            port=None,
            enable_unix_socket=None,
            unix_socket_file=None,
            max_workers=None,
            enable_metrics=None,
            enable_tls=None,
            tls_certificate_file=None,
            tls_key_file=None,
            graceful_shutdown_timeout=None,
            enable_web_sockets=None,
            api_path_prefixes=None,
            enable_http_file_upload=None,
            enable_openapi_spec=None,
            openapi_spec_file=None,
            enable_audit_logs=None,
        )


class ServerOptionsProvider:
    def __init__(self, context: SocaContextProtocol, options: SocaServerOptions = None):
        self.context = context
        if options is None:
            self.options = SocaServerOptions.default()
        else:
            self.options = options

        self._is_running_in_ec2 = None

    def is_running_in_ec2(self) -> bool:
        if self._is_running_in_ec2 is not None:
            return self._is_running_in_ec2
        if self.context.aws() is not None:
            self._is_running_in_ec2 = self.context.aws().is_running_in_ec2()
        else:
            self._is_running_in_ec2 = False
        return Utils.get_as_bool(self._is_running_in_ec2, False)

    @property
    def enable_http(self) -> bool:
        if self.options.enable_http is not None:
            return self.options.enable_http
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_http',
            DEFAULT_ENABLE_HTTP,
            module_id=self.context.module_id(),
        )

    @property
    def enable_http_file_upload(self) -> bool:
        if self.options.enable_http_file_upload is not None:
            return self.options.enable_http_file_upload
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_http_file_upload',
            DEFAULT_ENABLE_HTTP_FILE_UPLOAD,
            module_id=self.context.module_id(),
        )

    @property
    def enable_tls(self) -> bool:
        if self.options.enable_tls is not None:
            return self.options.enable_tls
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_tls',
            DEFAULT_ENABLE_TLS,
            module_id=self.context.module_id(),
        )

    @property
    def enable_web_sockets(self) -> bool:
        if self.options.enable_web_sockets is not None:
            return self.options.enable_web_sockets
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_web_sockets',
            DEFAULT_ENABLE_WEBSOCKETS,
            module_id=self.context.module_id(),
        )

    @property
    def enable_metrics(self) -> bool:
        if self.options.enable_metrics is not None:
            return self.options.enable_metrics
        enable_metrics = self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_metrics',
            module_id=self.context.module_id(),
        )
        if enable_metrics is not None:
            return enable_metrics
        if self.is_running_in_ec2() and Utils.is_linux():
            return True
        return False

    @property
    def enable_unix_socket(self) -> bool:
        if self.options.enable_unix_socket is not None:
            return self.options.enable_unix_socket
        enable_unix_socket = self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_unix_socket',
            module_id=self.context.module_id(),
        )
        if enable_unix_socket is not None:
            return enable_unix_socket
        if self.is_running_in_ec2() and Utils.is_linux():
            return True
        return False

    @property
    def hostname(self) -> str:
        if Utils.is_not_empty(self.options.hostname):
            return self.options.hostname
        hostname = self.context.config().get_string(
            f'{self.context.module_name()}.server.hostname',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(hostname):
            return hostname
        if self.is_running_in_ec2():
            return (
                self.context.aws()
                .instance_metadata()
                .get_instance_identity_document()
                .privateIp
            )
        return DEFAULT_HOSTNAME

    @property
    def port(self) -> int:
        if self.options.port is not None:
            return self.options.port
        port = self.context.config().get_int(
            f'{self.context.module_name()}.server.port',
            module_id=self.context.module_id(),
        )
        if port is not None:
            return port
        if self.enable_tls:
            return DEFAULT_HTTPS_PORT
        else:
            return DEFAULT_HTTP_PORT

    @property
    def tls_certificate_file(self) -> Optional[str]:
        if not self.enable_tls:
            return None
        if Utils.is_not_empty(self.options.tls_certificate_file):
            return self.options.tls_certificate_file
        tls_certificate_file = self.context.config().get_string(
            f'{self.context.module_name()}.server.tls_certificate_file',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(tls_certificate_file):
            return tls_certificate_file
        return os.path.join(
            EnvironmentUtils.idea_app_deploy_dir(), 'certs', 'server.crt'
        )

    @property
    def tls_key_file(self) -> Optional[str]:
        if not self.enable_tls:
            return None
        if Utils.is_not_empty(self.options.tls_key_file):
            return self.options.tls_key_file
        tls_key_file = self.context.config().get_string(
            f'{self.context.module_name()}.server.tls_key_file',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(tls_key_file):
            return tls_key_file
        return os.path.join(
            EnvironmentUtils.idea_app_deploy_dir(), 'certs', 'server.key'
        )

    @property
    def unix_socket_file(self) -> Optional[str]:
        if not self.enable_unix_socket:
            return None
        if Utils.is_not_empty(self.options.unix_socket_file):
            return self.options.unix_socket_file
        unix_socket_file = self.context.config().get_string(
            f'{self.context.module_name()}.server.unix_socket_file',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(unix_socket_file):
            return unix_socket_file
        return DEFAULT_UNIX_SOCKET_FILE

    @property
    def max_workers(self) -> int:
        if self.options.max_workers is not None:
            return self.options.max_workers
        max_workers = self.context.config().get_int(
            f'{self.context.module_name()}.server.max_workers',
            module_id=self.context.module_id(),
        )
        if max_workers is not None:
            return max_workers
        return DEFAULT_MAX_WORKERS

    @property
    def graceful_shutdown_timeout(self) -> float:
        if self.options.graceful_shutdown_timeout is not None:
            return self.options.graceful_shutdown_timeout
        graceful_shutdown_timeout = self.context.config().get_float(
            f'{self.context.module_name()}.server.graceful_shutdown_timeout',
            module_id=self.context.module_id(),
        )
        if graceful_shutdown_timeout is not None:
            return graceful_shutdown_timeout
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    @property
    def api_path_prefixes(self) -> List[str]:
        if Utils.is_not_empty(self.options.api_path_prefixes):
            return self.options.api_path_prefixes
        api_path_prefixes = self.context.config().get_list(
            f'{self.context.module_name()}.server.api_path_prefixes',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(api_path_prefixes):
            return api_path_prefixes
        return api_path_prefixes

    @property
    def protocol(self) -> str:
        if self.enable_tls:
            return 'https'
        else:
            return 'http'

    @property
    def enable_openapi_spec(self) -> bool:
        if self.options.enable_openapi_spec is not None:
            return self.options.enable_openapi_spec
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_openapi_spec',
            DEFAULT_ENABLE_OPENAPI_SPEC,
            module_id=self.context.module_id(),
        )

    @property
    def openapi_spec_file(self) -> Optional[str]:
        if not self.enable_openapi_spec:
            return None
        if Utils.is_not_empty(self.options.openapi_spec_file):
            return self.options.openapi_spec_file
        openapi_spec_file = self.context.config().get_string(
            f'{self.context.module_name()}.server.openapi_spec_file',
            module_id=self.context.module_id(),
        )
        if Utils.is_not_empty(openapi_spec_file):
            return openapi_spec_file
        return os.path.join(
            EnvironmentUtils.idea_app_deploy_dir(required=True),
            self.context.module_name(),
            'resources',
            'api',
            'openapi.yml',
        )

    @property
    def enable_audit_logs(self) -> bool:
        if self.options.enable_audit_logs is not None:
            return self.options.enable_audit_logs
        return self.context.config().get_bool(
            f'{self.context.module_name()}.server.enable_audit_logs',
            DEFAULT_ENABLE_AUDIT_LOGS,
            module_id=self.context.module_id(),
        )

    def build(self) -> SocaServerOptions:
        return SocaServerOptions(
            enable_http=self.enable_http,
            hostname=self.hostname,
            port=self.port,
            enable_unix_socket=self.enable_unix_socket,
            unix_socket_file=self.unix_socket_file,
            max_workers=self.max_workers,
            enable_metrics=self.enable_metrics,
            enable_tls=self.enable_tls,
            tls_certificate_file=self.tls_certificate_file,
            tls_key_file=self.tls_key_file,
            graceful_shutdown_timeout=self.graceful_shutdown_timeout,
            enable_web_sockets=self.enable_web_sockets,
            enable_openapi_spec=self.enable_openapi_spec,
            openapi_spec_file=self.openapi_spec_file,
            enable_audit_logs=self.enable_audit_logs,
        )

    def validate(self):
        if self.enable_http:
            if Utils.is_empty(self.hostname):
                raise exceptions.invalid_params('options.hostname is required')
            if Utils.is_empty(self.port):
                raise exceptions.invalid_params('options.port is required')

        if self.enable_unix_socket:
            if Utils.is_empty(self.unix_socket_file):
                raise exceptions.invalid_params('options.unix_socket_file is required')

        if not self.enable_http and not self.enable_unix_socket:
            raise exceptions.invalid_params(
                'either enable_http or enable_unix_socket or both must be enabled.'
            )

        if self.enable_tls:
            if Utils.is_empty(self.tls_certificate_file):
                raise exceptions.invalid_params(
                    'options.tls_certificate_file is required'
                )
            if not Utils.is_file(self.tls_certificate_file):
                raise exceptions.file_not_found(self.tls_certificate_file)
            if Utils.is_empty(self.tls_key_file):
                raise exceptions.invalid_params('options.tls_key_file is required')
            if not Utils.is_file(self.tls_key_file):
                raise exceptions.file_not_found(self.tls_key_file)

        if self.enable_openapi_spec:
            openapi_spec_file = self.openapi_spec_file
            if not Utils.is_file(openapi_spec_file):
                raise exceptions.file_not_found(openapi_spec_file)


class ApiInvocationHandler:
    def __init__(
        self,
        context: SocaContextProtocol,
        server: 'SocaServer',
        api_invoker: ApiInvokerProtocol,
        group_name_helper: GroupNameHelper,
    ):
        self.context = context
        self.logger = context.logger('api')
        self.api_invoker = api_invoker
        self.server = server
        self.group_name_helper = group_name_helper

    @staticmethod
    def get_namespace(payload: Dict) -> str:
        header = Utils.get_value_as_dict('header', payload)
        return Utils.get_value_as_string('namespace', header, 'Unknown')

    @staticmethod
    def get_error_code(response: Dict) -> str:
        return Utils.get_value_as_string(
            'error_code', response, errorcodes.GENERAL_ERROR
        )

    def publish_metrics(self, context: ApiInvocationContext):
        if not self.server.options.enable_metrics:
            return

        metrics_provider = self.context.config().get_string('metrics.provider')
        if Utils.is_empty(metrics_provider):
            return

        namespace = context.namespace
        success = context.is_success()

        BaseMetrics(context=self.context).with_required_dimension(
            name='api', value=namespace
        ).invocation(MetricName='api_invocations', Value=context.get_total_time_ms())

        if not success:
            error_code = context.error_code
            if Utils.is_empty(error_code):
                error_code = 'UNKNOWN_ERROR'
            BaseMetrics(context=self.context).with_required_dimension(
                name='api', value=namespace
            ).with_required_dimension(name='error_code', value=error_code).count(
                MetricName='count'
            )

    def check_is_running(self):
        if not self.server.is_running():
            raise exceptions.soca_exception(
                error_code=errorcodes.SERVER_IS_SHUTTING_DOWN,
                message='server is shutting down',
            )

    @staticmethod
    def validate_and_preprocess_request(request: Dict):
        header = Utils.get_value_as_dict('header', request)

        if header is None:
            raise exceptions.invalid_params('header is required')

        namespace = Utils.get_value_as_string('namespace', header)
        if Utils.is_empty(namespace):
            raise exceptions.invalid_params('header.namespace is required')

        request_id = Utils.get_value_as_string('request_id', header)
        if Utils.is_empty(request_id):
            request_id = Utils.uuid()
            header['request_id'] = request_id

    def get_token(self, http_request) -> Optional[Dict]:
        token = Utils.get_value_as_list('token', http_request.args)
        if Utils.is_not_empty(token):
            return {'token_type': 'Bearer', 'token': Utils.get_first(token)}

        return self.server.get_authorization_header(http_request)

    def is_authenticated_request(self, http_request) -> bool:
        username = self.get_username(http_request)
        return Utils.is_not_empty(username)

    def get_username(self, http_request) -> Optional[str]:
        try:
            token_service = self.api_invoker.get_token_service()
            if token_service is None:
                return None
            token = self.get_token(http_request)
            token_type = Utils.get_value_as_string('token_type', token)
            if Utils.is_empty(token_type):
                return None
            if token_type != 'Bearer':
                return None
            access_token = Utils.get_value_as_string('token', token)
            return token_service.get_username(access_token)
        except Exception:  # noqa
            return None

    def _invoke(self, http_request) -> Dict:
        request = http_request.json
        request_logged = False

        invocation_context: Optional[ApiInvocationContext] = None

        try:
            # In newer Sanic versions (>23.6.0), socket detection changes
            # We need a robust method to detect unix socket connections

            # Method 1: If socket is None, assume it's a unix socket (Sanic 23.6.0 behavior)
            if http_request.socket is None:
                invocation_source = constants.API_INVOCATION_SOURCE_UNIX_SOCKET
            # Method 2: For newer Sanic versions, check if the request is to the unix socket server
            # This check only works if the server is configured to listen on a unix socket
            elif (
                self.server is not None
                and hasattr(self.server, '_unix_server')
                and self.server._unix_server is not None
                and self.server._unix_app is not None
                and http_request.app == self.server._unix_app
            ):
                invocation_source = constants.API_INVOCATION_SOURCE_UNIX_SOCKET
            else:
                invocation_source = constants.API_INVOCATION_SOURCE_HTTP

            invocation_context = ApiInvocationContext(
                context=self.context,
                request=request,
                invocation_source=invocation_source,
                group_name_helper=self.group_name_helper,
                logger=self.logger,
                token=self.get_token(http_request),
                token_service=self.api_invoker.get_token_service(),
            )

            # validate request prior to logging
            invocation_context.validate_request()

            tracing_request = self.api_invoker.get_request_logging_payload(
                invocation_context
            )

            invocation_context.log_request(tracing_request)
            request_logged = True

            self.validate_and_preprocess_request(request)

            self.check_is_running()

            self.api_invoker.invoke(invocation_context)

            response = invocation_context.response

            if response is None:
                raise exceptions.soca_exception(
                    error_code=errorcodes.NOT_SUPPORTED,
                    message=f'namespace: {invocation_context.namespace} not supported',
                )

        except exceptions.SocaException as e:
            message = e.message
            if e.ref is not None and isinstance(e.ref, Exception):
                message += f' (RootCause: {str(e.ref)})'
            invocation_context.fail(error_code=e.error_code, message=message)

        except Exception as e:
            message = f'{e}'
            self.logger.exception(message)
            invocation_context.fail(
                error_code=errorcodes.GENERAL_ERROR, message=message
            )

        finally:
            if not request_logged:
                invocation_context.log_request()

            tracing_response = self.api_invoker.get_response_logging_payload(
                invocation_context
            )
            invocation_context.log_response(tracing_response)

        # publish metrics
        self.publish_metrics(context=invocation_context)

        return invocation_context.response

    def invoke(self, http_request) -> Dict:
        try:
            return self._invoke(http_request)
        except BaseException as e:
            message = f'Critical exception: {e}'
            self.logger.exception(message)
            return {
                'header': {'namespace': 'ErrorResponse', 'request_id': Utils.uuid()},
                'success': False,
                'message': message,
            }


class SocaServer(SocaService):
    """
    IDEA Server
    """

    def __init__(
        self,
        context: SocaContextProtocol,
        api_invoker: ApiInvokerProtocol,
        options: SocaServerOptions,
    ):
        if Utils.is_empty(api_invoker):
            raise exceptions.invalid_params('api_invoker is required')

        super().__init__(context)
        self._context = context
        self._logger = context.logger('server')
        self._group_name_helper = GroupNameHelper(context)

        self.options = ServerOptionsProvider(context, options)
        self.options.validate()

        self._api_invocation_handler = ApiInvocationHandler(
            context=self._context,
            server=self,
            api_invoker=api_invoker,
            group_name_helper=self._group_name_helper,
        )

        self._server_loop: Optional[asyncio.AbstractEventLoop] = None
        self._server_thread: Optional[Thread] = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.options.max_workers,
            thread_name_prefix=f'{self._context.module_id()}-server-worker',
        )
        self._is_running = Event()

        self._sanic_config: Optional[sanic.config.Config] = None
        self._sanic_router: Optional[sanic.router.Router] = None
        self._sanic_signal_router: Optional[sanic.signals.SignalRouter] = None

        self._unix_app: Optional[sanic.Sanic] = None
        self._unix_server: Optional[AsyncioServer] = None

        self._http_app: Optional[sanic.Sanic] = None
        self._http_server: Optional[AsyncioServer] = None

        self._is_started_future = Future()

        self.metrics_api_token_file = '/root/metrics_api_token.txt'
        self.metrics_api_token = None

    @staticmethod
    def is_another_instance_running(hostname: str, port: int) -> bool:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((hostname, port))
            return result == 0
        finally:
            if sock is not None:
                sock.close()

    @property
    def sanic_router(self) -> sanic.router.Router:
        if self._sanic_router is not None:
            return self._sanic_router
        self._sanic_router = sanic.router.Router()
        return self._sanic_router

    @property
    def sanic_signal_router(self) -> sanic.signals.SignalRouter:
        if self._sanic_signal_router is not None:
            return self._sanic_signal_router
        self._sanic_signal_router = sanic.signals.SignalRouter()
        return self._sanic_signal_router

    @property
    def http_app(self) -> sanic.Sanic:
        if self._http_app is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message='Server is not yet initialized.',
            )
        return self._http_app

    @property
    def unix_app(self) -> sanic.Sanic:
        if self._unix_app is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message='Server is not yet initialized.',
            )
        return self._unix_app

    def add_route(
        self,
        handler: RouteHandler,
        uri: str,
        name: Optional[str] = None,
        methods: Iterable[str] = frozenset({'GET'}),
    ):
        _method_names = ', '.join(list(methods))
        if self.options.enable_http:
            for path_prefix in self.options.api_path_prefixes:
                _auto_name = f'http-{path_prefix}-{uri}-{_method_names}'
                _name = name if Utils.is_not_empty(name) else _auto_name
                self.http_app.add_route(
                    handler, f'{path_prefix}{uri}', methods, name=_name
                )
        else:
            for path_prefix in self.options.api_path_prefixes:
                _auto_name = f'unix-{path_prefix}-{uri}-{_method_names}'
                _name = name if Utils.is_not_empty(name) else _auto_name
                self.unix_app.add_route(
                    handler, f'{path_prefix}{uri}', methods, name=_name
                )

    def initialize(self):
        if self.options.enable_http:
            self._http_app = sanic.Sanic(
                name='http-server',
                config=SANIC_APP_CONFIG,
                router=self.sanic_router,
                signal_router=self.sanic_signal_router,
                log_config=SANIC_LOGGING_CONFIG,
            )

        if self.options.enable_unix_socket:
            unix_socket_parent = str(pathlib.Path(self.options.unix_socket_file).parent)
            if not os.path.isdir(unix_socket_parent):
                os.makedirs(unix_socket_parent, exist_ok=True)

            self._unix_app = sanic.Sanic(
                name='unix-server',
                config=SANIC_APP_CONFIG,
                router=self.sanic_router,
                signal_router=self.sanic_signal_router,
                log_config=SANIC_LOGGING_CONFIG,
            )

        if self.options.enable_metrics:
            if not Utils.is_file(self.metrics_api_token_file):
                with open(self.metrics_api_token_file, 'w') as f:
                    f.write(Utils.uuid())
            with open(self.metrics_api_token_file, 'r') as f:
                self.metrics_api_token = f.read()

        if self.options.enable_http:
            self.http_app.register_listener(setup_options, 'before_server_start')
            self.http_app.register_middleware(add_cors_headers, 'response')

            # health check routes
            self.http_app.add_route(
                self.health_check_route,
                '/healthcheck',
                methods=['GET'],
                name='httpapp_healthcheck',
            )
            self.add_route(
                handler=self.health_check_route, uri='/healthcheck', methods=['GET']
            )

            # file transfer routes - always use streaming for memory efficiency
            if self.options.enable_http_file_upload:
                # Use streaming for file uploads to handle large files without loading into RAM
                self.add_route(
                    handler=self.file_upload_stream_route,
                    uri='/api/v1/upload',
                    methods=['PUT'],
                )
                self.add_route(
                    handler=self.file_upload_stream_route,
                    uri='/api/v1/<namespace:str>',
                    methods=['PUT'],
                )
                self.add_route(
                    handler=self.generate_download_url_route,
                    uri='/api/v1/generate-download-url',
                    methods=['POST'],
                )
                self.add_route(
                    handler=self.generate_download_url_route,
                    uri='/api/v1/<namespace:str>/generate-download-url',
                    methods=['POST'],
                )
                self.add_route(
                    handler=self.file_download_route,
                    uri='/api/v1/download',
                    methods=['GET'],
                )
                self.add_route(
                    handler=self.file_download_route,
                    uri='/api/v1/<namespace:str>',
                    methods=['GET'],
                )

            # metrics routes
            if self.options.enable_metrics:
                self.add_route(
                    handler=self.metrics_route, uri='/metrics', methods=['GET']
                )

            # open api spec routes
            if self.options.enable_openapi_spec:
                self.add_route(
                    handler=self.openapi_spec_route,
                    uri='/api/v1/openapi.yml',
                    methods=['GET'],
                )

        self.add_route(handler=self.api_route, uri='/api/v1', methods=['POST'])
        self.add_route(
            handler=self.api_route, uri='/api/v1/<namespace:str>', methods=['POST']
        )

    async def invoke_api_task(self, http_request):
        result = self._executor.submit(
            lambda http_request_: self._api_invocation_handler.invoke(http_request_),
            http_request,
        )
        return await asyncio.wrap_future(result)

    @staticmethod
    async def health_check_route(_):
        return sanic.response.json({'success': True}, dumps=Utils.to_json)

    @staticmethod
    def unauthorized_access():
        return sanic.response.json(
            {'success': False, 'error_code': errorcodes.UNAUTHORIZED_ACCESS},
            dumps=Utils.to_json,
        )

    async def metrics_route(self, http_request):
        authorization = self.get_authorization_header(http_request)
        if Utils.is_empty(authorization):
            return sanic.response.json(
                {'success': False, 'error_code': errorcodes.UNAUTHORIZED_ACCESS},
                dumps=Utils.to_json,
            )
        token_type = Utils.get_value_as_string('token_type', authorization)
        _token = Utils.get_value_as_string('token', authorization)
        if Utils.is_any_empty(token_type, _token):
            return sanic.response.json(
                {'success': False, 'error_code': errorcodes.UNAUTHORIZED_ACCESS},
                dumps=Utils.to_json,
            )
        if token_type != 'Bearer':
            return sanic.response.json(
                {'success': False, 'error_code': errorcodes.UNAUTHORIZED_ACCESS},
                dumps=Utils.to_json,
            )
        if _token != self.metrics_api_token:
            return sanic.response.json(
                {'success': False, 'error_code': errorcodes.UNAUTHORIZED_ACCESS},
                dumps=Utils.to_json,
            )
        # todo - enable gzip if accepted
        output = generate_latest()
        return sanic.response.text(Utils.from_bytes(output))

    async def api_route(self, http_request, **_):
        response = await self.invoke_api_task(http_request)
        return sanic.response.json(response, dumps=Utils.to_json)

    async def openapi_spec_route(self, _):
        openapi_spec_file = pathlib.Path(self.options.openapi_spec_file)

        spec_template_env = Jinja2Utils.env_using_file_system_loader(
            search_path=str(openapi_spec_file.parent)
        )
        spec_template = spec_template_env.get_template(openapi_spec_file.name)

        server_url = self._context.config().get_cluster_external_endpoint()
        if Utils.is_not_empty(self.options.api_path_prefixes):
            path_prefix = self.options.api_path_prefixes[0]
            server_url = f'{server_url}{path_prefix}/api/v1'
        spec_template_data = {
            'module_id': self._context.module_id(),
            'module_name': self._context.module_name(),
            'module_version': self._context.module_version(),
            'server_url': server_url,
        }

        spec_content = spec_template.render(**spec_template_data)
        # refer: https://stackoverflow.com/questions/332129/yaml-media-type
        # switch to text/vnd.yaml once yaml mime type is accepted by IANA
        return sanic.response.text(spec_content, 200, content_type='text/x-yaml')

    @staticmethod
    def get_query_param_as_string(name: str, http_request) -> Optional[str]:
        param_value = None
        param_list = Utils.get_value_as_list(name, http_request.args)
        if Utils.is_not_empty(param_list):
            param_value = param_list[0]
        return param_value

    @staticmethod
    def get_authorization_header(http_request) -> Optional[Dict]:
        if 'Authorization' not in http_request.headers:
            return None
        authorization = Utils.get_value_as_string('Authorization', http_request.headers)
        if Utils.is_empty(authorization):
            return None
        kv = authorization.split(' ')
        if len(kv) != 2:
            return None
        return {'token_type': kv[0], 'token': kv[1]}

    @stream_decorator
    async def file_upload_stream_route(self, http_request, **_):
        """
        Stream-based file upload handler that processes large files without loading them into RAM.
        Uses multipart streaming to handle file uploads efficiently.
        """
        try:
            username = self._api_invocation_handler.get_username(http_request)
            if Utils.is_empty(username):
                raise exceptions.unauthorized_access()

            cwd = self.get_query_param_as_string('cwd', http_request)
            if Utils.is_empty(cwd):
                raise exceptions.invalid_params('cwd parameter is required')

            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f'Streaming file upload request - User: {username}, Directory: {cwd}'
                )

            # Validate directory access before processing upload
            helper = FileSystemHelper(self._context, username=username)
            helper.check_access(cwd, check_dir=True, check_read=True, check_write=True)

            # Parse multipart data in streaming fashion
            result = await self._process_streaming_upload(
                http_request, helper, cwd, username
            )
            return sanic.response.json(result, dumps=Utils.to_json)

        except exceptions.SocaException as e:
            self._logger.error(
                f'Streaming file upload failed - User: {username}, Directory: {cwd}, Error: {e.message}'
            )
            return sanic.response.json(
                {'error_code': e.error_code, 'message': e.message, 'success': False},
                dumps=Utils.to_json,
            )
        except Exception as e:
            self._logger.error(
                f'Streaming file upload error - User: {username}, Directory: {cwd}, Error: {str(e)}'
            )
            return sanic.response.json(
                {
                    'error_code': errorcodes.GENERAL_ERROR,
                    'message': str(e),
                    'success': False,
                },
                dumps=Utils.to_json,
            )

    async def _process_streaming_upload(
        self, http_request, helper: FileSystemHelper, cwd: str, username: str
    ) -> Dict:
        """
        Process streaming multipart upload without loading files into memory.
        """
        import re
        import aiofiles
        import shutil
        import os

        # Get Content-Type header to extract boundary
        content_type = http_request.headers.get('content-type', '')
        boundary_match = re.search(r'boundary=([^;]+)', content_type)
        if not boundary_match:
            raise exceptions.invalid_params(
                'Missing multipart boundary in Content-Type header'
            )

        boundary = boundary_match.group(1).strip('"')
        boundary_bytes = f'--{boundary}'.encode()
        end_boundary_bytes = f'--{boundary}--'.encode()

        files_uploaded = []
        files_skipped = []
        files_renamed = []  # Track files that were renamed due to conflicts

        # Buffer for parsing multipart data
        buffer = b''
        current_file = None
        current_file_handle = None
        current_headers = {}
        parsing_headers = False

        try:
            # Stream the request body
            while True:
                chunk = await http_request.stream.read()
                if chunk is None:
                    break

                buffer += chunk

                # Process buffer for multipart boundaries and data
                while True:
                    if current_file is None and not parsing_headers:
                        # Look for start of new part
                        boundary_pos = buffer.find(boundary_bytes)
                        if boundary_pos == -1:
                            # Keep last len(boundary_bytes) bytes in case boundary spans chunks
                            if len(buffer) > len(boundary_bytes):
                                buffer = buffer[-len(boundary_bytes) :]
                            break

                        # Check if this is the end boundary
                        if buffer.find(end_boundary_bytes) == boundary_pos:
                            # End of all parts
                            buffer = b''
                            break

                        # Move past boundary and CRLF
                        buffer = buffer[boundary_pos + len(boundary_bytes) :]
                        if buffer.startswith(b'\r\n'):
                            buffer = buffer[2:]
                        parsing_headers = True
                        current_headers = {}
                        continue

                    if parsing_headers:
                        # Parse headers until we find double CRLF
                        headers_end = buffer.find(b'\r\n\r\n')
                        if headers_end == -1:
                            break  # Need more data

                        headers_data = buffer[:headers_end].decode('utf-8')
                        buffer = buffer[headers_end + 4 :]  # Skip \r\n\r\n

                        # Parse individual headers
                        for header_line in headers_data.split('\r\n'):
                            if ':' in header_line:
                                key, value = header_line.split(':', 1)
                                current_headers[key.strip().lower()] = value.strip()

                        # Check if this is a file upload
                        content_disposition = current_headers.get(
                            'content-disposition', ''
                        )
                        filename_match = re.search(
                            r'filename="([^"]*)"', content_disposition
                        )

                        if filename_match:
                            filename = filename_match.group(1)
                            secure_filename = Utils.to_secure_filename(filename)

                            if Utils.is_empty(secure_filename):
                                files_skipped.append(filename)
                                parsing_headers = False
                                continue

                            # Handle file name conflicts by auto-renaming
                            file_path, was_renamed = self._get_unique_file_path(
                                cwd, secure_filename, username
                            )
                            current_file = file_path

                            # Track renamed files
                            if was_renamed:
                                files_renamed.append(
                                    {
                                        'original_name': filename,
                                        'final_name': os.path.basename(file_path),
                                    }
                                )

                            # Open file for writing
                            current_file_handle = await aiofiles.open(file_path, 'wb')

                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    f'Starting streaming upload - User: {username}, File: {secure_filename}'
                                )

                        parsing_headers = False
                        continue

                    if current_file is not None and current_file_handle is not None:
                        # Look for next boundary in the data
                        next_boundary = buffer.find(b'\r\n--' + boundary.encode())

                        if next_boundary == -1:
                            # No boundary found, write all but last few bytes (in case boundary spans chunks)
                            if len(buffer) > len(boundary_bytes) + 10:
                                write_data = buffer[: -len(boundary_bytes) - 10]
                                buffer = buffer[-len(boundary_bytes) - 10 :]
                                await current_file_handle.write(write_data)
                            break
                        else:
                            # Found boundary, write data up to boundary
                            write_data = buffer[:next_boundary]
                            await current_file_handle.write(write_data)

                            # Close current file
                            await current_file_handle.close()
                            current_file_handle = None

                            # Set file ownership
                            group_name = helper.group_name_helper.get_user_group(
                                username
                            )
                            shutil.chown(current_file, user=username, group=group_name)

                            files_uploaded.append(current_file)

                            if self._logger.isEnabledFor(logging.DEBUG):
                                file_size = os.path.getsize(current_file)
                                self._logger.debug(
                                    f'Completed streaming upload - User: {username}, File: {os.path.basename(current_file)}, Size: {file_size} bytes'
                                )

                            current_file = None

                            # Move buffer past the CRLF before boundary
                            buffer = buffer[next_boundary + 2 :]  # Skip \r\n
                            continue

                    # If we get here, we need more data
                    break

        finally:
            # Clean up any open file handle
            if current_file_handle is not None:
                await current_file_handle.close()
                # Remove incomplete file
                if current_file and os.path.exists(current_file):
                    os.remove(current_file)

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f'Streaming upload completed - User: {username}, Directory: {cwd}, '
                f'Uploaded: {len(files_uploaded)}, Skipped: {len(files_skipped)}, Renamed: {len(files_renamed)}'
            )

        return {
            'success': True,
            'payload': {
                'files_uploaded': files_uploaded,
                'files_skipped': files_skipped,
                'files_renamed': files_renamed,
            },
        }

    def _get_unique_file_path(
        self, directory: str, filename: str, username: str
    ) -> tuple:
        """
        Generate a unique file path by auto-renaming if the file already exists.
        Returns (file_path, was_renamed)
        """
        import os

        original_path = os.path.join(directory, filename)

        # If file doesn't exist, use original name
        if not os.path.exists(original_path):
            return original_path, False

        # File exists, generate unique name
        name, ext = os.path.splitext(filename)
        counter = 1

        while counter < 1000:  # Prevent infinite loop
            new_filename = f'{name}({counter}){ext}'
            new_path = os.path.join(directory, new_filename)

            if not os.path.exists(new_path):
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        f'File conflict resolved - User: {username}, Original: {filename}, Renamed: {new_filename}'
                    )
                return new_path, True

            counter += 1

        # If we've tried 1000 variations, use timestamp-based naming as fallback
        import time

        timestamp = int(time.time())
        fallback_filename = f'{name}_{timestamp}{ext}'
        fallback_path = os.path.join(directory, fallback_filename)

        self._logger.warning(
            f'File conflict fallback - User: {username}, Original: {filename}, Fallback: {fallback_filename}'
        )

        return fallback_path, True

    async def generate_download_url_route(self, http_request, **_):
        """Generate a temporary signed URL for secure file downloads"""
        try:
            username = self._api_invocation_handler.get_username(http_request)
            if Utils.is_empty(username):
                raise exceptions.unauthorized_access()

            # Parse request body
            body = http_request.json
            file_path = Utils.get_value_as_string('file', body)

            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f'Generate download URL request - User: {username}, File: {file_path}'
                )

            if Utils.is_empty(file_path):
                return sanic.response.json(
                    {
                        'error_code': errorcodes.INVALID_PARAMS,
                        'message': 'file parameter is required',
                        'success': False,
                    },
                    dumps=Utils.to_json,
                )

            # Validate file/directory access
            helper = FileSystemHelper(self._context, username=username)
            if os.path.isdir(file_path):
                helper.check_access(
                    file_path, check_dir=True, check_read=True, check_write=False
                )
            else:
                helper.check_access(
                    file_path, check_dir=False, check_read=True, check_write=False
                )

            # Generate a temporary token valid for 5 seconds
            payload = {
                'username': username,
                'file': file_path,
                'exp': int(time.time()) + 5,  # 5 second expiry
                'iat': int(time.time()),
                'purpose': 'file_download',
            }

            # Use the dedicated JWT signing secret from cluster settings
            jwt_secret = self._get_jwt_signing_secret()
            temp_token = jwt.encode(payload, jwt_secret, algorithm='HS256')

            # Generate the download URL with the temporary token
            base_url = f'{http_request.scheme}://{http_request.host}'
            # Include the API path prefix to ensure proper ALB routing
            api_path_prefix = (
                self.options.api_path_prefixes[0]
                if self.options.api_path_prefixes
                else ''
            )
            download_url = f'{base_url}{api_path_prefix}/api/v1/download?file={Utils.url_encode(file_path)}&temp_token={temp_token}'

            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f'Download URL generated - User: {username}, File: {file_path}, Expires: 5s'
                )

            return sanic.response.json(
                {'download_url': download_url, 'expires_in': 5, 'success': True},
                dumps=Utils.to_json,
            )

        except exceptions.SocaException as e:
            self._logger.error(
                f'Download URL generation failed - User: {username}, File: {file_path}, Error: {e.message}'
            )
            return sanic.response.json(
                {'error_code': e.error_code, 'message': e.message, 'success': False},
                dumps=Utils.to_json,
            )
        except Exception as e:
            self._logger.error(
                f'Download URL generation error - User: {username}, File: {file_path}, Error: {str(e)}'
            )
            return sanic.response.json(
                {
                    'error_code': errorcodes.GENERAL_ERROR,
                    'message': str(e),
                    'success': False,
                },
                dumps=Utils.to_json,
            )

    async def file_download_route(self, http_request, **_):
        username = None

        # Try Bearer token first (for API calls)
        username = self._api_invocation_handler.get_username(http_request)

        # If no Bearer token, try temporary download token (for direct downloads)
        if Utils.is_empty(username):
            temp_token = self.get_query_param_as_string('temp_token', http_request)
            if Utils.is_not_empty(temp_token):
                try:
                    import jwt

                    # Decode the temporary token using the same secret
                    jwt_secret = self._get_jwt_signing_secret()
                    payload = jwt.decode(temp_token, jwt_secret, algorithms=['HS256'])

                    # Validate the token purpose
                    if payload.get('purpose') != 'file_download':
                        raise jwt.InvalidTokenError('Invalid token purpose')

                    username = payload.get('username')
                    requested_file = self.get_query_param_as_string(
                        'file', http_request
                    )
                    token_file = payload.get('file')

                    # Ensure the token is for the requested file
                    if requested_file != token_file:
                        raise jwt.InvalidTokenError('Token file mismatch')

                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            f'Valid temporary download token - User: {username}, File: {token_file}'
                        )

                except jwt.ExpiredSignatureError:
                    self._logger.warning(
                        'Temporary download token expired for file download request'
                    )
                    return sanic.response.json(
                        {
                            'error_code': errorcodes.AUTH_TOKEN_EXPIRED,
                            'message': 'Download link expired',
                            'success': False,
                        },
                        dumps=Utils.to_json,
                    )
                except jwt.InvalidTokenError as e:
                    self._logger.warning(
                        f'Invalid temporary download token - Error: {str(e)}'
                    )
                    username = None

        if Utils.is_empty(username):
            return sanic.response.json(
                {'error_code': errorcodes.UNAUTHORIZED_ACCESS, 'success': False},
                dumps=Utils.to_json,
            )

        download_file = None
        download_files = Utils.get_value_as_list('file', http_request.args)
        if Utils.is_not_empty(download_files):
            download_file = download_files[0]

        if Utils.is_empty(download_file):
            self._logger.error(
                f'File download request missing parameter - User: {username}, Error: file parameter is required'
            )
            return sanic.response.json(
                {
                    'error_code': errorcodes.INVALID_PARAMS,
                    'message': 'file parameter is required',
                    'success': False,
                },
                dumps=Utils.to_json,
            )

        helper = FileSystemHelper(self._context, username=username)
        try:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f'File download access check - User: {username}, File: {download_file}'
                )
            helper.check_access(download_file, check_read=True, check_write=False)
        except exceptions.SocaException as e:
            self._logger.error(
                f'File download access denied - User: {username}, File: {download_file}, Error: {e.message}'
            )
            return sanic.response.json(
                {'error_code': e.error_code, 'message': e.message, 'success': False},
                dumps=Utils.to_json,
            )

        # Get file info for proper headers
        if not os.path.exists(download_file):
            self._logger.error(
                f'File download failed - User: {username}, File: {download_file}, Error: File not found'
            )
            return sanic.response.json(
                {
                    'error_code': errorcodes.FILE_NOT_FOUND,
                    'message': f'File not found: {download_file}',
                    'success': False,
                },
                dumps=Utils.to_json,
            )

        # Check if the requested item is a directory
        if os.path.isdir(download_file):
            self._logger.info(
                f'Directory download requested - User: {username}, Directory: {download_file}'
            )

            # Create zip archive for directory download
            try:
                from ideadatamodel.filesystem import DownloadFilesRequest

                # Use the FileSystemHelper to create a zip archive
                download_request = DownloadFilesRequest(files=[download_file])
                zip_file_path = helper.download_files(download_request)

                # Stream the zip file
                zip_size = os.path.getsize(zip_file_path)
                zip_filename = os.path.basename(zip_file_path)

                self._logger.info(
                    f'Directory zip created - User: {username}, Directory: {download_file}, '
                    f'Zip: {zip_filename}, Size: {zip_size} bytes'
                )

                # Log large directory downloads
                if zip_size > 1024 * 1024 * 1024:  # 1GB+
                    self._logger.warning(
                        f'Large directory download - User: {username}, Directory: {download_file}, '
                        f'Zip size: {zip_size // 1024 // 1024 // 1024}GB'
                    )

                return await self._stream_file_download(
                    zip_file_path, zip_filename, zip_size, username
                )

            except Exception as e:
                self._logger.error(
                    f'Directory zip creation failed - User: {username}, Directory: {download_file}, Error: {str(e)}'
                )
                return sanic.response.json(
                    {
                        'error_code': errorcodes.GENERAL_ERROR,
                        'message': f'Failed to create directory archive: {str(e)}',
                        'success': False,
                    },
                    dumps=Utils.to_json,
                )
        else:
            # Handle regular file download
            file_size = os.path.getsize(download_file)
            filename = os.path.basename(download_file)
            self._logger.info(
                f'File download started - User: {username}, File: {download_file}, Size: {file_size} bytes'
            )

            # Log large file downloads
            if file_size > 1024 * 1024 * 1024:  # 1GB+
                self._logger.warning(
                    f'Large file download - User: {username}, File: {download_file}, '
                    f'Size: {file_size // 1024 // 1024 // 1024}GB'
                )

            # Use file_stream for ALL downloads to provide consistent browser progress bars
            # This ensures users always get download progress regardless of file size
            return await self._stream_file_download(
                download_file, filename, file_size, username
            )

    def _get_adaptive_chunk_size(self, file_size: int) -> int:
        """
        Calculate adaptive chunk size based on file size for optimal throughput behind ALB.
        Larger chunks reduce TCP overhead and improve throughput for high-bandwidth connections.

        Args:
            file_size: Size of the file in bytes

        Returns:
            Optimal chunk size in bytes
        """
        # Small files (< 1MB): Use 64KB chunks for responsiveness
        if file_size < 1024 * 1024:
            return 64 * 1024

        # Medium files (1MB - 10MB): Use 256KB chunks
        elif file_size < 10 * 1024 * 1024:
            return 256 * 1024

        # Large files (10MB - 100MB): Use 512KB chunks
        elif file_size < 100 * 1024 * 1024:
            return 512 * 1024

        # Very large files (100MB - 1GB): Use 1MB chunks
        elif file_size < 1024 * 1024 * 1024:
            return 1024 * 1024

        # Huge files (> 1GB): Use 2MB chunks for maximum throughput
        else:
            return 2 * 1024 * 1024

    async def _stream_file_download(
        self, file_path: str, filename: str, file_size: int, username: str = None
    ):
        """
        Stream file downloads using Sanic's file_stream with proper headers for browser progress.
        This enables native browser download progress bars for all file sizes.
        Uses adaptive chunk sizing optimized for ALB and high-bandwidth connections.
        """
        import mimetypes
        import urllib.parse

        # Determine content type
        content_type, _ = mimetypes.guess_type(file_path)
        if not content_type:
            content_type = 'application/octet-stream'

        # Properly encode filename for Content-Disposition header
        # Use RFC 6266 encoding for international characters
        encoded_filename = urllib.parse.quote(filename)

        # Calculate optimal chunk size based on file size
        chunk_size = self._get_adaptive_chunk_size(file_size)

        # Set up headers that enable browser progress bars
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}',
            'Content-Length': str(file_size),  # Critical for browser progress
            'Content-Type': content_type,
            'Accept-Ranges': 'bytes',  # Enable resume capability
            'Cache-Control': 'no-cache',  # Prevent caching of potentially sensitive files
        }

        if self._logger.isEnabledFor(logging.DEBUG):
            user_context = f'User: {username}, ' if username else ''
            chunk_size_mb = chunk_size / (1024 * 1024)
            self._logger.debug(
                f'File download streaming - {user_context}File: {file_path}, Size: {file_size} bytes, '
                f'Chunk size: {chunk_size_mb:.1f}MB'
            )

        # Use Sanic's built-in file_stream which handles chunking efficiently
        # Adding Content-Length header automatically disables chunked encoding
        return await sanic.response.file_stream(
            file_path,
            chunk_size=chunk_size,  # Adaptive chunk size for optimal throughput
            headers=headers,
        )

    def _get_jwt_signing_secret(self) -> str:
        """
        Get the JWT signing secret for secure download URLs.
        The secret is created by the cluster-manager CDK stack and its ARN is stored in cluster settings.
        """
        # Get the secret ARN from cluster settings
        secret_arn = self._context.config().get_string(
            f'{self._context.module_id()}.jwt_signing_secret_arn', required=True
        )

        # Get the secret value from AWS Secrets Manager
        response = (
            self._context.aws().secretsmanager().get_secret_value(SecretId=secret_arn)
        )
        secret_data = Utils.from_json(response['SecretString'])

        # The secret is generated with a 'secret' key
        return secret_data['secret']

    def _remove_unix_socket(self):
        if not self.options.enable_unix_socket:
            return
        if Utils.is_socket(self.options.unix_socket_file):
            os.unlink(self.options.unix_socket_file)

    async def _server_startup(self):
        if self.options.enable_http:
            await self._http_server.startup()
            await self._http_server.before_start()
        else:
            await self._unix_server.startup()
            await self._unix_server.before_start()

    def _run_server(self):
        try:
            self._server_loop = asyncio.new_event_loop()

            ssl_context = None
            if self.options.enable_tls:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_context.load_cert_chain(
                    certfile=self.options.tls_certificate_file,
                    keyfile=self.options.tls_key_file,
                )

            if self.options.enable_http:
                if self.is_another_instance_running(
                    self.options.hostname, self.options.port
                ):
                    raise exceptions.SocaException(
                        error_code=errorcodes.PORT_IS_ALREADY_IN_USE,
                        message=f'Failed to start soca-server. Port: {self.options.port} is already in use.',
                    )

                self._http_server = create_server(
                    host=self.options.hostname,
                    port=self.options.port,
                    app=self._http_app,
                    ssl=ssl_context,
                    loop=self._server_loop,
                    run_async=True,
                    reuse_port=True,
                    protocol=WebSocketProtocol
                    if self.options.enable_web_sockets
                    else HttpProtocol,
                )

            if self.options.enable_unix_socket:
                self._unix_server = create_server(
                    host=DUMMY_HOSTNAME,
                    port=DUMMY_PORT,
                    unix=self.options.unix_socket_file,
                    app=self._unix_app,
                    loop=self._server_loop,
                    run_async=True,
                    reuse_port=True,
                )
                os.chmod(self.options.unix_socket_file, 0o700)

            self._server_loop.run_until_complete(self._server_startup())
            self._server_loop.run_until_complete(self._http_server.serve_coro)
            if self.options.enable_unix_socket:
                self._server_loop.run_until_complete(self._unix_server.serve_coro)

            startup_message = str(
                f'server started. listening on - '
                f'{self.options.protocol}://{self.options.hostname}:{self.options.port} (api)'
            )
            if self.options.enable_unix_socket:
                startup_message += (
                    f', {Utils.url_encode(self.options.unix_socket_file)} (unix socket)'
                )
            self._logger.info(startup_message)

            result = True, None
            self._is_started_future.set_result(result)

            self._server_loop.run_forever()

        except Exception as e:
            result = False, e
            self._is_started_future.set_result(result)

    def is_running(self) -> bool:
        return self._is_running.is_set()

    def start(self):
        if self.is_running():
            return

        self._is_running.set()

        self._server_thread = Thread(
            target=self._run_server, name=f'{self._context.module_id()}-run-server'
        )
        self._server_thread.start()
        result = self._is_started_future.result()
        if not result[0]:
            self._is_running.clear()
            raise result[1]

    def stop(self):
        if not self.is_running():
            return

        # clear _is_running, so that no new client connections will be queued
        self._is_running.clear()

        # shutdown executor and wait for current pending requests to complete and allow to respond back
        self._executor.shutdown(wait=True, cancel_futures=True)

        # we are in main thread now...
        mainloop = asyncio.get_event_loop()

        connections = []

        if self.options.enable_unix_socket:
            self._logger.info('stopping unix server ...')
            self._unix_server.close()
            self._unix_server.wait_closed()
            connections += self._unix_server.connections

        self._logger.info('stopping http server ...')
        self._http_server.close()
        self._http_server.wait_closed()
        connections += self._http_server.connections

        for connection in connections:
            connection.close_if_idle()

        # Gracefully shutdown timeout
        # refer to sanic.server.runners.serve() for more details.
        graceful = self.options.graceful_shutdown_timeout
        if connections and graceful > 0:
            self._logger.info(f'waiting for graceful shutdown timeout: {graceful} ...')
        start_shutdown: float = 0
        while connections and (start_shutdown < graceful):
            mainloop.run_until_complete(asyncio.sleep(0.1))
            start_shutdown = start_shutdown + 0.1

        self._http_app.shutdown_tasks(graceful - start_shutdown)
        if self.options.enable_unix_socket:
            self._unix_app.shutdown_tasks(graceful - start_shutdown)

        for connection in connections:
            if hasattr(connection, 'websocket') and connection.websocket:
                connection.websocket.fail_connection(code=1001)
            else:
                connection.abort()

        mainloop.run_until_complete(self._http_server.after_stop())

        # close the loop
        if self._server_loop.is_running():
            # _loop.stop() is not threadsafe. since we start a loop from a different thread,
            #   call stop using call_soon_threadsafe
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)

        self._server_thread.join()

        if self.options.enable_unix_socket:
            self._remove_unix_socket()

        self._logger.info('server stopped.')
