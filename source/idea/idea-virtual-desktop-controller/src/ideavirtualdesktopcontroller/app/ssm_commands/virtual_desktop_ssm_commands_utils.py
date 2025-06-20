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
from ideadatamodel import VirtualDesktopBaseOS
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandsDB,
    VirtualDesktopSSMCommand,
    VirtualDesktopSSMCommandType,
)


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
                "DCV_Session_ID=$(dcv list-sessions -j | jq -r '.[].id')",
                'DCV_Describe_Session=$(dcv describe-session $DCV_Session_ID -j)',
                "CPUAveragePerformanceLast10Secs=$(top -d 5 -b -n2 | grep 'Cpu(s)' | tail -n 1 | awk '{print $2 + $4}')",
                "SSH_Connection_Count=$(last -Fi | grep 'still logged in' | grep -v 0.0.0.0 | wc -l)",
                "SSH_Last_Disconnect_Time=$(last -Fi | awk '!/0.0.0.0|still logged in|wtmp|^\\s*$/ {print $11, $12, $13, $14}' | sort -k3M -k4n -k5 -k6n | tail -n 1)",
                '[ -n "$SSH_Last_Disconnect_Time" ] && SSH_Last_Disconnect_ISO=$(date -u -d "$SSH_Last_Disconnect_Time" +"%Y-%m-%dT%H:%M:%S.%6NZ") || SSH_Last_Disconnect_ISO=""',
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
