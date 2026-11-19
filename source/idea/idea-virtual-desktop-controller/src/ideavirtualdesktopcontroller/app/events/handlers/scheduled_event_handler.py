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
from datetime import timedelta
from typing import Optional

import ideavirtualdesktopcontroller
from ideadatamodel import DayOfWeek, VirtualDesktopSessionPermissionActorType

from ideasdk.utils import DateTimeUtils, Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEvent,
)
from ideavirtualdesktopcontroller.app.events.handlers.dcv_broker_userdata_execution_complete_event_handler import (
    DCVBrokerUserdataExecutionCompleteEventHandler,
)
from ideavirtualdesktopcontroller.app.events.handlers.base_event_handler import (
    BaseVirtualDesktopControllerEventHandler,
)
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants


class ScheduledEventHandler(BaseVirtualDesktopControllerEventHandler):
    # the broker table settling below: built on first use, done once tables were seen
    _broker_tables = None
    _broker_tables_settled = False

    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context, 'scheduled-event-handler')
        self.DAY_OF_WEEKS = [
            DayOfWeek.MONDAY,
            DayOfWeek.TUESDAY,
            DayOfWeek.WEDNESDAY,
            DayOfWeek.THURSDAY,
            DayOfWeek.FRIDAY,
            DayOfWeek.SATURDAY,
            DayOfWeek.SUNDAY,
        ]

    def handle_event(self, message_id: str, sender_id: str, event: VirtualDesktopEvent):
        if not self.is_sender_scheduled_event_lambda_role(sender_id):
            raise self.message_source_validation_failed(
                f'Corrupted sender_id: {sender_id}. Ignoring message'
            )

        event_time = Utils.get_value_as_string('time', event.detail, '')
        if Utils.is_empty(event_time):
            self.log_error(
                message_id=message_id,
                message=f'event time is {event_time}. Invalid message. Returning.',
            )
            return

        # The time in the event is UTC because AWS always sends events in UTC Time zone,
        # hence we need to convert it to cluster time zone.
        event_time = DateTimeUtils.to_datetime_in_timezone(
            event_time, self.context.cluster_timezone()
        )
        self.log_info(
            message_id=message_id,
            message=f'Handling scheduled event at time {event_time} in {self.context.cluster_timezone()}',
        )
        day_of_week = self.DAY_OF_WEEKS[event_time.weekday()]
        schedule_db_entries = self.schedule_db.get_schedules_for_day_of_week(
            day_of_week
        )
        for schedule_db_entry in schedule_db_entries:
            # Confirm absence from the primary table before removing a stale schedule.
            try:
                session = (
                    self.context.aws()
                    .dynamodb_table()
                    .Table(self.session_db.table_name)
                    .get_item(
                        Key={
                            sessions_constants.USER_SESSION_DB_HASH_KEY: schedule_db_entry.idea_session_owner,
                            sessions_constants.USER_SESSION_DB_RANGE_KEY: schedule_db_entry.idea_session_id,
                        },
                        ConsistentRead=True,
                        ProjectionExpression=sessions_constants.USER_SESSION_DB_RANGE_KEY,
                    )
                )
            except Exception as e:
                self.log_exception(message_id=message_id, exception=e)
                continue
            if not session.get('Item'):
                # Schedules are written before the session; let creation finish first.
                if schedule_db_entry.updated_on is not None and (
                    DateTimeUtils.current_datetime() - schedule_db_entry.updated_on
                    < timedelta(minutes=10)
                ):
                    continue
                try:
                    self.schedule_db.delete(schedule_db_entry)
                except Exception as e:
                    self.log_exception(message_id=message_id, exception=e)
                    continue
                self.log_debug(
                    message_id=message_id,
                    message=f'Removed stale schedule {schedule_db_entry.schedule_id} for deleted session {schedule_db_entry.idea_session_id}',
                )
                continue
            self.log_info(
                message_id=message_id,
                message=f'Triggering schedule {schedule_db_entry.schedule_id}',
            )
            # one unreadable row must not stop the other schedules or the sweeps below
            try:
                self.schedule_utils.trigger_schedule(
                    event_time.time(), schedule_db_entry
                )
            except Exception as e:
                self.log_error(
                    message_id=message_id,
                    message=f'schedule {schedule_db_entry.schedule_id} of {schedule_db_entry.idea_session_id} could not be run: {e}',
                )

        # a share table read that fails ends this sweep for the tick, not the sweeps below
        try:
            cursor: Optional[str] = None
            loop_break = False
            sessions_info = set()
            group_desktops = set()
            while not loop_break:
                permissions, cursor = self.session_permissions_db.list_all_from_db(
                    cursor
                )
                for permission in permissions:
                    if (
                        permission.expiry_date is None
                        or event_time < permission.expiry_date
                    ):
                        # a group's members are written to the host when enforced: each tick
                        # rewrites them, which bounds how long a member change takes to apply
                        if (
                            permission.actor_type
                            == VirtualDesktopSessionPermissionActorType.GROUP
                        ):
                            group_desktops.add(
                                (
                                    permission.idea_session_id,
                                    permission.idea_session_owner,
                                )
                            )
                        continue
                    try:
                        self.log_info(
                            message_id=message_id,
                            message=f'Session Permission for session {permission.idea_session_id} has expired. Expiry Date {permission.expiry_date}. Deleting Permission',
                        )
                        desktop = (
                            permission.idea_session_id,
                            permission.idea_session_owner,
                        )
                        # queued first: the renderer already leaves expired shares out, so a
                        # failure after this still removes the access on the host
                        if desktop not in sessions_info:
                            self.events_utils.publish_enforce_session_permissions_event(
                                idea_session_id=desktop[0],
                                idea_session_owner=desktop[1],
                            )
                            sessions_info.add(desktop)
                        self.session_permissions_db.delete(permission)
                    except Exception as e:
                        self.log_error(
                            message_id=message_id,
                            message=f'expired share on {permission.idea_session_id} could not be removed: {e}',
                        )

                loop_break = Utils.is_empty(cursor)

            for desktop in group_desktops - sessions_info:
                self.events_utils.publish_enforce_session_permissions_event(
                    idea_session_id=desktop[0], idea_session_owner=desktop[1]
                )
        except Exception as e:
            self.log_exception(message_id=message_id, exception=e)

        # desktops whose host never became usable. nothing else moves them out of
        # PROVISIONING, and the instance is billed for as long as they sit there.
        try:
            failed = self.session_utils.fail_stuck_provisioning_sessions()
            if failed > 0:
                self.log_warning(
                    message_id=message_id,
                    message=f'Failed {failed} desktop(s) whose host never finished provisioning',
                )
        except Exception as e:
            self.log_exception(message_id=message_id, exception=e)

        # desktops that launched before their project's bedrock instance profile existed.
        # last, and never fatal: schedules and permission expiry must not be retried for it
        try:
            repaired = self.session_utils.repair_project_instance_profiles()
            if repaired > 0:
                self.log_info(
                    message_id=message_id,
                    message=f'Moved {repaired} desktop(s) onto their project profile',
                )
        except Exception as e:
            self.log_exception(message_id=message_id, exception=e)

        # broker tables created after the controller started get their billing, scaling and
        # tags here; once some have been seen, the boot event and the next start cover the rest
        if not ScheduledEventHandler._broker_tables_settled:
            try:
                if ScheduledEventHandler._broker_tables is None:
                    ScheduledEventHandler._broker_tables = (
                        DCVBrokerUserdataExecutionCompleteEventHandler(self.context)
                    )
                if ScheduledEventHandler._broker_tables.settle_tables(message_id) > 0:
                    ScheduledEventHandler._broker_tables_settled = True
            except Exception as e:
                # retried on the next tick; one line, not a stack trace each time
                self.log_warning(
                    message_id=message_id,
                    message=f'broker tables could not be checked: {e}',
                )

        # stopped desktops past the cleanup cutoff, and records whose instance is gone. opt-in,
        # and on its own short budget so this handler stays inside the queue visibility timeout.
        try:
            counters = self.session_utils.clean_up_stopped_sessions()
            deleted = counters.get('deleted', 0) + counters.get('orphans_cleaned', 0)
            if deleted > 0:
                self.log_info(
                    message_id=message_id,
                    message=f'The stopped desktop cleanup deleted {deleted} desktop(s)',
                )
        except Exception as e:
            self.log_exception(message_id=message_id, exception=e)
