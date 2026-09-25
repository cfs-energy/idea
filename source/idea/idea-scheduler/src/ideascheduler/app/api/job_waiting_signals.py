from typing import List, Optional

import ideascheduler
from ideadatamodel import SocaJob, SocaJobState

MAX_PROVISIONING_RETRIES_KEY = 'scheduler.job_provisioning.max_provisioning_retries'
MAX_PROVISIONING_RETRIES_DEFAULT = 3


def is_awaiting_provisioning(job: SocaJob) -> bool:
    """
    the job has neither started nor had capacity provisioned for it.

    the provisioning attempt count is stale for any other job: the counter is cleared on
    release, not on a successful provision.
    """
    if job.start_time is not None:
        return False
    return not job.is_provisioned()


def get_max_provisioning_attempts(
    context: 'ideascheduler.AppContext',
) -> Optional[int]:
    max_attempts = context.config().get_int(
        MAX_PROVISIONING_RETRIES_KEY, default=MAX_PROVISIONING_RETRIES_DEFAULT
    )
    if max_attempts is None or max_attempts <= 0:
        # the cap is disabled: there is no 'of M' to report
        return None
    return max_attempts


def get_provisioning_attempt(
    failed_attempts: int, max_attempts: Optional[int]
) -> Optional[int]:
    """
    the attempt the job is on, 1-based.

    the persistent per-job counter records attempts that failed, so the job is on the
    next one. at the cap the job is held and the last attempt is the one reported.
    """
    if failed_attempts < 0:
        return None
    attempt = failed_attempts + 1
    if max_attempts is not None and attempt > max_attempts:
        return max_attempts
    return attempt


def get_blocking_limit_info(context: 'ideascheduler.AppContext', job: SocaJob):
    try:
        queue = context.queue_profiles.get_provisioning_queue(
            queue_profile_name=job.queue_type
        )
        if queue is not None and queue.is_queue_blocked_by_limits():
            return queue.get_limit_info()
    except Exception:
        return None
    return None


def get_blocking_limit_type(
    context: 'ideascheduler.AppContext', job: SocaJob
) -> Optional[str]:
    info = get_blocking_limit_info(context, job)
    return info.limit_type if info is not None else None


def apply_waiting_signals(
    context: 'ideascheduler.AppContext', jobs: Optional[List[SocaJob]]
) -> None:
    """
    attach the waiting signals to jobs that have not started. mutates in place.
    """
    if not jobs:
        return

    max_attempts = get_max_provisioning_attempts(context=context)

    for job in jobs:
        if job is None:
            continue
        if not is_awaiting_provisioning(job=job) or job.state == SocaJobState.FINISHED:
            if job.error_message and not job.status_reason:
                job.status_reason = job.error_message
            continue

        failed_attempts = context.job_cache.get_job_provisioning_retry_count(
            job_id=job.job_id
        )
        job.provisioning_attempt = get_provisioning_attempt(
            failed_attempts=failed_attempts, max_attempts=max_attempts
        )
        job.max_provisioning_attempts = max_attempts

        if job.state == SocaJobState.HELD:
            # a held job is not queued behind a limit. provisioning stopped retrying it,
            # so naming a queue limit would point at the wrong thing.
            job.status_reason = build_status_reason(job)
            continue

        limit_info = get_blocking_limit_info(context, job)
        job.blocking_limit_type = (
            limit_info.limit_type if limit_info is not None else None
        )
        job.status_reason = build_status_reason(job, limit_info)


def build_status_reason(job: SocaJob, limit_info=None) -> Optional[str]:
    if job.disposition == 'deleted':
        return job.status_reason or 'Cancelled by the owner.'
    if job.state == SocaJobState.HELD:
        if job.reason_class != 'retries_exhausted':
            return job.status_reason or job.error_message or 'Job held'
        attempt = job.provisioning_attempt
        cap = job.max_provisioning_attempts
        prefix = (
            f'Held after attempt {attempt} of {cap}' if attempt and cap else 'Job held'
        )
        error = (job.error_message or 'provisioning stopped').split('Last error: ')[-1]
        error = error.split(' Use qdel')[0].rstrip('. ')
        return f'{prefix}: {error}.'
    if limit_info is not None:
        types = (
            ', '.join(job.params.instance_types or [])
            if job.params
            else 'requested capacity'
        )
        nodes = job.desired_nodes()
        limits = []
        if (
            limit_info.queue_current is not None
            and limit_info.queue_threshold is not None
        ):
            limits.append(
                f'queue limit {limit_info.queue_current}/{limit_info.queue_threshold}'
            )
        if (
            limit_info.group_current is not None
            and limit_info.group_threshold is not None
        ):
            limits.append(
                f'group limit {limit_info.group_current}/{limit_info.group_threshold}'
            )
        suffix = ', ' + ', '.join(limits) if limits else ''
        return (
            f'Waiting for {nodes} instances of {types or "requested capacity"}{suffix}.'
        )
    if job.error_message:
        return job.error_message
    if job.state in (SocaJobState.QUEUED, SocaJobState.WAITING):
        return 'Waiting for requested capacity.'
    return None
