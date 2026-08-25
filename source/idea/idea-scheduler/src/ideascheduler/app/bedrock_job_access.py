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

from ideadatamodel import GetProjectRequest, SocaQueueManagementParams
from ideasdk.utils import Utils
from ideasdk.utils.error_redaction import redact_aws_identifiers

from enum import Enum
from typing import Optional

# compute nodes run under their project's bedrock instance role only when this is true.
# off by default: enabling bedrock for projects grants virtual desktops model access
# without changing what a compute node runs as.
BEDROCK_ENABLED_CONFIG_KEY = 'scheduler.bedrock.enabled'

# written by the scheduler stack only when the module is deployed while bedrock is
# enabled, which is when the scheduler role gains iam:PassRole for project roles.
# without it a launch template naming a project profile is refused by EC2.
BEDROCK_PASS_ROLE_CONFIG_KEY = 'scheduler.bedrock.project_pass_role_arn'

REDEPLOY_MESSAGE = (
    'the scheduler module is not permitted to launch compute nodes under a project '
    'role. Ask an administrator to redeploy the scheduler module with Bedrock enabled.'
)


class BedrockJobAccessState(str, Enum):
    # the job does not run under a project role: the integration is off for jobs, or
    # the project has no bedrock access to hand to it.
    NOT_APPLICABLE = 'NOT_APPLICABLE'
    # the project's instance profile is resolved and authorized for the job's queue.
    AVAILABLE = 'AVAILABLE'
    # the project expects access that is not usable yet. a reconcile or a recovered
    # projects api makes it usable, so the job waits rather than running without it.
    NOT_READY = 'NOT_READY'
    # the project expects access that cannot be granted until configuration changes.
    NOT_AUTHORIZED = 'NOT_AUTHORIZED'


class BedrockJobAccess:
    """
    the outcome of resolving a job's bedrock access. instance_profile_arn is set only
    in the AVAILABLE state; message is set only when the project expected access the
    job did not get.
    """

    def __init__(
        self,
        state: BedrockJobAccessState,
        instance_profile_arn: Optional[str] = None,
        message: Optional[str] = None,
    ):
        self.state = state
        self.instance_profile_arn = instance_profile_arn
        self.message = message

    @property
    def is_available(self) -> bool:
        return self.state == BedrockJobAccessState.AVAILABLE

    def __str__(self):
        if self.message is None:
            return f'{self.state.value}'
        return f'{self.state.value}: {self.message}'


def is_enabled_for_jobs(context: 'ideascheduler.AppContext') -> bool:
    """
    whether compute nodes may run under a project's bedrock instance role. read on every
    resolution rather than cached: the setting is a runtime kill switch, and the config
    tree it comes from refreshes on the cluster settings stream.
    """
    return context.config().get_bool(BEDROCK_ENABLED_CONFIG_KEY, default=False)


class BedrockJobAccessResolver:
    """
    resolves the instance profile a job's compute nodes run under.

    a project with bedrock enabled runs its compute nodes under the project instance
    role, the same identity its virtual desktops use. the model allowlist is compiled
    into that role's policy, so a job inherits the project's list as it stands and
    follows later edits to it - there is no per-job subset and nothing to pin.

    resolution is deliberately positive-only: the project instance profile is returned
    only when every precondition is read back. anything unresolved reports the state
    rather than falling through to the standard compute node profile silently, so the
    provisioning gate can hold the job instead of running it without the access its
    project advertises.
    """

    def __init__(
        self,
        context: 'ideascheduler.AppContext',
        project_name: Optional[str],
        queue_management: Optional[SocaQueueManagementParams] = None,
    ):
        self.context = context
        self.project_name = project_name
        self.queue_management = queue_management

    def is_enabled(self) -> bool:
        return is_enabled_for_jobs(self.context)

    def _is_allowed_by_queue(self, instance_profile_arn: str) -> bool:
        if self.queue_management is None:
            return True
        if self.queue_management.is_allowed_instance_profile(instance_profile_arn):
            return True
        # an allowed list is written with names as often as arns, and both name the
        # same instance profile.
        name = instance_profile_arn.split('/')[-1]
        return self.queue_management.is_allowed_instance_profile(name)

    def resolve(self) -> BedrockJobAccess:
        if not self.is_enabled():
            return BedrockJobAccess(BedrockJobAccessState.NOT_APPLICABLE)

        if Utils.is_empty(self.project_name):
            return BedrockJobAccess(BedrockJobAccessState.NOT_APPLICABLE)

        try:
            project = self.context.projects_client.get_project(
                GetProjectRequest(project_name=self.project_name)
            ).project
        except Exception as e:
            # an unreadable project cannot say the job has no bedrock access.
            return BedrockJobAccess(
                BedrockJobAccessState.NOT_READY,
                message=f'project ({self.project_name}) could not be read to resolve '
                f'its Bedrock access: {redact_aws_identifiers(e)}',
            )

        if project is None or not project.is_bedrock_enabled():
            return BedrockJobAccess(BedrockJobAccessState.NOT_APPLICABLE)

        if len(project.bedrock.get_model_ids()) == 0:
            # bedrock is on for the project but no model is allowed, so the project
            # role would carry no model access to give the job.
            return BedrockJobAccess(BedrockJobAccessState.NOT_APPLICABLE)

        # checked before the provisioned state, so a cluster where bedrock was turned
        # off and the module redeployed reports the deployment rather than reporting a
        # torn down project as still reconciling.
        if Utils.is_empty(
            self.context.config().get_string(BEDROCK_PASS_ROLE_CONFIG_KEY)
        ):
            return BedrockJobAccess(
                BedrockJobAccessState.NOT_AUTHORIZED,
                message=f'project ({self.project_name}) has Bedrock enabled but '
                f'{REDEPLOY_MESSAGE}',
            )

        instance_profile_arn = project.bedrock.instance_profile_arn
        if Utils.is_empty(instance_profile_arn):
            return BedrockJobAccess(
                BedrockJobAccessState.NOT_READY,
                message=f'project ({self.project_name}) has Bedrock enabled but its '
                f'instance profile has not been provisioned yet.',
            )

        model_errors = project.bedrock.get_model_errors()
        if len(Utils.get_as_dict(project.bedrock.inference_profile_arns, {})) == 0:
            if len(model_errors) > 0:
                reasons = ', '.join(
                    f'{model_id} ({reason})'
                    for model_id, reason in sorted(model_errors.items())
                )
                return BedrockJobAccess(
                    BedrockJobAccessState.NOT_AUTHORIZED,
                    message=f'no model allowed on project ({self.project_name}) could '
                    f'be provisioned: {reasons}.',
                )
            return BedrockJobAccess(
                BedrockJobAccessState.NOT_READY,
                message=f'project ({self.project_name}) has Bedrock enabled but none '
                f'of its models have been provisioned yet.',
            )

        if not self._is_allowed_by_queue(instance_profile_arn):
            return BedrockJobAccess(
                BedrockJobAccessState.NOT_AUTHORIZED,
                message=f'the instance profile carrying Bedrock access for project '
                f'({self.project_name}) is not authorized for this queue. Ask an '
                f'administrator to add the project instance profile to the queue '
                f'profile allowed_instance_profiles.',
            )

        return BedrockJobAccess(
            BedrockJobAccessState.AVAILABLE,
            instance_profile_arn=instance_profile_arn,
        )
