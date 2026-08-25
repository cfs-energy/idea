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
Test Cases for surfacing a capacity failure on a waiting job

when EC2 has no capacity for the requested instance type, the auto-scaling group keeps
retrying and cloudformation waits out its whole stabilization window. the rejection is
reported only on the scaling activity, so without reading it the job owner is left with
a job that has been queued for hours and no reason for it anywhere.
"""

import logging
from threading import Event
from typing import Dict, List, Optional

import arrow
import pytest

from ideadatamodel import (
    JobUpdate,
    ProvisioningStatus,
    SocaJob,
    SocaJobParams,
    SocaJobState,
    errorcodes,
)
from ideadatamodel.aws.cloudformation_stack import CloudFormationStack
from ideadatamodel.aws.cloudformation_stack_resources import (
    CloudFormationStackResources,
)
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions
from ideasdk.aws import AWSUtil
from ideasdk.utils import Utils
from ideascheduler.app.provisioning import JobCache, JobProvisioningUtil
from ideascheduler.app.provisioning.job_monitor.job_monitor import JobMonitor
from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
    NodeHouseKeepingSession,
)

LOG_TAG = 'test_capacity_failure_visibility'
ASG_NAME = 'idea-mock-compute-ondemand-asg'
INSUFFICIENT_CAPACITY = (
    'Could not launch On-Demand Instances. InsufficientInstanceCapacity - '
    'We currently do not have sufficient r8g.24xlarge capacity in the '
    'Availability Zone you requested (us-east-2b). Launching EC2 instance failed.'
)


class MockScheduler:
    def __init__(self, jobs: Optional[List[SocaJob]] = None):
        self.jobs = jobs if jobs is not None else []
        self.job_attributes: Dict[str, Dict] = {}

    def list_jobs(self, queue=None, job_ids=None, **kwargs) -> List[SocaJob]:
        return self.jobs

    def set_job_attributes(self, job_id: str, attributes: Dict) -> bool:
        self.job_attributes[job_id] = attributes
        return True

    def hold_job(self, job_id: str) -> bool:
        return True

    def reset_job(self, job_id: str) -> bool:
        return True


class MockJobMonitor:
    def job_modified(self, job: SocaJob):
        pass


@pytest.fixture()
def job_cache(context):
    cache = JobCache(context=context)
    context.job_cache = cache
    # the job cache is a sqlite file shared by every test run. the housekeeper scans the
    # whole jobs table, so jobs left behind by other tests would drive its decisions.
    cache.get_jobs_table().delete()
    return cache


@pytest.fixture()
def mock_scheduler(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler
    context.job_monitor = MockJobMonitor()
    return scheduler


def mock_job() -> SocaJob:
    job_id = Utils.short_uuid()
    compute_stack = f'idea-mock-compute-ondemand-{job_id}'
    return SocaJob(
        cluster_name='idea-mock',
        job_id=job_id,
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='compute',
        state=SocaJobState.QUEUED,
        provisioned=True,
        provisioning_options=SocaJobProvisioningOptions(),
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            compute_stack=compute_stack,
            stack_id=f'arn:aws:cloudformation:us-east-1:123456789012:stack/{compute_stack}',
        ),
    )


def creating_stack(compute_stack: str, age_secs: int) -> CloudFormationStack:
    return CloudFormationStack(
        entry={
            'StackId': f'arn:aws:cloudformation:us-east-1:123456789012:stack/{compute_stack}',
            'StackName': compute_stack,
            'StackStatus': 'CREATE_IN_PROGRESS',
            'CreationTime': arrow.utcnow().shift(seconds=-age_secs).datetime,
        }
    )


def stack_resources(with_asg: bool) -> CloudFormationStackResources:
    resources = []
    if with_asg:
        resources.append(
            {
                'ResourceType': 'AWS::AutoScaling::AutoScalingGroup',
                'PhysicalResourceId': ASG_NAME,
            }
        )
    return CloudFormationStackResources(entry={'StackResources': resources})


def make_provisioning_util(context, job: SocaJob, with_asg: bool = True):
    util = JobProvisioningUtil(
        context=context, jobs=[job], logger=logging.getLogger(LOG_TAG)
    )
    util._stack_resources = stack_resources(with_asg=with_asg)
    return util


@pytest.fixture()
def scaling_activities(monkeypatch):
    """
    scripted describe_scaling_activities. `calls` records the groups that were queried,
    so a test can prove no call was made at all.
    """
    state = {'activities': [], 'calls': []}

    def describe(_self, auto_scaling_group_name: str, max_records: int = 10):
        state['calls'].append(auto_scaling_group_name)
        return state['activities']

    monkeypatch.setattr(AWSUtil, 'autoscaling_describe_scaling_activities', describe)
    return state


def test_capacity_failure_reason_reads_latest_failed_activity(
    context, scaling_activities
):
    """
    the reason must come from the most recent failed activity, not the newest activity:
    a successful scale-in after the rejection must not hide it.
    """
    scaling_activities['activities'] = [
        {'StatusCode': 'Successful', 'StatusMessage': 'terminated an instance'},
        {'StatusCode': 'Failed', 'StatusMessage': INSUFFICIENT_CAPACITY},
    ]
    job = mock_job()

    reason = make_provisioning_util(context, job).get_capacity_failure_reason()

    assert reason == INSUFFICIENT_CAPACITY
    assert scaling_activities['calls'] == [ASG_NAME]


def test_capacity_failure_reason_falls_back_to_description(context, scaling_activities):
    scaling_activities['activities'] = [
        {'StatusCode': 'Failed', 'Description': 'Launching a new EC2 instance'},
    ]
    job = mock_job()

    reason = make_provisioning_util(context, job).get_capacity_failure_reason()

    assert reason == 'Launching a new EC2 instance'


def test_capacity_failure_reason_none_while_launch_is_pending(
    context, scaling_activities
):
    """
    a launch still in flight is not a failure and must not be reported as one.
    """
    scaling_activities['activities'] = [
        {'StatusCode': 'PreInService', 'StatusMessage': 'launching an instance'},
    ]

    reason = make_provisioning_util(context, mock_job()).get_capacity_failure_reason()

    assert reason is None


def test_capacity_failure_reason_none_without_auto_scaling_group(
    context, scaling_activities
):
    """
    a spot fleet stack, or a stack whose group is not created yet, has no group to query.
    """
    reason = make_provisioning_util(
        context, mock_job(), with_asg=False
    ).get_capacity_failure_reason()

    assert reason is None
    assert scaling_activities['calls'] == []


def housekeeping_session(context) -> NodeHouseKeepingSession:
    return NodeHouseKeepingSession(context=context, logger=logging.getLogger(LOG_TAG))


def run_cleanup_with_creating_stack(
    context, monkeypatch, job: SocaJob, stack_age_secs: int
):
    monkeypatch.setattr(
        JobProvisioningUtil,
        'check_status',
        lambda _self: ProvisioningStatus.IN_PROGRESS,
    )
    monkeypatch.setattr(
        JobProvisioningUtil,
        'get_capacity_failure_reason',
        lambda _self: INSUFFICIENT_CAPACITY,
    )
    monkeypatch.setattr(
        AWSUtil,
        'cloudformation_describe_stack',
        lambda _self, stack_name: creating_stack(
            compute_stack=stack_name, age_secs=stack_age_secs
        ),
    )
    housekeeping_session(context).retry_provisioning_cleanup()


def test_housekeeper_publishes_capacity_failure_on_the_job(
    context, job_cache, mock_scheduler, monkeypatch
):
    """
    a stack that has been creating long enough to be suspect must have the reason it has
    no node recorded on the job, where the owner reads it with qstat -f.
    """
    job = mock_job()
    job_cache.sync(jobs=[job])

    run_cleanup_with_creating_stack(context, monkeypatch, job, stack_age_secs=300)

    cached_job = job_cache.get_job(job_id=job.job_id)
    assert cached_job.error_message is not None
    assert 'InsufficientInstanceCapacity' in cached_job.error_message
    # the reason reaches the job in the scheduler, where the owner reads it
    published = mock_scheduler.job_attributes[job.job_id]['error_message']
    assert published.startswith(errorcodes.CAPACITY_UNAVAILABLE)
    assert 'InsufficientInstanceCapacity' in published


def test_housekeeper_does_not_report_a_stack_that_is_still_young(
    context, job_cache, mock_scheduler, monkeypatch
):
    """
    a stack that has only just started is coming up normally. reporting a capacity
    failure for it would put an error on every job that provisions.
    """
    job = mock_job()
    job_cache.sync(jobs=[job])

    run_cleanup_with_creating_stack(context, monkeypatch, job, stack_age_secs=60)

    cached_job = job_cache.get_job(job_id=job.job_id)
    assert cached_job.error_message is None
    assert job.job_id not in mock_scheduler.job_attributes


def make_job_monitor(context) -> JobMonitor:
    job_monitor = JobMonitor.__new__(JobMonitor)
    job_monitor._context = context
    job_monitor._logger = context.logger('test-job-monitor')
    job_monitor._exit = Event()
    return job_monitor


def test_capacity_wait_is_cleared_when_the_job_starts(context, job_cache):
    """
    the wait reason is history once the job runs. left on the job it is re-attached to
    the finished job record, which reads as a job that failed.
    """
    job = mock_job()
    job_cache.sync(jobs=[job])
    job_cache.set_job_provisioning_error(
        job_id=job.job_id,
        error_code=errorcodes.CAPACITY_UNAVAILABLE,
        message=INSUFFICIENT_CAPACITY,
    )
    assert job_cache.get_job(job_id=job.job_id).error_message is not None

    job.state = SocaJobState.RUNNING
    context.scheduler = MockScheduler(jobs=[job])
    make_job_monitor(context)._sync_jobs(
        job_updates={JobUpdate(queue=job.queue, job_id=job.job_id)},
        job_type='running',
    )

    assert job_cache.get_job(job_id=job.job_id).error_message is None
    assert context.scheduler.job_attributes[job.job_id] == {'error_message': None}
