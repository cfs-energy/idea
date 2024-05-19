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

from ideasdk.protocols import ApiInvokerProtocol
from ideasdk.api import ApiInvocationContext
from ideasdk.auth import TokenService

import ideascheduler
from ideasdk.app import SocaAppAPI
from ideasdk.filesystem.filebrowser_api import FileBrowserAPI
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI
from ideascheduler.app.api.scheduler_api import SchedulerAPI


class SchedulerApiInvoker(ApiInvokerProtocol):

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self.openpbs_api = OpenPBSAPI(context)
        self.scheduler_admin_api = SchedulerAdminAPI(context)
        self.scheduler_api = SchedulerAPI(context)
        self.file_browser_api = FileBrowserAPI(context)
        self.app_api = SocaAppAPI(context)

    def get_token_service(self) -> TokenService:
        return self._context.token_service

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace
        if namespace.startswith('OpenPBSHook.'):
            self.openpbs_api.invoke(context)
        elif namespace.startswith('SchedulerAdmin.'):
            self.scheduler_admin_api.invoke(context)
        elif namespace.startswith('Scheduler.'):
            self.scheduler_api.invoke(context)
        elif namespace.startswith('App.'):
            return self.app_api.invoke(context)
