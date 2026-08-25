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
Test Cases for the job provisioning retry cap
"""

from typing import Dict

import pytest

from ideadatamodel import (
    errorcodes,
    exceptions,
    SocaJob,
    SocaJobParams,
    SocaJobState,
)
from ideasdk.utils import Utils
from ideascheduler.app.provisioning import (
    JobCache,
    JobProvisioner,
    JobProvisioningUtil,
)
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    ProvisionJobsResult,
)


class MockScheduler:
    """records hold/attribute calls made by the retry cap"""

    def __init__(self):
        self.held_jobs = []
        self.job_attributes: Dict[str, Dict] = {}

    def hold_job(self, job_id: str) -> bool:
        self.held_jobs.append(job_id)
        return True

    def set_job_attributes(self, job_id: str, attributes: Dict) -> bool:
        self.job_attributes[job_id] = attributes
        return True


@pytest.fixture()
def job_cache(context):
    cache = JobCache(context=context)
    context.job_cache = cache
    return cache


@pytest.fixture()
def mock_scheduler(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler
    return scheduler


def mock_job(job_id: str) -> SocaJob:
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id,
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='normal',
        state=SocaJobState.QUEUED,
        params=SocaJobParams(nodes=1, cpus=1),
    )


def test_retry_count_increment_get_and_clear(context, job_cache):
    job_id = Utils.short_uuid()
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 0
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 1
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 2
    assert job_cache.increment_job_provisioning_retry(job_id=job_id) == 3
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 3
    job_cache.clear_job_provisioning_retries(job_id=job_id)
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 0


def test_retry_count_survives_provisioning_error_lifecycle(context, job_cache):
    """
    the provisioning error is cleared on every successful stack creation, but the retry
    count must survive it - otherwise a job that cycles failed stacks resets its own cap.
    """
    job_id = Utils.short_uuid()
    job_cache.increment_job_provisioning_retry(job_id=job_id)
    job_cache.set_job_provisioning_error(
        job_id=job_id, error_code='MOCK_ERROR', message='mock error'
    )
    job_cache.clear_job_provisioning_error(job_id=job_id)
    assert job_cache.get_job_provisioning_retry_count(job_id=job_id) == 1


def test_track_provisioning_failure_below_cap(context, job_cache, mock_scheduler):
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is False
    )
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is False
    )
    assert len(mock_scheduler.held_jobs) == 0
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 2


def test_track_provisioning_failure_without_a_cap_never_holds(
    context, job_cache, mock_scheduler
):
    """max_provisioning_retries of 0 disables the cap, as the job monitor reads it"""
    context.config().put('scheduler.job_provisioning.max_provisioning_retries', 0)
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    for _ in range(5):
        assert (
            provisioning_util.track_provisioning_failure(
                job=job, message='mock failure'
            )
            is False
        )

    assert mock_scheduler.held_jobs == []
    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 5


def test_track_provisioning_failure_exhausts_and_holds(
    context, job_cache, mock_scheduler
):
    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])

    for _ in range(2):
        assert (
            provisioning_util.track_provisioning_failure(
                job=job, message='mock failure'
            )
            is False
        )
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is True
    )

    # job is held in the scheduler and the reason is visible on the job
    assert mock_scheduler.held_jobs == [job.job_id]
    assert 'error_message' in mock_scheduler.job_attributes[job.job_id]

    # final disposition is recorded in the job cache
    cached_job = job_cache.get_job(job_id=job.job_id)
    assert cached_job is not None
    assert cached_job.state == SocaJobState.HELD
    assert cached_job.error_message is not None
    assert 'provisioning failed 3 times' in cached_job.error_message


def test_track_provisioning_failure_hold_failure_is_not_fatal(
    context, job_cache, mock_scheduler
):
    def failing_hold(job_id: str) -> bool:
        raise exceptions.soca_exception(
            error_code=errorcodes.SCHEDULER_ERROR, message='qhold failed'
        )

    mock_scheduler.hold_job = failing_hold

    job = mock_job(job_id=Utils.short_uuid())
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])
    for _ in range(2):
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
    assert (
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        is True
    )


def make_provisioner(context) -> JobProvisioner:
    provisioner = JobProvisioner.__new__(JobProvisioner)
    provisioner._context = context
    provisioner._logger = context.logger('test-job-provisioner')
    return provisioner


def test_track_failed_jobs_ignores_wait_states(context, job_cache, mock_scheduler):
    provisioner = make_provisioner(context)
    job = mock_job(job_id=Utils.short_uuid())

    # stack still in progress - no error code, no exception
    result = ProvisionJobsResult(status=False)
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]

    # waiting on licenses
    result = ProvisionJobsResult(
        status=False, error_code=errorcodes.NOT_ENOUGH_LICENSES
    )
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]

    assert job_cache.get_job_provisioning_retry_count(job_id=job.job_id) == 0
    assert len(mock_scheduler.held_jobs) == 0


def test_track_failed_jobs_counts_failures_and_holds(
    context, job_cache, mock_scheduler
):
    provisioner = make_provisioner(context)
    job = mock_job(job_id=Utils.short_uuid())
    result = ProvisionJobsResult(
        status=False,
        error_code=errorcodes.GENERAL_ERROR,
        exception=exceptions.soca_exception(
            error_code=errorcodes.GENERAL_ERROR, message='mock provisioning failure'
        ),
    )

    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == [job]
    # third failed cycle exhausts the cap - job is held and must not be re-queued
    assert provisioner._track_failed_jobs(jobs=[job], result=result) == []
    assert mock_scheduler.held_jobs == [job.job_id]
