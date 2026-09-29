"""Account state gates admission and queued work; executing jobs retain their allocation."""

from ideadatamodel import GetUserRequest, SocaJobState, exceptions, errorcodes


QUEUED_STATES = {SocaJobState.QUEUED, SocaJobState.HELD, SocaJobState.WAITING}


def user_enabled(context, username):
    try:
        user = context.accounts_client.get_user(GetUserRequest(username=username)).user
    except exceptions.SocaException as error:
        if error.error_code == errorcodes.AUTH_USER_NOT_FOUND:
            return False
        raise
    return user is not None and user.enabled is True


def require_enabled(context, username):
    if not user_enabled(context, username):
        raise exceptions.unauthorized_access('Job owner is disabled')


def delete_queued_job(context, job):
    # Re-read PBS because the queued snapshot may predate dispatch.
    current = context.scheduler.get_job(job.job_id)
    if current is not None and current.state in QUEUED_STATES:
        from ideascheduler.app.provisioning.lifecycle_events import SYSTEM_DELETION

        context.job_cache.sync(jobs=[current])
        context.scheduler.delete_job(job.job_id)
        context.job_cache.record_deleted_job(
            job=current,
            error_code=SYSTEM_DELETION,
            message='Deleted by the system because the job owner is disabled or no longer exists.',
        )
        queue = context.queue_profiles.get_provisioning_queue(
            queue_profile_name=job.queue_type
        )
        if queue is not None:
            queue.delete(job_id=job.job_id)


def sweep_disabled_jobs(context):
    enabled = {}
    for job in context.scheduler.list_jobs():
        if job.state not in QUEUED_STATES:
            continue
        try:
            if job.owner not in enabled:
                enabled[job.owner] = user_enabled(context, job.owner)
            if not enabled[job.owner]:
                delete_queued_job(context, job)
        except Exception:
            # One unavailable owner or PBS deletion must not starve unrelated queues.
            context.logger('accounts-guard').exception(
                f'Account sweep failed for job {job.job_id}'
            )
