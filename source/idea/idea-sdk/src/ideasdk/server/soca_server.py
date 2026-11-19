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
from ideasdk.filesystem.user_identity import os_refusal

from typing import BinaryIO, Optional, Dict, List, Iterable, Tuple
import asyncio
import ipaddress
from concurrent.futures import ThreadPoolExecutor, Future
from threading import Thread, Event
import pathlib
import os
from stat import S_ISGID, S_ISREG
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

# how long the download link signing secret is kept before it is read again
JWT_SECRET_CACHE_SECONDS = 300

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
# workers for requests on the local socket, apart from the DEFAULT_MAX_WORKERS of the network
LOCAL_SOCKET_WORKERS = 4
# how often the public load balancer's subnets are read again, and retried after a failure
LOAD_BALANCER_SUBNETS_REFRESH_SECONDS = 3600
LOAD_BALANCER_SUBNETS_RETRY_SECONDS = 300
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 10
DEFAULT_ENABLE_AUDIT_LOGS = True


# A part's own headers (name, file name, type): file bytes are never capped, these are
MAX_UPLOAD_PART_HEADER_BYTES = 8 * 1024
# How many folders deep a file in a folder upload may be, below the destination
MAX_UPLOAD_FOLDER_DEPTH = 32

# what a caller is told about a failure the product did not expect: the detail is logged
UNEXPECTED_ERROR_MESSAGE = 'The server could not complete this request. Try again.'

# the api metric dimension for every namespace no handler is registered for
UNREGISTERED_NAMESPACE_DIMENSION = 'unknown'

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


class _Upload:
    """
    An uploaded file being written: the open file, and the folder it was created in, held
    open with what the file is, for removing it through that folder if it is cut short.
    """

    def __init__(self, file: BinaryIO, path: str, dir_fd: int, dev: int, ino: int):
        self.file = file
        self.path = path
        self.name = os.path.basename(path)
        self.dir_fd = dir_fd
        self.dev = dev
        self.ino = ino

    def finish(self):
        # closing writes what is still buffered: a full disk can refuse it here. The
        # folder stays open: close_folder is its one place to close
        self.file.close()

    def close_folder(self):
        # the folder descriptor is closed here and nowhere else, once: a number closed
        # twice can be another request's descriptor by the second time
        if self.dir_fd is not None:
            dir_fd, self.dir_fd = self.dir_fd, None
            os.close(dir_fd)


