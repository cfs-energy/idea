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

import ideascheduler

from ideadatamodel.scheduler import (
    SocaJobParams,
    SocaScalingMode,
    SocaQueueMode,
    HpcApplication,
    HpcQueueProfile,
    ListHpcApplicationsRequest,
    CreateHpcApplicationRequest
)
from ideadatamodel import exceptions, errorcodes, Project, SocaUserInputModuleMetadata, SocaUserInputSectionMetadata, SocaUserInputParamMetadata, SocaPaginator
from ideasdk.utils import Utils

from typing import List
import os


class SchedulerDefaultSettings:
    """
    Default Settings for Scheduler

    These settings are created out of the box. These settings can be customized using API or Web UI based on requirements.
    """

    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger('scheduler-defaults')

        self.queue_profiles: List[HpcQueueProfile] = []

    def is_existing_queue_profile(self, queue_profile_name: str) -> bool:
        try:
            self.context.queue_profiles.get_queue_profile(queue_profile_name=queue_profile_name)
            return True
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND:
                return False
            raise e

    def create_queue_profiles(self):

        compute_node_os = self.context.config().get_string('scheduler.compute_node_os', required=True)
        compute_node_ami = self.context.config().get_string('scheduler.compute_node_ami', required=True)
        project = self.context.projects_client.get_default_project().project

        # queue profile: compute
        if not self.is_existing_queue_profile(queue_profile_name='compute'):
            queue_profile_compute = self.context.queue_profiles.create_queue_profile(
                queue_profile=HpcQueueProfile(
                    title='Compute',
                    name='compute',
                    description='Compute queue profile for provisioning ephemeral jobs. EC2 instances will be provisioned using'
                                ' a new CloudFormation stack for each Job submitted to any queue in this queue profile.',
                    queues=['high', 'normal', 'low'],
                    queue_mode=SocaQueueMode.FIFO,
                    scaling_mode=SocaScalingMode.SINGLE_JOB,
                    terminate_when_idle=0,
                    keep_forever=False,
                    default_job_params=SocaJobParams(
                        instance_types=['c5.large'],
                        base_os=compute_node_os,
                        instance_ami=compute_node_ami
                    ),
                    projects=[Project(project_id=project.project_id)]
                )
            )
            self.context.queue_profiles.enable_queue_profile(queue_profile_id=queue_profile_compute.queue_profile_id)
            self.logger.info(f'queue profile created: {Utils.to_json(queue_profile_compute)}')

        # queue profile: job-shared
        if not self.is_existing_queue_profile(queue_profile_name='job-shared'):
            queue_profile_job_shared = self.context.queue_profiles.create_queue_profile(
                queue_profile=HpcQueueProfile(
                    title='Job Shared',
                    name='job-shared',
                    description='Job Shared queue profile for provisioning shared jobs. Similar jobs will re-use '
                                'existing provisioned capacity if available, or new EC2 instances will be launched.',
                    queues=['job-shared'],
                    queue_mode=SocaQueueMode.FIFO,
                    scaling_mode=SocaScalingMode.BATCH,
                    terminate_when_idle=3,
                    keep_forever=False,
                    default_job_params=SocaJobParams(
                        instance_types=['t3.large', 't3.xlarge', 't3.2xlarge'],
                        base_os=compute_node_os,
                        instance_ami=compute_node_ami
                    ),
                    projects=[Project(project_id=project.project_id)]
                )
            )
            self.context.queue_profiles.enable_queue_profile(queue_profile_id=queue_profile_job_shared.queue_profile_id)
            self.logger.info(f'queue profile created: {Utils.to_json(queue_profile_job_shared)}')

        # queue profile: test
        if not self.is_existing_queue_profile(queue_profile_name='test'):
            queue_profile_test = self.context.queue_profiles.create_queue_profile(
                queue_profile=HpcQueueProfile(
                    title='Test',
                    name='test',
                    description='Queue profile to be used for testing',
                    queues=['test'],
                    queue_mode=SocaQueueMode.FIFO,
                    scaling_mode=SocaScalingMode.SINGLE_JOB,
                    terminate_when_idle=0,
                    keep_forever=False,
                    default_job_params=SocaJobParams(
                        instance_types=['t3.micro'],
                        base_os=compute_node_os,
                        instance_ami=compute_node_ami
                    ),
                    projects=[Project(project_id=project.project_id)]
                )
            )
            self.context.queue_profiles.enable_queue_profile(queue_profile_id=queue_profile_test.queue_profile_id)
            self.logger.info(f'queue profile created: {Utils.to_json(queue_profile_test)}')

    def create_applications(self):

        result = self.context.applications.list_applications(ListHpcApplicationsRequest(paginator=SocaPaginator(page_size=1)))
        if result.listing is not None and len(result.listing) > 0:
            return

        self.logger.info('creating demo hpc application ....')
        get_project_result = self.context.projects_client.get_default_project()
        project = get_project_result.project

        self.context.applications.create_application(CreateHpcApplicationRequest(
            application=HpcApplication(
                title='Demo Application',
                description='Application to demonstrate and test instance provisioning and job submissions using sleep',
                thumbnail_url='/home/logo.png',
                job_script_type='jinja2',
                job_script_interpreter='pbs',
                job_script_template=os.linesep.join([
                    '#!/bin/bash',
                    '#PBS -q {{ queue_name }}',
                    '#PBS -N  {{ job_name }}',
                    '#PBS -P {{ project_name }}',
                    '#PBS -l instance_type={{ instance_type }}',
                    '# You can use Jinja templating',
                    '# ex: {{ job_name | upper }}',
                    '/bin/sleep {{ sleep_time }}'
                ]),
                projects=[Project(project_id=project.project_id)],
                form_template=SocaUserInputModuleMetadata(
                    sections=[
                        SocaUserInputSectionMetadata(
                            params=[
                                SocaUserInputParamMetadata(**{
                                    "name": "_heading-1",
                                    "title": "Recommended Parameters",
                                    "param_type": "heading2"
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "instance_type",
                                    "title": "Instance Type",
                                    "description": "What type of instance do you want to use?",
                                    "param_type": "select",
                                    "data_type": "str",
                                    "validate": {
                                        "required": True
                                    },
                                    "default": "m5.large",
                                    "choices": [
                                        {
                                            "title": "Small (1 CPUs, 8GB RAM)",
                                            "value": "m5.large"
                                        },
                                        {
                                            "title": "Medium (2 CPUs, 16GB RAM)",
                                            "value": "m5.xlarge"
                                        },
                                        {
                                            "title": "Large (4 CPUs, 32GB RAM)",
                                            "value": "m5.2xlarge"
                                        }
                                    ]
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "cpus",
                                    "title": "CPUs",
                                    "description": "How many CPUs do you want?",
                                    "help_text": "The number of instances will be automatically calculated based on Instance Type and CPU count",
                                    "param_type": "text",
                                    "data_type": "int",
                                    "default": 1,
                                    "validate": {
                                        "required": True,
                                        "min": 1,
                                        "max": 5000
                                    }
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "job_name",
                                    "title": "Job Name",
                                    "description": "Enter a job name",
                                    "help_text": "Job name should not contain any white spaces or special characters",
                                    "param_type": "text",
                                    "data_type": "str",
                                    "validate": {
                                        "required": True,
                                        "regex": "^([A-Za-z0-9_-]+){3,18}$"
                                    }
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "queue_name",
                                    "title": "Queue Name",
                                    "description": "Which queue do you want to use? (queue_name)",
                                    "param_type": "select",
                                    "data_type": "str",
                                    "validate": {
                                        "required": True
                                    },
                                    "default": "low",
                                    "choices": [
                                        {
                                            "title": "Queue 1 (Low Priority)",
                                            "value": "low"
                                        },
                                        {
                                            "title": "Queue 2 (High Priority)",
                                            "value": "high"
                                        }
                                    ]
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "sleep_time",
                                    "title": "Sleep Time",
                                    "description": "How much time do you want the job to sleep?",
                                    "param_type": "select",
                                    "data_type": "str",
                                    "validate": {
                                        "required": True
                                    },
                                    "default": "60",
                                    "choices": [
                                        {
                                            "title": "1 minute",
                                            "value": "60"
                                        },
                                        {
                                            "title": "5 minutes",
                                            "value": "120"
                                        },
                                        {
                                            "title": "10 minutes",
                                            "value": "600"
                                        }
                                    ]
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "advanced_mode",
                                    "title": "Click to see new section",
                                    "description": "",
                                    "help_text": "",
                                    "param_type": "confirm",
                                    "data_type": "bool",
                                    "validate": {
                                        "required": False
                                    },
                                    "choices": []
                                }),
                                SocaUserInputParamMetadata(**{
                                    "name": "hidden_section",
                                    "title": "New section displayed only if previous toggle button is checked",
                                    "description": "Check 'when' section under Advanced Mode",
                                    "help_text": "",
                                    "param_type": "text",
                                    "data_type": "str",
                                    "default": "",
                                    "validate": {
                                        "required": False
                                    },
                                    "choices": [],
                                    "when": {
                                        "eq": True,
                                        "param": "advanced_mode"
                                    }
                                })

                            ]
                        )
                    ]
                )
            )
        ))

    def initialize(self):
        self.create_queue_profiles()
        self.create_applications()
