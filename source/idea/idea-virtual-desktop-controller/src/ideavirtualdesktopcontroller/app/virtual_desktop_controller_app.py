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
from ideadatamodel import constants
from ideasdk.client import (
    NotificationsAsyncClient,
    ProjectsClient,
    SocaClientOptions,
    AccountsClient,
)
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.auth import TokenService, TokenServiceOptions
from ideasdk.server import SocaServerOptions

import ideavirtualdesktopcontroller
from ideavirtualdesktopcontroller.app.api import VirtualDesktopApiInvoker
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    preferred_subnet_pin_warning,
)
from ideavirtualdesktopcontroller.app.clients.dcv_broker_client.dcv_broker_client import (
    DCVBrokerClient,
)
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    EventsClient,
)
from ideavirtualdesktopcontroller.app.events.service.controller_queue_monitor_service import (
    ControllerQueueMonitorService,
)
from ideavirtualdesktopcontroller.app.events.service.event_queue_monitoring_service import (
    EventsQueueMonitoringService,
)
from ideavirtualdesktopcontroller.app.permission_profiles.virtual_desktop_permission_profile_db import (
    VirtualDesktopPermissionProfileDB,
)
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_counters_db import (
    VirtualDesktopSessionCounterDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)
import threading

from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_utils import (
    VirtualDesktopSoftwareStackUtils,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandsDB,
)

import os
import yaml


