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

from ideavirtualdesktopcontroller.app.sessions.dcv_session_recovery import (
    DcvSessionRecovery,
)

import ideavirtualdesktopcontroller
from ideadatamodel import VirtualDesktopSessionState
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)
from ideavirtualdesktopcontroller.app.events.handlers.dcv_host_event_handlers.dcv_host_ready_event_handler import (
    DCVHostReadyEventHandler,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_counters_db import (
    VirtualDesktopSessionCounterType,
)


class DCVHostRebootCompleteEventHandler(BaseVirtualDesktopControllerEventHandler):
    RESUME_REQUEST_COUNT_THRESHOLD = 6
    # built on first use: only a host that never had a DCV session needs it
    _host_ready = None

    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'dcv-host-reboot-complete-handler')

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        sender_instance_id = self.get_dcv_instance_id_from_sender_id(sender_id)
        if Utils.is_empty(sender_instance_id):
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
            message=f'Received message from dcv-host for idea_session_id {idea_session_id}, {idea_session_owner}',
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

        # the host reports only its own desktop. the controller sends this event too, for a
        # desktop resuming from hibernation, so its role is accepted for any session.
        if (
            session.server is None or session.server.instance_id != sender_instance_id
        ) and not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Sender {sender_id} is not the host of session {idea_session_id}. Ignoring message'
            )

        if Utils.is_empty(session.dcv_session_id) and session.state in {
            VirtualDesktopSessionState.RESUMING,
            VirtualDesktopSessionState.ERROR,
            VirtualDesktopSessionState.CREATING,
        }:
            self._create_missing_session(message_id, sender_id, event, session)
            return

        # if session is RESUMING/READY/STOPPED/ERROR continue, else ignore
        if session.state not in {
            VirtualDesktopSessionState.RESUMING,
            VirtualDesktopSessionState.READY,
            VirtualDesktopSessionState.ERROR,
            VirtualDesktopSessionState.STOPPED,
        }:
            self.log_error(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} is in state: {session.state}. Not handling.',
            )
            return

        self.log_info(
            message_id=message_id,
            message=f'Idea session {session.idea_session_id}:{session.name} is in state {session.state}. Handling reboot complete event',
        )

        if (
            not session.hibernation_enabled or session.reboot_deadline
        ) and DcvSessionRecovery(self).check(session, resume=True):
            return

        # ALWAYS reset counters on reboot, regardless of session state
        # This ensures we give the session another chance after reboot
        validate_counter = self.session_counter_db.get(
            session.idea_session_id,
            VirtualDesktopSessionCounterType.VALIDATE_DCV_SESSION_CREATED_COUNTER,
        )
        validate_counter.counter = 0  # Reset to 0 regardless of current value
        self.session_counter_db.create_or_update(validate_counter)
        self.log_info(
            message_id=message_id,
            message=f'Reset validation counter for idea session {session.idea_session_id}:{session.name} after reboot',
        )

        create_counter = self.session_counter_db.get(
            session.idea_session_id,
            VirtualDesktopSessionCounterType.DCV_SESSION_CREATION_REQUEST_ACCEPTED_COUNTER,
        )
        create_counter.counter = 0  # Reset to 0 regardless of current value
        self.session_counter_db.create_or_update(create_counter)
        self.log_info(
            message_id=message_id,
            message=f'Reset creation counter for idea session {session.idea_session_id}:{session.name} after reboot',
        )

        # If session is in ERROR state, update it to RESUMING state to restart the process
        if session.state == VirtualDesktopSessionState.ERROR:
            session.state = VirtualDesktopSessionState.RESUMING
            session = self.session_db.update(session)
            self.log_info(
                message_id=message_id,
                message=f'Updated session {session.idea_session_id}:{session.name} state from ERROR to RESUMING',
            )

        # if session RESUMING -> this is user trying to resume session
        # if session READY -> the machine has been restarted that means the session was active but either AWS or the user did a reboot.
        # either case we need to treat as reboot and resume session, or it is a bogus message. Check with the broker for session state first.
        if session.state in {VirtualDesktopSessionState.READY}:
            self.log_info(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} is state: {session.state}. Needs additional validations.',
            )
            response = self.context.dcv_broker_client.describe_sessions([session])
            current_session_info = Utils.get_value_as_dict(
                session.dcv_session_id,
                Utils.get_value_as_dict('sessions', response, {}),
                {},
            )
            state = Utils.get_value_as_string('state', current_session_info, None)
            if state == 'CREATING':
                # session is stable. not need to worry.
                self.log_info(
                    message_id=message_id,
                    message=f'Idea session {session.idea_session_id}:{session.name} is stable with state: {state}. No need to handle event. Returning.',
                )
                return
            elif state == 'READY':
                counter_db_entry = self.session_counter_db.get(
                    session.idea_session_id,
                    VirtualDesktopSessionCounterType.DCV_SESSION_RESUMED_COUNTER,
                )
                if counter_db_entry.counter < self.RESUME_REQUEST_COUNT_THRESHOLD:
                    counter_db_entry.counter = counter_db_entry.counter + 1
                    self.session_counter_db.create_or_update(counter_db_entry)
                    raise self.do_not_delete_message_exception(
                        f'Idea session {session.idea_session_id}:{session.name} might stable with state: {state}. No need to handle event now. Resume request count: {counter_db_entry.counter}. Threshold: {self.RESUME_REQUEST_COUNT_THRESHOLD}'
                    )
                else:
                    # session is stable for sure.
                    self.session_counter_db.delete(counter_db_entry)
                    self.log_info(
                        message_id=message_id,
                        message=f'Idea session {session.idea_session_id}:{session.name} is stable with state: {state}. No need to handle event.',
                    )
                    return

            self.log_warning(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} is NOT stable with state: {state}. Handling.',
            )
        else:
            self.log_info(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} is state: {session.state}. Resuming Session.',
            )

        session = self.context.dcv_broker_client.resume_session(session)

        if Utils.is_not_empty(session.failure_reason):
            # Error in resuming now. DO NOT PROCESS THE MESSAGE YET.
            raise self.do_not_delete_message_exception(
                f'Issue in resuming session: {session.failure_reason}. Will try again later'
            )

        self.log_info(
            message_id=message_id,
            message=f'Submitted resume request for Idea session {session.idea_session_id}:{session.name}.',
        )

        if session.state is not VirtualDesktopSessionState.RESUMING:
            session.state = VirtualDesktopSessionState.RESUMING
            session = self.session_db.update(session)

        server = self.server_db.get(instance_id=session.server.instance_id)
        if server.state != 'CREATED':
            server.state = 'CREATED'
            self.server_db.update(server)

    def _create_missing_session(
        self,
        message_id: str,
        sender_id: str,
        event: VirtualDesktopEvent,
        session,
    ):
        """
        the host never had a DCV session, so there is none to resume: it gets the creation the
        host-ready event would have started. a redelivery finds the desktop in CREATING and
        retries the creation without resetting its attempt count.
        """
        if session.state != VirtualDesktopSessionState.CREATING:
            create_counter = self.session_counter_db.get(
                session.idea_session_id,
                VirtualDesktopSessionCounterType.DCV_SESSION_CREATION_REQUEST_ACCEPTED_COUNTER,
            )
            create_counter.counter = 0
            self.session_counter_db.create_or_update(create_counter)
            session.state = VirtualDesktopSessionState.CREATING
            session.reboot_deadline = 0
            self.session_db.update(session)
            self.log_info(
                message_id=message_id,
                message=f'Idea session {session.idea_session_id}:{session.name} has no DCV session; creating one',
            )
        if self._host_ready is None:
            self._host_ready = DCVHostReadyEventHandler(self.context)
        self._host_ready.handle_event(message_id, sender_id, event)
