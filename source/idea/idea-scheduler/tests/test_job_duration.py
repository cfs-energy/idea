"""
Test Cases for job duration computations used by cost estimation
"""

from ideasdk.config.soca_config import SocaConfig
import logging
from typing import List, Optional

import arrow
from ideadatamodel import (
    CloudFormationStack,
    EC2InstanceUnitPrice,
    HpcQueueProfile,
    SocaAnyPayload,
    SocaFSxLustreConfig,
    SocaJob,
    SocaJobExecutionHost,
    SocaJobParams,
    SocaMemory,
    SocaMemoryUnit,
)
from ideascheduler.app.aws.pricing_helper import PricingHelper
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

    # the fake client cannot answer the pricing api, so compute prices at zero and the
    # estimate is still built.
    compute = [
        item
        for item in job.estimated_bom_cost.line_items
        if item.title.startswith('Compute On-Demand')
    ]
    assert compute
    assert compute[0].unit_price.amount == 0.0


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


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))


class NoPriceAwsUtil:
    """the pricing api with no answer: no endpoint in this partition, or it failed"""

    @staticmethod
    def get_ec2_instance_type_unit_price(instance_type):
        return None


class NoPriceContext:
    def __init__(self):
        self.recorded = RecordingLogger()
        self._aws_util = NoPriceAwsUtil()

    def aws_util(self):
        return self._aws_util

    def logger(self, name=None):
        return self.recorded

    def config(self):
        return SocaConfig({})


def price_helper_without_pricing():
    context = NoPriceContext()
    job = SocaJob(
        job_id='14906',
        params=SocaJobParams(nodes=1, cpus=1, instance_types=['c5.large']),
    )
    return context, PricingHelper(context=context, job=job, total_time_secs=3600)


def test_unavailable_ec2_price_is_priced_at_zero_not_returned_as_none():
    """
    a None here would propagate to every .ondemand read and take the whole estimate away
    from the jobs page.
    """
    _, helper = price_helper_without_pricing()

    unit_price = helper.get_instance_type_unit_price()
    assert unit_price is not None
    assert unit_price.ondemand == 0.0
    assert unit_price.reserved == 0.0


def test_unavailable_ec2_price_is_warned_with_the_instance_type():
    """priced at zero silently would read as a free instance to whoever sees the total"""
    context, helper = price_helper_without_pricing()

    helper.get_instance_type_unit_price()

    warnings = ' '.join(context.recorded.warnings)
    assert 'c5.large' in warnings
    assert 'zero' in warnings


COST_ESTIMATION_CONFIG = {
    'scheduler': {
        'cost_estimation': {
            'ec2_boot_penalty_seconds': 300,
            'ebs_gp3_storage': 0.08,
            'ebs_io1_storage': 0.125,
            'provisioned_iops': 0.065,
            'fsx_lustre': 0.000194,
            'default_fsx_lustre_size': 1200,
        }
    }
}


class PricedAwsUtil:
    """the pricing api, or the published price list, with an answer"""

    @staticmethod
    def get_ec2_instance_type_unit_price(instance_type):
        return EC2InstanceUnitPrice(ondemand=0.085, reserved=0.054)


class BomContext(NoPriceContext):
    """
    the storage rates a bill of materials needs, so the only thing that can be missing
    from the estimate is the instance hour.
    """

    def __init__(self, aws_util=None):
        super().__init__()
        if aws_util is not None:
            self._aws_util = aws_util

    def config(self):
        return SocaConfig(COST_ESTIMATION_CONFIG)


def bom_helper(aws_util=None):
    job = SocaJob(
        job_id='14906',
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            instance_types=['c5.large'],
            root_storage_size=SocaMemory(value=10, unit=SocaMemoryUnit.GB),
            scratch_storage_size=SocaMemory.zero(SocaMemoryUnit.GB),
            fsx_lustre=SocaFSxLustreConfig(enabled=False),
        ),
    )
    return PricingHelper(
        context=BomContext(aws_util=aws_util), job=job, total_time_secs=3600
    )


def test_an_estimate_built_without_a_price_says_so():
    """
    the amount stays a number so anything summing amounts keeps working, and the flag
    is the only thing that separates it from a job that really cost nothing.
    """
    bom = bom_helper().compute_estimated_bom_cost()

    assert bom.price_unavailable is True
    assert bom.total.amount is not None


def test_an_estimate_built_from_a_real_price_is_not_flagged():
    """a priced job that came to nothing must keep reading as a priced job"""
    bom = bom_helper(aws_util=PricedAwsUtil()).compute_estimated_bom_cost()

    assert bom.price_unavailable is None
    assert bom.total.amount > 0


def test_every_price_property_survives_an_unavailable_price():
    """
    get_instance_type_unit_price is the one call the rest of this file reads .ondemand
    and .reserved off, so the guard has to hold for all of them.
    """
    _, helper = price_helper_without_pricing()

    assert helper.ec2_ondemand_price == 0
    assert helper.ec2_reserved_price == 0
    assert helper.ec2_spot_unit_price == 0.0
