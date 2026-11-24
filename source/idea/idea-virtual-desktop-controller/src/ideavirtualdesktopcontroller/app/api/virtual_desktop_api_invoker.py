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

from typing import Dict, Optional

import ideavirtualdesktopcontroller
from ideasdk.api import ApiInvocationContext
from ideasdk.app import SocaAppAPI
from ideasdk.auth import TokenService
from ideasdk.protocols import ApiInvokerProtocol
from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
    VirtualDesktopAdminAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_dcv_api import (
    VirtualDesktopDCVAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
    VirtualDesktopUserAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_utils_api import (
    VirtualDesktopUtilsAPI,
)


CONNECTION_INFO_NAMESPACES = {
    'VirtualDesktop.GetSessionConnectionInfo',
    'VirtualDesktopAdmin.GetSessionConnectionInfo',
}


class VirtualDesktopApiInvoker(ApiInvokerProtocol):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        self._context = context
        self.INVOKER_MAP = {
            'VirtualDesktopUtils': VirtualDesktopUtilsAPI(context),
            'VirtualDesktopDCV': VirtualDesktopDCVAPI(context),
            'VirtualDesktopAdmin': VirtualDesktopAdminAPI(context),
            'VirtualDesktop': VirtualDesktopUserAPI(context),
            'App': SocaAppAPI(context),
        }

    def get_namespaces(self):
        # every namespace a handler answers here, read from the handlers' own maps: a
        # handler added on any branch has its own metric series with no table to update
        names = set()
        for api in self.INVOKER_MAP.values():
            names |= set(getattr(api, 'namespace_handler_map', {}) or ())
            names |= set(getattr(api, 'NAMESPACES', ()) or ())
        return frozenset(names)

    def get_token_service(self) -> TokenService:
        return self._context.token_service

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace
        leading_namespace = namespace.split('.')[0]

        return self.INVOKER_MAP[leading_namespace].invoke(context)

    def get_response_logging_payload(
        self, context: ApiInvocationContext
    ) -> Optional[Dict]:
        """with payload tracing on, a connection response is logged without its broker token"""
        if context.namespace not in CONNECTION_INFO_NAMESPACES:
            return None
        response = context.get_response(deep_copy=True)
        connection_info = (response.get('payload') or {}).get('connection_info')
        if isinstance(connection_info, dict) and connection_info.get('access_token'):
            connection_info['access_token'] = '*****'
        return response
