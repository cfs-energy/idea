from ideadatamodel import errorcodes, SocaJob, SocaJobState, CustomFileLoggerParams
from ideadatamodel.model_utils import ModelUtils
from ideasdk.protocols import SocaContextProtocol
from ideasdk.utils import Utils

from typing import Optional
from threading import RLock
import datetime
import logging

EVENT_SCHEMA_VERSION = 1

EVENT_TYPE_ATTEMPT = 'provisioning.attempt'
EVENT_TYPE_RETRY = 'provisioning.retry'
EVENT_TYPE_STACK_CREATED = 'provisioning.stack-created'
EVENT_TYPE_STACK_FAILED = 'provisioning.stack-failed'
EVENT_TYPE_CAPACITY_WAIT = 'provisioning.capacity-wait'
EVENT_TYPE_HELD_AT_CAP = 'provisioning.held-at-cap'
EVENT_TYPE_DISPOSITION = 'provisioning.disposition'

DISPOSITION_RAN = 'ran'
DISPOSITION_FAILED = 'failed'
DISPOSITION_HELD = 'held'
DISPOSITION_DELETED = 'deleted'

OUTCOME_PENDING = 'pending'
OUTCOME_SUCCESS = 'success'
OUTCOME_FAILURE = 'failure'
OUTCOME_CANCELLED = 'cancelled'

# every event carries an outcome so a consumer can aggregate without keeping its own
# event_type lookup table. disposition events take their outcome from the disposition.
EVENT_TYPE_OUTCOMES = {
    EVENT_TYPE_ATTEMPT: OUTCOME_PENDING,
    EVENT_TYPE_RETRY: OUTCOME_PENDING,
    EVENT_TYPE_CAPACITY_WAIT: OUTCOME_PENDING,
    EVENT_TYPE_HELD_AT_CAP: OUTCOME_PENDING,
    EVENT_TYPE_STACK_CREATED: OUTCOME_SUCCESS,
    EVENT_TYPE_STACK_FAILED: OUTCOME_FAILURE,
}

DISPOSITION_OUTCOMES = {
    DISPOSITION_RAN: OUTCOME_SUCCESS,
    DISPOSITION_FAILED: OUTCOME_FAILURE,
    DISPOSITION_HELD: OUTCOME_FAILURE,
    DISPOSITION_DELETED: OUTCOME_CANCELLED,
}

REASON_CLASS_BUDGET = 'budget'
REASON_CLASS_SERVICE_QUOTA = 'service_quota'
REASON_CLASS_LICENSES = 'licenses'
REASON_CLASS_DRY_RUN = 'dry_run'
REASON_CLASS_RESERVED_INSTANCES = 'reserved_instances'
REASON_CLASS_CAPACITY = 'capacity'
REASON_CLASS_ACCESS = 'access'
REASON_CLASS_CLOUDFORMATION = 'cloudformation'
REASON_CLASS_STACK_TIMEOUT = 'stack_timeout'
REASON_CLASS_RETRIES_EXHAUSTED = 'retries_exhausted'
REASON_CLASS_UNKNOWN = 'unknown'

# maps provisioning error codes to a stable reason class, so consumers do not
# need to track the full (and growing) set of error codes.
ERROR_CODE_REASON_CLASSES = {
    errorcodes.BUDGET_NOT_FOUND: REASON_CLASS_BUDGET,
    errorcodes.BUDGETS_PROJECT_IS_REQUIRED: REASON_CLASS_BUDGET,
    errorcodes.BUDGETS_USER_NOT_CONFIGURED: REASON_CLASS_BUDGET,
    errorcodes.BUDGETS_LIMIT_EXCEEDED: REASON_CLASS_BUDGET,
    errorcodes.SERVICE_QUOTA_NOT_FOUND: REASON_CLASS_SERVICE_QUOTA,
    errorcodes.SERVICE_QUOTA_NOT_AVAILABLE: REASON_CLASS_SERVICE_QUOTA,
    errorcodes.NOT_ENOUGH_LICENSES: REASON_CLASS_LICENSES,
    errorcodes.SCHEDULER_INVALID_LICENSE_RESOURCE_CONFIGURATION: REASON_CLASS_LICENSES,
    errorcodes.EC2_DRY_RUN_FAILED: REASON_CLASS_DRY_RUN,
    errorcodes.EC2_RESERVED_INSTANCES_NOT_PURCHASED: REASON_CLASS_RESERVED_INSTANCES,
    errorcodes.EC2_RESERVED_INSTANCES_NOT_AVAILABLE: REASON_CLASS_RESERVED_INSTANCES,
    errorcodes.DESCRIBE_RESERVED_INSTANCES_FAILED: REASON_CLASS_RESERVED_INSTANCES,
    errorcodes.CAPACITY_UNAVAILABLE: REASON_CLASS_CAPACITY,
    errorcodes.SHARED_CAPACITY_UNAVAILABLE: REASON_CLASS_CAPACITY,
    errorcodes.SHARED_CAPACITY_INVALID_QUEUE: REASON_CLASS_CAPACITY,
    errorcodes.SHARED_CAPACITY_MISMATCH: REASON_CLASS_CAPACITY,
    errorcodes.SPOT_FLEET_CAPACITY_UPDATE_IN_PROGRESS: REASON_CLASS_CAPACITY,
    errorcodes.MAX_PROVISIONED_INSTANCES_LIMIT: REASON_CLASS_CAPACITY,
    errorcodes.RETRY_JOB_PROVISIONING: REASON_CLASS_CAPACITY,
    errorcodes.UNAUTHORIZED_ACCESS: REASON_CLASS_ACCESS,
    errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED: REASON_CLASS_CLOUDFORMATION,
    errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED: REASON_CLASS_RETRIES_EXHAUSTED,
}


