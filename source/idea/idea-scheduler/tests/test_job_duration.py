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
Test Cases for job duration computations used by cost estimation
"""

from ideasdk.config.soca_config import SocaConfig
import logging
from typing import List, Optional

import arrow
from ideadatamodel import (
    CloudFormationStack,
    HpcQueueProfile,
    SocaAnyPayload,
    SocaFSxLustreConfig,
    SocaJob,
    SocaJobExecutionHost,
    SocaJobParams,
    SocaMemory,
    SocaMemoryUnit,
)
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
    ProcessFinishedJob,
)
from ideascheduler.app.scheduler.openpbs import OpenPBSJob

# 50 hours, 47 minutes, 18 seconds. mod 86400 = 10038 secs, what a day-remainder truncation stores
MULTI_DAY_SECONDS = 182838

QUEUE_PROFILE = HpcQueueProfile(name='mock-queue-profile')


class MockJobCache:
    def __init__(self):
        self.deleted_execution_hosts: List[str] = []

    @staticmethod
    def get_job_execution_hosts(job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        return []

    def delete_job_execution_hosts(self, job_id: str):
        self.deleted_execution_hosts.append(job_id)


def build_finished_job_processor(job: SocaJob) -> ProcessFinishedJob:
    logger = logging.getLogger('test_job_duration')
    return ProcessFinishedJob(
        context=SocaAnyPayload(job_cache=MockJobCache()),
        logger=logger,
        job=job,
        job_export_logger=logger,
    )


def build_pbs_job(**kwargs) -> OpenPBSJob:
    attributes = {
        'id': '14906.ip-10-0-0-9',
        'Job_Owner': 'mockuser@ip-10-0-0-9',
        'queue': 'normal',
        'job_state': 'F',
        'Resource_List': {
            'select': '1:ncpus=1',
            'nodect': 1,
            'ncpus': 1,
            'instance_type': 'c5.large',
            'walltime': '96:00:00',
        },
    }
    attributes.update(kwargs)
    return OpenPBSJob(**attributes)


def test_job_total_time_seconds_multi_day_delta_is_exact():
    """
    total time of a job running for more than 24 hours must not be truncated to the
    sub-day remainder of the timedelta
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = SocaJob(
        job_id='14906',
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=MULTI_DAY_SECONDS).datetime,
        params=SocaJobParams(walltime='96:00:00'),
    )

    assert job.get_total_time_seconds() == MULTI_DAY_SECONDS


def test_job_total_time_seconds_falls_back_to_walltime():
    """
    a job with no execution timestamps at all is still estimated using the requested walltime
    """
    job = SocaJob(job_id='14906', params=SocaJobParams(walltime='96:00:00'))

    assert job.get_total_time_seconds() == 96 * 60 * 60


def test_finished_job_multi_day_execution_time_is_exact():
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = SocaJob(
        job_id='14906',
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=MULTI_DAY_SECONDS).datetime,
        params=SocaJobParams(walltime='96:00:00'),
    )

    processor = build_finished_job_processor(job=job)
    processor.apply_job_execution_context()

    assert job.total_time_secs == MULTI_DAY_SECONDS
    assert job.get_total_time_seconds() == MULTI_DAY_SECONDS


def test_finished_job_terminated_before_start_is_not_priced_at_walltime():
    """
    job deleted (qdel / web portal) after capacity was provisioned, but before it started
    executing. cost must not be revised to the requested walltime.
    """
    job = SocaJob(job_id='14906', params=SocaJobParams(walltime='96:00:00'))

    processor = build_finished_job_processor(job=job)
    processor.apply_job_execution_context()

    assert job.total_time_secs == 0
    assert job.get_total_time_seconds() == 0


class RaisingJobCache:
    """job cache whose execution-hosts fetch fails, simulating a transient cache error."""

    @staticmethod
    def get_job_execution_hosts(job_id: str):
        raise RuntimeError('job cache unavailable')

    def delete_job_execution_hosts(self, job_id: str):
        pass


def test_finished_job_duration_survives_execution_hosts_error():
    """
    a job-cache error while fetching execution hosts must not leave total_time_secs unset:
    that would let get_total_time_seconds() substitute the requested walltime and over-bill
    the finished job. duration is computed independently of the cache.
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = SocaJob(
        job_id='14906',
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=MULTI_DAY_SECONDS).datetime,
        params=SocaJobParams(walltime='96:00:00'),
    )
    logger = logging.getLogger('test_job_duration')
    processor = ProcessFinishedJob(
        context=SocaAnyPayload(job_cache=RaisingJobCache()),
        logger=logger,
        job=job,
        job_export_logger=logger,
    )

    processor.apply_job_execution_context()

    # elapsed time, not the 96h (345600s) requested walltime
    assert job.total_time_secs == MULTI_DAY_SECONDS
    assert job.get_total_time_seconds() == MULTI_DAY_SECONDS


def test_pbs_finished_job_end_time_from_resources_used(context):
    """
    job that ran to completion for more than 24 hours: end time is derived from
    resources_used.walltime
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    pbs_job = build_pbs_job(
        stime=str(start_time.int_timestamp),
        mtime=str(start_time.shift(seconds=MULTI_DAY_SECONDS + 30).int_timestamp),
        resources_used={'walltime': '50:47:18'},
    )

    job = pbs_job.as_soca_job(context=context, queue_profile=QUEUE_PROFILE)

    assert job.start_time == start_time.datetime
    assert job.get_total_time_seconds() == MULTI_DAY_SECONDS


