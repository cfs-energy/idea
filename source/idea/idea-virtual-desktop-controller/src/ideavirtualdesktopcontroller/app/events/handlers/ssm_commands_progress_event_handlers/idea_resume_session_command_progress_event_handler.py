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

from botocore.exceptions import ClientError

import ideavirtualdesktopcontroller
from ideadatamodel import VirtualDesktopSessionState
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)


RESUME_FAILED_MESSAGE = 'This desktop restarted but its remote display service did not start. Stop and start the desktop to try again, or contact your administrator.'


class IDEAResumeSessionCommandProgressEventHandler(
    BaseVirtualDesktopControllerEventHandler
):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'resume-session-command-progress-event-handler')

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        status = Utils.get_value_as_string('status', event.detail, '')
        _ = Utils.get_value_as_string('instance_id', event.detail, None)
        idea_session_id = Utils.get_value_as_string(
            'idea_session_id', event.detail, None
        )
        idea_session_owner = Utils.get_value_as_string(
            'idea_session_owner', event.detail, None
        )
        command_id = Utils.get_value_as_string('command_id', event.detail, None)

        if status in {'Success', 'Failed', 'TimedOut', 'Cancelled'}:
            session = self.session_db.get_from_db(
                idea_session_owner=idea_session_owner, idea_session_id=idea_session_id
            )
            # a desktop deleted, stopped or otherwise moved on since the command was sent
            # keeps its state
            if session is None or session.state != VirtualDesktopSessionState.RESUMING:
                self.log_info(
                    message_id=message_id,
                    message=f'resume command {command_id} ended {status}; desktop {idea_session_id} is '
                    f'{session.state if session else "gone"}, leaving it',
                )
                self.ssm_commands_db.delete(command_id=command_id)
                return
            if status == 'Success':
                session.state = VirtualDesktopSessionState.INITIALIZING
            else:
                # FAILED
                session.state = VirtualDesktopSessionState.ERROR
                session.failure_reason = RESUME_FAILED_MESSAGE
            try:
                # only while the desktop is still resuming: a delete or stop since the read wins
                written = self.session_db.update(
                    session,
                    unless_changed=(sessions_constants.USER_SESSION_DB_STATE_KEY,),
                )
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') != (
                    'ConditionalCheckFailedException'
                ):
                    raise
                self.log_info(
                    message_id=message_id,
                    message=f'resume command {command_id} ended {status}; desktop '
                    f'{idea_session_id} changed meanwhile, leaving it',
                )
                self.ssm_commands_db.delete(command_id=command_id)
                return
            if status == 'Success':
                try:
                    self.events_utils.publish_validate_dcv_session_creation_event(
                        idea_session_id=session.idea_session_id,
                        idea_session_owner=session.owner,
                    )
                except Exception:
                    # nothing else moves an INITIALIZING desktop on: the state goes back to
                    # RESUMING so the redelivered result is handled again
                    written.state = VirtualDesktopSessionState.RESUMING
                    try:
                        self.session_db.update(
                            written,
                            unless_changed=(
                                sessions_constants.USER_SESSION_DB_STATE_KEY,
                            ),
                        )
                    except Exception as e:
                        self.log_error(
                            message_id=message_id,
                            message=f'desktop {idea_session_id} could not be put back to '
                            f'resuming after its validation was not queued: {e}',
                        )
                    raise
                self.log_info(
                    message_id=message_id,
                    message=f'Session: {session.idea_session_id}:{session.name} moved to Initializing state',
                )
                self.ssm_commands_db.delete(command_id=command_id)
            else:
                self.log_error(
                    message_id=message_id,
                    message=f'Session: {session.idea_session_id}:{session.name} moved to Error state',
                )
        elif status == 'InProgress':
            self.log_debug(
                message_id=message_id,
                message=f'Ignoring message because state is {status} for idea_session_id: {idea_session_id}',
            )
        else:
            self.log_error(
                message_id=message_id,
                message=f'Ignoring message because state is {status} for idea_session_id: {idea_session_id}',
            )
