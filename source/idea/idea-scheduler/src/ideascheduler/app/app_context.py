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

from ideadatamodel import constants, SocaJob
from ideasdk.context import SocaContext, SocaContextOptions
from ideasdk.auth import TokenService
from ideasdk.client import AccountsClient, ProjectsClient, NotificationsAsyncClient
from ideasdk.utils import EnvironmentUtils, Utils
from ideasdk.shell import ShellInvoker

import ideascheduler

from ideascheduler.app.app_protocols import (
    InstanceCacheProtocol,
    SocaSchedulerProtocol,
    JobCacheProtocol,
    JobMonitorProtocol,
    NodeMonitorProtocol,
    DocumentStoreProtocol,
    JobSubmissionTrackerProtocol,
    HpcQueueProfilesServiceProtocol,
    HpcApplicationsProtocol,
    LicenseServiceProtocol,
    JobNotificationsProtocol
)
from ideascheduler.app.metrics import JobProvisioningMetrics

import os
import base64
import uuid
import boto3
from botocore import config as botocore_config
import json
import yaml
import time
from typing import Optional, Union
import click
from pathlib import Path


class SchedulerAppContext(SocaContext):

    def __init__(self, options: SocaContextOptions = None):
        super().__init__(
            options=options
        )

        # sdk services
        self.token_service: Optional[TokenService] = None
        self.accounts_client: Optional[AccountsClient] = None
        self.projects_client: Optional[ProjectsClient] = None
        self.notifications_client: Optional[NotificationsAsyncClient] = None
        self.job_notifications: Optional[JobNotificationsProtocol] = None

        # app services
        self.document_store: Optional[DocumentStoreProtocol] = None
        self.scheduler: Optional[SocaSchedulerProtocol] = None
        self.instance_cache: Optional[InstanceCacheProtocol] = None
        self.job_cache: Optional[JobCacheProtocol] = None
        self.job_monitor: Optional[JobMonitorProtocol] = None
        self.node_monitor: Optional[NodeMonitorProtocol] = None
        self.job_submission_tracker: Optional[JobSubmissionTrackerProtocol] = None
        self.queue_profiles: Optional[HpcQueueProfilesServiceProtocol] = None
        self.applications: Optional[HpcApplicationsProtocol] = None
        self.license_service: Optional[LicenseServiceProtocol] = None
        self.metrics: Optional[JobProvisioningMetrics] = None

        self.shell: Optional[ShellInvoker] = None

    def is_ready(self) -> bool:
        job_cache_ready = self.job_cache.is_ready()
        instance_cache_ready = self.instance_cache.is_ready()
        return job_cache_ready and instance_cache_ready

    def env_app_deploy_dir(self) -> str:
        return EnvironmentUtils.idea_app_deploy_dir(required=True)

    def get_scheduler_app_deploy_dir(self) -> str:
        # this will be /opt/idea/app/scheduler
        return os.path.join(self.env_app_deploy_dir(), 'scheduler')

    def get_scheduler_dir(self):
        # this will be /apps/<cluster-name>/<module-id>
        cluster_home_dir = self.config().get_string('cluster.home_dir', required=True)
        return os.path.join(cluster_home_dir, self.module_id())

    def get_job_dir(self, job: SocaJob):
        # this will be /apps/<cluster-name>/<module-id>/jobs/<job_id_or_job_group>
        if job.is_ephemeral_capacity():
            job_id_or_job_group = job.job_id
        else:
            job_id_or_job_group = job.get_job_group()
        return os.path.join(self.get_scheduler_dir(), 'jobs', job_id_or_job_group)

    def get_resources_dir(self) -> str:
        if Utils.is_true(EnvironmentUtils.idea_dev_mode(), False):
            script_dir = Path(os.path.abspath(__file__))
            scheduler_project_dir = script_dir.parent.parent.parent.parent
            return os.path.join(scheduler_project_dir, 'resources')
        else:
            return os.path.join(self.env_app_deploy_dir(), 'scheduler', 'resources')

    def get_bootstrap_dir(self) -> str:
        if Utils.is_true(EnvironmentUtils.idea_dev_mode(), False):
            script_dir = Path(os.path.abspath(__file__))
            idea_source_dir = script_dir.parent.parent.parent.parent.parent
            return os.path.join(idea_source_dir, 'idea-bootstrap')
        else:
            return os.path.join(self.get_resources_dir(), 'bootstrap')
