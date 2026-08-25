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

"""
waiting signals for a job that has not started yet.

two facts the platform already knows and never showed the owner: which provisioning
attempt the job is on, and which queue limit is holding it. neither is a queue position
or a start-time estimate - the data for those does not exist, because PBS models a fixed
node pool while IDEA creates nodes on demand.

elapsed queue time is deliberately not here: the client already holds queue_time and
start_time and derives the wait from them, so adding a server-computed copy would put two
numbers for the same thing on one page.

the signals are per-job and per-request. they are attached to the SocaJob returned by the
owner-scoped active jobs listing and are never persisted; the blocking limit is reported
as a type only, because its threshold and current usage are cluster-wide values.
"""

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


def get_blocking_limit_type(
    context: 'ideascheduler.AppContext', job: SocaJob
) -> Optional[str]:
    """
    the limit type holding the job's queue profile, or None. type only - the threshold
    and the current usage describe the whole queue, not this owner.
    """
    try:
        provisioning_queue = context.queue_profiles.get_provisioning_queue(
            queue_profile_name=job.queue_type
        )
    except Exception:  # noqa - a deleted or renamed queue profile must not fail the read
        return None
    if provisioning_queue is None:
        return None
    if not provisioning_queue.is_queue_blocked_by_limits():
        return None
    limit_info = provisioning_queue.get_limit_info()
    if limit_info is None:
        return None
    return limit_info.limit_type


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
        if not is_awaiting_provisioning(job=job):
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
            continue

        job.blocking_limit_type = get_blocking_limit_type(context=context, job=job)