class VirtualDesktopControllerApp(ideasdk.app.SocaApp):
    """
    virtual desktop app
    """

    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        config_file: str,
        env_file: str = None,
        config_overrides_file: str = None,
        validation_level: int = constants.CONFIG_LEVEL_CRITICAL,
        **kwargs,
    ):
        api_path_prefix = context.config().get_string(
            'virtual-desktop-controller.server.api_context_path',
            f'/{context.module_id()}',
        )
        super().__init__(
            context=context,
            config_file=config_file,
            api_invoker=VirtualDesktopApiInvoker(context=context),
            env_file=env_file,
            config_overrides_file=config_overrides_file,
            validation_level=validation_level,
            server_options=SocaServerOptions(
                api_path_prefixes=[api_path_prefix], enable_metrics=True
            ),
            **kwargs,
        )
        self.context = context
        self._software_stacks_reindexed = False
        # leadership may not be settled when the app initializes; one deferred retry
        self._reindex_retries = 1

    def app_initialize(self):
        self._initialize_templates()
        self._initialize_clients()
        self._initialize_dbs()
        self._initialize_services()
        self._reindex_software_stacks()

    def _initialize_dbs(self):
        self._session_counter_db = VirtualDesktopSessionCounterDB(
            self.context
        ).initialize()
        self._ssm_commands_db = VirtualDesktopSSMCommandsDB(self.context).initialize()
        self._server_db = VirtualDesktopServerDB(self.context).initialize()
        self._software_stack_db = VirtualDesktopSoftwareStackDB(
            self.context
        ).initialize()
        self._schedule_db = VirtualDesktopScheduleDB(self.context).initialize()
        self._session_db = VirtualDesktopSessionDB(
            context=self.context,
            server_db=self._server_db,
            software_stack_db=self._software_stack_db,
            schedule_db=self._schedule_db,
        ).initialize()
        self._permission_profile_db = VirtualDesktopPermissionProfileDB(
            self.context
        ).initialize()
        self._session_permissions_db = VirtualDesktopSessionPermissionDB(
            self.context
        ).initialize()
        self._initialize_image_builds()

    def _initialize_image_builds(self):
        """the records table plus the sweep of builds a restart orphaned; startup never fails over it"""
        from ideasdk.aws.image_builds import ImageBuildRecordsDB
        from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
            image_builds_table_name,
        )

        try:
            ImageBuildRecordsDB(
                self.context, image_builds_table_name(self.context)
            ).initialize()
        except Exception as e:
            self.context.logger('virtual-desktop-controller-app').error(
                f'image build records could not be initialized at startup: {e}'
            )

    def _reindex_software_stacks(self):
        """
        Rows deleted from DynamoDB out of band leave entries in the search index that
        the portal lists but cannot act on. Rebuild the index from the table once per
        app, and never fail startup over it.
        """
        if self._software_stacks_reindexed:
            return
        logger = self.context.logger('virtual-desktop-controller-app')
        # only the leader rebuilds, or every controller task would replay the whole
        # table through the analytics sink
        try:
            leader = self.context.is_leader()
        except Exception as e:
            logger.warning(f'leader status unknown ({e})')
            leader = None
        if not leader:
            if self._reindex_retries > 0:
                self._reindex_retries -= 1
                logger.info(
                    'not the leader yet; the software stack index rebuild is checked again in 60 seconds'
                )
                threading.Timer(60, self._reindex_software_stacks).start()
            else:
                self._software_stacks_reindexed = True
                logger.info(
                    'not the leader; the software stack index rebuild runs on the leader task'
                )
            return
        self._software_stacks_reindexed = True
        try:
            VirtualDesktopSoftwareStackUtils(
                context=self.context, db=self._software_stack_db
            ).reindex_from_db()
        except Exception as e:
            logger.error(f'failed to rebuild the software stack index at startup: {e}')

    def _initialize_session_template(self):
        session_template_file = os.path.join(
            self.context.get_resources_dir(), 'opensearch', 'session_entry_template.yml'
        )
        with open(session_template_file, 'r') as f:
            sessions_index_template = yaml.safe_load(f)

        if Utils.is_empty(sessions_index_template):
            return

        sessions_index_template['index_patterns'] = [
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias")}-*'
        ]
        sessions_index_template['aliases'] = {
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias")}': {}
        }
        self.context.sessions_template_version = self.context.analytics_service().initialize_template(
            template_name=f'{self.context.cluster_name()}_{self.context.module_id()}_user_sessions_template',
            template_body=sessions_index_template,
        )

    def _initialize_software_stack_template(self):
        software_stack_template_file = os.path.join(
            self.context.get_resources_dir(),
            'opensearch',
            'software_stack_entry_template.yml',
        )
        with open(software_stack_template_file, 'r') as f:
            software_stack_index_template = yaml.safe_load(f)

        if Utils.is_empty(software_stack_index_template):
            return

        software_stack_index_template['index_patterns'] = [
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias")}-*'
        ]
        software_stack_index_template['aliases'] = {
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias")}': {}
        }
        self.context.software_stack_template_version = self.context.analytics_service().initialize_template(
            template_name=f'{self.context.cluster_name()}_{self.context.module_id()}_software_stack_template',
            template_body=software_stack_index_template,
        )

    def _initialize_session_permission_template(self):
        session_permission_template_file = os.path.join(
            self.context.get_resources_dir(),
            'opensearch',
            'session_permission_entry_template.yml',
        )
        with open(session_permission_template_file, 'r') as f:
            session_permission_index_template = yaml.safe_load(f)

        if Utils.is_empty(session_permission_index_template):
            return

        session_permission_index_template['index_patterns'] = [
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.session_permission.alias")}-*'
        ]
        session_permission_index_template['aliases'] = {
            f'{self.context.config().get_string("virtual-desktop-controller.opensearch.session_permission.alias")}': {}
        }
        self.context.session_permission_template_version = self.context.analytics_service().initialize_template(
            template_name=f'{self.context.cluster_name()}_{self.context.module_id()}_session_permission_template',
            template_body=session_permission_index_template,
        )

    def _initialize_templates(self):
        self._initialize_session_template()
        self._initialize_software_stack_template()
        self._initialize_session_permission_template()

    def _initialize_clients(self):
        group_name_helper = GroupNameHelper(self.context)
        provider_url = self.context.config().get_string(
            'identity-provider.cognito.provider_url', required=True
        )
        domain_url = self.context.config().get_string(
            'identity-provider.cognito.domain_url', required=True
        )
        administrators_group_name = group_name_helper.get_cluster_administrators_group()
        managers_group_name = group_name_helper.get_cluster_managers_group()
        cluster_manager_module_id = self.context.config().get_module_id(
            constants.MODULE_CLUSTER_MANAGER
        )

        client_id = self.context.config().get_secret(
            'virtual-desktop-controller.client_id', required=True
        )
        client_secret = self.context.config().get_secret(
            'virtual-desktop-controller.client_secret', required=True
        )

        self.context.token_service = TokenService(
            context=self.context,
            options=TokenServiceOptions(
                cognito_user_pool_provider_url=provider_url,
                cognito_user_pool_domain_url=domain_url,
                client_id=client_id,
                client_secret=client_secret,
                client_credentials_scope=[
                    'dcv-session-manager/sm_scope',
                    f'{cluster_manager_module_id}/read',
                ],
                administrators_group_name=administrators_group_name,
                managers_group_name=managers_group_name,
            ),
        )

        internal_endpoint = self.context.config().get_cluster_internal_endpoint()
        self.context.projects_client = ProjectsClient(
            context=self.context,
            options=SocaClientOptions(
                endpoint=f'{internal_endpoint}/{cluster_manager_module_id}/api/v1',
                enable_logging=False,
                verify_ssl=False,
            ),
            token_service=self.context.token_service,
        )
        self.context.accounts_client = AccountsClient(
            context=self.context,
            options=SocaClientOptions(
                endpoint=f'{internal_endpoint}/{cluster_manager_module_id}/api/v1',
                enable_logging=False,
                verify_ssl=False,
            ),
            token_service=self.context.token_service,
        )

        self.context.notification_async_client = NotificationsAsyncClient(
            context=self.context
        )
        self.context.events_client = EventsClient(context=self.context)
        self.context.dcv_broker_client = DCVBrokerClient(context=self.context)

    def _initialize_services(self):
        self.context.event_queue_monitor_service = EventsQueueMonitoringService(
            context=self.context
        )
        self.context.controller_queue_monitor_service = ControllerQueueMonitorService(
            context=self.context
        )

    def app_start(self):
        subnet_pin_warning = preferred_subnet_pin_warning(self.context)
        if subnet_pin_warning is not None:
            self.context.logger('virtual-desktop-controller-app').warning(
                subnet_pin_warning
            )
        self.context.event_queue_monitor_service.start()
        self.context.controller_queue_monitor_service.start()

    def app_stop(self):
        if Utils.is_not_empty(self.context.event_queue_monitor_service):
            self.context.event_queue_monitor_service.stop()

        if Utils.is_not_empty(self.context.controller_queue_monitor_service):
            self.context.controller_queue_monitor_service.stop()

        if Utils.is_not_empty(self.context.projects_client):
            self.context.projects_client.destroy()
