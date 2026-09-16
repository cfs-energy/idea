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
from ideadatamodel import (
    ListSessionsRequest,
    SocaPaginator,
    VirtualDesktopWeekSchedule,
    DayOfWeek,
    VirtualDesktopSessionState,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)


class UserDisabledEventHandler(BaseVirtualDesktopControllerEventHandler):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'user-state-handler')

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_controller_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        username = Utils.get_value_as_string('username', event.detail, None)

        if Utils.is_empty(username):
            self.log_warning(
                message_id=message_id,
                message=f'Invalid username {username}. NO=OP. Returning.',
            )
            return

        cursor = None
        while True:
            response = self.session_db.list_all_for_user(
                request=ListSessionsRequest(paginator=SocaPaginator(cursor=cursor)),
                username=username,
            )
            for session in response.listing:
                # Delete schedule rows before stopping; queued resume events also check the owner.
                self.schedule_utils.delete_schedules_for_session(session)
                session.schedule = VirtualDesktopWeekSchedule(
                    **{
                        day.value: self.schedule_db.get_empty_schedule(day)
                        for day in DayOfWeek
                    }
                )
                self.session_db.update(session)
                if session.state in (
                    VirtualDesktopSessionState.STOPPED,
                    VirtualDesktopSessionState.STOPPING,
                ):
                    continue
                session.force = True
                _, failed = self.session_utils.stop_sessions([session])
                if failed:
                    raise self.do_not_delete_message_exception(
                        'Disabled user session could not be stopped'
                    )
            cursor = response.paginator.cursor if response.paginator else None
            if not cursor:
                break
