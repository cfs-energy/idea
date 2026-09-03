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
import os
import logging
import random
import re
from threading import RLock
from typing import List, Dict, Optional

import botocore.exceptions

import ideavirtualdesktopcontroller
from botocore.exceptions import ClientError

from ideadatamodel import constants, VirtualDesktopArchitecture
from ideadatamodel import errorcodes, exceptions
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopGPU,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopSoftwareStack,
    GetUserRequest,
    GetProjectRequest,
    Project,
)
from ideasdk.bootstrap import BootstrapPackageBuilder, BootstrapUserDataBuilder
from ideasdk.context import BootstrapContext
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.metrics.cloudwatch import CloudWatchAgentLogFileOptions
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEventType,
)
from ideavirtualdesktopcontroller.app.events.events_utils import EventsUtils

# launch errors worth trying in another subnet/AZ before giving up on the request.
RETRYABLE_LAUNCH_ERROR_CODES = {
    'InsufficientInstanceCapacity',
    'InsufficientHostCapacity',
    'Unsupported',
}

# InvalidParameterValue covers everything from a bad volume size to an unusable AMI, and only
# the zone scoped uses of it are worth another subnet. the message is what distinguishes them.
AZ_SCOPED_LAUNCH_ERROR = re.compile(r'availability zone|subnet', re.IGNORECASE)

# what the requesting user is told when a desktop cannot be launched. the AWS error code and
# message stay in the controller log; the user gets a sentence they can act on.
LAUNCH_FAILURE_MESSAGES = {
    'InsufficientInstanceCapacity': 'AWS has no {instance_type} capacity available right now. Try a smaller size, or try again in a few minutes.',
    'Unsupported': '{instance_type} is not offered in the networks this cluster uses. Choose a different size.',
    'InstanceLimitExceeded': 'This cluster has reached its limit for {instance_type}. Choose a smaller size, or ask an administrator to raise the limit.',
    'VcpuLimitExceeded': 'This cluster has reached its limit for {instance_type}. Choose a smaller size, or ask an administrator to raise the limit.',
    'MaxSpotInstanceCountExceeded': 'This cluster has reached its limit for {instance_type}. Choose a smaller size, or ask an administrator to raise the limit.',
    'OptInRequired': 'The image this desktop is built from has not been subscribed to in the AWS Marketplace. Ask an administrator.',
    'InvalidAMIID.NotFound': 'The image this desktop is built from is no longer available. Ask an administrator.',
    'InvalidAMIID.Malformed': 'The image this desktop is built from is no longer available. Ask an administrator.',
    'InvalidAMIID.Unavailable': 'The image this desktop is built from is no longer available. Ask an administrator.',
    'UnauthorizedOperation': 'This cluster is not permitted to start {instance_type}. Ask an administrator.',
    'RequestLimitExceeded': 'AWS is rate limiting this cluster right now. Wait a moment and try again.',
}

LAUNCH_FAILURE_DEFAULT_MESSAGE = 'Your virtual desktop could not be started. Ask an administrator to check the virtual desktop controller log.'

# the iam:PassRole grant for project roles is created at module deploy time, not by
# toggling the setting at runtime, the usual cause of a refused launch under a project role.
PASS_PROJECT_ROLE_FAILURE_MESSAGE = 'This desktop runs under the IAM role of its project, and the virtual desktop controller is not permitted to pass that role. Ask an administrator to redeploy the virtual-desktop-controller module with Bedrock enabled.'

# the bootstrap tags the host with this before it deliberately aborts, so the reason a
# desktop never came up outlives a host that never became reachable.
BOOTSTRAP_STATUS_TAG = 'idea:BootstrapStatus'

# every value fail_gpu_drivers in idea-bootstrap can write.
BOOTSTRAP_STATUS_MESSAGES = {
    'gpu-driver-install-failed': 'The GPU driver failed to install on the host for this desktop, so it never became usable. The host has been released. Ask an administrator to check the GPU driver bootstrap log for this instance type.',
    'gpu-driver-mapping-missing': 'The host for this desktop has GPU hardware that IDEA has no driver for, so it never became usable. The host has been released. Choose a different instance type, or ask an administrator.',
    'gpu-kernel-devel-missing': 'No kernel headers were available on the host for this desktop, so the GPU driver could not be built and the desktop never became usable. The host has been released. Ask an administrator to check the software stack image.',
}

BOOTSTRAP_STATUS_DEFAULT_MESSAGE = 'The host for this desktop stopped its own bootstrap with status "{bootstrap_status}", so the desktop never became usable. The host has been released. Ask an administrator to check the bootstrap log for this instance.'


def build_bootstrap_failure_message(bootstrap_status: str) -> str:
    """
    the plain reason for a host that gave up during bootstrap. an unrecognised status is
    reported as itself rather than as a timeout: it still names where to look.
    """
    template = BOOTSTRAP_STATUS_MESSAGES.get(
        bootstrap_status, BOOTSTRAP_STATUS_DEFAULT_MESSAGE
    )
    return template.format(bootstrap_status=bootstrap_status)


def build_launch_failure_message(
    error_code: str, instance_type: str, project_instance_profile: bool = False
) -> str:
    """
    the short, plain reason shown to the user when the desktop host cannot be launched.
    """
    if error_code == 'UnauthorizedOperation' and project_instance_profile:
        return PASS_PROJECT_ROLE_FAILURE_MESSAGE
    template = LAUNCH_FAILURE_MESSAGES.get(error_code, LAUNCH_FAILURE_DEFAULT_MESSAGE)
    return template.format(instance_type=instance_type)


# most capable first: a desktop should reach for the best model its project allows.
BEDROCK_MODEL_CLASS_PREFERENCE = ('opus', 'sonnet', 'haiku')


