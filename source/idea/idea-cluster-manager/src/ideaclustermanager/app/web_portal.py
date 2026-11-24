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

import ideaclustermanager
import ideaclustermanager.app.app_messages as app_messages

from ideasdk.utils import Utils, EnvironmentUtils, Jinja2Utils, ModuleMetadataHelper
from ideasdk.server import SocaServer
from ideadatamodel import errorcodes, exceptions, constants

import asyncio
import hmac
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sanic
from typing import Dict, Optional
from datetime import datetime


# any key S3 holds, without control characters, ending in an image extension of any case
LOGO_KEY_PATTERN = r'[^\x00-\x1f\x7f\\]+\.(png|jpe?g|gif|svg|webp)'


class WebPortal:
    # Holds a random key for the sign-in this browser started. The state row keeps only
    # its hash, the callback stores tokens only for the browser holding it, and the page
    # hands the code that redeems them only to that browser. One cookie per sign-in,
    # named after its state, so sign-ins in two tabs do not replace each other's key.
    # Sent as __Host-<name>: host-only, so no other host under the domain can set it.
    SSO_BROWSER_COOKIE = 'idea-sso'
    # The sign-in routes need no account and wait on DynamoDB and the user pool: that
    # work runs on these threads, never on the loop that serves every request, and a
    # request past this many waiting is refused at once.
    SSO_WORKERS = 4
    SSO_PENDING = 32
    # sign-in cookies one browser keeps: starting another clears the oldest, so
    # abandoned sign-ins cannot grow the request header past the server's limit
    SSO_SIGN_INS_KEPT = 4

    def __init__(self, context: ideaclustermanager.AppContext, server: SocaServer):
        self.context = context
        self.server = server
        self.logger = context.logger('web-portal')
        self.module_metadata_helper = ModuleMetadataHelper()
        self.web_app_dir = self.get_web_app_dir()
        self.web_resources_context_path = (
            self.context.config()
            .get_string('cluster-manager.web_resources_context_path', '/')
            .rstrip('/')
            or '/'
        )
        self.web_template_env = Jinja2Utils.env_using_file_system_loader(
            search_path=self.web_app_dir, auto_escape=True
        )
        self._sso_executor = ThreadPoolExecutor(
            max_workers=self.SSO_WORKERS, thread_name_prefix='web-portal-sso'
        )
        self._sso_pending = threading.BoundedSemaphore(self.SSO_PENDING)

    @classmethod
    def sso_cookie_name(cls, state: str) -> Optional[str]:
        # a state is a uuid; anything else names no cookie this server set
        if not isinstance(state, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', state):
            return None
        return f'{cls.SSO_BROWSER_COOKIE}-{state}'

    @classmethod
    def sso_browser_key(cls, http_request, state: str) -> str:
        name = cls.sso_cookie_name(state)
        if name is None:
            return ''
        return cls._host_cookie(http_request, name)

    @staticmethod
    def _host_cookie(http_request, name: str) -> str:
        # the exact __Host- name only: an unprefixed cookie, or the framework's fallback
        # to a __Secure- one, can come from another host under the domain
        value = dict.get(http_request.cookies, f'__Host-{name}')
        if isinstance(value, list):
            value = value[0] if value else None
        return value or ''

    async def _off_loop(self, fn, *args):
        if not self._sso_pending.acquire(blocking=False):
            raise exceptions.general_exception('single sign-on is busy')
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._sso_executor, fn, *args
            )
        finally:
            self._sso_pending.release()

    @staticmethod
    def is_dev_mode() -> bool:
        return Utils.get_as_bool(EnvironmentUtils.idea_dev_mode(), False)

    @property
    def dev_mode_web_portal_root_dir(self) -> str:
        script_dir = Path(os.path.abspath(__file__))
        return str(script_dir.parent.parent.parent.parent)

    def get_web_app_dir(self) -> str:
        if self.is_dev_mode():
            web_sources_dir = os.path.join(self.dev_mode_web_portal_root_dir, 'webapp')
            html_build_dir = os.path.join(web_sources_dir, 'build-dev-mode')
            os.makedirs(html_build_dir, exist_ok=True)
            index_page = os.path.join(html_build_dir, 'index.html')
            with open(index_page, 'w') as f:
                f.write(app_messages.DEV_MODE_INDEX_PAGE)
            return html_build_dir
        else:
            return os.path.join(Utils.app_deploy_dir(), 'cluster-manager', 'webapp')

    def get_logo_url(self) -> str:
        logo = self.context.config().get_string('cluster-manager.web_portal.logo')
        if Utils.is_not_empty(logo):
            # logo is provided as an external url or another file in public folder.
            if logo.startswith('https://') or logo.startswith('/'):
                return logo

            # a key of an image in the cluster's bucket: the sign-in page is served
            # without authentication, so no other object is signed for it
            if re.fullmatch(LOGO_KEY_PATTERN, logo, re.IGNORECASE) and '..' not in logo:
                return self.context.aws_util().create_s3_presigned_url(
                    key=logo,
                    expires_in=12 * 60 * 60,  # 12 hours
                )
            self.logger.warning('web_portal.logo is not an image key: default logo')

        return self.make_route_path(
            '/logo.png'
        )  # default IDEA logo in public directory

    def get_copyright_text(self) -> str:
        copyright_text = self.context.config().get_string(
            'cluster-manager.web_portal.copyright_text'
        )

        if Utils.is_empty(copyright_text):
            copyright_text = constants.DEFAULT_COPYRIGHT_TEXT

        copyright_text = copyright_text.replace('{year}', str(datetime.now().year))
        return copyright_text

    def build_app_init_data(self, http_request) -> Dict:
        sso_enabled = self.context.config().get_bool(
            'identity-provider.cognito.sso_enabled', False
        )

        # build module metadata and applicable api context paths
        module_set_id = Utils.get_as_string(
            self.server.get_query_param_as_string('module_set', http_request),
            self.context.module_set(),
        )
        modules = []
        module_set = self.context.config().get_config(
            f'global-settings.module_sets.{module_set_id}', default=None
        )
        if not module_set:
            raise exceptions.invalid_params('unknown module set')
        for module_name in module_set:
            module_metadata = self.module_metadata_helper.get_module_metadata(
                module_name
            )
            api_context_path = None
            module_id = module_set.get_string(f'{module_name}.module_id')
            # send from server side, instead of having path info hard-coded clients
            # scope for further enhancements to read context path from applicable module's configuration
            if module_metadata.type == constants.MODULE_TYPE_APP:
                api_context_path = f'/{module_id}/api/v1'
            modules.append(
                {
                    **Utils.to_dict(module_metadata),
                    'module_id': module_id,
                    'api_context_path': api_context_path,
                }
            )

        app_init_data = {
            'version': ideaclustermanager.__version__,
            'sso': sso_enabled,
            'sso_url': self.make_route_path('/sso'),
            'cluster_name': self.context.cluster_name(),
            'aws_region': self.context.aws().aws_region(),
            'title': self.context.config().get_string(
                'cluster-manager.web_portal.title',
                'Integrated Digital Engineering on AWS',
            ),
            'subtitle': self.context.config().get_string(
                'cluster-manager.web_portal.subtitle',
                f'{self.context.cluster_name()} ({self.context.aws().aws_region()})',
            ),
            'logo': self.get_logo_url(),
            'copyright_text': self.get_copyright_text(),
            'default_log_level': self.context.config().get_int(
                'cluster-manager.web_portal.default_log_level', 3
            ),
            'module_set': module_set_id,
            'modules': modules,
            'web_terminal_enabled': self.context.config().get_bool(
                'cluster-manager.web_terminal.enabled', True
            ),
            'session_management': self.context.config().get_string(
                'cluster-manager.web_portal.session_management', 'in-memory'
            ),
            # the maintenance notice travels with the page so it renders on the sign-in
            # screen, where there is no token to call the settings api with. once signed in
            # the portal re-reads it from the api every minute; this value is only as fresh
            # as the page load.
            'maintenance': {
                'enabled': self.context.config().get_bool(
                    'cluster-manager.maintenance.enabled', False
                ),
                'message': self.context.config().get_string(
                    'cluster-manager.maintenance.message', ''
                ),
                'ends_at': self.context.config().get_string(
                    'cluster-manager.maintenance.ends_at', ''
                ),
            },
        }

        if sso_enabled:
            sso_auth_status = self.server.get_query_param_as_string(
                'sso_auth_status', http_request
            )
            if Utils.is_not_empty(sso_auth_status):
                app_init_data['sso_auth_status'] = sso_auth_status

            sso_auth_code = self.server.get_query_param_as_string(
                'sso_auth_code', http_request
            )
            # only the cookie of the sign-in this code belongs to: a stale code finds
            # none and leaves a sign-in in progress alone
            browser_key = self.sso_browser_key(http_request, sso_auth_code)
            if Utils.is_not_empty(sso_auth_code) and Utils.is_not_empty(browser_key):
                app_init_data['sso_auth_code'] = f'{sso_auth_code}.{browser_key}'

        return app_init_data

    async def index_route(self, http_request):
        template = self.web_template_env.get_template('index.html')
        try:
            app_init_data = self.build_app_init_data(http_request)
        except exceptions.SocaException as error:
            if error.error_code != errorcodes.INVALID_PARAMS:
                raise
            return sanic.response.text('unknown module set', status=400)
        result = template.render(
            app_init_data=Utils.base64_encode(Utils.to_json(app_init_data))
        )

        # add X-Frame-Options header to mitigate ClickJacking (embedding WebPortal within an iframe)
        response = sanic.response.html(
            body=result, status=200, headers={'X-Frame-Options': 'SAMEORIGIN'}
        )
        if 'sso_auth_code' in app_init_data:
            # the page carries a one-time code: no cache keeps it
            response.headers['Cache-Control'] = 'no-store'
            # the page now holds the key: this sign-in's cookie has done its work
            response.delete_cookie(
                self.sso_cookie_name(app_init_data['sso_auth_code'].partition('.')[0]),
                path='/',
                host_prefix=True,
            )
        return response

    async def sso_initiate_route(self, http_request):
        sso_enabled = self.context.config().get_bool(
            'identity-provider.cognito.sso_enabled', False
        )
        if not sso_enabled:
            return sanic.response.redirect(self.web_resources_context_path)

        try:
            domain_url = self.context.config().get_string(
                'identity-provider.cognito.domain_url', required=True
            )
            sso_client_id = self.context.config().get_string(
                'identity-provider.cognito.sso_client_id', required=True
            )
            sso_idp_provider_name = self.context.config().get_string(
                'identity-provider.cognito.sso_idp_provider_name', required=True
            )
            external_endpoint_url = (
                self.context.config().get_cluster_external_endpoint()
            )
            state = Utils.uuid()
            redirect_uri = (
                f'{external_endpoint_url}{self.make_route_path("/sso/oauth2/callback")}'
            )
            query_params = [
                'response_type=code',
                f'redirect_uri={Utils.url_encode(redirect_uri)}',
                f'state={state}',
                f'client_id={sso_client_id}',
                f'identity_provider={sso_idp_provider_name}',
            ]
            authorization_url = f'{domain_url}/authorize?{"&".join(query_params)}'

            browser_key = secrets.token_urlsafe(32)

            sso_state_dao = self.context.accounts.sso_state_dao
            await self._off_loop(
                sso_state_dao.create_sso_state,
                {
                    'state': state,
                    'redirect_uri': redirect_uri,
                    'browser_key_hash': Utils.sha256(browser_key),
                },
            )

            self.logger.debug(f'redirecting: {authorization_url}')
            response = sanic.response.redirect(authorization_url)
            # Lax: the identity provider's redirect back is a top-level navigation
            response.add_cookie(
                self.sso_cookie_name(state),
                browser_key,
                path='/',
                secure=True,
                httponly=True,
                samesite='Lax',
                max_age=sso_state_dao.STATE_SECONDS,
                host_prefix=True,
            )
            prefix = f'__Host-{self.SSO_BROWSER_COOKIE}-'
            held = (
                []
                if http_request is None
                else [
                    k for k in dict.keys(http_request.cookies) if k.startswith(prefix)
                ]
            )
            # a browser sends cookies of one path oldest first (RFC 6265 5.4)
            for key in held[: max(0, len(held) - (self.SSO_SIGN_INS_KEPT - 1))]:
                response.delete_cookie(
                    key[len('__Host-') :], path='/', host_prefix=True
                )
            return response
        except Exception as e:
            self.logger.exception(f'failed to initiate sso: {e}')
            return sanic.response.redirect(
                f'{self.web_resources_context_path}?sso_auth_status=FAIL'
            )

    async def sso_oauth2_callback_route(self, http_request):
        sso_enabled = self.context.config().get_bool(
            'identity-provider.cognito.sso_enabled', False
        )
        if not sso_enabled:
            return sanic.response.redirect(self.web_resources_context_path)

        try:
            authorization_code = self.server.get_query_param_as_string(
                'code', http_request
            )
            state = self.server.get_query_param_as_string('state', http_request)
            error = self.server.get_query_param_as_string('error', http_request)
            error_description = self.server.get_query_param_as_string(
                'error_description', http_request
            )

            if Utils.is_not_empty(error):
                # the provider's text, quoted and bounded
                self.logger.warning(
                    f'sso auth failure: error={error[:200]!r}, '
                    f'error_description={(error_description or "")[:500]!r}'
                )
                return sanic.response.redirect(
                    f'{self.web_resources_context_path}?sso_auth_status=FAIL'
                )

            if Utils.is_empty(authorization_code):
                raise exceptions.invalid_params('code is required')
            if Utils.is_empty(state):
                raise exceptions.invalid_params('state is required')
            browser_key = self.sso_browser_key(http_request, state)
            if Utils.is_empty(browser_key):
                # no sign-in this browser started: nothing to look up
                return sanic.response.redirect(
                    f'{self.web_resources_context_path}?sso_auth_status=FAIL'
                )

            location = await self._off_loop(
                self._finish_sso,
                authorization_code,
                state,
                browser_key,
            )
            return sanic.response.redirect(location)

        except Exception as e:
            self.logger.exception(f'failed to process sso oauth2 callback: {e}')
            return sanic.response.redirect(
                f'{self.web_resources_context_path}?sso_auth_status=FAIL'
            )

    def _finish_sso(self, authorization_code: str, state: str, browser_key: str) -> str:
        """
        the callback's DynamoDB and user pool work, on the sign-in threads
        :return: where the browser goes next
        """
        failed = f'{self.web_resources_context_path}?sso_auth_status=FAIL'

        sso_state = self.context.accounts.sso_state_dao.get_sso_state(state=state)
        # only the browser that started this sign-in may finish it
        if sso_state is None or not hmac.compare_digest(
            Utils.sha256(browser_key),
            Utils.get_value_as_string('browser_key_hash', sso_state, ''),
        ):
            raise exceptions.invalid_params(
                f'sso state not found for this browser: {state}'
            )

        auth_result = self.context.token_service.get_access_token_using_sso_auth_code(
            authorization_code=authorization_code,
            redirect_uri=sso_state['redirect_uri'],
        )

        # decode the access token and check if the username matches to the user that exists in DB
        claims = self.context.token_service.decode_token(auth_result.access_token)
        # the username only: neither the sign-in code nor the claims go to the log
        username = Utils.get_value_as_string('username', claims)
        self.logger.info(f'sso sign-in: username={username}')

        if Utils.is_empty(username):
            self.logger.error('sso sign-in: no username in the token')
            return failed

        self.logger.debug(f'SSO auth: Looking up user: {username}')
        existing_user = self.context.accounts.user_dao.get_user(username=username)

        if existing_user is None:
            self.logger.warning(
                f'SSO auth: {username} is not previously known. User must be created before first SSO attempt'
            )
            self.logger.info(f'disabling federated user in user pool: {username}')
            self.context.user_pool.admin_disable_user(username=username)
            self.logger.info(f'deleting federated user in user pool: {username}')
            self.context.user_pool.admin_delete_user(username=username)
            return failed
        if not Utils.get_value_as_bool('enabled', existing_user, False):
            # disabled here though the user pool may not have caught up yet
            self.logger.warning(f'SSO auth: {username} is disabled')
            return failed

        self.logger.debug(f'Updating SSO state for user {username}')
        self.context.accounts.sso_state_dao.update_sso_state(
            {
                'state': state,
                'access_token': auth_result.access_token,
                'refresh_token': auth_result.refresh_token,
                'expires_in': auth_result.expires_in,
                'id_token': auth_result.id_token,
                'token_type': auth_result.token_type,
            }
        )
        return f'{self.web_resources_context_path}?sso_auth_status=SUCCESS&sso_auth_code={state}'

    def make_route_path(self, path: str) -> str:
        if not path.startswith('/'):
            path = f'/{path}'

        return f'{self.web_resources_context_path.rstrip("/")}{path}'

    def initialize(self):
        # index route
        self.server.http_app.add_route(
            self.index_route, self.web_resources_context_path, name='index'
        )
        # the page's own file too, so it carries the same frame protection; a fixed
        # route is matched before the static one below
        self.server.http_app.add_route(
            self.index_route, self.make_route_path('/index.html'), name='index_html'
        )
        # sso route
        self.server.http_app.add_route(
            self.sso_initiate_route, self.make_route_path('/sso'), name='sso_initiate'
        )
        # sso oauth 2 callback route
        self.server.http_app.add_route(
            self.sso_oauth2_callback_route,
            self.make_route_path('/sso/oauth2/callback'),
            name='oauth2_callback',
        )
        # add static resources at the end
        self.server.http_app.static(
            self.web_resources_context_path, self.web_app_dir, name='static-content'
        )