def test_pbs_terminated_job_end_time_from_mtime(context):
    """
    job terminated via qdel reports no resources_used.walltime. end time falls back to the
    time the job entered the finished state.
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    end_time = start_time.shift(seconds=MULTI_DAY_SECONDS)
    pbs_job = build_pbs_job(
        stime=str(start_time.int_timestamp), mtime=str(end_time.int_timestamp)
    )

    job = pbs_job.as_soca_job(context=context, queue_profile=QUEUE_PROFILE)

    assert job.end_time == end_time.datetime
    assert job.get_total_time_seconds() == MULTI_DAY_SECONDS


def test_pbs_terminated_job_before_start_has_no_end_time(context):
    """
    job deleted before it started executing has no start time. mtime must not be used as
    an execution end time.
    """
    pbs_job = build_pbs_job(mtime=str(arrow.utcnow().int_timestamp))

    job = pbs_job.as_soca_job(context=context, queue_profile=QUEUE_PROFILE)

    assert job.start_time is None
    assert job.end_time is None


def test_finished_job_no_end_time_measured_to_now_not_walltime():
    """
    finished job with a start time but no derivable end time (missing/unparsable
    mtime, clock skew, non-F qstat state): measure to now, never the requested
    walltime
    """
    job = SocaJob(
        job_id='14906',
        start_time=arrow.utcnow().shift(hours=-2).datetime,
        params=SocaJobParams(walltime='96:00:00'),
    )

    processor = build_finished_job_processor(job=job)
    processor.apply_job_execution_context()

    assert job.total_time_secs is not None
    assert 7100 <= job.total_time_secs <= 7300
    assert job.get_total_time_seconds() != 96 * 60 * 60


class MockMetrics:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))

        return record


def test_jobs_provisioning_duration_metric_is_positive():
    """
    jobs_provisioning_duration is time from provisioning to running: start_time
    minus provisioning_time, never the inverse (which is negative by construction)
    """
    start_time = arrow.get('2026-08-01T00:02:05Z')
    job = SocaJob(
        job_id='14906',
        provisioning_time=start_time.shift(seconds=-125).datetime,
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=MULTI_DAY_SECONDS).datetime,
        queue_time=start_time.shift(seconds=-155).datetime,
        params=SocaJobParams(walltime='96:00:00'),
    )

    metrics = MockMetrics()
    logger = logging.getLogger('test_job_duration')
    processor = ProcessFinishedJob(
        context=SocaAnyPayload(job_cache=MockJobCache(), metrics=metrics),
        logger=logger,
        job=job,
        job_export_logger=logger,
    )
    processor.publish_job_metrics()

    recorded = dict(
        (name, kwargs) for name, kwargs in metrics.calls if 'duration_secs' in kwargs
    )
    assert recorded['jobs_provisioning_duration']['duration_secs'] == 125
    assert recorded['jobs_running_duration']['duration_secs'] == MULTI_DAY_SECONDS


def test_finished_job_bom_cost_end_to_end(context):
    """
    drive the pricing helper end to end: a missing cost_estimation config key or a
    pricing regression leaves estimated_bom_cost None (the exception is swallowed)
    """
    start_time = arrow.get('2026-08-01T00:00:00Z')
    job = SocaJob(
        job_id='14906',
        cluster_name='idea-mock',
        queue='normal',
        queue_type='normal',
        start_time=start_time.datetime,
        end_time=start_time.shift(seconds=MULTI_DAY_SECONDS).datetime,
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            instance_types=['c5.large'],
            walltime='96:00:00',
            root_storage_size=SocaMemory(value=10, unit=SocaMemoryUnit.GB),
            scratch_storage_size=SocaMemory.zero(SocaMemoryUnit.GB),
            fsx_lustre=SocaFSxLustreConfig(enabled=False),
        ),
    )
    # execution context needs the (mocked) job cache; pricing needs the real context
    build_finished_job_processor(job=job).apply_job_execution_context()
    logger = logging.getLogger('test_job_duration')
    processor = ProcessFinishedJob(
        context=context, logger=logger, job=job, job_export_logger=logger
    )
    processor.compute_and_apply_estimated_costs()

    assert job.total_time_secs == MULTI_DAY_SECONDS
    assert job.estimated_bom_cost is not None
    assert job.estimated_bom_cost.total is not None


def test_provisioning_timeout_after_24_hours():
    """
    a stack stuck in provisioning for more than 24 hours must still time out, even when the
    sub-day remainder of its age is below the timeout
    """
    stack = CloudFormationStack(
        entry={'CreationTime': arrow.utcnow().shift(hours=-24, minutes=-5).datetime}
    )

    provisioning_util = JobProvisioningUtil(
        context=SocaAnyPayload(config=lambda: SocaConfig({})),
        jobs=[SocaJob(job_id='14906')],
        logger=logging.getLogger('test_job_duration'),
    )
    provisioning_util._stack = stack

    assert provisioning_util.stack_provisioning_timeout_secs == 1800
    assert provisioning_util.is_provisioning_timeout() is True
