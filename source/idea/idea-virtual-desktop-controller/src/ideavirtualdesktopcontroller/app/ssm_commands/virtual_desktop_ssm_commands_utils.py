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
from typing import List

import ideavirtualdesktopcontroller
from ideadatamodel import VirtualDesktopBaseOS, VirtualDesktopSession
from ideasdk.utils import Utils, Jinja2Utils
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandsDB,
    VirtualDesktopSSMCommand,
    VirtualDesktopSSMCommandType,
)


# Bump whenever the refresh set or the contents of its templates change.
BOOTSTRAP_REFRESH_VERSION = 4
BOOTSTRAP_REFRESH_TEMPLATES = (
    '_templates/linux/ssh_post_quantum_kex.jinja2',
    '_templates/linux/gnome_online_accounts.jinja2',
    '_templates/linux/openpbs_client_server.jinja2',
)


def bootstrap_refresh_variables(config, logger=None) -> dict:
    """
    what the refresh renders its templates with: the scheduler name a PBS client talks to
    and the zone it resolves it through, derived exactly as openpbs_client.jinja2 does at
    first bootstrap, so a desktop from before a scheduler move picks the current name up.
    the name is always the first label, the server's own server_name: qsub completes a short
    job id with it, and a dependency on any other spelling never releases.
    bootstrap_refresh tells a template shared with first bootstrap which path it is on.
    """
    scheduler_dns_name = config.get_string('scheduler.private_dns_name')
    if not scheduler_dns_name:
        return {
            'pbs_server_name': None,
            'private_zone': None,
            'bootstrap_refresh': True,
        }
    stable = bool(config.get_bool('scheduler.use_stable_server_name', default=False))
    private_zone = (
        config.get_string('cluster.route53.private_hosted_zone_name')
        if stable
        else None
    )
    if stable and not private_zone and logger is not None:
        # the rest of the refresh (ssh kex, online accounts) still goes out
        logger.warning(
            'cluster.route53.private_hosted_zone_name is not set: the refresh leaves the '
            'scheduler search path alone'
        )
    return {
        'pbs_server_name': scheduler_dns_name.split('.')[0],
        'private_zone': private_zone or None,
        'bootstrap_refresh': True,
    }


