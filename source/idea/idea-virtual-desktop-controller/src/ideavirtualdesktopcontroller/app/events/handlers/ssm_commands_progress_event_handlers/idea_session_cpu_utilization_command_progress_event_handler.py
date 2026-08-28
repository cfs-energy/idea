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

from typing import Dict, Optional, Tuple

import ideavirtualdesktopcontroller
from ideadatamodel import VirtualDesktopSession
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)
from datetime import datetime, timezone, timedelta
from dateutil.parser import parse


def _to_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if Utils.is_empty(value):
        return None
    try:
        return _to_utc(parse(value))
    except (ValueError, OverflowError, TypeError):
        return None


def evaluate_session_idle(
    ssm_output: Optional[Dict],
    cpu_utilization_threshold: float,
    idle_autostop_delay: float,
    current_time: datetime,
) -> Tuple[bool, str]:
    """
    Decide whether a session is idle, and say why.
    A session is idle only when every activity signal is quiet; a signal that is
    missing or unreadable counts as active, so a blind spot never stops a session.
    """
    if Utils.is_empty(ssm_output):
        return False, 'the activity sample is empty'

    dcv = Utils.get_value_as_dict('DCV', ssm_output, None)
    if Utils.is_empty(dcv):
        return False, 'the activity sample carries no DCV session details'

    cpu_utilization = Utils.get_value_as_float(
        'CPUAveragePerformanceLast10Secs', ssm_output, None
    )
    if cpu_utilization is None:
        return False, 'the activity sample carries no CPU utilization'
    if cpu_utilization >= cpu_utilization_threshold:
        return (
            False,
            f'CPU utilization: {cpu_utilization} is not below threshold: {cpu_utilization_threshold}',
        )

    connection_count = Utils.get_value_as_int('num-of-connections', dcv, None)
    if connection_count is None:
        return False, 'the activity sample carries no connection count'
    if connection_count > 0:
        return False, f'{connection_count} connection(s) are still open'

    # a user working over ssh with no DCV client attached is not idle; Windows
    # hosts report no login sessions and fall back to the DCV signals alone
    login_session_count = Utils.get_value_as_int('SSH_Connection_Count', ssm_output, 0)
    if login_session_count > 0:
        return (
            False,
            f'{login_session_count} interactive login session(s) are still open',
        )

    last_activity_value = Utils.get_value_as_string(
        'last-disconnection-time', dcv, None
    )
    if Utils.is_empty(last_activity_value):
        # a session nobody ever connected to counts as idle from its creation time
        last_activity_value = Utils.get_value_as_string('creation-time', dcv, None)
    last_activity = _parse_timestamp(last_activity_value)
    if last_activity is None:
        return False, f'unusable DCV timestamp: {last_activity_value}'

    last_login_out_value = Utils.get_value_as_string(
        'SSH_Last_Disconnect_ISO', ssm_output, None
    )
    if Utils.is_not_empty(last_login_out_value):
        last_login_out = _parse_timestamp(last_login_out_value)
        if last_login_out is None:
            return False, f'unreadable last login timestamp: {last_login_out_value}'
        last_activity = max(last_activity, last_login_out)

    if last_activity + timedelta(minutes=idle_autostop_delay) >= current_time:
        return (
            False,
            f'last activity was: {last_activity}, within the idle autostop delay of {idle_autostop_delay} minutes',
        )

    return (
        True,
        f'CPU utilization: {cpu_utilization} is below threshold: {cpu_utilization_threshold}, '
        f'no connections or login sessions are open, and last activity was: {last_activity}, '
        f'beyond the idle autostop delay of {idle_autostop_delay} minutes',
    )


class IDEASessionCPUUtilizationCommandProgressEventHandler(
    BaseVirtualDesktopControllerEventHandler
):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'cpu-utilization-command-progress-event-handler')
        self._ssm_client = self.context.aws().ssm()

    def _get_idle_autostop_delay(self, session: VirtualDesktopSession) -> float:
        cluster_default = self.context.config().get_int(
            'virtual-desktop-controller.dcv_session.idle_autostop_delay',
            required=True,
        )
        max_user_delay = self.context.config().get_int(
            'virtual-desktop-controller.dcv_session.idle_autostop_delay_max',
            default=0,
        )
        if Utils.is_empty(session):
            return float(cluster_default)
        return float(
            session.get_effective_idle_autostop_delay(cluster_default, max_user_delay)
        )

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
                cpu_utilization_threshold = self.context.config().get_float(
                    'virtual-desktop-controller.dcv_session.cpu_utilization_threshold',
                    required=True,
                )
                idle_autostop_delay = self._get_idle_autostop_delay(session)
                # datetime.now(timezone.utc), not naive-local relabeled as UTC:
                # the comparison target is a UTC disconnect timestamp.
                current_time = datetime.now(timezone.utc).replace(microsecond=0)
                is_idle, reason = evaluate_session_idle(
                    ssm_output=ssm_output,
                    cpu_utilization_threshold=cpu_utilization_threshold,
                    idle_autostop_delay=idle_autostop_delay,
                    current_time=current_time,
                )
                if not is_idle:
                    self.log_info(
                        message_id=message_id,
                        message=f'Will not stop session {idea_session_id} (owner: {idea_session_owner}) since {reason}',
                    )
                    return

                self.log_info(
                    message_id=message_id,
                    message=f'Will stop session {idea_session_id} (owner: {idea_session_owner}) since {reason}',
                )
                success_list, fail_list = self.session_utils.stop_sessions([session])
                # we know there is only 1 session in either of success or fail list
                if Utils.is_not_empty(fail_list):
                    raise self.do_not_delete_message_exception(
                        f'Error in stopping idea_session_id: {fail_list[0].idea_session_id}:{fail_list[0].name}. Error: {fail_list[0].failure_reason}. NOT stopping the session now. Will handle later'
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
