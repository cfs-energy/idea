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

import ideavirtualdesktopcontroller
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)
from datetime import datetime, timezone, timedelta
from dateutil.parser import parse


class IDEASessionCPUUtilizationCommandProgressEventHandler(
    BaseVirtualDesktopControllerEventHandler
):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'cpu-utilization-command-progress-event-handler')
        self._ssm_client = self.context.aws().ssm()

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        status = Utils.get_value_as_string('status', event.detail, '')
        instance_id = Utils.get_value_as_string('instance_id', event.detail, None)
        idea_session_id = Utils.get_value_as_string(
            'idea_session_id', event.detail, None
        )
        idea_session_owner = Utils.get_value_as_string(
            'idea_session_owner', event.detail, None
        )
        command_id = Utils.get_value_as_string('command_id', event.detail, None)

        if status in {'Success', 'Failed'}:
            session = self.session_db.get_from_db(
                idea_session_owner=idea_session_owner, idea_session_id=idea_session_id
            )
            if status == 'Success':
                ssm_command_output = self._ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                standard_output_content = Utils.get_value_as_string(
                    'StandardOutputContent', ssm_command_output, ''
                )
                ssm_output = Utils.from_json(standard_output_content)
                session_current_connection = ssm_output['DCV']['num-of-connections']
                if ssm_output['DCV']['last-disconnection-time'] == '':
                    # handle case where user launched DCV but never accessed it
                    last_dcv_disconnect = parse(ssm_output['DCV']['creation-time'])
                else:
                    last_dcv_disconnect = parse(
                        ssm_output['DCV']['last-disconnection-time']
                    )
                cpu_utilization = Utils.get_value_as_float(
                    'CPUAveragePerformanceLast10Secs', ssm_output, 0
                )
                cpu_utilization_threshold = self.context.config().get_float(
                    'virtual-desktop-controller.dcv_session.cpu_utilization_threshold',
                    required=True,
                )
                idle_autostop_delay = self.context.config().get_float(
                    'virtual-desktop-controller.dcv_session.idle_autostop_delay',
                    required=True,
                )
                if cpu_utilization < cpu_utilization_threshold:
                    if session_current_connection == 0:
                        current_time = parse(
                            datetime.now()
                            .replace(microsecond=0)
                            .replace(tzinfo=timezone.utc)
                            .isoformat()
                        )
                        if (
                            last_dcv_disconnect + timedelta(minutes=idle_autostop_delay)
                            < current_time
                        ):
                            # we can stop the session
                            self.log_info(
                                message_id=message_id,
                                message=f'Will stop session {idea_session_id} (owner: {idea_session_owner}) since CPU Utilization: {cpu_utilization} is less than threshold: {cpu_utilization_threshold} and last DCV connection was: {last_dcv_disconnect}',
                            )
                            success_list, fail_list = self.session_utils.stop_sessions(
                                [session]
                            )
                            # we know there is only 1 session in either of success or fail list
                            if Utils.is_not_empty(fail_list):
                                raise self.do_not_delete_message_exception(
                                    f'Error in stopping idea_session_id: {fail_list[0].idea_session_id}:{fail_list[0].name}. Error: {fail_list[0].failure_reason}. NOT stopping the session now. Will handle later'
                                )
                else:
                    # we can not stop the session
                    self.log_error(
                        message_id=message_id,
                        message=f'Will not stop session {idea_session_id} (owner: {idea_session_owner}) since CPU Utilization: {cpu_utilization} is greater than threshold: {cpu_utilization_threshold}',
                    )
            else:
                # FAILED
                self.log_error(
                    message_id=message_id,
                    message=f'CPU Utilization command execution failed for session {idea_session_id} (owner: {idea_session_owner}). Will try to stop session later.',
                )
        else:
            self.log_error(
                message_id=message_id,
                message=f'Ignoring message because state is {status} for idea_session_id: {idea_session_id} (owner: {idea_session_owner})',
            )
