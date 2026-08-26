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
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import ideavirtualdesktopcontroller
from ideadatamodel import (
    ListSessionsRequest,
    Notification,
    SocaPaginator,
    VirtualDesktopSession,
    VirtualDesktopServer,
    VirtualDesktopSessionState,
)
from ideadatamodel import constants, exceptions
from ideasdk.analytics.analytics_service import (
    AnalyticsEntry,
    EntryAction,
    EntryContent,
)
from ideasdk.utils import Utils, DateTimeUtils
from ideavirtualdesktopcontroller.app.events.events_utils import EventsUtils
from ideavirtualdesktopcontroller.app.permission_profiles.virtual_desktop_permission_profile_db import (
    VirtualDesktopPermissionProfileDB,
)
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_utils import (
    VirtualDesktopScheduleUtils,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_utils import (
    VirtualDesktopServerUtils,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
    VirtualDesktopSessionPermissionUtils,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
    build_bootstrap_failure_message,
    resolve_project_instance_profile_arn,
)

# a desktop in one of these states has no host worth moving onto another instance profile:
# it is on its way out, or it failed and would be retried by every pass for nothing.
INSTANCE_PROFILE_REPAIR_SKIPPED_STATES = {
    VirtualDesktopSessionState.DELETING,
    VirtualDesktopSessionState.DELETED,
    VirtualDesktopSessionState.ERROR,
}

# the queue behind this scheduled event makes a message visible again after 30s; going over
# that would replay it, so the pass gives up remaining work and resumes on the next message.
INSTANCE_PROFILE_REPAIR_TIME_BUDGET_MS = 10 * 1000

# a gpu driver build takes about ten minutes and ec2 mac launches up to twenty, so a shorter
# timeout would release healthy desktops; raise it here rather than in code; 0 disables the sweep.
PROVISIONING_TIMEOUT_CONFIG_KEY = (
    'virtual-desktop-controller.dcv_session.provisioning_timeout_seconds'
)
PROVISIONING_TIMEOUT_SECONDS_DEFAULT = 1800

PROVISIONING_TIMEOUT_TIME_BUDGET_MS = 10 * 1000

# a stopped desktop is billed for its volume for as long as it sits there, and a record whose
# instance is gone holds one of the owner's session slots for nothing. off unless enabled.
STOPPED_SESSION_CLEANUP_CONFIG_PREFIX = (
    'virtual-desktop-controller.dcv_session.stopped_session_cleanup'
)
STOPPED_SESSION_CLEANUP_KEEP_TAGS_DEFAULT = ['idea:keep', 'ideal:keep']
STOPPED_SESSION_CLEANUP_STATES = {
    VirtualDesktopSessionState.STOPPED,
    VirtualDesktopSessionState.ERROR,
}
STOPPED_SESSION_CLEANUP_COUNTERS = (
    'candidates',
    'kept',
    'no_stop_time',
    'not_due',
    'warned',
    'raced',
    'deleted',
    'orphans_cleaned',
    'failed',
)
# the two sweeps above may already have used most of the 30s the queue allows this message.
STOPPED_SESSION_CLEANUP_TIME_BUDGET_MS = 5 * 1000

CLEANUP_WARNING_NOTIFICATION_PREFIX = (
    'virtual-desktop-controller.dcv_session.notifications.cleanup_warning'
)

# the tag an admin exemption writes onto the instance, so it shows in ec2 and is honored by
# tooling outside idea. the session flag is what the cleanup pass itself reads.
CLEANUP_EXEMPT_TAG = 'idea:keep'

# ec2 formats StateTransitionReason like 'User initiated (2026-07-30 14:02:11 GMT)'
STATE_TRANSITION_TIME_PATTERN = re.compile(
    r'\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} GMT)\)'
)
STATE_TRANSITION_TIME_FORMAT = '%Y-%m-%d %H:%M:%S GMT'

# no bootstrap status on the host: all that is established is that it never checked in.
PROVISIONING_TIMEOUT_FAILURE_MESSAGE = 'The host for this desktop did not finish starting up within {minutes} minutes and never reported back to the cluster, so it has been released. Try again, and ask an administrator to check the host bootstrap log if it keeps happening.'


def parse_stop_time(state_transition_reason: Optional[str]) -> Optional[datetime]:
    """when ec2 stopped the instance, or None when the reason does not carry a time"""
    match = STATE_TRANSITION_TIME_PATTERN.search(state_transition_reason or '')
    if match is None:
        return None
    return datetime.strptime(match.group(1), STATE_TRANSITION_TIME_FORMAT).replace(
        tzinfo=timezone.utc
    )


def instance_state(instance: Optional[Dict]) -> str:
    return Utils.get_value_as_string(
        'Name', Utils.get_value_as_dict('State', instance, {}), ''
    )


class VirtualDesktopSessionUtils:
    # where the last sweep stopped, so a table larger than one pass's time budget is still
    # walked to the end across passes. None starts again at the first page.
    _instance_profile_repair_cursor: Optional[str] = None
    _provisioning_timeout_cursor: Optional[str] = None
    _stopped_session_cleanup_cursor: Optional[str] = None

    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        db: VirtualDesktopSessionDB,
        session_permission_db: VirtualDesktopSessionPermissionDB,
        permission_profile_db: VirtualDesktopPermissionProfileDB,
    ):
        self.context = context
        self._controller_utils = VirtualDesktopControllerUtils(self.context)
        self.events_utils = EventsUtils(context=self.context)
        self._session_db = db
        self._session_permission_db = session_permission_db
        self._permission_profile_db = permission_profile_db
        self._schedule_utils = VirtualDesktopScheduleUtils(
            context=self.context, db=self._session_db.schedule_db
        )
        self._session_permission_utils = VirtualDesktopSessionPermissionUtils(
            context=self.context,
            db=self._session_permission_db,
            permission_profile_db=self._permission_profile_db,
        )
        self._server_utils = VirtualDesktopServerUtils(
            context=self.context, db=self._session_db.server_db
        )
        self._logger = context.logger('virtual-desktop-session-utils')

    def create_session(self, session: VirtualDesktopSession) -> VirtualDesktopSession:
        # request has been validated and everything.
        try:
            session.server = self._server_utils.provision_host_for_session(session)
        except exceptions.SocaException as e:
            # the plain reason built while provisioning. str() would prefix the error code.
            session.failure_reason = e.message
            return session
        except Exception as e:
            session.failure_reason = f'{e}'
            return session

        if not session.server:
            session.failure_reason = (
                'Unable to create DCV host. Contact the Administrator.'
            )
            return session

        session = self._schedule_utils.update_schedule_for_session(
            self._schedule_utils.get_default_schedules(), session
        )
        session.state = VirtualDesktopSessionState.PROVISIONING
        return self._session_db.create(session)

    def repair_project_instance_profile(self, session: VirtualDesktopSession) -> bool:
        """
        move a desktop that launched before its project's bedrock instance profile existed
        onto that profile. the launch falls back to the shared dcv host profile, and
        without this the desktop is denied every bedrock call for the life of the instance.

        anything that cannot be established leaves the desktop as it is: the recorded
        profile is only corrected from what ec2 reports, never guessed.
        """
        if session.state in INSTANCE_PROFILE_REPAIR_SKIPPED_STATES:
            return False
        if Utils.is_empty(session.server) or Utils.is_empty(session.server.instance_id):
            return False

        project_instance_profile_arn = resolve_project_instance_profile_arn(
            self.context, self._logger, session
        )
        if project_instance_profile_arn is None:
            return False
        if session.server.instance_profile_arn == project_instance_profile_arn:
            return False

        instance_id = session.server.instance_id
        association = self._controller_utils.get_instance_profile_association(
            instance_id
        )
        if association is None:
            return False

        applied_instance_profile_arn = Utils.get_value_as_string(
            'Arn', Utils.get_value_as_dict('IamInstanceProfile', association, {})
        )
        if applied_instance_profile_arn == project_instance_profile_arn:
            # the desktop is already on the project profile, only the record was behind.
            self._record_applied_instance_profile(session, project_instance_profile_arn)
            return False

        if not self._controller_utils.apply_instance_profile(
            instance_id=instance_id,
            instance_profile_arn=project_instance_profile_arn,
            association_id=Utils.get_value_as_string('AssociationId', association),
        ):
            return False

        self._logger.info(
            f'moved {session.idea_session_id} host {instance_id} from instance profile '
            f'{applied_instance_profile_arn} to its project profile '
            f'{project_instance_profile_arn}'
        )
        self._record_applied_instance_profile(session, project_instance_profile_arn)
        return True

    def repair_project_instance_profiles(
        self, time_budget_ms: int = INSTANCE_PROFILE_REPAIR_TIME_BUDGET_MS
    ) -> int:
        """
        the same repair across every desktop, for the project profiles that were
        provisioned after their desktops launched. one desktop that cannot be repaired
        does not stop the rest, and a pass that runs out of time resumes at the page it
        stopped on: a desktop already on its project profile costs no aws call.
        """
        if not self._is_bedrock_enabled():
            return 0

        repaired = 0
        deadline = Utils.current_time_ms() + time_budget_ms
        cursor: Optional[str] = self._instance_profile_repair_cursor
        loop_break = False
        while not loop_break:
            result = self._session_db.list_all_from_db(
                ListSessionsRequest(paginator=SocaPaginator(cursor=cursor))
            )
            for session in Utils.get_as_list(result.listing, []):
                if Utils.current_time_ms() >= deadline:
                    self._logger.info(
                        'instance profile repair is out of time for this pass, the '
                        'remaining desktops are checked on the next one'
                    )
                    self._instance_profile_repair_cursor = cursor
                    return repaired
                try:
                    if self.repair_project_instance_profile(session):
                        repaired += 1
                except Exception as e:
                    self._logger.warning(
                        f'could not check the instance profile of session '
                        f'{session.idea_session_id}: {e}'
                    )
            cursor = result.cursor
            loop_break = Utils.is_empty(cursor)

        self._instance_profile_repair_cursor = None
        return repaired

    def _is_bedrock_enabled(self) -> bool:
        # no project carries an instance profile while the feature is off, so the whole
        # pass is skipped rather than reading every project to find that out.
        config = self.context.config()
        module_id = config.get_module_id(constants.MODULE_CLUSTER_MANAGER)
        return config.get_bool(f'{module_id}.bedrock.enabled', False)

    def _record_applied_instance_profile(
        self, session: VirtualDesktopSession, instance_profile_arn: str
    ):
        session.server.instance_profile_arn = instance_profile_arn
        self._session_db.update(session)

    def provisioning_timeout_seconds(self) -> int:
        return self.context.config().get_int(
            PROVISIONING_TIMEOUT_CONFIG_KEY,
            default=PROVISIONING_TIMEOUT_SECONDS_DEFAULT,
        )

    def fail_stuck_provisioning_session(
        self, session: VirtualDesktopSession, timeout_seconds: int
    ) -> bool:
        """
        fail a desktop whose host never became usable, and release the instance it is
        still being billed for. nothing else moves a desktop out of PROVISIONING: the host
        reports itself ready, and a host that aborted its own bootstrap never does.

        releasing someone's desktop is destructive, so anything that cannot be established
        leaves it running: a session whose record or creation time cannot be read, a
        session still inside the timeout, and an ec2 answer that could not be read.
        """
        if Utils.is_empty(session):
            return False
        if session.state != VirtualDesktopSessionState.PROVISIONING:
            return False
        if Utils.is_empty(session.server) or Utils.is_empty(session.server.instance_id):
            return False
        if session.created_on is None:
            self._logger.warning(
                f'session {session.idea_session_id} has no creation time, how long it '
                f'has been provisioning cannot be established. leaving it alone'
            )
            return False

        # PROVISIONING is only ever entered at creation, so created_on is when the launch
        # started and is not moved by anything else writing the record.
        provisioning_seconds = (
            Utils.current_time_ms() - Utils.to_milliseconds(session.created_on)
        ) / 1000
        if provisioning_seconds <= timeout_seconds:
            return False

        bootstrap_status = self._controller_utils.get_bootstrap_status(
            session.server.instance_id
        )
        if bootstrap_status is None:
            self._logger.warning(
                f'session {session.idea_session_id} has been provisioning for '
                f'{int(provisioning_seconds)}s, but ec2 could not be read for host '
                f'{session.server.instance_id}. leaving it alone'
            )
            return False

        if Utils.is_not_empty(bootstrap_status):
            session.failure_reason = build_bootstrap_failure_message(bootstrap_status)
        else:
            session.failure_reason = PROVISIONING_TIMEOUT_FAILURE_MESSAGE.format(
                minutes=int(timeout_seconds / 60)
            )
        session.state = VirtualDesktopSessionState.ERROR

        self._logger.error(
            f'session {session.idea_session_id} for {session.owner} has been '
            f'provisioning for {int(provisioning_seconds)}s on host '
            f'{session.server.instance_id}, bootstrap status: '
            f'{bootstrap_status if Utils.is_not_empty(bootstrap_status) else "none"}. '
            f'{session.failure_reason}'
        )
        # recorded before the host is released, so a release that fails still leaves the desktop
        # out of PROVISIONING and logged; deleting the failed desktop releases the host again.
        self._session_db.update(session)

        response = self._server_utils.terminate_dcv_hosts([session.server], force=True)
        if 'ERROR' in response:
            self._logger.error(
                f'could not release host {session.server.instance_id} of session '
                f'{session.idea_session_id}: {Utils.get_value_as_string("ERROR", response)}'
            )
        return True

    def fail_stuck_provisioning_sessions(
        self, time_budget_ms: int = PROVISIONING_TIMEOUT_TIME_BUDGET_MS
    ) -> int:
        """
        the same check across every desktop. one desktop that cannot be checked does not
        stop the rest, and a pass that runs out of time resumes at the page it stopped
        on: a desktop inside its timeout costs no aws call.
        """
        timeout_seconds = self.provisioning_timeout_seconds()
        if timeout_seconds <= 0:
            return 0

        failed = 0
        deadline = Utils.current_time_ms() + time_budget_ms
        cursor: Optional[str] = self._provisioning_timeout_cursor
        loop_break = False
        while not loop_break:
            result = self._session_db.list_all_from_db(
                ListSessionsRequest(paginator=SocaPaginator(cursor=cursor))
            )
            for session in Utils.get_as_list(result.listing, []):
                if Utils.current_time_ms() >= deadline:
                    self._logger.info(
                        'the provisioning timeout sweep is out of time for this pass, '
                        'the remaining desktops are checked on the next one'
                    )
                    self._provisioning_timeout_cursor = cursor
                    return failed
                try:
                    if self.fail_stuck_provisioning_session(session, timeout_seconds):
                        failed += 1
                except Exception as e:
                    self._logger.warning(
                        f'could not check how long session '
                        f'{session.idea_session_id if Utils.is_not_empty(session) else None} '
                        f'has been provisioning: {e}'
                    )
            cursor = result.cursor
            loop_break = Utils.is_empty(cursor)

        self._provisioning_timeout_cursor = None
        return failed

    def set_cleanup_exemption(
        self,
        session: VirtualDesktopSession,
        exempt: bool,
        reason: Optional[str],
        actor: str,
    ) -> VirtualDesktopSession:
        """
        an admin override the stopped desktop cleanup honors ahead of anything it reads from
        ec2. the flag on the session record is authoritative; the tag mirrors it onto the
        instance and a tag that cannot be written is logged rather than failing the request.
        """
        reason = Utils.get_as_string(reason, default='').strip() or actor
        session.cleanup_exempt = True if exempt else None
        session.cleanup_exempt_reason = reason if exempt else None
        session = self._session_db.update(session)

        instance_id = session.server.instance_id if session.server else None
        if Utils.is_empty(instance_id):
            return session
        try:
            if exempt:
                self._controller_utils.create_tag(
                    instance_id, CLEANUP_EXEMPT_TAG, reason[:256]
                )
            else:
                self._controller_utils.delete_tag(instance_id, CLEANUP_EXEMPT_TAG)
        except Exception as e:
            self._logger.warning(
                f'could not update the {CLEANUP_EXEMPT_TAG} tag on {instance_id} for '
                f'session {session.idea_session_id}: {e}'
            )
        return session

    def _tear_down_session(
        self, session: VirtualDesktopSession, outcome: str, why: str, dry_run: bool
    ) -> str:
        who = f'session {session.idea_session_id} ({session.name}) of {session.owner}'
        if dry_run:
            self._logger.info(f'[dry run] would delete {who}: {why}')
            return outcome
        # the path VirtualDesktopAdmin.DeleteSessions takes: the host, the record, its schedules
        # and permissions go together, and the record deletion sends the owner the deleted notification
        _, failed = self.terminate_sessions([session])
        if failed:
            self._logger.error(f'could not delete {who}: {failed[0].failure_reason}')
            return 'failed'
        self._logger.info(f'deleted {who}: {why}')
        return outcome

    def _warn_owner(
        self,
        session: VirtualDesktopSession,
        stopped_at: datetime,
        stopped_for: timedelta,
        deletion_at: datetime,
        dry_run: bool,
    ) -> str:
        who = f'session {session.idea_session_id} ({session.name}) of {session.owner}'
        deletion_date = deletion_at.strftime('%Y-%m-%d %H:%M UTC')
        what = f'stopped for {stopped_for.days} days, deletion on {deletion_date}'
        if dry_run:
            self._logger.info(f'[dry run] would warn {who}: {what}')
            return 'warned'

        prefix = CLEANUP_WARNING_NOTIFICATION_PREFIX
        if self.context.config().get_bool(f'{prefix}.enabled', default=True):
            self.context.notification_async_client.send_notification(
                Notification(
                    username=session.owner,
                    template_name=self.context.config().get_string(
                        f'{prefix}.email_template', required=True
                    ),
                    params={
                        'cluster_name': self.context.cluster_name(),
                        'session': session,
                        'session_name': session.name,
                        'stopped_days': stopped_for.days,
                        'deletion_date': deletion_date,
                    },
                )
            )
        else:
            self._logger.info(
                f'{who}: the deletion notice is not enabled, the warning window still applies'
            )
        # recorded after the send, so a send that raises is tried again on the next pass
        session.cleanup_warning_sent_on = datetime.now(timezone.utc)
        session.cleanup_warning_stop_time = stopped_at
        self._session_db.update(session)
        self._logger.info(f'warned {who}: {what}')
        return 'warned'

    def clean_up_stopped_session(
        self,
        session: VirtualDesktopSession,
        stopped_after_days: int,
        warn_days_before: int,
        keep_tags: set,
        dry_run: bool,
    ) -> Optional[str]:
        """
        the counter this desktop lands in, or None when the cleanup has no interest in it.
        deleting someone's desktop is destructive, so anything that cannot be established
        leaves it alone: an ec2 answer that could not be read, a stop time that does not
        parse, and a desktop that is no longer stopped when it is read again before acting.
        with a warning window, the owner is told first and given the whole window.
        """
        if (
            Utils.is_empty(session)
            or session.state not in STOPPED_SESSION_CLEANUP_STATES
        ):
            return None
        if Utils.is_empty(session.server) or Utils.is_empty(session.server.instance_id):
            return None
        if session.cleanup_exempt:
            return 'kept'

        instance_id = session.server.instance_id
        instance = self._controller_utils.describe_instance(instance_id)
        if instance is None:
            return 'failed'

        state = instance_state(instance)
        if Utils.is_empty(instance) or state == 'terminated':
            # a record whose instance is gone holds a session slot for nothing: always cleaned
            return self._tear_down_session(
                session,
                'orphans_cleaned',
                f'host {instance_id} is {state or "gone"}',
                dry_run,
            )

        # ERROR is only ever an orphan candidate; a stopped host under a failed record is a
        # desktop someone may still want to look at
        if session.state != VirtualDesktopSessionState.STOPPED or state != 'stopped':
            return None

        tag_keys = {
            Utils.get_value_as_string('Key', tag, '')
            for tag in Utils.get_value_as_list('Tags', instance, [])
        }
        if keep_tags & tag_keys:
            return 'kept'

        stopped_at = parse_stop_time(
            Utils.get_value_as_string('StateTransitionReason', instance, '')
        )
        if stopped_at is None:
            self._logger.warning(
                f'session {session.idea_session_id} of {session.owner}: no stop time in '
                f'the state transition reason of host {instance_id}, leaving it alone'
            )
            return 'no_stop_time'
        now = datetime.now(timezone.utc)
        stopped_for = now - stopped_at
        cutoff = timedelta(days=stopped_after_days)
        if warn_days_before > 0:
            warning = timedelta(days=warn_days_before)
            # a warning is for one stop episode: a stop time other than the one it was sent
            # for means the desktop ran in between, and the clock starts again
            warned_at = (
                session.cleanup_warning_sent_on
                if Utils.to_milliseconds(session.cleanup_warning_stop_time)
                == Utils.to_milliseconds(stopped_at)
                else None
            )
            if warned_at is None:
                if stopped_for < cutoff - warning:
                    return 'not_due'
                deletion_at = max(stopped_at + cutoff, now + warning)
                return self._warn_owner(
                    session, stopped_at, stopped_for, deletion_at, dry_run
                )
            if stopped_for < cutoff or now < warned_at + warning:
                return 'not_due'
        elif stopped_for < cutoff:
            return 'not_due'

        if not dry_run:
            # read again right before acting: a desktop resumed since the first read stays
            instance = self._controller_utils.describe_instance(instance_id)
            if instance is None:
                return 'failed'
            if instance_state(instance) != 'stopped':
                self._logger.info(
                    f'session {session.idea_session_id} of {session.owner}: host '
                    f'{instance_id} is now {instance_state(instance)}, leaving it alone'
                )
                return 'raced'

        return self._tear_down_session(
            session,
            'deleted',
            f'stopped for {stopped_for.days} days, cutoff {stopped_after_days}',
            dry_run,
        )

    def clean_up_stopped_sessions(
        self, time_budget_ms: int = STOPPED_SESSION_CLEANUP_TIME_BUDGET_MS
    ) -> Dict[str, int]:
        """
        the same check across every desktop, at most max_per_pass deletions per pass. one
        desktop that cannot be checked does not stop the rest, and a pass that runs out
        of time or deletions resumes at the page it stopped on.
        """
        config = self.context.config()
        prefix = STOPPED_SESSION_CLEANUP_CONFIG_PREFIX
        if not config.get_bool(f'{prefix}.enabled', default=False):
            return {}
        dry_run = config.get_bool(f'{prefix}.dry_run', default=True)
        stopped_after_days = config.get_int(f'{prefix}.stopped_after_days', default=30)
        warn_days_before = config.get_int(f'{prefix}.warn_days_before', default=7)
        max_per_pass = config.get_int(f'{prefix}.max_per_pass', default=25)
        keep_tags = set(
            Utils.get_as_list(
                config.get_list(
                    f'{prefix}.keep_tags',
                    default=STOPPED_SESSION_CLEANUP_KEEP_TAGS_DEFAULT,
                ),
                STOPPED_SESSION_CLEANUP_KEEP_TAGS_DEFAULT,
            )
        )

        counters = {key: 0 for key in STOPPED_SESSION_CLEANUP_COUNTERS}
        # dry run takes no action, so the cap does not apply and the whole table is reported
        deletions = 0
        deadline = Utils.current_time_ms() + time_budget_ms
        cursor: Optional[str] = self._stopped_session_cleanup_cursor
        loop_break = False
        while not loop_break:
            result = self._session_db.list_all_from_db(
                ListSessionsRequest(paginator=SocaPaginator(cursor=cursor))
            )
            for session in Utils.get_as_list(result.listing, []):
                if Utils.current_time_ms() >= deadline or deletions >= max_per_pass:
                    self._logger.info(
                        'the stopped desktop cleanup is out of time or deletions for this '
                        'pass, the remaining desktops are checked on the next one'
                    )
                    self._stopped_session_cleanup_cursor = cursor
                    self._log_cleanup_pass(counters, dry_run)
                    return counters
                try:
                    outcome = self.clean_up_stopped_session(
                        session,
                        stopped_after_days,
                        warn_days_before,
                        keep_tags,
                        dry_run,
                    )
                except Exception as e:
                    outcome = 'failed'
                    self._logger.warning(
                        f'could not check session '
                        f'{session.idea_session_id if Utils.is_not_empty(session) else None} '
                        f'for the stopped desktop cleanup: {e}'
                    )
                if outcome is None:
                    continue
                counters['candidates'] += 1
                counters[outcome] += 1
                if not dry_run and outcome in ('deleted', 'orphans_cleaned'):
                    deletions += 1
            cursor = result.cursor
            loop_break = Utils.is_empty(cursor)

        self._stopped_session_cleanup_cursor = None
        self._log_cleanup_pass(counters, dry_run)
        return counters

    def _log_cleanup_pass(self, counters: Dict[str, int], dry_run: bool):
        summary = ' '.join(f'{key}={value}' for key, value in counters.items())
        self._logger.info(
            f'stopped desktop cleanup pass{" (dry run)" if dry_run else ""}: {summary}'
        )

    def stop_sessions(
        self, sessions: List[VirtualDesktopSession]
    ) -> tuple[List[VirtualDesktopSession], List[VirtualDesktopSession]]:
        success_response_list: List[VirtualDesktopSession] = []
        fail_response_list: List[VirtualDesktopSession] = []
        session_map: Dict[str:VirtualDesktopSession] = {}
        invalid_dcv_sessions: List[VirtualDesktopSession] = []
        sessions_to_delete: List[VirtualDesktopSession] = []

        for session_orig in sessions:
            session = self._session_db.get_from_db(
                idea_session_owner=session_orig.owner,
                idea_session_id=session_orig.idea_session_id,
            )
            if Utils.is_empty(session):
                # the lookup missed, so `session` is None - report against the request.
                session_orig.failure_reason = (
                    f'Invalid IDEA session id: {session_orig.idea_session_id}:{session_orig.name} '
                    f'for user: {session_orig.owner}. Nothing to stop'
                )
                self._logger.error(session_orig.failure_reason)
                fail_response_list.append(session_orig)
                continue

            # Allow forced operations to proceed regardless of session state
            if not (
                Utils.is_not_empty(session_orig.force) and session_orig.force
            ) and session.state not in {
                VirtualDesktopSessionState.READY,
                VirtualDesktopSessionState.RESUMING,
            }:
                session.failure_reason = f"IDEA session id: {session.idea_session_id}:{session.name} for user: {session.owner} is in {session.state} state. Can't stop. Wait for it to be READY or RESUMING."
                self._logger.error(session.failure_reason)
                fail_response_list.append(session)
                continue

            if Utils.is_empty(session.dcv_session_id):
                # DCV Session doesn't exist. no point deleting.
                self._logger.info("DCV Session doesn't exist. no point deleting.")
                invalid_dcv_sessions.append(session)
                continue

            session.force = session_orig.force
            sessions_to_delete.append(session)
            session_map[session.dcv_session_id] = session

        success_list, error_list = self.context.dcv_broker_client.delete_sessions(
            sessions_to_delete
        )

        servers_to_stop: List[VirtualDesktopServer] = []
        servers_to_hibernate: List[VirtualDesktopServer] = []

        for session in success_list:
            session = session_map[session.dcv_session_id]
            session.state = VirtualDesktopSessionState.STOPPING
            # the recorded reason is persisted, so an earlier failure would be read as
            # the reason this desktop stopped.
            session.failure_reason = None
            session = self._session_db.update(session)
            self.events_utils.publish_validate_dcv_session_deletion_event(
                idea_session_id=session.idea_session_id,
                idea_session_owner=session.owner,
            )
            success_response_list.append(session)

        for session in invalid_dcv_sessions:
            if session.hibernation_enabled:
                servers_to_hibernate.append(session.server)
            else:
                servers_to_stop.append(session.server)
            session.state = VirtualDesktopSessionState.STOPPING
            session.failure_reason = None
            session = self._session_db.update(session)
            success_response_list.append(session)

        for session in error_list:
            session_map[session.dcv_session_id].failure_reason = session.failure_reason
            fail_response_list.append(session_map[session.dcv_session_id])

        self._server_utils.stop_or_hibernate_servers(
            servers_to_stop, servers_to_hibernate
        )
        return success_response_list, fail_response_list

    def resume_sessions(
        self, sessions: List[VirtualDesktopSession]
    ) -> tuple[List[VirtualDesktopSession], List[VirtualDesktopSession]]:
        success_response_list: List[VirtualDesktopSession] = []
        fail_response_list: List[VirtualDesktopSession] = []
        servers_to_start: List[VirtualDesktopServer] = []
        session_server_map: Dict[str:VirtualDesktopSession] = {}

        for session_orig in sessions:
            session = self._session_db.get_from_db(
                idea_session_owner=session_orig.owner,
                idea_session_id=session_orig.idea_session_id,
            )
            if Utils.is_empty(session):
                session_orig.failure_reason = (
                    f'Invalid IDEA session id: {session_orig.idea_session_id}:{session_orig.name} '
                    f'for user: {session_orig.owner}. Nothing to resume'
                )
                self._logger.error(session_orig.failure_reason)
                fail_response_list.append(session_orig)
                continue

            if session.state not in {VirtualDesktopSessionState.STOPPED}:
                # trying to resume a session that is not stopped. Error.
                session.failure_reason = f"IDEA session id: {session.idea_session_id}:{session.name} for user: {session.owner} is in {session.state} state. Can't resume a session not in STOPPED state"
                self._logger.error(session.failure_reason)
                fail_response_list.append(session)
                continue

            servers_to_start.append(session.server)
            session_server_map[session.server.instance_id] = session

        response = self._server_utils.start_dcv_hosts(servers_to_start)

        for server in servers_to_start:
            if 'ERROR' in response:
                session_server_map[
                    server.instance_id
                ].failure_reason = Utils.get_value_as_string(
                    'ERROR', response, 'There is an error, please check with Admin.'
                )
                fail_response_list.append(session_server_map[server.instance_id])
            else:
                session_server_map[
                    server.instance_id
                ].state = VirtualDesktopSessionState.RESUMING
                # the recorded reason is persisted, so an earlier failure would be read as
                # the reason this desktop resumed.
                session_server_map[server.instance_id].failure_reason = None
                # the cleanup's deletion notice was for this stop; the next stop starts fresh
                session_server_map[server.instance_id].cleanup_warning_sent_on = None
                session_server_map[server.instance_id].cleanup_warning_stop_time = None
                session = self._session_db.update(
                    session_server_map[server.instance_id]
                )
                success_response_list.append(session)

        return success_response_list, fail_response_list

    def reboot_sessions(
        self, sessions: List[VirtualDesktopSession]
    ) -> tuple[List[VirtualDesktopSession], List[VirtualDesktopSession]]:
        success_response_list: List[VirtualDesktopSession] = []
        fail_response_list: List[VirtualDesktopSession] = []
        servers_to_reboot: List[VirtualDesktopServer] = []
        sessions_to_reboot: List[VirtualDesktopSession] = []
        sessions_to_reboot_pending_validation: List[VirtualDesktopSession] = []

        for session_orig in sessions:
            session = self._session_db.get_from_db(
                idea_session_owner=session_orig.owner,
                idea_session_id=session_orig.idea_session_id,
            )
            if Utils.is_empty(session):
                # the lookup missed, so `session` is None - report against the request.
                session_orig.failure_reason = (
                    f'Invalid IDEA session id: {session_orig.idea_session_id}:{session_orig.name} '
                    f'for user: {session_orig.owner}. Nothing to reboot'
                )
                self._logger.error(session_orig.failure_reason)
                fail_response_list.append(session_orig)
                continue

            if session.state not in {
                VirtualDesktopSessionState.READY,
                VirtualDesktopSessionState.ERROR,
            }:
                session.failure_reason = f"IDEA session id: {session.idea_session_id}:{session.name} for user: {session.owner} is in {session.state} state. Can't reboot. Wait for it to be READY or ERROR."
                self._logger.error(session.failure_reason)
                fail_response_list.append(session)
                continue

            session.force = session_orig.force
            if Utils.is_not_empty(session.force) and session.force:
                sessions_to_reboot.append(session)
            else:
                sessions_to_reboot_pending_validation.append(session)

        sessions_with_count = (
            self.context.dcv_broker_client.get_active_counts_for_sessions(
                sessions_to_reboot_pending_validation
            )
        )

        for session in sessions_with_count:
            if session.connection_count > 0:
                session.failure_reason = f'There exists {session.connection_count} active connection(s) for idea_session_id: {session.idea_session_id}:{session.name}. Please terminate.'
                self._logger.error(session.failure_reason)
                fail_response_list.append(session)
                continue

            sessions_to_reboot.append(session)

        session_server_map: Dict[str, VirtualDesktopSession] = {}
        for session in sessions_to_reboot:
            servers_to_reboot.append(session.server)
            session_server_map[session.server.instance_id] = session

        response = self._server_utils.reboot_dcv_hosts(servers_to_reboot)

        # the reboot call can fail after the sessions were picked, so the state write and
        # the success list both wait on its result.
        for server in servers_to_reboot:
            session = session_server_map[server.instance_id]
            if 'ERROR' in response:
                session.failure_reason = Utils.get_value_as_string(
                    'ERROR', response, 'There is an error, please check with Admin.'
                )
                self._logger.error(session.failure_reason)
                fail_response_list.append(session)
            else:
                session.state = VirtualDesktopSessionState.RESUMING
                # rebooting is how a desktop comes back from ERROR, and the recorded
                # reason is persisted: leaving it would misreport the next failure.
                session.failure_reason = None
                success_response_list.append(self._session_db.update(session))

        return success_response_list, fail_response_list

    def terminate_sessions(
        self, sessions: List[VirtualDesktopSession]
    ) -> tuple[List[VirtualDesktopSession], List[VirtualDesktopSession]]:
        success_response_list: List[VirtualDesktopSession] = []
        fail_response_list: List[VirtualDesktopSession] = []
        session_map: Dict[str, VirtualDesktopSession] = {}
        stopped_sessions: List[VirtualDesktopSession] = []
        sessions_to_delete: List[VirtualDesktopSession] = []

        for session_orig in sessions:
            session = self._session_db.get_from_db(
                idea_session_owner=session_orig.owner,
                idea_session_id=session_orig.idea_session_id,
            )
            if Utils.is_empty(session):
                # the lookup missed, so `session` is None here: an admin deleting someone
                # else's session without naming the owner lands in this branch.
                session_orig.failure_reason = (
                    f'Invalid IDEA session id: {session_orig.idea_session_id}:{session_orig.name} '
                    f'for user: {session_orig.owner}. Nothing to delete'
                )
                self._logger.info(session_orig.failure_reason)
                fail_response_list.append(session_orig)
                continue

            self._logger.info(
                f'Found db entry for {session.idea_session_id}:{session.name} for user: {session.owner}. DCV Session ID: {session.dcv_session_id}'
            )
            if Utils.is_empty(session.dcv_session_id) or session.state in {
                VirtualDesktopSessionState.STOPPED
            }:
                self._logger.info(
                    f'dcv session id: {session.dcv_session_id} for user: {session.owner}. Current state: {session.state}. Nothing to delete'
                )
                session.force = (
                    session_orig.force
                )  # Preserve force flag for stopped sessions too
                stopped_sessions.append(session)
                continue

            session_map[session.dcv_session_id] = session
            session.force = session_orig.force
            sessions_to_delete.append(session)

        success_list, error_list = self.context.dcv_broker_client.delete_sessions(
            sessions_to_delete
        )

        session_db_entries_to_delete = []
        for session in success_list:
            session = session_map[session.dcv_session_id]
            session.state = VirtualDesktopSessionState.DELETING
            session = self._session_db.update(session)
            self.events_utils.publish_validate_dcv_session_deletion_event(
                idea_session_id=session.idea_session_id,
                idea_session_owner=session.owner,
            )
            success_response_list.append(session)

        # Handle stopped sessions - always use force termination for deletions
        servers_to_terminate = []

        for session in stopped_sessions:
            session_db_entries_to_delete.append(session)
            if session.server:
                servers_to_terminate.append(session.server)

        # Always use force termination for session deletions to ensure immediate cleanup
        if servers_to_terminate:
            self._server_utils.terminate_dcv_hosts(servers_to_terminate, force=True)
        for session in session_db_entries_to_delete:
            self._schedule_utils.delete_schedules_for_session(session)
            self._session_permission_utils.delete_permissions_for_session(session)
            self._session_db.delete(session)
            session.state = VirtualDesktopSessionState.DELETED
            session.updated_on = DateTimeUtils.current_datetime()
            success_response_list.append(session)

        for session in error_list:
            session_map[session.dcv_session_id].failure_reason = session.failure_reason
            fail_response_list.append(session_map[session.dcv_session_id])

        return success_response_list, fail_response_list

    def delete_session_entry_from_opensearch(self, idea_session_id: str):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias", required=True)}-{self.context.sessions_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=idea_session_id,
                entry_action=EntryAction.DELETE_ENTRY,
                entry_content=EntryContent(index_id=index_name),
            )
        )

    def update_session_entry_to_opensearch(self, session: VirtualDesktopSession):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias", required=True)}-{self.context.sessions_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=session.idea_session_id,
                entry_action=EntryAction.UPDATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name,
                    entry_record=self._session_db.convert_session_object_to_db_dict(
                        session
                    ),
                ),
            )
        )

    def index_session_entry_to_opensearch(self, session: VirtualDesktopSession):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias", required=True)}-{self.context.sessions_template_version}'
        index_dict = self._session_db.convert_session_object_to_index_dict(session)
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=session.idea_session_id,
                entry_action=EntryAction.CREATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name, entry_record=index_dict
                ),
            )
        )
