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
from ideadatamodel import VirtualDesktopSessionState, VirtualDesktopSession
from ideasdk.utils import Utils, DateTimeUtils

from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)


class ValidateDCVSessionDeletionEventHandler(BaseVirtualDesktopControllerEventHandler):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'validate-dcv-session-deletion-handler')
        # Timeout after which we force the EC2 action even if DCV validation fails
        # This prevents instances from running indefinitely when DCV broker validation gets stuck
        # Reduced default from 10 minutes to 2 minutes for faster cleanup of stuck sessions
        self.dcv_validation_timeout_minutes = context.config().get_int(
            'dcv_session.validation_timeout_minutes', default=2
        )

    def _continue_stop_session(self, message_id: str, session: VirtualDesktopSession):
        if session.hibernation_enabled:
            self.log_debug(
                message_id=message_id,
                message=f'Continuing to hibernate session... {session.idea_session_id}:{session.name}',
            )
            self.server_utils.stop_or_hibernate_servers(
                servers_to_hibernate=[session.server]
            )
            # For hibernation, set to STOPPED immediately as hibernation is synchronous
            session.state = VirtualDesktopSessionState.STOPPED
        else:
            self.log_debug(
                message_id=message_id,
                message=f'Continuing to stop session... {session.idea_session_id}:{session.name}',
            )
            self.server_utils.stop_or_hibernate_servers(
                servers_to_stop=[session.server]
            )

            # Check if the EC2 instance is already stopped after the stop call
            # This handles the case where an instance in ERROR state is already stopped
            # and stop_instances() doesn't generate a state change event
            try:
                ec2_response = (
                    self.context.aws()
                    .ec2()
                    .describe_instances(InstanceIds=[session.server.instance_id])
                )
                instances = []
                for reservation in ec2_response.get('Reservations', []):
                    instances.extend(reservation.get('Instances', []))

                if instances:
                    instance_state = instances[0].get('State', {}).get('Name', '')
                    self.log_debug(
                        message_id=message_id,
                        message=f'EC2 instance {session.server.instance_id} state after stop call: {instance_state}',
                    )

                    if instance_state == 'stopped':
                        # Instance is already stopped, set session to STOPPED immediately
                        session.state = VirtualDesktopSessionState.STOPPED
                        # Also update server state to match
                        server = self.server_db.get(
                            instance_id=session.server.instance_id
                        )
                        if server:
                            server.state = 'STOPPED'
                            self.server_db.update(server)
                        self.log_info(
                            message_id=message_id,
                            message=f'Instance {session.server.instance_id} was already stopped, setting session {session.idea_session_id} to STOPPED state',
                        )
                    else:
                        # Instance is stopping or in some other state, use normal flow
                        session.state = VirtualDesktopSessionState.STOPPING
                else:
                    # Couldn't find instance, use normal stopping flow
                    session.state = VirtualDesktopSessionState.STOPPING

            except Exception as e:
                # If we can't check the instance state, fall back to normal stopping flow
                self.log_warning(
                    message_id=message_id,
                    message=f'Could not verify EC2 instance state for {session.server.instance_id}: {str(e)}. Using normal stopping flow.',
                )
                session.state = VirtualDesktopSessionState.STOPPING

        # Update the session state and timestamp
        session.updated_on = DateTimeUtils.current_datetime()
        self.session_db.update(session)

    def _continue_delete_session(self, message_id: str, session: VirtualDesktopSession):
        self.log_info(
            message_id=message_id,
            message=f'Continuing to delete session... {session.idea_session_id}:{session.name}',
        )

        try:
            self.schedule_utils.delete_schedules_for_session(session)
            self.session_permission_utils.delete_permissions_for_session(session)
            # delete session entry
            self.session_db.delete(session)
            self.log_info(
                message_id=message_id,
                message=f'Attempting to terminate EC2 instance for session {session.idea_session_id}, instance_id: {session.server.instance_id if session.server else "None"} (terminate is always immediate)',
            )

            if session.server:
                # Note: force parameter is ignored by terminate_instances - terminate is always immediate
                termination_result = self.server_utils.terminate_dcv_hosts(
                    [session.server], force=True
                )

                if 'ERROR' in termination_result:
                    self.log_error(
                        message_id=message_id,
                        message=f'EC2 termination failed for session {session.idea_session_id}: {termination_result["ERROR"]}',
                    )
                else:
                    self.log_info(
                        message_id=message_id,
                        message=f'EC2 termination initiated successfully for session {session.idea_session_id}',
                    )
            else:
                self.log_warning(
                    message_id=message_id,
                    message=f'No server found for session {session.idea_session_id} - skipping EC2 termination',
                )

        except Exception as e:
            self.log_error(
                message_id=message_id,
                message=f'Exception during session deletion for {session.idea_session_id}: {str(e)}',
            )

    def _has_exceeded_validation_timeout(self, session: VirtualDesktopSession) -> bool:
        """
        Check if the session has been in STOPPING or DELETING state longer than the configured timeout.
        This prevents EC2 instances from running indefinitely when DCV broker validation gets stuck.
        """
        if not session.updated_on:
            # If no updated_on timestamp, assume timeout exceeded to force cleanup
            self._logger.warning(
                f'Session {session.idea_session_id} has no updated_on timestamp, forcing timeout to prevent indefinite waiting'
            )
            return True

        current_time = DateTimeUtils.current_datetime()
        time_in_current_state = current_time - session.updated_on
        timeout_minutes = self.dcv_validation_timeout_minutes

        # Convert timeout to seconds for comparison
        timeout_seconds = timeout_minutes * 60
        time_in_state_seconds = time_in_current_state.total_seconds()

        self._logger.info(
            f'Session {session.idea_session_id} timeout check: '
            f'updated_on={session.updated_on}, current_time={current_time}, '
            f'time_in_state={time_in_state_seconds}s, timeout={timeout_seconds}s, '
            f'exceeded={time_in_state_seconds > timeout_seconds}'
        )

        return time_in_state_seconds > timeout_seconds

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        idea_session_id = Utils.get_value_as_string(
            'idea_session_id', event.detail, None
        )
        idea_session_owner = Utils.get_value_as_string(
            'idea_session_owner', event.detail, None
        )

        self.log_info(
            message_id=message_id,
            message=f'Received session stop validation message for idea_session_id {idea_session_id}, {idea_session_owner}',
        )
        if Utils.is_empty(idea_session_id) or Utils.is_empty(idea_session_owner):
            self.log_error(
                message_id=message_id,
                message=f'idea_session_id: {idea_session_id}, owner: {idea_session_owner}',
            )
            return

        session = self.session_db.get_from_db(
            idea_session_owner=idea_session_owner, idea_session_id=idea_session_id
        )
        if Utils.is_empty(session):
            self.log_error(message_id=message_id, message='Invalid IDEA SESSION ID.')
            return

        if session.state not in {
            VirtualDesktopSessionState.STOPPING,
            VirtualDesktopSessionState.DELETING,
        }:
            self.log_info(
                message_id=message_id,
                message=f'IDEA Session: {session.idea_session_id} in state {session.state}. NO=OP. Returning.',
            )
            return

        response = self.context.dcv_broker_client.describe_sessions([session])
        current_session_info = Utils.get_value_as_dict(
            session.dcv_session_id,
            Utils.get_value_as_dict('sessions', response, {}),
            {},
        )
        state = Utils.get_value_as_string('state', current_session_info, None)

        self.log_debug(
            message_id=message_id,
            message=f'DCV broker returned state: {state} for session {session.idea_session_id}:{session.name}. '
            f'Session updated_on: {session.updated_on}, session state: {session.state}',
        )

        if Utils.is_empty(state) or state == 'DELETED':
            # session is deleted. We can continue.
            self.log_info(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} is deleted with state: {state}. Validation complete.',
            )
            if session.state is VirtualDesktopSessionState.DELETING:
                self._continue_delete_session(message_id, session)
            elif session.state is VirtualDesktopSessionState.STOPPING:
                self._continue_stop_session(message_id, session)
            else:
                self.log_info(
                    message_id=message_id,
                    message=f'State not being handled: {session.state}. Will not handle.',
                )
        else:
            # Check if we've exceeded the timeout waiting for DCV validation
            timeout_exceeded = self._has_exceeded_validation_timeout(session)
            self.log_info(
                message_id=message_id,
                message=f'Timeout check result for session {session.idea_session_id}: {timeout_exceeded}',
            )

            if timeout_exceeded:
                self.log_warning(
                    message_id=message_id,
                    message=f'DCV validation timeout exceeded for session {session.idea_session_id}:{session.name}. '
                    f'Forcing EC2 action to prevent indefinite running costs. DCV state: {state}',
                )
                if session.state is VirtualDesktopSessionState.DELETING:
                    self._continue_delete_session(message_id, session)
                elif session.state is VirtualDesktopSessionState.STOPPING:
                    self._continue_stop_session(message_id, session)
            else:
                raise self.do_not_delete_message_exception(
                    f'Idea session {session.idea_session_id}:{session.name} is not deleted with state: {state}. Not doing anything will try again.'
                )