def build_bedrock_env(
    inference_profile_arns: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """
    the env a bedrock client needs to work on a desktop without being configured by hand. The
    profile arn is the model id because invoking the bare model id is refused.
    """
    provisioned = {
        model_id: arn
        for model_id, arn in (inference_profile_arns or {}).items()
        if Utils.is_not_empty(arn)
    }
    if len(provisioned) == 0:
        return {}

    # newest first: the numeric tokens of a model id order its versions, so within a class
    # claude-opus-4-5 is preferred over claude-opus-4-1.
    newest_first = sorted(
        provisioned,
        key=lambda model_id: (
            [int(token) for token in re.findall(r'\d+', model_id)],
            model_id,
        ),
        reverse=True,
    )

    def first_of_class(*classes: str) -> Optional[str]:
        for wanted in classes:
            for model_id in newest_first:
                if wanted in model_id.lower():
                    return provisioned[model_id]
        return None

    main = (
        first_of_class(*BEDROCK_MODEL_CLASS_PREFERENCE) or provisioned[newest_first[0]]
    )
    return {
        'CLAUDE_CODE_USE_BEDROCK': '1',
        'ANTHROPIC_MODEL': main,
        'ANTHROPIC_SMALL_FAST_MODEL': first_of_class('haiku') or main,
    }


def describe_bedrock_models(
    inference_profile_arns: Optional[Dict[str, str]],
) -> List[str]:
    """
    the model ids behind the env, for the login banner. The env carries profile arns, which say
    nothing to whoever reads them, so the banner names what the desktop actually got.
    """
    env = build_bedrock_env(inference_profile_arns)
    if len(env) == 0:
        return []

    by_arn = {arn: model_id for model_id, arn in (inference_profile_arns or {}).items()}
    main = by_arn.get(env['ANTHROPIC_MODEL'], env['ANTHROPIC_MODEL'])
    fast = by_arn.get(
        env['ANTHROPIC_SMALL_FAST_MODEL'], env['ANTHROPIC_SMALL_FAST_MODEL']
    )
    messages = [f'> Claude Code is set up for this project on {main}']
    if fast != main:
        messages.append(f'>   with {fast} for small fast requests')
    return messages


# concise replies and auto mode are the defaults a desktop arrives with. Seeded as user settings so
# they can be changed; see the virtual desktop Claude Code documentation.
CLAUDE_CODE_DEFAULT_OUTPUT_STYLE = 'Concise'

CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY = (
    'virtual-desktop-controller.bedrock.claude_code.permission_mode'
)


CLAUDE_CODE_PERMISSION_MODE_DEFAULT = 'auto'
CLAUDE_CODE_PERMISSION_MODE_NONE = 'none'


def resolve_claude_code_permission_mode(config) -> str:
    """
    the permission mode a desktop is seeded with. a cluster without the setting gets auto;
    the value `none` seeds no permissions block, leaving the client on its own default.
    """
    mode = Utils.get_as_string(
        config.get_string(
            CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY,
            default=CLAUDE_CODE_PERMISSION_MODE_DEFAULT,
        ),
        default=CLAUDE_CODE_PERMISSION_MODE_DEFAULT,
    ).strip()
    if mode.lower() == CLAUDE_CODE_PERMISSION_MODE_NONE:
        return ''
    return mode


def build_auto_mode_environment(
    cluster_name: str, aws_region: str, cluster_s3_bucket: str
) -> List[str]:
    """
    what the auto mode classifier should treat as inside the boundary, so routine work on a desktop
    does not prompt. Derived from the cluster, so it adds no admin configuration.
    """
    return [
        '$defaults',
        f'Organization: the {cluster_name} IDEA cluster, a shared HPC and virtual '
        f'desktop environment on AWS',
        'Primary use of Claude Code: research and engineering work on a shared virtual desktop',
        f'Cloud provider(s): AWS, region {aws_region}',
        f'Trusted cloud buckets: s3://{cluster_s3_bucket}',
        f'Key internal services: the {cluster_name} IDEA portal and its job scheduler',
    ]


def resolve_bedrock_project(
    context: ideavirtualdesktopcontroller.AppContext,
    logger: logging.Logger,
    session: VirtualDesktopSession,
) -> Optional[Project]:
    """
    the session's project as stored, when it is genuinely entitled to model access. Re-read from
    the stored record, so nothing in a request payload can select what the desktop gets.
    """
    project_id = None
    if Utils.is_not_empty(session.project):
        project_id = session.project.project_id
    if Utils.is_empty(project_id):
        return None

    try:
        project = context.projects_client.get_project(
            GetProjectRequest(project_id=project_id)
        ).project
    except Exception as e:
        logger.warning(
            f'failed to read project {project_id} while resolving its bedrock '
            f'instance profile, the shared dcv host profile applies: {e}'
        )
        return None

    if project is None or not project.is_bedrock_enabled():
        return None

    # the desktop still launches when the project budget is spent, it launches without
    # model access. an evaluation that could not be made counts as spent.
    if project.bedrock_budget is not None and project.bedrock_budget.is_blocking():
        logger.warning(
            f'project {project_id} does not have budget for Amazon Bedrock, the shared '
            f'dcv host profile applies: {project.bedrock_budget.status} - '
            f'{project.bedrock_budget.message}'
        )
        return None

    return project


def resolve_project_instance_profile_arn(
    context: ideavirtualdesktopcontroller.AppContext,
    logger: logging.Logger,
    session: VirtualDesktopSession,
) -> Optional[str]:
    """the bedrock instance profile the session's project is entitled to, or None."""
    project = resolve_bedrock_project(context, logger, session)
    if project is None:
        return None

    if Utils.is_empty(project.bedrock.instance_profile_arn):
        logger.warning(
            f'project {project.project_id} has bedrock enabled but no instance profile '
            f'has been provisioned yet, the shared dcv host profile applies'
        )
        return None

    return project.bedrock.instance_profile_arn


def resolve_project_bedrock_env(
    context: ideavirtualdesktopcontroller.AppContext,
    logger: logging.Logger,
    session: VirtualDesktopSession,
) -> Dict[str, str]:
    """
    the bedrock env for the session's project, empty when it has no model access. Gated exactly as
    the instance profile is, so a desktop is never pointed at a profile it cannot invoke.
    """
    project = resolve_bedrock_project(context, logger, session)
    if project is None:
        return {}
    return build_bedrock_env(project.bedrock.inference_profile_arns)


def preferred_subnet_pin_warning(
    context: 'ideavirtualdesktopcontroller.AppContext',
) -> Optional[str]:
    """
    a preferred subnet with autoretry turned off is a hard pin: every desktop lands in
    one availability zone and fails while that zone is out of capacity. returns the
    warning to log, or None when the combination is not present.
    """
    preferred_subnet = context.config().get_string(
        'cluster.network.preferred_subnet_id', default=None
    )
    if Utils.is_empty(preferred_subnet):
        return None

    subnet_autoretry = context.config().get_bool(
        'vdc.dcv_session.network.subnet_autoretry',
        default=Utils.get_as_bool(constants.DEFAULT_VDI_SUBNET_AUTORETRY, default=True),
    )
    if subnet_autoretry:
        return None

    return (
        f'cluster.network.preferred_subnet_id is set to {preferred_subnet} while '
        f'virtual-desktop-controller.dcv_session.network.subnet_autoretry is false. '
        f'Desktops are pinned to that subnet with no fallback, so every desktop will fail '
        f'while its availability zone is out of capacity. Enable subnet_autoretry, or '
        f'clear the preferred subnet.'
    )


class VirtualDesktopControllerUtils:
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-controller-utils')
        self.s3_client = self.context.aws().s3()
        self.ec2_client = self.context.aws().ec2()
        self.eventbridge_client = self.context.aws().eventbridge()
        self.ssm_client = self.context.aws().ssm()
        self.sqs_client = self.context.aws().sqs()
        self.events_utils = EventsUtils(context=self.context)
        self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY = (
            'aws.ec2.all-instance-types-names-list'
        )
        self.INSTANCE_INFO_CACHE_KEY = 'aws.ec2.all-instance-types-data'
        self.instance_types_lock = RLock()
        self.group_name_helper = GroupNameHelper(self.context)

    def create_tag(self, instance_id: str, tag_key: str, tag_value: str):
        self.ec2_client.create_tags(
            Resources=[instance_id], Tags=[{'Key': tag_key, 'Value': tag_value}]
        )

    def delete_tag(self, instance_id: str, tag_key: str):
        self.ec2_client.delete_tags(Resources=[instance_id], Tags=[{'Key': tag_key}])

    def describe_instance(self, instance_id: str) -> Optional[Dict]:
        """
        the ec2 instance record. {} means ec2 no longer knows the id; None means ec2 could
        not be read, which must not be acted on as though the instance were gone.
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except ClientError as e:
            if e.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
                return {}
            self._logger.warning(f'could not read instance {instance_id}: {e}')
            return None

        for reservation in Utils.get_value_as_list('Reservations', response, []):
            for instance in Utils.get_value_as_list('Instances', reservation, []):
                return instance
        self._logger.warning(f'ec2 reported no instance for {instance_id}')
        return None

    def get_bootstrap_status(self, instance_id: str) -> Optional[str]:
        """
        what the bootstrap recorded on the instance before it gave up. an empty string
        means ec2 answered and the host recorded nothing, an instance that no longer
        exists included. None means ec2 could not be read, which must not be acted on as
        though the host had recorded nothing.

        the tags come off describe_instances rather than describe_tags so this needs no
        grant the controller does not already hold.
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except ClientError as e:
            if e.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
                return ''
            self._logger.warning(
                f'could not read the bootstrap status of {instance_id}: {e}'
            )
            return None

        reservations = Utils.get_value_as_list('Reservations', response, [])
        if Utils.is_empty(reservations):
            self._logger.warning(
                f'ec2 reported no reservation for {instance_id}, its bootstrap status '
                f'cannot be established'
            )
            return None

        instances = Utils.get_value_as_list('Instances', reservations[0], [])
        if Utils.is_empty(instances):
            self._logger.warning(
                f'ec2 reported no instance for {instance_id}, its bootstrap status '
                f'cannot be established'
            )
            return None

        for tag in Utils.get_value_as_list('Tags', instances[0], []):
            if Utils.get_value_as_string('Key', tag) == BOOTSTRAP_STATUS_TAG:
                return Utils.get_value_as_string('Value', tag, '')
        return ''

    def get_instance_profile_association(self, instance_id: str) -> Optional[Dict]:
        """
        the iam instance profile association ec2 reports for the instance. an empty dict
        means there is none. None means the answer could not be established, which must
        not be acted on as though the instance carried no role.
        """
        try:
            response = self.ec2_client.describe_iam_instance_profile_associations(
                Filters=[
                    {'Name': 'instance-id', 'Values': [instance_id]},
                    {'Name': 'state', 'Values': ['associating', 'associated']},
                ]
            )
        except ClientError as e:
            self._logger.warning(
                f'could not read the instance profile association of {instance_id}: {e}'
            )
            return None

        associations = Utils.get_value_as_list(
            'IamInstanceProfileAssociations', response, []
        )
        if Utils.is_empty(associations):
            return {}
        return associations[0]

    def apply_instance_profile(
        self,
        instance_id: str,
        instance_profile_arn: str,
        association_id: Optional[str] = None,
    ) -> bool:
        """
        put a running or stopped host onto the given iam instance profile. credentials a
        process on the host has already cached stay on the old role until they expire, so
        access follows within the credential lifetime rather than at once.
        """
        try:
            if Utils.is_empty(association_id):
                self.ec2_client.associate_iam_instance_profile(
                    IamInstanceProfile={'Arn': instance_profile_arn},
                    InstanceId=instance_id,
                )
            else:
                self.ec2_client.replace_iam_instance_profile_association(
                    IamInstanceProfile={'Arn': instance_profile_arn},
                    AssociationId=association_id,
                )
        except ClientError as e:
            self._logger.error(
                f'could not put instance {instance_id} on instance profile '
                f'{instance_profile_arn}: {e}'
            )
            return False
        return True

    def _build_and_upload_bootstrap_package(
        self, session: VirtualDesktopSession
    ) -> str:
        bootstrap_context = BootstrapContext(
            config=self.context.config(),
            module_name=constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
            module_id=self.context.module_id(),
            module_set=self.context.module_set(),
            base_os=session.software_stack.base_os.value,
            instance_type=session.server.instance_type,
        )
        bootstrap_context.vars.session = session
        bootstrap_context.vars.session_owner = session.owner
        bootstrap_context.vars.idea_session_id = session.idea_session_id
        bootstrap_context.vars.project = session.project.name
        bootstrap_context.vars.base_os = session.software_stack.base_os.value
        bootstrap_context.vars.bedrock_env = resolve_project_bedrock_env(
            self.context, self._logger, session
        )
        bedrock_project = resolve_bedrock_project(self.context, self._logger, session)
        bootstrap_context.vars.bedrock_model_messages = (
            describe_bedrock_models(bedrock_project.bedrock.inference_profile_arns)
            if bedrock_project is not None
            else []
        )
        bootstrap_context.vars.auto_mode_environment = build_auto_mode_environment(
            cluster_name=bootstrap_context.cluster_name,
            aws_region=bootstrap_context.aws_region,
            cluster_s3_bucket=bootstrap_context.cluster_s3_bucket,
        )
        bootstrap_context.vars.claude_code_output_style = (
            CLAUDE_CODE_DEFAULT_OUTPUT_STYLE
        )
        bootstrap_context.vars.claude_code_permission_mode = (
            resolve_claude_code_permission_mode(self.context.config())
        )

        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(session.software_stack.base_os).lower()

        self._logger.info(
            f'{session.idea_session_id} OS detection: base_os={session.software_stack.base_os}, is_windows={is_windows}'
        )

        # Configure CloudWatch logging and metrics for virtual desktop hosts
        # Configure CloudWatch with custom metrics options to include instance dimensions
        cloudwatch_logs_enabled = self.context.config().get_bool(
            'cluster.cloudwatch_logs.enabled', False
        ) and self.context.config().get_bool(
            'virtual-desktop-controller.cloudwatch_logs.enabled', False
        )

        metrics_provider = self.context.config().get_string('metrics.provider')
        cloudwatch_metrics_enabled = (
            Utils.is_not_empty(metrics_provider)
            and metrics_provider == constants.METRICS_PROVIDER_CLOUDWATCH
        )

        self._logger.debug(
            f'{session.idea_session_id} CloudWatch settings: logs_enabled={cloudwatch_logs_enabled}, metrics_enabled={cloudwatch_metrics_enabled}, metrics_provider={metrics_provider}, is_windows={is_windows}'
        )

        if cloudwatch_logs_enabled or cloudwatch_metrics_enabled:
            from ideasdk.metrics.cloudwatch import (
                CloudWatchAgentConfigOptions,
                CloudWatchAgentLogsOptions,
                CloudWatchAgentMetricsOptions,
                CloudWatchAgentConfig,
            )

            # Define log files based on OS
            log_files = []
            if cloudwatch_logs_enabled:
                if is_windows:
                    # For Windows, we rely on windows_events section in the template for Event Logs
                    # Only include custom log files here if needed
                    log_files = []
                else:
                    # Define log files for Linux virtual desktop hosts
                    log_files = [
                        CloudWatchAgentLogFileOptions(
                            file_path='/var/log/messages',
                            log_group_name=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-host',
                            log_stream_name='system_{ip_address}',
                        ),
                        CloudWatchAgentLogFileOptions(
                            file_path='/var/log/syslog',
                            log_group_name=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-host',
                            log_stream_name='syslog_{ip_address}',
                        ),
                    ]

            # Create custom metrics options with instance dimensions
            include_metrics = [
                'cpu',
                'disk',
                'diskio',
                'mem',
                'net',
                'netstat',
                'statsd',
                'processes',
            ]

            # Add OS-specific metrics
            if not is_windows:
                include_metrics.append('swap')
            else:
                include_metrics.append('paging_file')

            if bootstrap_context.is_nvidia_gpu():
                include_metrics.append('nvidia_gpu')

            # Add instance dimensions to track individual virtual desktop hosts
            metrics_options = CloudWatchAgentMetricsOptions.default_options(
                namespace='CWAgent', include=include_metrics
            )
            # Set include_dimensions after creating the default options
            metrics_options.include_dimensions = ['InstanceId']
            self._logger.debug(
                f'{session.idea_session_id} Metrics options include_dimensions set to: {metrics_options.include_dimensions}'
            )

            # Build the CloudWatch agent configuration
            cloudwatch_agent_obj = CloudWatchAgentConfig(
                cluster_config=self.context.config(),
                options=CloudWatchAgentConfigOptions(
                    module_id=bootstrap_context.module_id,
                    base_os=bootstrap_context.base_os,
                    enable_logs=cloudwatch_logs_enabled,
                    logs=CloudWatchAgentLogsOptions(files=log_files)
                    if cloudwatch_logs_enabled
                    else None,
                    enable_metrics=cloudwatch_metrics_enabled,
                    metrics=metrics_options if cloudwatch_metrics_enabled else None,
                ),
            )

            # Debug the include_dimensions before building
            self._logger.debug(
                f'{session.idea_session_id} CloudWatchAgentConfig include_dimensions: {cloudwatch_agent_obj.include_dimensions}'
            )

            cloudwatch_config = cloudwatch_agent_obj.build()

            bootstrap_context.vars.cloudwatch_agent_config = cloudwatch_config
            self._logger.debug(
                f'{session.idea_session_id} CloudWatch agent config created for {bootstrap_context.base_os}: {len(str(cloudwatch_config))} chars'
            )
            self._logger.debug(
                f'{session.idea_session_id} CloudWatch agent config content: {Utils.to_json(cloudwatch_config)}'
            )
        else:
            self._logger.debug(
                f'{session.idea_session_id} CloudWatch agent config NOT created for {bootstrap_context.base_os} - conditions not met (logs_enabled={cloudwatch_logs_enabled}, metrics_enabled={cloudwatch_metrics_enabled})'
            )

        if not is_windows:
            escape_chars = '\\'
        else:
            escape_chars = '`'

        # TODO: Deprecate
        bootstrap_context.vars.dcv_host_ready_message = f'{{{escape_chars}"event_group_id{escape_chars}":{escape_chars}"{session.idea_session_id}{escape_chars}",{escape_chars}"event_type{escape_chars}":{escape_chars}"{VirtualDesktopEventType.DCV_HOST_READY_EVENT}{escape_chars}",{escape_chars}"detail{escape_chars}":{{{escape_chars}"idea_session_id{escape_chars}":{escape_chars}"{session.idea_session_id}{escape_chars}",{escape_chars}"idea_session_owner{escape_chars}":{escape_chars}"{session.owner}{escape_chars}"}}}}'

        component = 'virtual-desktop-host-linux'
        if is_windows:
            component = 'virtual-desktop-host-windows'

        bootstrap_package_archive_file = BootstrapPackageBuilder(
            bootstrap_context=bootstrap_context,
            source_directory=self.context.get_bootstrap_dir(),
            target_package_basename=f'dcv-host-{session.idea_session_id}',
            components=[component],
            tmp_dir=os.path.join(
                f'{self.context.config().get_string("shared-storage.apps.mount_dir", required=True)}',
                self.context.cluster_name(),
                self.context.module_id(),
                'dcv-host-bootstrap',
                session.owner,
                f'{Utils.to_secure_filename(session.name)}-{session.idea_session_id}',
            ),
            force_build=True,
            base_os=str(session.software_stack.base_os.value),
            logger=self._logger,
        ).build()

        self._logger.debug(
            f'{session.idea_session_id} built bootstrap package: {bootstrap_package_archive_file}'
        )
        cluster_s3_bucket = self.context.config().get_string(
            'cluster.cluster_s3_bucket', required=True
        )
        upload_key = f'idea/{self.context.module_id()}/dcv-host-bootstrap/{Utils.to_secure_filename(session.name)}-{session.idea_session_id}/{os.path.basename(bootstrap_package_archive_file)}'
        self._logger.debug(
            f'{session.idea_session_id} uploading bootstrap package: {upload_key}'
        )
        try:
            self.s3_client.upload_file(
                Bucket=cluster_s3_bucket,
                Filename=bootstrap_package_archive_file,
                Key=upload_key,
            )
            self._logger.debug(
                f'{session.idea_session_id} successfully uploaded bootstrap package to S3'
            )
        except botocore.exceptions.ClientError as err:
            error_code = (
                err.response['Error']['Code']
                if 'Error' in err.response and 'Code' in err.response['Error']
                else 'Unknown'
            )
            error_message = (
                err.response['Error']['Message']
                if 'Error' in err.response and 'Message' in err.response['Error']
                else str(err)
            )
            self._logger.error(f'S3 upload failed: {error_code} - {error_message}')
            self._logger.error(
                f'S3 bucket: {cluster_s3_bucket}, key: {upload_key}, file exists: {os.path.exists(bootstrap_package_archive_file)}'
            )
            raise
        return f's3://{cluster_s3_bucket}/{upload_key}'

    def _build_userdata(self, session: VirtualDesktopSession):
        install_commands = ['/bin/bash virtual-desktop-host-linux/setup.sh']

        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(session.software_stack.base_os).lower()

        if is_windows:
            install_commands = [
                'cd "virtual-desktop-host-windows"',
                'Import-Module .\\Install.ps1',
                'Install-WindowsEC2Instance -ConfigureForEVDI',
            ]

        https_proxy = self.context.config().get_string(
            'cluster.network.https_proxy', required=False, default=''
        )
        no_proxy = self.context.config().get_string(
            'cluster.network.no_proxy', required=False, default=''
        )
        proxy_config = {}
        if Utils.is_not_empty(https_proxy):
            proxy_config = {
                'http_proxy': https_proxy,
                'https_proxy': https_proxy,
                'no_proxy': no_proxy,
            }

        user_data_builder = BootstrapUserDataBuilder(
            base_os=str(session.software_stack.base_os.value),
            aws_region=self.context.config().get_string(
                'cluster.aws.region', required=True
            ),
            bootstrap_package_uri=self._build_and_upload_bootstrap_package(session),
            install_commands=install_commands,
            proxy_config=proxy_config,
            substitution_support=False,
        )

        return user_data_builder.build()

    def provision_dcv_host_for_session(self, session: VirtualDesktopSession) -> dict:
        tags = {
            constants.IDEA_TAG_NAME: f'{self.context.cluster_name()}-{session.name}-{session.owner}',
            constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_DCV_HOST,
            constants.IDEA_TAG_CLUSTER_NAME: self.context.cluster_name(),
            constants.IDEA_TAG_MODULE_ID: self.context.module_id(),
            constants.IDEA_TAG_MODULE_NAME: self.context.module_name(),
            constants.IDEA_TAG_MODULE_VERSION: self.context.module_version(),
            constants.IDEA_TAG_BACKUP_PLAN: f'{self.context.cluster_name()}-{self.context.module_id()}',
            constants.IDEA_TAG_PROJECT: session.project.name,
            constants.IDEA_TAG_DCV_SESSION_ID: 'TBD',
            constants.IDEA_TAG_JOB_OWNER: session.owner,
        }

        # Add owner email if available
        try:
            user_result = self.context.accounts_client.get_user(
                GetUserRequest(username=session.owner)
            )
            if user_result and user_result.user and user_result.user.email:
                tags[constants.IDEA_TAG_JOB_OWNER_EMAIL] = user_result.user.email
                self._logger.info(
                    f'Adding owner email tag for {session.idea_session_id}: {user_result.user.email}'
                )
        except Exception as e:
            self._logger.warning(
                f'Failed to fetch owner email for {session.idea_session_id}: {str(e)}'
            )

        if Utils.is_not_empty(session.project.tags):
            for tag in session.project.tags:
                tags[tag.key] = tag.value

        custom_tags = self.context.config().get_list(
            'global-settings.custom_tags', default=[]
        )
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)
        tags = {**custom_tags_dict, **tags}

        # a None-valued tag fails RunInstances validation without naming the tag, so empties
        # are dropped; refuse outright when the project could not be resolved instead of launching an unattributed host.
        if Utils.is_empty(tags.get(constants.IDEA_TAG_PROJECT)):
            raise exceptions.soca_exception(
                error_code=errorcodes.INVALID_PARAMS,
                message=(
                    f'the project for this session could not be resolved, so the desktop was '
                    f'not launched. check that {session.owner} has access to it.'
                ),
            )

        aws_tags = []
        for key, value in tags.items():
            if Utils.is_empty(value):
                self._logger.warning(
                    f'dropping tag {key} for {session.idea_session_id}: no value'
                )
                continue
            aws_tags.append({'Key': key, 'Value': Utils.get_as_string(value)})

        metadata_http_tokens = self.context.config().get_string(
            'virtual-desktop-controller.dcv_session.metadata_http_tokens', required=True
        )

        # Our desired launch tenancy
        desired_tenancy = Utils.get_as_string(
            session.software_stack.launch_tenancy, default='default'
        )

        kms_key_id = self.context.config().get_string(
            'cluster.ebs.kms_key_id', required=False, default=None
        )
        if kms_key_id is None:
            kms_key_id = 'alias/aws/ebs'

        # Handle subnet processing for eVDI hosts

        randomize_subnet_method = self.context.config().get_bool(
            'vdc.dcv_session.network.randomize_subnets',
            default=Utils.get_as_bool(
                constants.DEFAULT_VDI_RANDOMIZE_SUBNETS, default=False
            ),
        )
        subnet_autoretry_method = self.context.config().get_bool(
            'vdc.dcv_session.network.subnet_autoretry',
            default=Utils.get_as_bool(
                constants.DEFAULT_VDI_SUBNET_AUTORETRY, default=True
            ),
        )
        # Determine if we have a specific list of subnets configured for VDI
        configured_vdi_subnets = self.context.config().get_list(
            'vdc.dcv_session.network.private_subnets', default=[]
        )
        # Required=True as these should always be available, and we want to error otherwise
        cluster_private_subnets = self.context.config().get_list(
            'cluster.network.private_subnets', required=True
        )

        _attempt_subnets = []
        # Use a subnet_id if specified
        if Utils.is_not_empty(session.server.subnet_id):
            # this comes in as a string from the API
            self._logger.debug(
                f'Using strict requested subnet_id: {session.server.subnet_id}'
            )
            _subnets = session.server.subnet_id.split(',')
            for _subnet in _subnets:
                _subnet = _subnet.strip()
                if _subnet not in [*cluster_private_subnets, *configured_vdi_subnets]:
                    self._logger.error(
                        f'User requested subnet_id ({_subnet}) is unknown. Ignoring request. Supported subnets: {", ".join([*cluster_private_subnets, *configured_vdi_subnets])}'
                    )
                    raise Exception(
                        f'Requested subnet_id ({_subnet}) is not available in the IDEA cluster configuration. Please contact your Administrator to validate the subnet_id or try again without an explicit subnet_id.'
                    )
                self._logger.debug(
                    f'Adding user supplied subnet_id to attempt list: {_subnet}'
                )
                _attempt_subnets.append(_subnet)
        elif configured_vdi_subnets:
            # A list from the config
            self._logger.debug(
                f'Found configured VDI subnets: {", ".join(configured_vdi_subnets)}'
            )
            _attempt_subnets = configured_vdi_subnets
        else:
            # fallback to a list of cluster private_subnets
            self._logger.debug(
                f'Fallback to cluster private_subnets: {", ".join(cluster_private_subnets)}'
            )
            _attempt_subnets = cluster_private_subnets

        # Shuffle the list if configured for random subnet
        if randomize_subnet_method:
            self._logger.debug(
                'Applying randomize to subnet list due to configuration.'
            )
            random.shuffle(_attempt_subnets)

        # the preferred subnet leads the attempt list when it is one of the candidates, so
        # a desktop lands in the shared filesystem's zone first with the rest behind it as
        # capacity fallback. a session naming its own subnet is left alone.
        if Utils.is_empty(session.server.subnet_id):
            preferred_subnet = self.context.config().get_string(
                'cluster.network.preferred_subnet_id', default=None
            )
            if (
                Utils.is_not_empty(preferred_subnet)
                and preferred_subnet in _attempt_subnets
            ):
                self._logger.debug(
                    f'Preferring subnet {preferred_subnet} ahead of the other candidates.'
                )
                _attempt_subnets = [preferred_subnet] + [
                    subnet for subnet in _attempt_subnets if subnet != preferred_subnet
                ]

        # At this stage _attempt_subnets contains the subnets
        # we want to attempt in the order that we prefer
        # (ordered or pre-shuffled)

        self._logger.info(
            f'Deployment Attempt Ready - Tenancy: {desired_tenancy} Retry: {subnet_autoretry_method} Randomize: {randomize_subnet_method}, Attempt_Subnets({len(_attempt_subnets)}): {", ".join(_attempt_subnets)}'
        )

        if not _attempt_subnets:
            raise exceptions.SocaException(
                error_code=errorcodes.CREATE_SESSION_FAILED,
                message='This cluster has no network configured for virtual desktops. Ask an administrator.',
            )

        # the bootstrap package does not depend on the subnet, so build and upload it once
        # instead of once per retry.
        try:
            user_data = self._build_userdata(session)
        except Exception as err:
            self._logger.error(f'Failed to build the bootstrap package: {err}')
            raise exceptions.SocaException(
                error_code=errorcodes.CREATE_SESSION_FAILED,
                message=LAUNCH_FAILURE_DEFAULT_MESSAGE,
            )

        # a profile other than the shared dcv host profile is a project role, which the launch
        # must be allowed to pass; this distinguishes an authorization failure on the role from one on the instance type.
        _shared_instance_profile_arn = self.context.config().get_string(
            'virtual-desktop-controller.dcv_host_instance_profile_arn'
        )
        _project_instance_profile = (
            Utils.is_not_empty(_shared_instance_profile_arn)
            and session.server.instance_profile_arn != _shared_instance_profile_arn
        )

        _deployment_loop = 0
        _attempt_provision = True

        while _attempt_provision:
            _deployment_loop += 1
            # We just .pop(0) since the list has been randomized already if it was requested
            _subnet_to_try = _attempt_subnets.pop(0)
            _remaining_subnet_count = len(_attempt_subnets)

            self._logger.info(
                f'Deployment attempt #{_deployment_loop}:  Subnet_id: {_subnet_to_try} Remaining Subnets: {_remaining_subnet_count}'
            )
            if _remaining_subnet_count <= 0:
                # this is our last attempt
                self._logger.debug(
                    'Final deployment attempt (_remaining_subnet_count == 0)'
                )
                _attempt_provision = False
            else:
                _attempt_provision = True

            response = None

            try:
                response = self.ec2_client.run_instances(
                    UserData=user_data,
                    ImageId=session.software_stack.ami_id,
                    InstanceType=session.server.instance_type,
                    Placement={'Tenancy': desired_tenancy},
                    TagSpecifications=[{'ResourceType': 'instance', 'Tags': aws_tags}],
                    MaxCount=1,
                    MinCount=1,
                    NetworkInterfaces=[
                        {
                            'DeviceIndex': 0,
                            'AssociatePublicIpAddress': False,
                            'SubnetId': _subnet_to_try,
                            'Groups': session.server.security_groups,
                        }
                    ],
                    IamInstanceProfile={'Arn': session.server.instance_profile_arn},
                    BlockDeviceMappings=[
                        {
                            'DeviceName': Utils.get_ec2_block_device_name(
                                str(session.software_stack.base_os.value)
                            ),
                            'Ebs': {
                                'DeleteOnTermination': True,
                                'VolumeSize': session.server.root_volume_size.int_val(),
                                'Encrypted': Utils.get_as_bool(
                                    constants.DEFAULT_VOLUME_ENCRYPTION_VDI,
                                    default=True,
                                ),
                                'KmsKeyId': kms_key_id,
                                'VolumeType': Utils.get_as_string(
                                    constants.DEFAULT_VOLUME_TYPE_VDI, default='gp3'
                                ),
                            },
                        }
                    ],
                    **(
                        {'KeyName': session.server.key_pair_name}
                        if 'windows' not in str(session.software_stack.base_os).lower()
                        else {}
                    ),
                    HibernationOptions={
                        'Configured': False
                        if Utils.is_empty(session.hibernation_enabled)
                        else session.hibernation_enabled
                    },
                    MetadataOptions={
                        'HttpTokens': metadata_http_tokens,
                        'HttpEndpoint': 'enabled',
                    },
                )
            except botocore.exceptions.ClientError as err:
                _error_code = Utils.get_as_string(
                    err.response['Error']['Code'], default='Unknown'
                )
                _error_message = Utils.get_as_string(
                    err.response['Error']['Message'], default='Unknown'
                )
                self._logger.error(
                    f'EC2 RunInstances error: {_error_code} - {_error_message}'
                )

                if (
                    (
                        _error_code in RETRYABLE_LAUNCH_ERROR_CODES
                        or (
                            _error_code == 'InvalidParameterValue'
                            and AZ_SCOPED_LAUNCH_ERROR.search(_error_message)
                        )
                    )
                    and subnet_autoretry_method
                    and _attempt_provision
                ):
                    self._logger.info(
                        f'Continuing next attempt with remaining subnets {len(_attempt_subnets)}: {_attempt_subnets}'
                    )
                    continue

                # every remaining case is terminal for this request. the reason travels back to
                # the requesting user instead of being dropped here.
                raise exceptions.SocaException(
                    error_code=errorcodes.CREATE_SESSION_FAILED,
                    message=build_launch_failure_message(
                        error_code=_error_code,
                        instance_type=session.server.instance_type,
                        project_instance_profile=_project_instance_profile,
                    ),
                )

            except Exception as err:
                self._logger.error(f'Encountered Deployment Exception: {err}')
                raise exceptions.SocaException(
                    error_code=errorcodes.CREATE_SESSION_FAILED,
                    message=LAUNCH_FAILURE_DEFAULT_MESSAGE,
                )

            # the subnet that won, which is not necessarily the one the request started on
            session.server.subnet_id = _subnet_to_try
            self._logger.info(
                f'Launched dcv host for {session.name} in subnet {_subnet_to_try} '
                f'on attempt #{_deployment_loop}'
            )
            if response:
                self._logger.debug(f'Returning response: {response}')
            return Utils.to_dict(response)

    def _add_instance_data_to_cache(self):
        _start_ec2_data = Utils.current_time_ms()
        self._logger.debug(f'Starting EC2 instance data collection: {_start_ec2_data}')

        with self.instance_types_lock:
            instance_type_names = (
                self.context.cache()
                .long_term()
                .get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
            )
            instance_info_data = (
                self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
            )
            if instance_type_names is None or instance_info_data is None:
                instance_info_data = {}

                ec2_paginator = (
                    self.context.aws().ec2().get_paginator('describe_instance_types')
                )
                ec2_iterator = ec2_paginator.paginate(MaxResults=100)

                for page in ec2_iterator:
                    current_instance_types = Utils.get_value_as_list(
                        'InstanceTypes', page
                    )
                    for current_instance_type in current_instance_types:
                        instance_type_name = Utils.get_value_as_string(
                            'InstanceType', current_instance_type, None
                        )
                        if instance_type_name is not None:
                            instance_info_data[instance_type_name] = (
                                current_instance_type
                            )

                self.context.cache().long_term().set(
                    key=self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY,
                    value=instance_info_data.keys(),
                )
                self.context.cache().long_term().set(
                    key=self.INSTANCE_INFO_CACHE_KEY, value=instance_info_data
                )
                _end_ec2_data = Utils.current_time_ms()
                self._logger.info(
                    f'Completed EC2 instance data cache for {len(instance_info_data)} instance types in {_end_ec2_data - _start_ec2_data}ms'
                )

    def is_gpu_instance(self, instance_type: str) -> bool:
        return self.get_gpu_manufacturer(instance_type) != VirtualDesktopGPU.NO_GPU

    def get_instance_type_info(self, instance_type: str) -> Dict:
        instance_types_data = (
            self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
        )
        if instance_types_data is None:
            # not found in cache, need to update it again.
            self._add_instance_data_to_cache()
            instance_types_data = (
                self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
            )
        return instance_types_data[instance_type]

    def get_instance_ram(self, instance_type: str) -> SocaMemory:
        instance_info = self.get_instance_type_info(instance_type)
        return SocaMemory(
            value=Utils.get_value_as_float(
                'SizeInMiB', Utils.get_value_as_dict('MemoryInfo', instance_info, {}), 0
            ),
            unit=SocaMemoryUnit.MiB,
        )

    def get_architecture(
        self, instance_type: str
    ) -> Optional[VirtualDesktopArchitecture]:
        instance_info = self.get_instance_type_info(instance_type)
        supported_archs = Utils.get_value_as_list(
            'SupportedArchitectures',
            Utils.get_value_as_dict('ProcessorInfo', instance_info, {}),
            [],
        )
        for supported_arch in supported_archs:
            if supported_arch == VirtualDesktopArchitecture.ARM64.value:
                return VirtualDesktopArchitecture.ARM64
            if supported_arch == VirtualDesktopArchitecture.X86_64.value:
                return VirtualDesktopArchitecture.X86_64

    def get_gpu_manufacturer(self, instance_type: str) -> VirtualDesktopGPU:
        instance_info = self.get_instance_type_info(instance_type)
        supported_gpus = Utils.get_value_as_list(
            'Gpus', Utils.get_value_as_dict('GpuInfo', instance_info, {}), []
        )
        if len(supported_gpus) == 0:
            return VirtualDesktopGPU.NO_GPU

        for supported_gpu in supported_gpus:
            if (
                Utils.get_value_as_string('Manufacturer', supported_gpu, '').lower()
                == VirtualDesktopGPU.NVIDIA.lower()
            ):
                return VirtualDesktopGPU.NVIDIA
            elif (
                Utils.get_value_as_string('Manufacturer', supported_gpu, '').lower()
                == VirtualDesktopGPU.AMD.lower()
            ):
                return VirtualDesktopGPU.AMD

        return VirtualDesktopGPU.NO_GPU

    def _get_instance_type_allow_deny(
        self, software_stack: VirtualDesktopSoftwareStack = None
    ) -> Dict[str, set]:
        # a software stack's own allow list wins over the cluster-wide one. entries without a
        # dot are families, entries with a dot are exact instance types.
        if Utils.is_not_empty(software_stack) and Utils.is_not_empty(
            software_stack.allowed_instance_types
        ):
            allowed_instance_types = software_stack.allowed_instance_types
            self._logger.debug(
                f'Using software stack-specific allowed instance types for stack {software_stack.stack_id}: {allowed_instance_types}'
            )
        else:
            allowed_instance_types = self.context.config().get_list(
                'virtual-desktop-controller.dcv_session.instance_types.allow',
                default=[],
            )

        denied_instance_types = set(
            self.context.config().get_list(
                'virtual-desktop-controller.dcv_session.instance_types.deny', default=[]
            )
        )

        allow_deny = {
            'allowed_names': set(),
            'allowed_families': set(),
            'denied_names': set(),
            'denied_families': set(),
        }
        for instance_type in allowed_instance_types:
            if '.' in instance_type:
                allow_deny['allowed_names'].add(instance_type)
            else:
                allow_deny['allowed_families'].add(instance_type)
        for instance_type in denied_instance_types:
            if '.' in instance_type:
                allow_deny['denied_names'].add(instance_type)
            else:
                allow_deny['denied_families'].add(instance_type)
        return allow_deny

    def _instance_type_rejection_reason(
        self,
        instance_type_name: str,
        instance_info: Dict,
        allow_deny: Dict[str, set],
        hibernation_support: bool,
        software_stack: VirtualDesktopSoftwareStack = None,
        gpu: VirtualDesktopGPU = None,
    ) -> Optional[str]:
        """
        why this instance type cannot serve this request, in words the requesting user can act
        on. None means the type is usable. single source of truth for the size filter.
        """
        instance_type_family = instance_type_name.split('.')[0]

        if (
            instance_type_name not in allow_deny['allowed_names']
            and instance_type_family not in allow_deny['allowed_families']
        ) or (
            instance_type_name in allow_deny['denied_names']
            or instance_type_family in allow_deny['denied_families']
        ):
            return f'{instance_type_name} is not one of the sizes your administrator makes available for virtual desktops.'

        if Utils.is_not_empty(software_stack):
            instance_ram = self.get_instance_ram(instance_type_name)
            if software_stack.min_ram > instance_ram:
                return (
                    f'{instance_type_name} has {instance_ram.gib():g} GB of memory. '
                    f'This desktop image needs at least {software_stack.min_ram.gib():g} GB.'
                )

        hibernation_supported = Utils.get_value_as_bool(
            'HibernationSupported', instance_info, default=False
        )
        if hibernation_support and not hibernation_supported:
            return f'{instance_type_name} cannot be hibernated. Turn hibernation off, or choose a size that supports it.'

        supported_archs = Utils.get_value_as_list(
            'SupportedArchitectures',
            Utils.get_value_as_dict('ProcessorInfo', instance_info, {}),
            [],
        )
        if (
            Utils.is_not_empty(software_stack)
            and software_stack.architecture.value not in supported_archs
        ):
            return f'{instance_type_name} uses a different processor type than this desktop image was built for.'

        # For Windows, proper GPU check is strict, Stacks with NO_GPU should return Instances without GPU.
        # Simplified Windows detection to match all Windows variations
        is_windows = (
            Utils.is_not_empty(software_stack)
            and 'windows' in str(software_stack.base_os).lower()
        )

        supported_gpus = Utils.get_value_as_list(
            'Gpus', Utils.get_value_as_dict('GpuInfo', instance_info, {}), []
        )
        perform_gpu_check = False
        gpu_to_check_against = None
        if Utils.is_not_empty(gpu):
            # We have gotten a GPU as a parameter. We need to perform a strict check
            perform_gpu_check = True
            gpu_to_check_against = gpu
        elif is_windows:
            # For Windows the GPU check is strict, Stacks with NO_GPU should return Instances without GPU.
            perform_gpu_check = False
            gpu_to_check_against = software_stack.gpu
        elif Utils.is_not_empty(software_stack) and not is_windows:
            # For Linux the GPU is not as strict, Stacks with NO_GPU can return Instances with GPU
            perform_gpu_check = software_stack.gpu != VirtualDesktopGPU.NO_GPU
            gpu_to_check_against = software_stack.gpu

        if perform_gpu_check:
            if gpu_to_check_against == VirtualDesktopGPU.NO_GPU:
                if len(supported_gpus) > 0:
                    return f'{instance_type_name} has a graphics card. This desktop image has to run on a size without one.'
            else:
                gpu_found = False
                for supported_gpu in supported_gpus:
                    gpu_found = (
                        gpu_to_check_against.value.lower()
                        == Utils.get_value_as_string(
                            'Manufacturer', supported_gpu, ''
                        ).lower()
                    )
                    if gpu_found:
                        break

                if not gpu_found:
                    return f'{instance_type_name} does not have the {gpu_to_check_against.value} graphics card this desktop image needs.'

        return None

    def get_instance_type_rejection_reason(
        self,
        instance_type_name: str,
        hibernation_support: bool,
        software_stack: VirtualDesktopSoftwareStack = None,
        gpu: VirtualDesktopGPU = None,
    ) -> Optional[str]:
        """
        the same filter get_valid_instance_types applies, asked about one requested size.
        """
        try:
            instance_info = self.get_instance_type_info(instance_type_name)
        except KeyError:
            return f'{instance_type_name} is not available in this region.'

        return self._instance_type_rejection_reason(
            instance_type_name=instance_type_name,
            instance_info=instance_info,
            allow_deny=self._get_instance_type_allow_deny(software_stack),
            hibernation_support=hibernation_support,
            software_stack=software_stack,
            gpu=gpu,
        )

    def get_valid_instance_types(
        self,
        hibernation_support: bool,
        software_stack: VirtualDesktopSoftwareStack = None,
        gpu: VirtualDesktopGPU = None,
    ) -> List[Dict]:
        instance_types_names = (
            self.context.cache()
            .long_term()
            .get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
        )
        instance_info_data = (
            self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
        )
        if instance_types_names is None or instance_info_data is None:
            # not found in cache, need to update it again.
            self._add_instance_data_to_cache()
            instance_types_names = (
                self.context.cache()
                .long_term()
                .get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
            )
            instance_info_data = (
                self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
            )

        # We now have a list of all instance types (Cache has been updated IF it was empty).
        valid_instance_types = []
        valid_instance_types_names = []

        allow_deny = self._get_instance_type_allow_deny(software_stack)

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                'get_valid_instance_types() - Instance Allow/Deny Summary'
            )
            self._logger.debug(
                f'Allowed instance Families: {allow_deny["allowed_families"]}'
            )
            self._logger.debug(f'Allowed instances: {allow_deny["allowed_names"]}')
            self._logger.debug(
                f'Denied instance Families: {allow_deny["denied_families"]}'
            )
            self._logger.debug(f'Denied instances: {allow_deny["denied_names"]}')

        for instance_type_name in instance_types_names:
            instance_info = instance_info_data[instance_type_name]
            rejection_reason = self._instance_type_rejection_reason(
                instance_type_name=instance_type_name,
                instance_info=instance_info,
                allow_deny=allow_deny,
                hibernation_support=hibernation_support,
                software_stack=software_stack,
                gpu=gpu,
            )
            if rejection_reason is not None:
                self._logger.debug(
                    f'Instance {instance_type_name} skipped - {rejection_reason}'
                )
                continue

            # All checks passed if we make it this far
            self._logger.debug(
                f'Instance {instance_type_name} - Added as valid_instance_types'
            )
            valid_instance_types_names.append(instance_type_name)
            valid_instance_types.append(instance_info)
        self._logger.debug(
            f'Returning {len(valid_instance_types)} valid_instance_types: {valid_instance_types_names}'
        )
        return valid_instance_types

    def describe_image_id(self, ami_id: str) -> dict:
        try:
            response = Utils.to_dict(self.ec2_client.describe_images(ImageIds=[ami_id]))
        except ClientError as e:
            self._logger.error(e)
            return {}

        images = Utils.get_value_as_list('Images', response, [])
        for image in images:
            return image
        return {}

    def create_image_for_instance_id(self, instance_id, image_name, image_description):
        if Utils.is_empty(image_name):
            image_name = f'IDEA-IMAGE-NAME-{instance_id}'

        if Utils.is_empty(image_description):
            image_description = f'IDEA-IMAGE-DESCRIPTION-{instance_id}'

        response = Utils.to_dict(
            self.ec2_client.create_image(
                Name=image_name,
                Description=image_description,
                NoReboot=True,
                InstanceId=instance_id,
            )
        )
        return response

    def is_active_directory(self) -> bool:
        provider = self.context.config().get_string(
            'directoryservice.provider', required=True
        )
        return provider in {
            constants.DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY,
            constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY,
        }

    def change_instance_type(
        self, instance_id: str, instance_type_name: str
    ) -> tuple[str, bool]:
        if Utils.is_empty(instance_id) or Utils.is_empty(instance_type_name):
            return f'Invalid {instance_id} or {instance_type_name}', False

        self._logger.info(
            f'Changing instance type for {instance_id} to {instance_type_name}'
        )
        # TODO: check if server is already stopped.
        try:
            _ = self.ec2_client.modify_instance_attribute(
                InstanceId=instance_id,
                Attribute='instanceType',
                Value=instance_type_name,
            )
        except Exception as e:
            self._logger.error(e)
            return repr(e), False
        return '', True

    def get_virtual_desktop_users_group(self) -> str:
        return self.group_name_helper.get_module_users_group(self.context.module_id())

    def get_virtual_desktop_admin_group(self) -> str:
        return self.group_name_helper.get_module_administrators_group(
            module_id=self.context.module_id()
        )
