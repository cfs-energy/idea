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

import ideasdk.app
from ideasdk.auth import TokenService, TokenServiceOptions
from ideadatamodel import constants
from ideasdk.server import SocaServerOptions
from ideasdk.client import ProjectsClient, AccountsClient, NotificationsAsyncClient, SocaClientOptions
from ideasdk.shell import ShellInvoker
from ideasdk.utils import GroupNameHelper

import ideascheduler
from ideascheduler.app.api import SchedulerApiInvoker
from ideascheduler.app.aws import InstanceCache, InstanceMonitor
from ideascheduler.app.metrics import JobProvisioningMetrics
from ideascheduler.app.provisioning import (
    JobCache, JobMonitor, NodeMonitor, JobSubmissionTracker,
    JobProvisioner, HpcQueueProfilesService
)
from ideascheduler.app.scheduler import SocaScheduler
from ideascheduler.app.documents import DocumentStore
from ideascheduler.app.scheduler_default_settings import SchedulerDefaultSettings
from ideascheduler.app.applications.hpc_applications_service import HpcApplicationsService
from ideascheduler.app.licenses.license_service import LicenseService
from ideascheduler.app.notifications.job_notifications import JobNotifications

from typing import Dict, Optional
import time


class SchedulerApp(ideasdk.app.SocaApp):
    """
    scheduler app
    """

    def __init__(self, context: ideascheduler.AppContext,
                 config_file: str,
                 env_file: str = None,
                 config_overrides_file: str = None,
                 validation_level: int = constants.CONFIG_LEVEL_CRITICAL,
                 **kwargs):

        api_path_prefix = context.config().get_string('scheduler.server.api_context_path', f'/{context.module_id()}')
        super().__init__(
            context=context,
            config_file=config_file,
            api_invoker=SchedulerApiInvoker(context=context),
            env_file=env_file,
            config_overrides_file=config_overrides_file,
            validation_level=validation_level,
            server_options=SocaServerOptions(
                api_path_prefixes=[api_path_prefix],
                enable_metrics=True
            ),
            **kwargs
        )
        self.context = context

        # these do not need to be in context and are required only during application lifecycle events
        self.job_provisioners: Dict[str, JobProvisioner] = {}
        self.instance_monitor: Optional[InstanceMonitor] = None

    def app_initialize(self):

        group_name_helper = GroupNameHelper(self.context)
        provider_url = self.context.config().get_string('identity-provider.cognito.provider_url', required=True)
        domain_url = self.context.config().get_string('identity-provider.cognito.domain_url', required=True)
        administrators_group_name = group_name_helper.get_cluster_administrators_group()
        managers_group_name = group_name_helper.get_cluster_managers_group()
        cluster_manager_module_id = self.context.config().get_module_id(constants.MODULE_CLUSTER_MANAGER)
        client_id = self.context.config().get_secret('scheduler.client_id', required=True)
        client_secret = self.context.config().get_secret('scheduler.client_secret', required=True)

        self.context.token_service = TokenService(
            context=self.context,
            options=TokenServiceOptions(
                cognito_user_pool_provider_url=provider_url,
                cognito_user_pool_domain_url=domain_url,
                client_id=client_id,
                client_secret=client_secret,
                client_credentials_scope=[
                    f'{cluster_manager_module_id}/read'
                ],
                administrators_group_name=administrators_group_name,
                managers_group_name=managers_group_name
            )
        )

        # clients
        internal_endpoint = self.context.config().get_cluster_internal_endpoint()

        # accounts client
        self.context.accounts_client = AccountsClient(
            context=self.context,
            options=SocaClientOptions(
                endpoint=f'{internal_endpoint}/{cluster_manager_module_id}/api/v1',
                enable_logging=False,
                verify_ssl=False
            ),
            token_service=self.context.token_service
        )

        # projects client
        self.context.projects_client = ProjectsClient(
            context=self.context,
            options=SocaClientOptions(
                endpoint=f'{internal_endpoint}/{cluster_manager_module_id}/api/v1',
                enable_logging=False,
                verify_ssl=False
            ),
            token_service=self.context.token_service
        )

        # if opensearch is enabled in the cluster, but the domain is still being created,
        # wait for opensearch domain to be ready.
        document_store = DocumentStore(context=self.context)
        if document_store.is_enabled():
            document_store.initialize()
        self.context.document_store = document_store

        self.context.instance_cache = InstanceCache(context=self.context)
        self.instance_monitor = InstanceMonitor(
            context=self.context,
            instance_cache=self.context.instance_cache
        )
        self.context.scheduler = SocaScheduler(context=self.context)

        # wait for scheduler to start
        # this happens when scheduler is restarted and there's race condition between supervisord and pbs
        # ideally, should be managed using system service dependencies, but below work around also works
        scheduler_ready = False
        while not scheduler_ready:
            scheduler_ready = self.context.scheduler.is_ready()
            if scheduler_ready:
                break
            self.logger.info('waiting for scheduler to start ...')
            time.sleep(5)

        self.context.metrics = JobProvisioningMetrics(context=self.context)
        self.context.job_cache = JobCache(context=self.context)
        self.context.job_monitor = JobMonitor(context=self.context)
        self.context.node_monitor = NodeMonitor(context=self.context)
        self.context.job_submission_tracker = JobSubmissionTracker(context=self.context)
        self.context.queue_profiles = HpcQueueProfilesService(context=self.context)
        self.context.applications = HpcApplicationsService(context=self.context)
        self.context.shell = ShellInvoker()
        self.context.license_service = LicenseService(context=self.context)
        self.context.notifications_client = NotificationsAsyncClient(context=self.context)
        self.context.job_notifications = JobNotifications(
            context=self.context,
            notifications_client=self.context.notifications_client
        )

    def app_start(self):
        self.instance_monitor.start()
        self.context.queue_profiles.start()
        self.context.job_monitor.start()
        self.context.node_monitor.start()

        # create default scheduler settings
        SchedulerDefaultSettings(self.context).initialize()

    def app_stop(self):
        if self.instance_monitor is not None:
            self.instance_monitor.stop()
        if self.context.job_monitor is not None:
            self.context.job_monitor.stop()
        if self.context.node_monitor is not None:
            self.context.node_monitor.stop()
        if self.context.queue_profiles is not None:
            self.context.queue_profiles.stop()
        if self.context.accounts_client is not None:
            self.context.accounts_client.destroy()
        if self.context.projects_client is not None:
            self.context.projects_client.destroy()
