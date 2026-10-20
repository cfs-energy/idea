"""Check broker health and retry a lost desktop session without restarting its host."""

from botocore.exceptions import ClientError
from ideadatamodel import VirtualDesktopSessionState, constants
from ideasdk.utils import Utils

RESTARTING_MESSAGE = (
    'Your desktop session is restarting. Try connecting again in a few minutes.'
)
RECOVERY_FAILED_MESSAGE = 'Your desktop session could not restart after 3 attempts. Stop and start the desktop to try again, or contact your administrator.'
RETRY_SECONDS = (60, 300, 900)
VALIDATION_SECONDS = 180


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
        self.key = 'dcv-session-' + session.idea_session_id
        lock = self.context.distributed_lock()
        lock.acquire(key=self.key)
        try:
            session = self.db.get_from_db(session.owner, session.idea_session_id)
            if session is None:
                return
            if resume:
                if session.state not in {
                    VirtualDesktopSessionState.READY,
                    VirtualDesktopSessionState.RESUMING,
                    VirtualDesktopSessionState.STOPPED,
                    VirtualDesktopSessionState.ERROR,
                }:
                    return
                self.start(session)
            elif (
                session.recovering
                and session.state == VirtualDesktopSessionState.INITIALIZING
            ):
                self.reconcile(session)
            elif session.state == VirtualDesktopSessionState.READY:
                response = self.broker.describe_sessions([session])
                current = response.get('sessions', {}).get(session.dcv_session_id, {})
                if current.get('state') != 'READY':
                    self.start(session)
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            raise self.handler.do_not_delete_message_exception(
                f'Session {session.idea_session_id} changed during recovery; re-read and retry'
            ) from error
        finally:
            lock.release(key=self.key)

    def start(self, session):
        session.state = VirtualDesktopSessionState.INITIALIZING
        session.recovering = True
        session.recovery_attempts = 0
        session.recovery_deadline = 0
        session.recovery_pending_create = False
        session.recovery_next_attempt_at = (
            Utils.current_time_ms() + RETRY_SECONDS[0] * 1000
        )
        session.failure_reason = RESTARTING_MESSAGE
        self.save(session)

    def healthy_instance(self, session):
        statuses = (
            self.context.aws()
            .ec2()
            .describe_instance_status(
                InstanceIds=[session.server.instance_id], IncludeAllInstances=True
            )
            .get('InstanceStatuses', [])
        )
        return len(statuses) == 1 and all(
            (
                statuses[0].get('InstanceState', {}).get('Name') == 'running',
                statuses[0].get('InstanceStatus', {}).get('Status') == 'ok',
                statuses[0].get('SystemStatus', {}).get('Status') == 'ok',
            )
        )

    def reconcile(self, session):
        now = Utils.current_time_ms()
        if now < session.recovery_next_attempt_at or (
            now < session.recovery_deadline and not session.recovery_pending_create
        ):
            return
        if not self.healthy_instance(session):
            return
        # Discover an accepted create even if the controller died before saving its id.
        entries = self.broker.describe_sessions_for_host(session)
        live = [entry for entry in entries if entry.get('state') != 'DELETED']
        ready = [entry for entry in live if entry.get('state') == 'READY']
        if len(live) == 1 and len(ready) == 1:
            self.context.distributed_lock().assert_held(key=self.key)
            session.dcv_session_id = ready[0]['id']
            self.handler.controller_utils.create_tag(
                session.server.instance_id,
                constants.IDEA_TAG_DCV_SESSION_ID,
                session.dcv_session_id,
            )
            session.recovering = False
            session.recovery_pending_create = False
            session.recovery_attempts = 0
            session.recovery_deadline = 0
            session.recovery_next_attempt_at = 0
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
                session.recovering = False
                session.failure_reason = RECOVERY_FAILED_MESSAGE
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
        if ready:
            return
        if live:
            # Deletion is asynchronous; the next check must confirm the host is empty.
            for entry in live:
                stale = session.model_copy(deep=True)
                stale.dcv_session_id = entry['id']
                stale.force = True
                self.context.distributed_lock().assert_held(key=self.key)
                self.broker.delete_sessions([stale])
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
