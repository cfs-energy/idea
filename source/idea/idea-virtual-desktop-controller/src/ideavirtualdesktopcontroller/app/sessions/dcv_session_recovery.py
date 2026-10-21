"""Check broker health and retry a lost desktop session without restarting its host."""

from botocore.exceptions import ClientError
from ideadatamodel import VirtualDesktopSessionState, constants
from ideasdk.utils import Utils

RESTARTING_MESSAGE = (
    'Your desktop session is restarting. Try connecting again in a few minutes.'
)
RECOVERY_FAILED_MESSAGE = 'Your desktop session could not restart after 3 attempts. Stop and start the desktop to try again, or contact your administrator.'
SESSION_UNAVAILABLE_MESSAGE = 'The broker could not confirm that your desktop session is ready within the recovery window. The session has been preserved because your applications may still be running. Contact your administrator.'
INSTANCE_UNHEALTHY_MESSAGE = 'Your desktop instance did not become healthy within 15 minutes of session recovery. Reboot the desktop to try again, or contact your administrator.'
RETRY_SECONDS = (60, 300, 900)
VALIDATION_SECONDS = 180
REBOOT_SECONDS = 900


class DcvSessionRecovery:
    def __init__(self, handler):
        self.handler = handler
        self.context = handler.context
        self.db = handler.session_db
        self.broker = self.context.dcv_broker_client

    def save(self, session):
        self.context.distributed_lock().assert_held(key=self.key)
        updated = self.db.update(session, check_version=True)
        session.updated_on = updated.updated_on
        session._updated_on_ms = updated._updated_on_ms

    def check(self, session, resume=False):
        """Return False when a planned reboot needs the normal resume or validation flow."""
        self.key = 'dcv-session-' + session.idea_session_id
        lock = self.context.distributed_lock()
        lock.acquire(key=self.key)
        try:
            session = self.db.get_from_db(session.owner, session.idea_session_id)
            if session is None:
                return True
            if session.reboot_deadline and session.state in {
                VirtualDesktopSessionState.READY,
                VirtualDesktopSessionState.RESUMING,
                VirtualDesktopSessionState.CREATING,
                VirtualDesktopSessionState.INITIALIZING,
            }:
                status = self.instance_status(session)
                if self.leave_inactive_instance(session, status):
                    return True
                if Utils.current_time_ms() >= session.reboot_deadline:
                    if self.healthy_instance(status):
                        self.start(session)
                    else:
                        self.clear_recovery(session)
                        session.state = VirtualDesktopSessionState.ERROR
                        session.failure_reason = 'Your desktop did not become healthy within 15 minutes of rebooting. Stop and start the desktop to try again.'
                        self.save(session)
                    return True
                if resume:
                    return session.state != VirtualDesktopSessionState.RESUMING
                return session.state != VirtualDesktopSessionState.INITIALIZING
            if resume:
                if session.state not in {
                    VirtualDesktopSessionState.READY,
                    VirtualDesktopSessionState.RESUMING,
                    VirtualDesktopSessionState.STOPPED,
                    VirtualDesktopSessionState.ERROR,
                }:
                    return True
                self.start(session)
            elif (
                session.recovering
                and session.state == VirtualDesktopSessionState.INITIALIZING
            ):
                self.reconcile(session)
            elif session.state == VirtualDesktopSessionState.READY:
                response = self.broker.describe_sessions([session])
                current = response.get('sessions', {}).get(session.dcv_session_id, {})
                if current.get('state') == 'READY':
                    if session.recovery_unready_observed:
                        session.recovery_unready_observed = False
                        self.save(session)
                elif session.recovery_unready_observed:
                    self.start(session)
                else:
                    session.recovery_unready_observed = True
                    self.save(session)
            return True
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            raise self.handler.do_not_delete_message_exception(
                f'Session {session.idea_session_id} changed during recovery; re-read and retry'
            ) from error
        finally:
            lock.release(key=self.key)

    def start(self, session):
        status = self.instance_status(session)
        if self.leave_inactive_instance(session, status):
            return
        if status.get('InstanceState', {}).get('Name') != 'running':
            return
        session.reboot_deadline = 0
        session.state = VirtualDesktopSessionState.INITIALIZING
        session.recovering = True
        session.recovery_attempts = 0
        session.recovery_deadline = 0
        session.recovery_pending_create = False
        session.recovery_unready_observed = False
        session.recovery_health_deadline = (
            Utils.current_time_ms() + REBOOT_SECONDS * 1000
        )
        session.recovery_next_attempt_at = (
            Utils.current_time_ms() + RETRY_SECONDS[0] * 1000
        )
        session.failure_reason = RESTARTING_MESSAGE
        self.save(session)

    def instance_status(self, session):
        statuses = (
            self.context.aws()
            .ec2()
            .describe_instance_status(
                InstanceIds=[session.server.instance_id], IncludeAllInstances=True
            )
            .get('InstanceStatuses', [])
        )
        return statuses[0] if len(statuses) == 1 else {}

    @staticmethod
    def healthy_instance(status):
        return all(
            (
                status.get('InstanceState', {}).get('Name') == 'running',
                status.get('InstanceStatus', {}).get('Status') == 'ok',
                status.get('SystemStatus', {}).get('Status') == 'ok',
            )
        )

    @staticmethod
    def clear_recovery(session):
        session.recovering = False
        session.recovery_attempts = 0
        session.recovery_deadline = 0
        session.recovery_next_attempt_at = 0
        session.recovery_pending_create = False
        session.recovery_health_deadline = 0
        session.recovery_unready_observed = False
        session.reboot_deadline = 0

    def leave_inactive_instance(self, session, status):
        state = status.get('InstanceState', {}).get('Name')
        if state not in {'stopped', 'stopping', 'terminated', 'shutting-down'}:
            return False
        self.clear_recovery(session)
        if state in {'stopped', 'stopping'}:
            session.state = VirtualDesktopSessionState.STOPPED
            session.failure_reason = None
        else:
            session.state = VirtualDesktopSessionState.ERROR
            session.failure_reason = f'Your desktop instance is {state} and its session cannot be recovered. Contact your administrator.'
        self.save(session)
        return True

    def reconcile(self, session):
        now = Utils.current_time_ms()
        status = self.instance_status(session)
        if self.leave_inactive_instance(session, status):
            return
        # Bound recoveries already in progress when this deadline was introduced.
        if not session.recovery_health_deadline:
            session.recovery_health_deadline = now + REBOOT_SECONDS * 1000
            self.save(session)
        healthy = self.healthy_instance(status)
        if not healthy and now >= session.recovery_health_deadline:
            self.clear_recovery(session)
            session.state = VirtualDesktopSessionState.ERROR
            session.failure_reason = INSTANCE_UNHEALTHY_MESSAGE
            self.save(session)
            return
        if now < session.recovery_next_attempt_at or (
            now < session.recovery_deadline and not session.recovery_pending_create
        ):
            return
        # Discover an accepted create even if the controller died before saving its id.
        entries = self.broker.describe_sessions_for_host(session)
        live = [entry for entry in entries if entry.get('state') != 'DELETED']
        if not live and not healthy:
            return
        ready = [entry for entry in live if entry.get('state') == 'READY']
        if len(live) == 1 and len(ready) == 1:
            self.context.distributed_lock().assert_held(key=self.key)
            session.dcv_session_id = ready[0]['id']
            self.handler.controller_utils.create_tag(
                session.server.instance_id,
                constants.IDEA_TAG_DCV_SESSION_ID,
                session.dcv_session_id,
            )
            self.clear_recovery(session)
            session.failure_reason = None
            self.context.distributed_lock().assert_held(key=self.key)
            self.handler.mark_ready(session, check_version=True)
            return
        if session.recovery_pending_create and not live:
            session.recovery_pending_create = False
            session.recovery_deadline = now + VALIDATION_SECONDS * 1000
            self.save(session)
            self.create(session)
            return
        if now < session.recovery_deadline:
            return
        if session.recovery_deadline:
            session.recovery_pending_create = False
            session.recovery_deadline = 0
            if session.recovery_attempts >= len(RETRY_SECONDS):
                session.state = VirtualDesktopSessionState.ERROR
                self.clear_recovery(session)
                session.failure_reason = (
                    SESSION_UNAVAILABLE_MESSAGE if live else RECOVERY_FAILED_MESSAGE
                )
            else:
                session.recovery_next_attempt_at = (
                    now + RETRY_SECONDS[session.recovery_attempts] * 1000
                )
            self.save(session)
            return
        session.recovery_pending_create = bool(live)
        session.recovery_attempts += 1
        session.recovery_deadline = now + VALIDATION_SECONDS * 1000
        self.save(session)
        self.context.distributed_lock().assert_held(key=self.key)
        if live:
            # Broker contact loss does not mean the user's applications have stopped.
            return
        self.create(session)

    def create(self, session):
        self.context.distributed_lock().assert_held(key=self.key)
        response = self.broker.create_session(session)
        if not response.failure_reason and response.dcv_session_id:
            session.dcv_session_id = response.dcv_session_id
            self.save(session)
            self.handler.controller_utils.create_tag(
                session.server.instance_id,
                constants.IDEA_TAG_DCV_SESSION_ID,
                session.dcv_session_id,
            )
