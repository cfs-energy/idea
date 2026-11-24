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
from ideavirtualdesktopcontroller.app.session_permissions import (
    constants,
)
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)


class IDEASessionPermissionsUpdateEventHandler(
    BaseVirtualDesktopControllerEventHandler
):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'idea-session-permissions-update-event-handler')

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        idea_session_id = Utils.get_value_as_string(
            'idea_session_id', event.detail, None
        )
        new_name = Utils.get_value_as_string('new_name', event.detail, None)
        new_instance_type = Utils.get_value_as_string(
            'new_instance_type', event.detail, None
        )
        new_state = Utils.get_value_as_string('new_state', event.detail, None)

        session_permissions = self.session_permissions_db.get_for_session(
            idea_session_id=idea_session_id
        )
        if Utils.is_empty(session_permissions):
            self.log_debug(
                message_id=message_id,
                message=f'No permissions found for idea session id: {idea_session_id}',
            )
            return

        details = {}
        if Utils.is_not_empty(new_name):
            details[constants.SESSION_PERMISSIONS_DB_IDEA_SESSION_NAME_KEY] = new_name
        if Utils.is_not_empty(new_instance_type):
            details[constants.SESSION_PERMISSIONS_DB_IDEA_SESSION_INSTANCE_TYPE_KEY] = (
                new_instance_type
            )
        if Utils.is_not_empty(new_state):
            details[constants.SESSION_PERMISSIONS_DB_IDEA_SESSION_STATE_KEY] = (
                VirtualDesktopSessionState(new_state).value
            )
        if not details:
            return
        for session_permission in session_permissions:
            # only the desktop's display fields: a share revoked or changed since the read stays so
            self.session_permissions_db.update_session_details(
                session_permission.idea_session_id,
                session_permission.actor_name,
                details,
            )