class ProvisioningLifecycleEvents:
    """
    Emits structured, consumer-agnostic job provisioning lifecycle events.

    Events are written as single-line JSON documents to a dedicated rotating log file:
        <logs_directory>/events/provisioning_lifecycle_events.log

    Any log shipper (CloudWatch agent, fluent-bit, vector, ...) can tail the file
    without code changes. Emission is local file I/O only - no AWS API calls.

    Every event carries schema_version, event_type, timestamp, timestamp_ms, outcome,
    terminal, and (when a job is in scope) job_id and attempt_number, so a consumer can
    correlate and aggregate without a per-event-type lookup table.

    attempt_number is the 1-based provisioning attempt the event belongs to, counted from
    the job's persistent retry count so every event of one attempt carries the same value
    and (job_id, attempt_number) identifies that attempt across its whole life. It is
    absent when the job has consumed no attempt. cycle_attempt is a separate 1-based index
    of the in-process retry within a single provisioning cycle, present only on the events
    emitted from inside that loop.

    Config:
        scheduler.provisioning_lifecycle_events.enabled (default: True)
        scheduler.provisioning_lifecycle_events.retention_days (default: 90)
    """

    def __init__(self, context: SocaContextProtocol):
        self._context = context
        self._logger = context.logger('provisioning-lifecycle-events')
        self._event_logger: Optional[logging.Logger] = None
        self._event_logger_lock = RLock()

    def is_enabled(self) -> bool:
        return self._context.config().get_bool(
            'scheduler.provisioning_lifecycle_events.enabled', default=True
        )

    def _get_event_logger(self) -> logging.Logger:
        if self._event_logger is not None:
            return self._event_logger
        with self._event_logger_lock:
            if self._event_logger is None:
                retention_days = self._context.config().get_int(
                    'scheduler.provisioning_lifecycle_events.retention_days',
                    default=90,
                )
                self._event_logger = self._context.logging().get_custom_file_logger(
                    CustomFileLoggerParams(
                        logger_name='provisioning_lifecycle_events',
                        log_dir_name='events',
                        log_file_name='provisioning_lifecycle_events.log',
                        when='midnight',
                        interval=1,
                        backupCount=retention_days,
                    )
                )
            return self._event_logger

    @staticmethod
    def _isoformat(value: Optional[datetime.datetime]) -> Optional[str]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.isoformat()

    @staticmethod
    def _seconds_between(
        start: Optional[datetime.datetime], end: Optional[datetime.datetime]
    ) -> Optional[int]:
        if start is None or end is None:
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        seconds = int((end - start).total_seconds())
        if seconds < 0:
            return None
        return seconds

    @staticmethod
    def _utcnow() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    @staticmethod
    def _safe_get(fn) -> Optional[str]:
        try:
            return fn()
        except Exception:
            return None

    def attempts_consumed(self, job: SocaJob) -> Optional[int]:
        """
        provisioning attempts the job has consumed, read from the job cache. the cache
        counts failed attempts only, so a job that reached provisioning succeeded on the
        attempt after its last failure. returns None when the count is unavailable.

        for a job that has left provisioning this is also the number of the attempt that
        produced the outcome, so it doubles as attempt_number on disposition events.
        """
        try:
            failures = self._context.job_cache.get_job_provisioning_retry_count(
                job_id=job.job_id
            )
            return failures + 1 if job.is_provisioned() else failures
        except Exception:
            return None

    def current_attempt(self, job: SocaJob) -> Optional[int]:
        """
        1-based number of the provisioning attempt now in flight: one more than the
        failures the job cache has recorded, since the attempt in progress is only
        counted once it fails. returns None when the count is unavailable.
        """
        try:
            return (
                self._context.job_cache.get_job_provisioning_retry_count(
                    job_id=job.job_id
                )
                + 1
            )
        except Exception:
            return None

    @staticmethod
    def get_reason_class(error_code: Optional[str]) -> str:
        if Utils.is_empty(error_code):
            return REASON_CLASS_UNKNOWN
        return ERROR_CODE_REASON_CLASSES.get(error_code, REASON_CLASS_UNKNOWN)

    @staticmethod
    def get_disposition(job: SocaJob) -> str:
        """
        disposition of a job that has left the scheduler queue.

        HELD is mapped first: the retry cap also emits 'held' when it parks a job, but
        this function is called directly to label the record of a job that never ran, and
        that record must say 'held' rather than 'deleted'.
        """
        if job.state == SocaJobState.HELD:
            return DISPOSITION_HELD
        if not job.is_provisioned() or job.start_time is None:
            # error_message is not a discriminator here: it is written on transient
            # waits too. a job the platform gave up on is held, and is caught above.
            return DISPOSITION_DELETED
        exit_status = Utils.get_as_int(job.exit_status, default=0)
        if exit_status != 0:
            return DISPOSITION_FAILED
        return DISPOSITION_RAN

    @staticmethod
    def _job_fields(job: Optional[SocaJob]) -> dict:
        if job is None:
            return {}
        fields = {
            'job_id': job.job_id,
            'owner': job.owner,
            'queue': job.queue,
            'queue_profile': job.queue_type,
            'project': job.project,
        }
        if job.scaling_mode is not None:
            fields['scaling_mode'] = str(job.scaling_mode.value)
        try:
            fields['job_group'] = job.get_job_group()
            capacity_type = job.capacity_type()
            if capacity_type is not None:
                fields['capacity_type'] = str(capacity_type.value)
        except Exception:  # noqa - job group / capacity type are optional enrichment
            pass
        return fields

    def _publish(
        self, event_type: str, payload: dict, terminal: bool = False, outcome=None
    ):
        try:
            if not self.is_enabled():
                return
            if payload.get('attempt_number') == 0:
                # attempt_number is a 1-based index; a job that consumed no attempt has none
                payload['attempt_number'] = None
            now = self._utcnow()
            event = {
                'schema_version': EVENT_SCHEMA_VERSION,
                'event_type': event_type,
                'timestamp': now.isoformat(),
                'timestamp_ms': Utils.current_time_ms(),
                'outcome': outcome
                if outcome is not None
                else EVENT_TYPE_OUTCOMES.get(event_type, OUTCOME_PENDING),
                'terminal': terminal,
                'cluster_name': self._context.cluster_name(),
                'module_id': self._context.module_id(),
            }
            for key, value in payload.items():
                if value is not None:
                    event[key] = value
            self._get_event_logger().critical(Utils.to_json(event))
        except Exception as e:
            self._logger.warning(
                f'failed to publish lifecycle event: {event_type} - {e}'
            )

    def job_provisioning_attempt(
        self,
        job: SocaJob,
        attempt_number: Optional[int] = 1,
        job_count: int = 1,
        cycle_attempt: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['cycle_attempt'] = cycle_attempt
        payload['job_count'] = job_count
        payload['queue_wait_seconds'] = self._seconds_between(
            job.queue_time, self._utcnow()
        )
        self._publish(EVENT_TYPE_ATTEMPT, payload)

    def job_provisioning_retry(
        self,
        job: SocaJob,
        attempt_number: Optional[int] = None,
        error_code: Optional[str] = None,
        job_count: int = 1,
        cycle_attempt: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['cycle_attempt'] = cycle_attempt
        payload['job_count'] = job_count
        payload['error_code'] = error_code
        payload['reason_class'] = (
            self.get_reason_class(error_code)
            if Utils.is_not_empty(error_code)
            else None
        )
        payload['queue_wait_seconds'] = self._seconds_between(
            job.queue_time, self._utcnow()
        )
        self._publish(EVENT_TYPE_RETRY, payload)

    def stack_created(
        self,
        job: SocaJob,
        stack_id: Optional[str],
        job_count: int = 1,
        attempt_number: Optional[int] = None,
        cycle_attempt: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['cycle_attempt'] = cycle_attempt
        payload['stack_id'] = stack_id
        payload['compute_stack'] = self._safe_get(job.get_compute_stack)
        payload['job_count'] = job_count
        payload['time_to_stack_seconds'] = self._seconds_between(
            job.queue_time, self._utcnow()
        )
        self._publish(EVENT_TYPE_STACK_CREATED, payload)

    def stack_failed(
        self,
        job: SocaJob,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
        reason_class: Optional[str] = None,
        attempt_number: Optional[int] = None,
        cycle_attempt: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['cycle_attempt'] = cycle_attempt
        payload['compute_stack'] = self._safe_get(job.get_compute_stack)
        payload['error_code'] = error_code
        payload['message'] = message
        if reason_class is None:
            reason_class = self.get_reason_class(error_code)
        payload['reason_class'] = reason_class
        self._publish(EVENT_TYPE_STACK_FAILED, payload)

    def capacity_wait(
        self,
        job: SocaJob,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
        job_count: int = 1,
        attempt_number: Optional[int] = None,
        cycle_attempt: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['cycle_attempt'] = cycle_attempt
        payload['error_code'] = error_code
        payload['message'] = message
        payload['job_count'] = job_count
        payload['queue_wait_seconds'] = self._seconds_between(
            job.queue_time, self._utcnow()
        )
        self._publish(EVENT_TYPE_CAPACITY_WAIT, payload)

    def held_at_cap(
        self,
        queue_profile: str,
        limit_type: Optional[str] = None,
        queue_threshold: Optional[int] = None,
        queue_current: Optional[int] = None,
        pending_jobs: Optional[int] = None,
        job: Optional[SocaJob] = None,
        attempt_number: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['queue_profile'] = queue_profile
        payload['limit_type'] = limit_type
        payload['queue_threshold'] = queue_threshold
        payload['queue_current'] = queue_current
        payload['pending_jobs'] = pending_jobs
        self._publish(EVENT_TYPE_HELD_AT_CAP, payload)

    def job_held(
        self,
        job: SocaJob,
        attempt_number: Optional[int] = None,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
        hold_failed: bool = False,
    ):
        """
        the provisioning retry cap parked this job: it will not be provisioned again
        until it is released or deleted.

        emitted as a disposition so an operator can tell a parked job from one still
        being retried. terminal is False because the job is still in the queue - a
        released job resumes and emits a further disposition when it finally leaves.

        hold_failed marks the case where the cap was reached but the hold call failed:
        the job is excluded from re-queue yet is not held in the scheduler either, which
        is the state most in need of an event.
        """
        payload = self._job_fields(job)
        payload['attempt_number'] = attempt_number
        payload['hold_failed'] = hold_failed
        payload['disposition'] = DISPOSITION_HELD
        payload['error_code'] = error_code
        payload['message'] = message
        payload['reason_class'] = self.get_reason_class(error_code)
        payload['queue_time'] = self._isoformat(job.queue_time)
        payload['queue_wait_seconds'] = self._seconds_between(
            job.queue_time, self._utcnow()
        )
        self._publish(
            EVENT_TYPE_DISPOSITION, payload, terminal=False, outcome=OUTCOME_FAILURE
        )

    def job_disposition(
        self,
        job: SocaJob,
        disposition: Optional[str] = None,
        attempt_number: Optional[int] = None,
    ):
        payload = self._job_fields(job)
        if disposition is None:
            disposition = self.get_disposition(job)
        payload['attempt_number'] = attempt_number
        payload['disposition'] = disposition
        payload['exit_status'] = job.exit_status
        payload['queue_time'] = self._isoformat(job.queue_time)
        payload['provisioning_time'] = self._isoformat(job.provisioning_time)
        payload['start_time'] = self._isoformat(job.start_time)
        payload['end_time'] = self._isoformat(job.end_time)
        payload['provisioning_seconds'] = self._seconds_between(
            job.queue_time, job.provisioning_time
        )
        payload['queue_wait_seconds'] = self._seconds_between(
            job.queue_time, job.start_time
        )
        payload['run_seconds'] = self._seconds_between(job.start_time, job.end_time)
        payload['total_seconds'] = self._seconds_between(job.queue_time, job.end_time)
        # cost enrichment: everything needed to price the job is on the SocaJob at
        # emission time. additive fields (schema stays v1); None values are dropped by
        # _publish, so a job missing params simply omits them.
        payload['instance_type'] = self._safe_get(lambda: job.default_instance_type)
        payload['nodes'] = self._safe_get(job.desired_nodes)
        payload['walltime_seconds'] = self._safe_get(
            lambda: ModelUtils.walltime_to_seconds(job.params.walltime)
        )
        payload['compute_stack'] = self._safe_get(job.get_compute_stack)
        payload['total_time_secs'] = job.total_time_secs
        payload['estimated_bom_cost_total'] = self._safe_get(
            lambda: job.estimated_bom_cost.total.amount
        )
        self._publish(
            EVENT_TYPE_DISPOSITION,
            payload,
            terminal=disposition != DISPOSITION_HELD,
            outcome=DISPOSITION_OUTCOMES.get(disposition, OUTCOME_FAILURE),
        )
