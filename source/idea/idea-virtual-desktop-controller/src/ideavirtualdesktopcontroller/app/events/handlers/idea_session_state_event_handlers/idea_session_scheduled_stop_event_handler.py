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
from ideadatamodel import VirtualDesktopSessionState
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)


class IDEASessionScheduledStopEventHandler(BaseVirtualDesktopControllerEventHandler):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'idea-session-scheduled-stop-handler')

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
        force = Utils.get_value_as_bool('force', event.detail, False)

        self.log_info(
            message_id=message_id,
            message=f'handle stop idea_session: {idea_session_id} for owner: {idea_session_owner} Force: {force}',
        )

        session = self.session_db.get_from_db(
            idea_session_id=idea_session_id, idea_session_owner=idea_session_owner
        )
        if Utils.is_empty(session):
            self.log_error(
                message_id=message_id,
                message=f'Trying to stop Invalid idea_session_id: {idea_session_id} and owner: {idea_session_owner}. No OP. Returning.',
            )
            return

        if session.state not in {VirtualDesktopSessionState.READY}:
            self.log_info(
                message_id=message_id,
                message=f'Session in state {session.state}. No OP. Returning.',
            )
            return

        if not force:
            # submit request to validate CPU Utilization
            self.ssm_commands_utils.submit_ssm_command_to_get_cpu_utilization(
                instance_id=session.server.instance_id,
                idea_session_id=session.idea_session_id,
                idea_session_owner=session.owner,
                base_os=session.base_os,
            )
            return

        session.force = force
        success_list, fail_list = self.session_utils.stop_sessions([session])
        # we know there is only 1 session in either of success or fail list
        if Utils.is_not_empty(fail_list):
            raise self.do_not_delete_message_exception(
                f'Error in stopping idea_session_id: {fail_list[0].idea_session_id}:{fail_list[0].name}. Error: {fail_list[0].failure_reason}. NOT stopping the session now. Will handle later'
            )