class VirtualDesktopSSMCommandsUtils:
    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        db: VirtualDesktopSSMCommandsDB,
    ):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-ssm-commands-utils')
        self._ssm_commands_db = db
        self._ssm_client = self.context.aws().ssm()

    def submit_ssm_command_to_resume_session(
        self,
        instance_id: str,
        idea_session_id: str,
        idea_session_owner: str,
        commands: List[str],
        document_name: str,
    ) -> str:
        response = self._ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName=document_name,
            Comment=f'idea_session_id: {idea_session_id}, owner: {idea_session_owner}',
            Parameters={'commands': commands},
            ServiceRoleArn=self.context.config().get_string(
                'virtual-desktop-controller.ssm_commands_pass_role_arn', required=True
            ),
            NotificationConfig={
                'NotificationArn': self.context.config().get_string(
                    'virtual-desktop-controller.ssm_commands_sns_topic_arn',
                    required=True,
                ),
                'NotificationEvents': ['All'],
                'NotificationType': 'Invocation',
            },
            CloudWatchOutputConfig={
                'CloudWatchOutputEnabled': True,
                'CloudWatchLogGroupName': f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/resume',
            },
            OutputS3BucketName=self.context.config().get_string(
                'cluster.cluster_s3_bucket', required=True
            ),
            OutputS3KeyPrefix=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/resume',
        )
        # self._logger.info(f'response is {response}')
        command_id = Utils.get_value_as_string(
            'CommandId', Utils.get_value_as_dict('Command', response, {}), ''
        )
        _ = self._ssm_commands_db.create(
            VirtualDesktopSSMCommand(
                command_id=command_id,
                command_type=VirtualDesktopSSMCommandType.RESUME_SESSION,
                additional_payload={
                    'idea_session_id': idea_session_id,
                    'idea_session_owner': idea_session_owner,
                    'instance_id': instance_id,
                },
            )
        )
        self._logger.info(
            f'SSM command to resume session sent for {idea_session_id}:, owner: {idea_session_owner}.'
        )
        return command_id

    def submit_ssm_command_to_disable_userdata_execution_on_windows(
        self, instance_id: str, idea_session_id: str, idea_session_owner: str
    ) -> str:
        response = self._ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunPowerShellScript',
            Comment='Enabling userdata execution for Windows EC2 Instance',
            Parameters={
                'commands': [
                    'Unregister-ScheduledTask -TaskName "Amazon Ec2 Launch - Instance Initialization" -Confirm$False'
                ]
            },
            ServiceRoleArn=self.context.config().get_string(
                'virtual-desktop-controller.ssm_commands_pass_role_arn', required=True
            ),
            NotificationConfig={
                'NotificationArn': self.context.config().get_string(
                    'virtual-desktop-controller.ssm_commands_sns_topic_arn',
                    required=True,
                ),
                'NotificationEvents': ['All'],
                'NotificationType': 'Invocation',
            },
            CloudWatchOutputConfig={
                'CloudWatchOutputEnabled': True,
                'CloudWatchLogGroupName': f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/disable-userdata',
            },
            OutputS3BucketName=self.context.config().get_string(
                'cluster.cluster_s3_bucket', required=True
            ),
            OutputS3KeyPrefix=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/disable-userdata',
        )
        # self._logger.info(f'response is {response}')
        command_id = Utils.get_value_as_string(
            'CommandId', Utils.get_value_as_dict('Command', response, {}), ''
        )
        _ = self._ssm_commands_db.create(
            VirtualDesktopSSMCommand(
                command_id=command_id,
                command_type=VirtualDesktopSSMCommandType.WINDOWS_DISABLE_USERDATA_EXECUTION,
                additional_payload={
                    'idea_session_id': idea_session_id,
                    'idea_session_owner': idea_session_owner,
                    'instance_id': instance_id,
                },
            )
        )
        self._logger.info(
            f'SSM command to disable userdata execution sent to {instance_id}.'
        )
        return command_id

    def submit_ssm_command_to_enable_userdata_execution_on_windows(
        self,
        instance_id: str,
        idea_session_id: str,
        idea_session_owner: str,
        software_stack_id: str,
    ) -> str:
        # Script that supports both EC2 Launch and EC2Launch v2
        commands = [
            # Check for EC2Launch v2 (newer version)
            '$EC2LaunchV2Path = "C:\\Program Files\\Amazon\\EC2Launch\\EC2Launch.exe"',
            # Check for EC2 Launch (older version)
            '$EC2LaunchPath = "C:\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Scripts\\InitializeInstance.ps1"',
            # Execute the appropriate command based on what's available
            'if (Test-Path $EC2LaunchV2Path) {',
            '    # For EC2Launch v2',
            '    & "C:\\Program Files\\Amazon\\EC2Launch\\EC2Launch.exe" reset',
            '} elseif (Test-Path $EC2LaunchPath) {',
            '    # For EC2 Launch',
            '    & "C:\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Scripts\\InitializeInstance.ps1" -Schedule',
            '} else {',
            '    Write-Error "Neither EC2Launch nor EC2Launch v2 found on this instance"',
            '}',
        ]

        response = self._ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunPowerShellScript',
            Comment='Enabling userdata execution for Windows EC2 Instance',
            Parameters={'commands': commands},
            ServiceRoleArn=self.context.config().get_string(
                'virtual-desktop-controller.ssm_commands_pass_role_arn', required=True
            ),
            NotificationConfig={
                'NotificationArn': self.context.config().get_string(
                    'virtual-desktop-controller.ssm_commands_sns_topic_arn',
                    required=True,
                ),
                'NotificationEvents': ['All'],
                'NotificationType': 'Invocation',
            },
            CloudWatchOutputConfig={
                'CloudWatchOutputEnabled': True,
                'CloudWatchLogGroupName': f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/enable-userdata',
            },
            OutputS3BucketName=self.context.config().get_string(
                'cluster.cluster_s3_bucket', required=True
            ),
            OutputS3KeyPrefix=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/enable-userdata',
        )
        # self._logger.info(f'response is {response}')
        command_id = Utils.get_value_as_string(
            'CommandId', Utils.get_value_as_dict('Command', response, {}), ''
        )
        _ = self._ssm_commands_db.create(
            VirtualDesktopSSMCommand(
                command_id=command_id,
                command_type=VirtualDesktopSSMCommandType.WINDOWS_ENABLE_USERDATA_EXECUTION,
                additional_payload={
                    'idea_session_id': idea_session_id,
                    'idea_session_owner': idea_session_owner,
                    'instance_id': instance_id,
                    'software_stack_id': software_stack_id,
                },
            )
        )
        self._logger.info(
            f'SSM command to enable userdata execution sent to {instance_id}.'
        )
        return command_id

    def submit_ssm_command_to_refresh_bootstrap(
        self, session: VirtualDesktopSession
    ) -> str:
        env = Jinja2Utils.env_using_file_system_loader(self.context.get_bootstrap_dir())
        variables = bootstrap_refresh_variables(self.context.config(), self._logger)
        block = '\n'.join(
            env.get_template(template).render(
                context={'base_os': session.base_os}, **variables
            )
            for template in BOOTSTRAP_REFRESH_TEMPLATES
        )
        response = self._ssm_client.send_command(
            InstanceIds=[session.server.instance_id],
            DocumentName='AWS-RunShellScript',
            Comment=f'Refresh bootstrap for {session.idea_session_id}',
            Parameters={
                'commands': [
                    'log_info() { echo "$@"; }\nlog_warning() { echo "$@"; }\n' + block
                ]
            },
            ServiceRoleArn=self.context.config().get_string(
                'virtual-desktop-controller.ssm_commands_pass_role_arn', required=True
            ),
            NotificationConfig={
                'NotificationArn': self.context.config().get_string(
                    'virtual-desktop-controller.ssm_commands_sns_topic_arn',
                    required=True,
                ),
                'NotificationEvents': ['All'],
                'NotificationType': 'Invocation',
            },
        )
        command_id = response['Command']['CommandId']
        self._ssm_commands_db.create(
            VirtualDesktopSSMCommand(
                command_id=command_id,
                command_type=VirtualDesktopSSMCommandType.REFRESH_BOOTSTRAP,
                additional_payload={
                    'idea_session_id': session.idea_session_id,
                    'idea_session_owner': session.owner,
                    'instance_id': session.server.instance_id,
                    'refresh_version': BOOTSTRAP_REFRESH_VERSION,
                },
            )
        )
        return command_id

    def submit_ssm_command_to_get_cpu_utilization(
        self,
        instance_id: str,
        idea_session_id: str,
        idea_session_owner: str,
        base_os: VirtualDesktopBaseOS,
    ):
        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(base_os).lower()

        if is_windows:
            document_name = 'AWS-RunPowerShellScript'
            commands = [
                '$DCV_Session_JSON = Invoke-Expression "& \'C:\\Program Files\\NICE\\DCV\\Server\\bin\\dcv\' list-sessions -j" | ConvertFrom-Json',
                '$DCV_Session_ID = $DCV_Session_JSON.id',
                '$DCV_Describe_Session = Invoke-Expression "& \'C:\\Program Files\\NICE\\DCV\\Server\\bin\\dcv\' describe-session $DCV_Session_ID -j" | ConvertFrom-Json',
                '$CPUAveragePerformanceLast10Secs = (GET-COUNTER -Counter "\\Processor(_Total)\\% Processor Time" -SampleInterval 2 -MaxSamples 5 |select -ExpandProperty countersamples | select -ExpandProperty cookedvalue | Measure-Object -Average).average',
                '$output = @{}',
                '$dcv = @{}',
                '$dcv["num-of-connections"] = $DCV_Describe_Session."num-of-connections"',
                '$dcv["creation-time"] = $DCV_Describe_Session."creation-time"',
                '$dcv["last-disconnection-time"] = $DCV_Describe_Session."last-disconnection-time"',
                '$output["DCV"] = $dcv',
                '$output["CPUAveragePerformanceLast10Secs"] = $CPUAveragePerformanceLast10Secs',
                '$output | ConvertTo-Json',
            ]
        else:
            document_name = 'AWS-RunShellScript'
            commands = [
                'set -eu',
                'export LC_ALL=C',
                "DCV_Session_ID=$(dcv list-sessions -j | jq -r '.[].id')",
                'DCV_Describe_Session=$(dcv describe-session $DCV_Session_ID -j)',
                "CPUAveragePerformanceLast10Secs=$(top -d 5 -b -n2 | grep 'Cpu(s)' | tail -n 1 | awk '{print $2 + $4}')",
                # logind distinguishes SSH logins from graphical desktop sessions.
                'SSH_Sessions=$(loginctl list-sessions --no-legend --no-pager)',
                'SSH_Login_Count=0',
                "for session in $(printf '%s\\n' \"$SSH_Sessions\" | awk '{print $1}'); do\n  properties=$(loginctl show-session \"$session\" -p Service -p Remote -p State) || exit 1\n  if printf '%s\\n' \"$properties\" | grep -qx 'Service=sshd' &&\n     printf '%s\\n' \"$properties\" | grep -qx 'Remote=yes' &&\n     printf '%s\\n' \"$properties\" | grep -Eq '^State=(active|online)$'; then\n    SSH_Login_Count=$((SSH_Login_Count + 1))\n  fi\ndone",
                # Tunnel-only connections may have no PAM session.
                "SSH_Established=$(ss -Hnt state established '( sport = :22 )')",
                "SSH_Established_Count=$(printf '%s\\n' \"$SSH_Established\" | awk 'NF {n++} END {print n+0}')",
                'SSH_Connection_Count=$SSH_Login_Count',
                'if [ "$SSH_Established_Count" -gt "$SSH_Connection_Count" ]; then SSH_Connection_Count=$SSH_Established_Count; fi',
                # Keep logout history where available, ordered by year before month.
                "SSH_Last_Disconnect_Time=$(if command -v last >/dev/null 2>&1; then LC_ALL=C last -Fi; fi | LC_ALL=C awk '!/0\\.0\\.0\\.0|still logged in|wtmp|^[[:space:]]*$/ && $14 ~ /^[0-9][0-9][0-9][0-9]$/ {print $11, $12, $13, $14}' | LC_ALL=C sort -k4,4n -k1,1M -k2,2n -k3,3 | tail -n 1)",
                '[ -n "$SSH_Last_Disconnect_Time" ] && SSH_Last_Disconnect_ISO=$(date -u -d "$SSH_Last_Disconnect_Time" +"%Y-%m-%dT%H:%M:%S.%6NZ") || SSH_Last_Disconnect_ISO=""',
                # Start the grace period when SSH closes, including hosts without wtmp.
                'install -d -m 700 /run/idea-idle-detection',
                'if [ "$SSH_Connection_Count" -gt 0 ] || [ -f /run/idea-idle-detection/ssh-active ] || [ ! -f /run/idea-idle-detection/ssh-disconnected ]; then\n  date -u +"%Y-%m-%dT%H:%M:%S.%6NZ" > /run/idea-idle-detection/ssh-disconnected\nfi\nif [ "$SSH_Connection_Count" -gt 0 ]; then\n  touch /run/idea-idle-detection/ssh-active\nelse\n  rm -f /run/idea-idle-detection/ssh-active\nfi',
                'SSH_Observed_Disconnect_ISO=$(cat /run/idea-idle-detection/ssh-disconnected)',
                'SSH_Last_Disconnect_ISO=$(printf \'%s\\n\' "$SSH_Last_Disconnect_ISO" "$SSH_Observed_Disconnect_ISO" | sort | tail -n 1)',
                'Final_JSON=$(jq -c -n --argjson dcv "$DCV_Describe_Session" --argjson cpuAvg "$CPUAveragePerformanceLast10Secs" --arg sshTime "$SSH_Last_Disconnect_ISO" --argjson sshCount "$SSH_Connection_Count" \'{"DCV": ($dcv | .["num-of-connections"] = ($sshCount | if . > $dcv["num-of-connections"] then . else $dcv["num-of-connections"] end) | .["last-disconnection-time"] = (if $dcv["last-disconnection-time"] == "" and $sshTime > $dcv["creation-time"] then $sshTime elif $dcv["last-disconnection-time"] != "" and $sshTime > $dcv["last-disconnection-time"] then $sshTime else $dcv["last-disconnection-time"] end)), "CPUAveragePerformanceLast10Secs": $cpuAvg, "SSH_Connection_Count": $sshCount, "SSH_Last_Disconnect_ISO": $sshTime}\')',
                'echo "$Final_JSON"',
            ]

        response = self._ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName=document_name,
            Comment=f'Checking CPU Utilization for {instance_id}',
            Parameters={'commands': commands},
            ServiceRoleArn=self.context.config().get_string(
                'virtual-desktop-controller.ssm_commands_pass_role_arn', required=True
            ),
            NotificationConfig={
                'NotificationArn': self.context.config().get_string(
                    'virtual-desktop-controller.ssm_commands_sns_topic_arn',
                    required=True,
                ),
                'NotificationEvents': ['All'],
                'NotificationType': 'Invocation',
            },
            CloudWatchOutputConfig={
                'CloudWatchOutputEnabled': True,
                'CloudWatchLogGroupName': f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/cpu-utilization',
            },
            OutputS3BucketName=self.context.config().get_string(
                'cluster.cluster_s3_bucket', required=True
            ),
            OutputS3KeyPrefix=f'/{self.context.cluster_name()}/{self.context.module_id()}/dcv-session/{idea_session_id}/cpu-utilization',
        )
        # self._logger.info(f'response is {response}')
        command_id = Utils.get_value_as_string(
            'CommandId', Utils.get_value_as_dict('Command', response, {}), ''
        )
        _ = self._ssm_commands_db.create(
            VirtualDesktopSSMCommand(
                command_id=command_id,
                command_type=VirtualDesktopSSMCommandType.CPU_UTILIZATION_CHECK_STOP_SCHEDULED_SESSION,
                additional_payload={
                    'idea_session_id': idea_session_id,
                    'idea_session_owner': idea_session_owner,
                    'instance_id': instance_id,
                },
            )
        )
        self._logger.info(
            f'SSM command to check CPU Utilization sent to {instance_id}.'
        )
        return command_id
