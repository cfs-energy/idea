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

    # every namespace a handler answers here: each has its own metric series
    NAMESPACES = (
        frozenset(
            {
                'OpenPBSHook.InstanceTypeInfo',
                # the server hooks' events, sent as OpenPBSHook.<event> over the local socket
                # (the execjob_* events go through the job status queue, not this server)
                'OpenPBSHook.modifyjob',
                'OpenPBSHook.movejob',
                'OpenPBSHook.queuejob',
                'OpenPBSHook.runjob',
                'Scheduler.DeleteJob',
                'Scheduler.GetActiveJob',
                'Scheduler.GetCompletedJob',
                'Scheduler.GetInstanceTypeOptions',
                'Scheduler.GetUserApplications',
                'Scheduler.ListActiveJobs',
                'Scheduler.ListCompletedJobs',
                'Scheduler.ListQueues',
                'Scheduler.SubmitJob',
                'SchedulerAdmin.BackfillJobMetrics',
                'SchedulerAdmin.BackfillScratchCleanup',
                'SchedulerAdmin.BuildComputeImage',
                'SchedulerAdmin.CheckHpcLicenseResourceAvailability',
                'SchedulerAdmin.CreateHpcApplication',
                'SchedulerAdmin.CreateHpcLicenseResource',
                'SchedulerAdmin.CreateQueueProfile',
                'SchedulerAdmin.DeleteHpcApplication',
                'SchedulerAdmin.DeleteHpcLicenseResource',
                'SchedulerAdmin.DeleteJob',
                'SchedulerAdmin.DeleteQueueProfile',
                'SchedulerAdmin.DisableQueueProfile',
                'SchedulerAdmin.EnableQueueProfile',
                'SchedulerAdmin.GetHpcApplication',
                'SchedulerAdmin.GetHpcLicenseResource',
                'SchedulerAdmin.GetImageSchedule',
                'SchedulerAdmin.GetJobMetricsBackfill',
                'SchedulerAdmin.GetQueueProfile',
                'SchedulerAdmin.GetScratchCleanupStatus',
                'SchedulerAdmin.GetUserApplications',
                'SchedulerAdmin.ListActiveJobs',
                'SchedulerAdmin.ListCompletedJobs',
                'SchedulerAdmin.ListComputeImages',
                'SchedulerAdmin.ListHpcApplications',
                'SchedulerAdmin.ListHpcLicenseResources',
                'SchedulerAdmin.ListImageRows',
                'SchedulerAdmin.ListNodes',
                'SchedulerAdmin.ListQueueProfiles',
                'SchedulerAdmin.ListScratchCleanupAudit',
                'SchedulerAdmin.ListScratchCleanupPreview',
                'SchedulerAdmin.ListScratchCleanupUpcoming',
                'SchedulerAdmin.PreviewScratchCleanup',
                'SchedulerAdmin.ProvisionAlwaysOnNodes',
                'SchedulerAdmin.RefreshImages',
                'SchedulerAdmin.RollbackImage',
                'SchedulerAdmin.SetImagePinned',
                'SchedulerAdmin.UpdateHpcApplication',
                'SchedulerAdmin.UpdateHpcLicenseResource',
                'SchedulerAdmin.UpdateQueueProfile',
            }
        )
        | SocaAppAPI.NAMESPACES
    )

    def get_namespaces(self):
        return self.NAMESPACES

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