class _UploadFolders(list):
    """
    One upload's folders: the list of those it made (for removing those left empty if it
    is cut short), and what it already checked as the user, so each destination and each
    folder costs one check per upload rather than one per file.
    """

    def __init__(self):
        super().__init__()
        # destination path -> (device, inode) of the folder checked as the user
        self.destinations = {}
        # (destination, folders) -> (device, inode) of the folder checked writable
        self.checked = {}


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
        self._registered_namespaces = frozenset(
            getattr(api_invoker, 'get_namespaces', tuple)() or ()
        )

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

        success = context.is_success()
        namespace = context.namespace
        # any other name, sent by anyone, would be a new metric series each time
        if namespace not in self._registered_namespaces:
            namespace = UNREGISTERED_NAMESPACE_DIMENSION

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

    def get_file_username(self, http_request) -> Optional[str]:
        """
        The person behind a request to the upload and download routes, held to the rule
        FileBrowser.* applies: a member of the module's users group, a manager or an
        administrator. Anyone else signed in gets None, as a request with no session does.
        """
        try:
            context = ApiInvocationContext(
                context=self.context,
                request={'header': {}},
                invocation_source=constants.API_INVOCATION_SOURCE_HTTP,
                group_name_helper=self.group_name_helper,
                logger=self.logger,
                token=self.get_token(http_request),
                token_service=self.api_invoker.get_token_service(),
            )
            if context.is_authorized_user():
                return context.get_username()
        except Exception:  # noqa
            pass
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
                client_address=(
                    None
                    if invocation_source == constants.API_INVOCATION_SOURCE_UNIX_SOCKET
                    else self.server.client_address(http_request)
                ),
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
            # the product's own refusal reaches the caller; what caused it stays in the log
            if e.ref is not None and isinstance(e.ref, Exception):
                self.logger.warning(
                    f'{invocation_context.loggable_namespace} failed with {e.error_code}, '
                    f'request_id: {invocation_context.request_id}, RootCause: {e.ref}'
                )
            invocation_context.fail(error_code=e.error_code, message=e.message)

        except Exception:
            if invocation_context is None:
                raise  # not even the request could be read: answered as a server fault
            self.logger.exception(
                f'{invocation_context.loggable_namespace} failed, '
                f'request_id: {invocation_context.request_id}'
            )
            invocation_context.fail(
                error_code=errorcodes.GENERAL_ERROR, message=UNEXPECTED_ERROR_MESSAGE
            )

        finally:
            if invocation_context is not None and not request_logged:
                invocation_context.log_refused_request()

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
        except BaseException:
            self.logger.exception('Critical exception')
            return {
                'header': {'namespace': 'ErrorResponse', 'request_id': Utils.uuid()},
                'success': False,
                'message': UNEXPECTED_ERROR_MESSAGE,
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
        # The local socket's own workers: the batch system's admission hook calls back on
        # it while a job submission waits for that hook on a general worker, so the two
        # must never share a pool. Local callers only (root on the host, ideactl).
        self._local_executor = ThreadPoolExecutor(
            max_workers=LOCAL_SOCKET_WORKERS,
            thread_name_prefix=f'{self._context.module_id()}-local-worker',
        )
        self._is_running = Event()
        # read in the background from start: no request waits on it
        self._load_balancer_networks_stop = Event()

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

    def client_address(self, http_request) -> Optional[str]:
        """
        The address a request over the network came from, for limits counted per address.
        Port 8443 is open to the whole VPC, so X-Forwarded-For is anyone's to write; it is
        read only when the connection comes from the public load balancer's subnets, and
        then only its last entry, the one the load balancer appended. Never waits: the
        subnets are read in the background.
        - through the public load balancer, with an address appended: that address
        - through it with none appended: None
        - any other peer, once the subnets are known: its socket address
        - the subnets not known (not read yet, the read refused or failing) or the
          external load balancer not public, so a node cannot be told from a client: None,
          which the limits treat as "address unknown"
        """
        networks = self._load_balancer_networks()
        if networks is None:
            return None
        try:
            peer = ipaddress.ip_address(http_request.ip)
        except (ValueError, TypeError):
            return None
        if any(peer in network for network in networks):
            hops = ','.join(http_request.headers.getall('x-forwarded-for', []))
            try:
                return str(ipaddress.ip_address(hops.split(',')[-1].strip()))
            except ValueError:
                return None  # no address appended: the load balancer node itself
        return str(peer)

    def _load_balancer_networks(self) -> Optional[list]:
        """the public load balancer's subnets as last read, or None while they are not known"""
        return getattr(self, '_load_balancer_networks_value', None)

    def _read_load_balancer_networks(self) -> Optional[list]:
        """
        The public subnets, which the external load balancer connects from when it is
        internet facing; desktops and compute are placed in the private ones. None when
        the external load balancer is internal (it shares its subnets with them) or no
        public subnet is configured. Raises when the subnets cannot be read.
        """
        config = self._context.config()
        subnet_ids = config.get_list('cluster.network.public_subnets', default=[])
        if not subnet_ids or not config.get_bool(
            'cluster.load_balancers.external_alb.public', default=True
        ):
            return None
        networks = []
        subnets = (
            self._context.aws().ec2().describe_subnets(SubnetIds=subnet_ids)['Subnets']
        )
        for subnet in subnets:
            networks.append(ipaddress.ip_network(subnet['CidrBlock']))
            for block in subnet.get('Ipv6CidrBlockAssociationSet', []):
                networks.append(ipaddress.ip_network(block['Ipv6CidrBlock']))
        return networks

    def _refresh_load_balancer_networks(self) -> bool:
        """one read; a failure keeps the last good value and is logged once per run of failures"""
        try:
            networks = self._read_load_balancer_networks()
        except Exception as e:
            if not getattr(self, '_load_balancer_networks_failing', False):
                self._load_balancer_networks_failing = True
                self._logger.error(
                    'The public load balancer subnets could not be read, so client '
                    'addresses are not recorded and per-address limits fall back to '
                    'per-username ones. The task role needs ec2:DescribeSubnets. '
                    f'Retrying every {LOAD_BALANCER_SUBNETS_RETRY_SECONDS} s: {e}'
                )
            return False
        if getattr(self, '_load_balancer_networks_failing', False):
            self._logger.info('The public load balancer subnets are read again')
        self._load_balancer_networks_failing = False
        self._load_balancer_networks_value = networks
        if networks is None and not getattr(
            self, '_load_balancer_untrusted_logged', False
        ):
            self._load_balancer_untrusted_logged = True
            self._logger.info(
                'The external load balancer is not public: client addresses are not '
                'recorded, and per-address limits fall back to per-username ones'
            )
        return True

    def _load_balancer_networks_loop(self):
        while not self._load_balancer_networks_stop.is_set():
            read = self._refresh_load_balancer_networks()
            self._load_balancer_networks_stop.wait(
                LOAD_BALANCER_SUBNETS_REFRESH_SECONDS
                if read
                else LOAD_BALANCER_SUBNETS_RETRY_SECONDS
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

    def _is_local_socket_request(self, http_request) -> bool:
        # as _invoke tells the source: no socket, or the unix app
        return http_request.socket is None or (
            self._unix_app is not None and http_request.app == self._unix_app
        )

    async def invoke_api_task(self, http_request):
        executor = (
            self._local_executor
            if self._is_local_socket_request(http_request)
            else self._executor
        )
        result = executor.submit(
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
        username = None
        cwd = None
        try:
            username = await self._get_username_off_loop(http_request)
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
            await self._off_loop(
                lambda: helper.check_access(
                    cwd, check_dir=True, check_read=True, check_write=True
                )
            )

            # Parse multipart data in streaming fashion
            result = await self._process_streaming_upload(
                http_request, helper, cwd, username
            )
            # a 200 would read as a finished upload in the browser
            return sanic.response.json(
                result, status=200 if result['success'] else 400, dumps=Utils.to_json
            )

        except Exception as e:
            # the disk filling up or a quota reached while writing is the person's to act on
            refusal = e if isinstance(e, exceptions.SocaException) else os_refusal(e)
            if refusal is not None:
                self._logger.error(
                    f'Streaming file upload failed: {refusal.error_code}'
                )
                # a 200 would read as a finished upload in the browser
                return sanic.response.json(
                    {
                        'error_code': refusal.error_code,
                        'message': refusal.message,
                        'success': False,
                    },
                    status=(
                        403
                        if refusal.error_code == errorcodes.UNAUTHORIZED_ACCESS
                        else 400
                    ),
                    dumps=Utils.to_json,
                )
            self._logger.error(
                f'Streaming file upload error - User: {username}, Directory: {cwd}, Error: {str(e)}'
            )
            return sanic.response.json(
                {
                    'error_code': errorcodes.GENERAL_ERROR,
                    'message': UNEXPECTED_ERROR_MESSAGE,
                    'success': False,
                },
                status=500,
                dumps=Utils.to_json,
            )

    async def _process_streaming_upload(
        self, http_request, helper: FileSystemHelper, cwd: str, username: str
    ) -> Dict:
        """
        Process streaming multipart upload without loading files into memory.
        """
        import re
        import os

        loop = asyncio.get_running_loop()

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
        # the file being written, and the write still running on it
        upload: Optional[_Upload] = None
        writing = None
        current_headers = {}
        parsing_headers = False
        # how far the part header being read has been searched for its end
        header_scanned = 0
        # the closing delimiter was read: what follows it is read and ignored
        finished = False
        # folders this upload made, for removing those left empty if it is cut short
        made_folders = _UploadFolders()
        completed = False

        try:
            # Stream the request body
            while True:
                chunk = await http_request.stream.read()
                if chunk is None:
                    break
                if finished:
                    continue

                buffer += chunk

                # Process buffer for multipart boundaries and data
                while True:
                    if upload is None and not parsing_headers:
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
                            finished = True
                            buffer = b''
                            break

                        # Move past boundary and CRLF
                        buffer = buffer[boundary_pos + len(boundary_bytes) :]
                        if buffer.startswith(b'\r\n'):
                            buffer = buffer[2:]
                        parsing_headers = True
                        header_scanned = 0
                        current_headers = {}
                        continue

                    if parsing_headers:
                        if len(buffer) < 2:
                            break  # Need more data
                        if buffer.startswith(b'--'):
                            # the closing delimiter, its -- read apart from its boundary
                            finished = True
                            buffer = b''
                            break
                        # Parse headers until we find double CRLF, searching only what
                        # arrived since the last search, and only so far: file bytes are
                        # never capped, a part's header is
                        headers_end = buffer.find(b'\r\n\r\n', header_scanned)
                        if (
                            headers_end == -1
                            or headers_end > MAX_UPLOAD_PART_HEADER_BYTES
                        ):
                            if len(buffer) > MAX_UPLOAD_PART_HEADER_BYTES:
                                raise exceptions.invalid_params(
                                    'The upload could not be read: a part header is '
                                    'too large.'
                                )
                            header_scanned = max(0, len(buffer) - 3)
                            break  # Need more data

                        headers_data = buffer[:headers_end].decode(
                            'utf-8', errors='replace'
                        )
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
                        filename = self._part_filename(content_disposition)

                        parts = (
                            None
                            if filename is None
                            else self._upload_path_parts(filename)
                        )
                        if filename is not None:
                            # a file in a folder being uploaded names its path in the
                            # destination: each part cleaned up as a file name is
                            secure_parts = (
                                [Utils.to_secure_filename(part) for part in parts]
                                if parts is not None
                                else None
                            )
                            if secure_parts is None or not all(secure_parts):
                                files_skipped.append(filename)
                                parsing_headers = False
                                continue
                            *folders, secure_filename = secure_parts

                            # the user's file, created exclusively and handed over before
                            # a byte is written; a taken name becomes the next free name(n).
                            # Cut short while it is being created, it is removed once created.
                            upload, was_renamed = await self._off_loop_or_clean_up(
                                lambda created: self._discard_upload(
                                    helper, created[0], made_folders
                                ),
                                self._create_upload,
                                helper,
                                cwd,
                                secure_filename,
                                username,
                                tuple(folders),
                                made_folders,
                            )
                            file_path = upload.path

                            # Track renamed files, by the name clash or by the name clean-up
                            final_name = '/'.join(
                                [*folders, os.path.basename(file_path)]
                            )
                            if was_renamed or final_name != filename:
                                files_renamed.append(
                                    {
                                        'original_name': filename,
                                        'final_name': final_name,
                                    }
                                )

                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    f'Starting streaming upload - User: {username}, File: {secure_filename}'
                                )

                        parsing_headers = False
                        continue

                    if upload is not None:
                        # Look for next boundary in the data
                        next_boundary = buffer.find(b'\r\n--' + boundary.encode())

                        if next_boundary == -1:
                            # No boundary found, write all but last few bytes (in case boundary spans chunks)
                            if len(buffer) > len(boundary_bytes) + 10:
                                write_data = buffer[: -len(boundary_bytes) - 10]
                                buffer = buffer[-len(boundary_bytes) - 10 :]
                                writing = loop.run_in_executor(
                                    None, upload.file.write, write_data
                                )
                                await writing
                            break
                        else:
                            # Found boundary, write data up to boundary
                            write_data = buffer[:next_boundary]
                            writing = loop.run_in_executor(
                                None, upload.file.write, write_data
                            )
                            await writing

                            # raises if the last bytes could not be written: the file is
                            # then removed below like any upload cut short
                            await self._off_loop(upload.finish)
                            # on the loop, with no await before upload is cleared: a
                            # cancel during the finish above leaves the folder to the
                            # clean-up, which closes it once
                            upload.close_folder()
                            files_uploaded.append(upload.path)

                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    f'Completed streaming upload - User: {username}, File: {upload.name}'
                                )

                            upload = None

                            # Move buffer past the CRLF before boundary
                            buffer = buffer[next_boundary + 2 :]  # Skip \r\n
                            continue

                    # If we get here, we need more data
                    break

            completed = finished
        finally:
            if upload is not None:
                # an upload cut short, by an error or by the browser going away at any await
                # above: shielded, so being cancelled again cannot stop the clean up halfway
                await asyncio.shield(
                    self._abandon_upload(helper, upload, writing, made_folders)
                )
            elif made_folders and not completed:
                await asyncio.shield(
                    self._off_loop(self._remove_empty_folders, helper, made_folders)
                )

        payload = {
            'files_uploaded': files_uploaded,
            'files_skipped': files_skipped,
            'files_renamed': files_renamed,
        }
        if not finished:
            # the body ended inside a part: whatever was in it is not saved, the parts that
            # completed before it are
            return {
                'success': False,
                'error_code': errorcodes.INVALID_PARAMS,
                'message': 'The upload ended before it was complete, and the file it was '
                'sending was not saved. Try again.',
                'payload': payload,
            }

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f'Streaming upload completed - User: {username}, Directory: {cwd}, '
                f'Uploaded: {len(files_uploaded)}, Skipped: {len(files_skipped)}, Renamed: {len(files_renamed)}'
            )

        return {'success': True, 'payload': payload}

    @staticmethod
    def _part_filename(content_disposition: str) -> Optional[str]:
        # the first filename="..." value, as the pattern it replaces read it, by two finds
        start = content_disposition.find('filename="')
        if start == -1:
            return None
        start += len('filename="')
        end = content_disposition.find('"', start)
        return None if end == -1 else content_disposition[start:end]

    def _candidate_upload_paths(self, directory: str, filename: str):
        """
        The names an upload may take, in order: the file's own, name(1) to name(999), then
        a timestamped fallback. Whether a name is free is decided by the exclusive create
        in _create_upload, never by looking first.
        """
        import time

        yield os.path.join(directory, filename), False
        name, ext = os.path.splitext(filename)
        for counter in range(1, 1000):
            yield os.path.join(directory, f'{name}({counter}){ext}'), True
        yield os.path.join(directory, f'{name}_{int(time.time())}{ext}'), True

    @staticmethod
    def _open_directory_nofollow(path: str) -> int:
        """
        a descriptor for an absolute directory path that holds no symbolic link, opened one
        component at a time without following links: a directory swapped for a link after
        the path was checked fails the open instead of redirecting it
        """
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in [p for p in path.split('/') if p]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
                # the new one is the one to close from here: never the old one twice
                previous, fd = fd, next_fd
                os.close(previous)
        except OSError:
            os.close(fd)
            raise exceptions.unauthorized_access()
        return fd

    def _discard_upload(
        self, helper: FileSystemHelper, upload: '_Upload', made_folders=None
    ):
        """
        Closes an upload cut short and removes what was written of it, then the folders
        the upload made that are left empty.
        """
        try:
            upload.file.close()
        except OSError:
            pass  # what could not be written is removed with the rest
        try:
            if upload.dir_fd is not None:
                self._remove_created_upload(
                    helper, upload.dir_fd, upload.name, upload.dev, upload.ino
                )
        finally:
            upload.close_folder()
        if made_folders:
            self._remove_empty_folders(helper, made_folders)

    async def _abandon_upload(
        self, helper: FileSystemHelper, upload: '_Upload', writing, made_folders=None
    ):
        if writing is not None:
            # a write cancelled on the loop still runs on its thread: let it end first
            await asyncio.wait([writing])
        await self._off_loop(self._discard_upload, helper, upload, made_folders)

    @staticmethod
    def _upload_path_parts(raw: str) -> Optional[List[str]]:
        """
        A file's path inside the upload's destination, as the browser sends it for a folder
        upload (photos/2024/a.jpg), split into its names; None for any other form: absolute,
        an empty, . or .. part, a backslash or control character, a part over 255 bytes, or
        deeper than MAX_UPLOAD_FOLDER_DEPTH folders.
        """
        # a backslash is a separator only to some clients: refused in a folder path, and
        # in a plain name cleaned away as before
        if len(raw) > 4096 or raw.startswith('/') or ('/' in raw and '\\' in raw):
            return None
        if any(ord(c) < 32 or ord(c) == 127 for c in raw):
            return None
        parts = raw.split('/')
        if len(parts) > MAX_UPLOAD_FOLDER_DEPTH + 1:
            return None
        for part in parts:
            if part in ('', '.', '..') or len(part.encode('utf-8')) > 255:
                return None
        return parts

    def _open_upload_folders(
        self,
        helper: FileSystemHelper,
        dir_fd: int,
        resolved: str,
        folders: tuple,
        made_folders: list,
    ) -> int:
        """
        Opens the folders below the destination one level at a time from its descriptor,
        never following a link, making a missing one as the user. An existing folder is
        used only if it is a real folder the user may write and enter. Returns the deepest
        one's descriptor and closes dir_fd.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for depth, name in enumerate(folders):
            try:
                try:
                    next_fd = os.open(name, flags, dir_fd=dir_fd)
                except FileNotFoundError:
                    try:
                        helper.run_as_user(
                            lambda name=name, dir_fd=dir_fd: os.mkdir(
                                name, 0o777, dir_fd=dir_fd
                            )
                        )
                        made = True
                    except FileExistsError:
                        made = False  # another part of this upload, or someone, just made it
                    next_fd = os.open(name, flags, dir_fd=dir_fd)
                    if made:
                        info = os.fstat(next_fd)
                        made_folders.append(
                            (resolved, folders[: depth + 1], info.st_dev, info.st_ino)
                        )
            except OSError:
                if os.path.islink(os.path.join(resolved, *folders[: depth + 1])):
                    raise exceptions.unauthorized_access(
                        'A link is where a folder of the upload belongs'
                    )
                raise exceptions.invalid_params(
                    f'A file is in the way of the folder: {name}'
                )
            finally:
                os.close(dir_fd)
            dir_fd = next_fd

            # checked already in this upload, and still the same folder: no new check
            info = os.fstat(dir_fd)
            key = (resolved, folders[: depth + 1])
            checked = getattr(made_folders, 'checked', None)
            if checked is not None and checked.get(key) == (info.st_dev, info.st_ino):
                continue

            def may_write(fd=dir_fd):
                os.fchdir(fd)
                return os.access('.', os.W_OK | os.X_OK)

            try:
                if not helper.run_as_user(may_write):
                    raise exceptions.unauthorized_access()
            except BaseException:
                os.close(dir_fd)
                raise
            if checked is not None:
                checked[key] = (info.st_dev, info.st_ino)
        return dir_fd

    def _remove_empty_folders(self, helper: FileSystemHelper, made_folders: list):
        """
        Removes, as the user, the folders an upload cut short made and left empty, deepest
        first, each only while its name is still the folder that was made; reached from
        the destination again without following links.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for resolved, path, dev, ino in sorted(made_folders, key=lambda m: -len(m[1])):
            try:
                fd = self._open_directory_nofollow(resolved)
            except exceptions.SocaException:
                continue
            try:
                for name in path[:-1]:
                    next_fd = os.open(name, flags, dir_fd=fd)
                    previous, fd = fd, next_fd
                    os.close(previous)

                def remove(fd=fd, name=path[-1]):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (dev, ino):
                        os.rmdir(name, dir_fd=fd)

                helper.run_as_user(remove)
            except (OSError, exceptions.SocaException):
                pass  # not empty, or no longer the folder made: it stays
            finally:
                os.close(fd)

    async def _off_loop_or_clean_up(self, clean_up, function, *args):
        """
        _off_loop for work that returns something to release: if the request is cancelled
        while the work runs, the work still finishes on its thread, and what it returns is
        handed to clean_up on a worker instead of being dropped.
        """
        work = self._executor.submit(function, *args)
        try:
            # cancelling stops the work if it has not started, and cannot once it has
            return await asyncio.wrap_future(work)
        except asyncio.CancelledError:

            def abandoned(done):
                if done.cancelled() or done.exception() is not None:
                    return
                try:
                    self._executor.submit(clean_up, done.result())
                except RuntimeError:
                    clean_up(
                        done.result()
                    )  # shutting down: no worker left to hand it to

            # on the work's own future, so it runs whatever the loop is doing by then
            work.add_done_callback(abandoned)
            raise

    @staticmethod
    def _unlink_if_created(dir_fd: int, name: str, dev: int, ino: int):
        # only while the name is still the file that was created
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == (dev, ino):
                os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass

    def _remove_created_upload(
        self, helper: FileSystemHelper, dir_fd: int, name: str, dev: int, ino: int
    ):
        """
        Removes an upload cut short, as the uploading user, from the folder descriptor it was
        created through: the entry is checked and unlinked only while it is still the file
        that was created, and with the user's own rights, so a file put in its place by
        someone else is never theirs to remove.
        """

        try:
            helper.run_as_user(lambda: self._unlink_if_created(dir_fd, name, dev, ino))
        except exceptions.SocaException as e:
            # what is left is the user's partial upload, under the name they gave it
            self._logger.warning(
                f'An interrupted upload could not be removed - File: {name}, Error: {e.error_code}'
            )

    def _create_upload(
        self,
        helper: FileSystemHelper,
        directory: str,
        filename: str,
        username: str,
        folders: tuple = (),
        made_folders: Optional[list] = None,
    ) -> tuple:
        """
        Creates the upload's file exclusively and gives it to the user by descriptor before
        anything is written. The folder is resolved once (a folder the user reached through
        a link of their own stays usable), checked as the user, then opened component by
        component without following links, and the file is created relative to it. A name
        that exists, a link planted at it (dangling or not) included, is neither opened nor
        followed: the next free name is taken. No path is chowned after the fact.
        Returns (upload, was_renamed): the upload holds the file open for writing and its
        folder open, for removing the file through it if the upload is cut short.
        folders are the file's folders below directory, for a folder upload: made as the
        user where missing (see _open_upload_folders) and listed in made_folders.
        """
        resolved = os.path.realpath(directory)
        destinations = getattr(made_folders, 'destinations', None)
        dir_fd = None
        if destinations is not None and resolved in destinations:
            # checked earlier in this upload: still the same folder, or checked again
            dir_fd = self._open_directory_nofollow(resolved)
            info = os.fstat(dir_fd)
            if destinations[resolved] != (info.st_dev, info.st_ino):
                os.close(dir_fd)
                dir_fd = None
        if dir_fd is None:
            helper.check_access(
                resolved, check_dir=True, check_read=False, check_write=True
            )
            dir_fd = self._open_directory_nofollow(resolved)
            if destinations is not None:
                info = os.fstat(dir_fd)
                destinations[resolved] = (info.st_dev, info.st_ino)
        if folders:
            dir_fd = self._open_upload_folders(
                helper,
                dir_fd,
                resolved,
                folders,
                made_folders if made_folders is not None else [],
            )
        try:
            for file_path, was_renamed in self._candidate_upload_paths(
                resolved, filename
            ):
                name = os.path.basename(file_path)
                try:
                    fd = os.open(
                        name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                        0o644,
                        dir_fd=dir_fd,
                    )
                except FileExistsError:
                    continue
                try:
                    helper.own(fd, keep_group=bool(os.fstat(dir_fd).st_mode & S_ISGID))
                except Exception:
                    info = os.fstat(fd)
                    os.close(fd)
                    self._remove_created_upload(
                        helper, dir_fd, name, info.st_dev, info.st_ino
                    )
                    # never handed over, so the server's own: in a sticky folder the user
                    # may not remove it, and an empty file would hold the name
                    self._unlink_if_created(dir_fd, name, info.st_dev, info.st_ino)
                    raise
                if was_renamed and self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        f'File conflict resolved - User: {username}, Original: {filename}, Renamed: {name}'
                    )
                info = os.fstat(fd)
                upload = _Upload(
                    os.fdopen(fd, 'wb'),
                    os.path.join(directory, *folders, name),
                    dir_fd,
                    info.st_dev,
                    info.st_ino,
                )
                return upload, was_renamed
        except BaseException:
            os.close(dir_fd)
            raise
        os.close(dir_fd)
        raise exceptions.general_exception(f'no free name for upload: {filename}')

    async def generate_download_url_route(self, http_request, **_):
        """Generate a temporary signed URL for secure file downloads"""
        # set before anything can raise: the handlers below name both in their log line
        username = file_path = None
        try:
            username = await self._get_username_off_loop(http_request)
            if Utils.is_empty(username):
                # no session: the same answer /download gives, and nothing to log
                return sanic.response.json(
                    {'error_code': errorcodes.UNAUTHORIZED_ACCESS, 'success': False},
                    status=401,
                    dumps=Utils.to_json,
                )

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
            await self._off_loop(
                lambda: helper.check_access(
                    file_path,
                    check_dir=os.path.isdir(file_path),
                    check_read=True,
                    check_write=False,
                )
            )

            # Use the dedicated JWT signing secret from cluster settings: read first, so
            # the time it takes is not taken from the link's five seconds
            jwt_secret = await self._get_jwt_signing_secret_off_loop()

            # Generate a temporary token valid for 5 seconds
            payload = {
                'username': username,
                'file': file_path,
                'exp': int(time.time()) + 5,  # 5 second expiry
                'iat': int(time.time()),
                'purpose': 'file_download',
            }
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
                    'message': UNEXPECTED_ERROR_MESSAGE,
                    'success': False,
                },
                dumps=Utils.to_json,
            )

    async def file_download_route(self, http_request, **_):
        # the browser saves whatever a 200 carries as the file, so every refusal here is a real status
        username = None

        # Try Bearer token first (for API calls)
        username = await self._get_username_off_loop(http_request)

        # If no Bearer token, try temporary download token (for direct downloads)
        if Utils.is_empty(username):
            temp_token = self.get_query_param_as_string('temp_token', http_request)
            if Utils.is_not_empty(temp_token):
                try:
                    import jwt

                    # Decode the temporary token using the same secret
                    jwt_secret = await self._get_jwt_signing_secret_off_loop()
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
                        status=401,
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
                status=401,
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
                status=400,
                dumps=Utils.to_json,
            )

        helper = FileSystemHelper(self._context, username=username)
        try:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f'File download access check - User: {username}, File: {download_file}'
                )
            is_dir, *identity = await self._off_loop(
                helper.identify_download, download_file
            )
        except exceptions.SocaException as e:
            self._logger.error(
                f'File download access denied - User: {username}, File: {download_file}, Error: {e.message}'
            )
            return sanic.response.json(
                {'error_code': e.error_code, 'message': e.message, 'success': False},
                status=404 if e.error_code == errorcodes.FILE_NOT_FOUND else 403,
                dumps=Utils.to_json,
            )

        # Check if the requested item is a directory
        if is_dir:
            self._logger.info(
                f'Directory download requested - User: {username}, Directory: {download_file}'
            )

            # Create zip archive for directory download
            try:
                from ideadatamodel.filesystem import DownloadFilesRequest

                # Use the FileSystemHelper to create a zip archive
                download_request = DownloadFilesRequest(files=[download_file])
                # building the archive holds a server worker: it counts towards the limits
                zip_file_path = await self._off_loop(
                    helper.download_files, download_request
                )
                _, *identity = await self._off_loop(
                    helper.identify_download, zip_file_path
                )
                # opening can wait on the mount: not on the loop
                source, zip_size = await self._off_loop(
                    self._open_checked_download, zip_file_path, identity
                )

                # Stream the zip file
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
                    source,
                    zip_filename,
                    zip_size,
                    username,
                    after_streamed=lambda: helper.remove_generated_archive(
                        zip_file_path
                    ),
                )

            except exceptions.SocaException as e:
                self._logger.error(
                    f'Directory download refused - User: {username}, Directory: {download_file}, Error: {e.message}'
                )
                return sanic.response.json(
                    {
                        'error_code': e.error_code,
                        'message': e.message,
                        'success': False,
                    },
                    status=403,
                    dumps=Utils.to_json,
                )
            except Exception as e:
                self._logger.error(
                    f'Directory zip creation failed - User: {username}, Directory: {download_file}, Error: {str(e)}'
                )
                return sanic.response.json(
                    {
                        'error_code': errorcodes.GENERAL_ERROR,
                        'message': UNEXPECTED_ERROR_MESSAGE,
                        'success': False,
                    },
                    status=500,
                    dumps=Utils.to_json,
                )
        else:
            # Handle regular file download
            try:
                source, file_size = await self._off_loop(
                    self._open_checked_download, download_file, identity
                )
                generated = await self._off_loop(
                    helper.is_generated_archive, download_file
                )
            except exceptions.SocaException as e:
                self._logger.error(
                    f'File download refused - User: {username}, File: {download_file}, Error: {e.message}'
                )
                return sanic.response.json(
                    {
                        'error_code': e.error_code,
                        'message': e.message,
                        'success': False,
                    },
                    status=403,
                    dumps=Utils.to_json,
                )
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
            # an archive built for this download: it has done its job once it has been sent
            return await self._stream_file_download(
                source,
                filename,
                file_size,
                username,
                after_streamed=(
                    (lambda: helper.remove_generated_archive(download_file))
                    if generated
                    else None
                ),
            )

    @staticmethod
    def _open_checked_download(path: str, identity) -> Tuple[BinaryIO, int]:
        """
        Opens, with the server's rights, the file the user's own check identified and no
        other: a path that names a different file by now, through a link swapped in after
        the check for example, is refused. Returns the open file and its size.
        """
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            raise exceptions.unauthorized_access()
        try:
            info = os.fstat(fd)
            if not S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != list(
                identity
            ):
                raise exceptions.unauthorized_access()
            return os.fdopen(fd, 'rb'), info.st_size
        except BaseException:
            os.close(fd)
            raise

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
        self,
        source: BinaryIO,
        filename: str,
        file_size: int,
        username: str = None,
        after_streamed=None,
    ):
        """
        Stream file downloads with proper headers for browser progress.
        This enables native browser download progress bars for all file sizes.
        Uses adaptive chunk sizing optimized for ALB and high-bandwidth connections.
        Sends from the open file it is given, and closes it once sent.
        """
        import mimetypes
        import urllib.parse

        # Determine content type
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = 'application/octet-stream'

        # Properly encode filename for Content-Disposition header
        # Use RFC 6266 encoding for international characters
        encoded_filename = urllib.parse.quote(filename)
        # the quoted form is for old clients only: a quote, backslash or control character
        # in a legal file name would end it or the header early
        plain_filename = ''.join(
            c if 32 <= ord(c) < 127 and c not in '"\\' else '_' for c in filename
        )

        # Calculate optimal chunk size based on file size
        chunk_size = self._get_adaptive_chunk_size(file_size)

        # Set up headers that enable browser progress bars
        headers = {
            'Content-Disposition': f'attachment; filename="{plain_filename}"; filename*=UTF-8\'\'{encoded_filename}',
            'Content-Length': str(file_size),  # Critical for browser progress
            'Content-Type': content_type,
            'Cache-Control': 'no-cache',  # Prevent caching of potentially sensitive files
            # a user's file comes from the portal's own origin: never run it as a page there
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': 'sandbox',
        }

        if self._logger.isEnabledFor(logging.DEBUG):
            user_context = f'User: {username}, ' if username else ''
            chunk_size_mb = chunk_size / (1024 * 1024)
            self._logger.debug(
                f'File download streaming - {user_context}File: {filename}, Size: {file_size} bytes, '
                f'Chunk size: {chunk_size_mb:.1f}MB'
            )

        # from the descriptor that was checked: a path would be looked up again
        # Adding Content-Length header automatically disables chunked encoding
        async def send_source(stream):
            # what Content-Length said and no more: a log still being written grows past it
            with source:
                loop = asyncio.get_running_loop()
                remaining = file_size
                while remaining > 0 and (
                    chunk := await loop.run_in_executor(
                        None, source.read, min(chunk_size, remaining)
                    )
                ):
                    remaining -= len(chunk)
                    await stream.write(chunk)

        response = sanic.response.ResponseStream(
            send_source, headers=headers, content_type=content_type
        )
        if after_streamed is not None:
            send = response.streaming_fn

            # not on a cancelled download: the file is still there to try again
            async def send_then_clean_up(stream):
                await send(stream)
                try:
                    # removing as the user forks a worker: not on the loop
                    await self._off_loop(after_streamed)
                except Exception as e:
                    self._logger.warning(
                        f'Clean up after download failed - File: {filename}, Error: {e}'
                    )

            response.streaming_fn = send_then_clean_up
        return response

    async def _off_loop(self, function, *args):
        # work done as the user forks and waits for the worker: on a server worker, with the
        # API calls, not on the loop that serves every request and the health check
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, function, *args
        )

    async def _get_username_off_loop(self, http_request) -> Optional[str]:
        # an api token is looked up in the database: not on the loop that serves every request
        return await asyncio.get_running_loop().run_in_executor(
            None, self._api_invocation_handler.get_file_username, http_request
        )

    async def _get_jwt_signing_secret_off_loop(self) -> str:
        cached = getattr(self, '_jwt_signing_secret', None)
        if (
            cached is not None
            and time.monotonic() - cached[1] < JWT_SECRET_CACHE_SECONDS
        ):
            return cached[0]
        secret = await asyncio.get_running_loop().run_in_executor(
            None, self._get_jwt_signing_secret
        )
        self._jwt_signing_secret = (secret, time.monotonic())
        return secret

    def _get_jwt_signing_secret(self) -> str:
        """
        Get the JWT signing secret for secure download URLs.
        The secret is created by the cluster-manager CDK stack and its ARN is stored in cluster settings.
        Read through _get_jwt_signing_secret_off_loop, which keeps it for a few minutes.
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

        if self.options.enable_http:
            Thread(
                target=self._load_balancer_networks_loop,
                name=f'{self._context.module_id()}-load-balancer-subnets',
                daemon=True,
            ).start()

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
        self._load_balancer_networks_stop.set()

        # Run application cleanup on the serving loop before closing its transports.
        deadline = time.monotonic() + 30
        for server in (self._http_server, self._unix_server):
            if server is not None and server.app.state.is_started:
                cleanup = asyncio.run_coroutine_threadsafe(
                    server.before_stop(), self._server_loop
                )
                try:
                    cleanup.result(timeout=max(0, deadline - time.monotonic()))
                except Exception as error:
                    cleanup.cancel()
                    self._logger.error(
                        'before_stop failed code=%s; continuing transport shutdown',
                        type(error).__name__,
                    )

        # shutdown executor and wait for current pending requests to complete and allow to respond back
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._local_executor.shutdown(wait=True, cancel_futures=True)

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
