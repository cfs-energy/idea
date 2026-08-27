"""
Test Cases for the cached ec2 dry run used by job-shared/batch queues
"""

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    errorcodes,
    exceptions,
    SocaJob,
    SocaJobParams,
)
from ideasdk.utils import Utils
from ideascheduler.app.provisioning import JobProvisioningUtil


def mock_job(job_group: str, instance_type: str = 'c5.large') -> SocaJob:
    # params must be complete enough to compute the capacity signature,
    # which is what the dry-run cache keys on.
    return SocaJob(
        cluster_name='idea-mock',
        job_id=Utils.short_uuid(),
        job_uid=Utils.short_uuid(),
        owner='mockuser',
        queue='normal',
        queue_type='normal',
        job_group=job_group,
        params=SocaJobParams(
            nodes=1,
            cpus=1,
            instance_types=[instance_type],
            instance_ami='ami-mockami1234567',
            base_os='amazonlinux2023',
            enable_ht_support=False,
        ),
    )


@pytest.fixture()
def dry_run_calls(monkeypatch):
    calls = []

    def mock_ec2_dry_run(self):
        calls.append(self.job.get_job_group())

    monkeypatch.setattr(JobProvisioningUtil, 'ec2_dry_run', mock_ec2_dry_run)
    return calls


def test_ec2_dry_run_cached_success_is_cached(context, dry_run_calls):
    job_group = Utils.short_uuid()
    for _ in range(3):
        provisioning_util = JobProvisioningUtil(
            context=context, jobs=[mock_job(job_group=job_group)]
        )
        provisioning_util.ec2_dry_run_cached()
    assert len(dry_run_calls) == 1


def test_ec2_dry_run_cached_per_capacity_signature(context, dry_run_calls):
    # same custom job_group with different instance types must not share
    # a cached verdict: the cache keys on the capacity signature.
    group = Utils.short_uuid()
    for instance_type in ('c5.large', 'm5.large', 'c5.large', 'm5.large'):
        provisioning_util = JobProvisioningUtil(
            context=context,
            jobs=[mock_job(job_group=group, instance_type=instance_type)],
        )
        provisioning_util.ec2_dry_run_cached()
    assert len(dry_run_calls) == 2

    # a different custom job_group with identical params shares the verdict
    provisioning_util = JobProvisioningUtil(
        context=context,
        jobs=[mock_job(job_group=Utils.short_uuid(), instance_type='c5.large')],
    )
    provisioning_util.ec2_dry_run_cached()
    assert len(dry_run_calls) == 2


def test_ec2_dry_run_cached_failure_is_cached(context, monkeypatch):
    calls = []

    def mock_ec2_dry_run(self):
        calls.append(self.job.get_job_group())
        raise exceptions.soca_exception(
            error_code=errorcodes.EC2_DRY_RUN_FAILED,
            message='EC2 dry run failed for instance_type: t3.micro',
        )

    monkeypatch.setattr(JobProvisioningUtil, 'ec2_dry_run', mock_ec2_dry_run)

    job_group = Utils.short_uuid()
    for _ in range(3):
        provisioning_util = JobProvisioningUtil(
            context=context, jobs=[mock_job(job_group=job_group)]
        )
        with pytest.raises(exceptions.SocaException) as exc_info:
            provisioning_util.ec2_dry_run_cached()
        assert exc_info.value.error_code == errorcodes.EC2_DRY_RUN_FAILED
        assert 't3.micro' in exc_info.value.message
    assert len(calls) == 1


def _dry_run_raising(code: str, calls: list):
    """A dry run that fails with a given EC2 error code, counting live attempts."""

    def mock_ec2_dry_run(self):
        calls.append(self.job.get_job_group())
        raise exceptions.SocaException(
            error_code=errorcodes.EC2_DRY_RUN_FAILED,
            message='EC2 dry run failed for instance_type: t3.micro',
            ref=ClientError(
                {'Error': {'Code': code, 'Message': 'boom'}}, 'RunInstances'
            ),
        )

    return mock_ec2_dry_run


def test_a_transient_failure_is_not_cached(context, monkeypatch):
    # the cache key carries no owner, so a throttle hit by one submitter would
    # otherwise reject every other submitter for the whole TTL window.
    calls = []
    monkeypatch.setattr(
        JobProvisioningUtil,
        'ec2_dry_run',
        _dry_run_raising('RequestLimitExceeded', calls),
    )

    job_group = Utils.short_uuid()
    for _ in range(3):
        provisioning_util = JobProvisioningUtil(
            context=context,
            jobs=[mock_job(job_group=job_group, instance_type='c5n.18xlarge')],
        )
        with pytest.raises(exceptions.SocaException):
            provisioning_util.ec2_dry_run_cached()

    assert len(calls) == 3, 'each submission must get its own live dry run'


def test_a_real_refusal_is_still_cached(context, monkeypatch):
    calls = []
    monkeypatch.setattr(
        JobProvisioningUtil,
        'ec2_dry_run',
        _dry_run_raising('InsufficientInstanceCapacity', calls),
    )

    job_group = Utils.short_uuid()
    for _ in range(3):
        provisioning_util = JobProvisioningUtil(
            context=context,
            jobs=[mock_job(job_group=job_group, instance_type='g4dn.xlarge')],
        )
        with pytest.raises(exceptions.SocaException):
            provisioning_util.ec2_dry_run_cached()

    assert len(calls) == 1


def test_a_transient_failure_does_not_poison_a_later_submission(context, monkeypatch):
    calls = []
    monkeypatch.setattr(
        JobProvisioningUtil, 'ec2_dry_run', _dry_run_raising('Throttling', calls)
    )
    job_group = Utils.short_uuid()
    provisioning_util = JobProvisioningUtil(
        context=context, jobs=[mock_job(job_group=job_group, instance_type='g5.xlarge')]
    )
    with pytest.raises(exceptions.SocaException):
        provisioning_util.ec2_dry_run_cached()

    # the throttle clears and the next submitter, sharing the signature, gets through
    monkeypatch.setattr(JobProvisioningUtil, 'ec2_dry_run', lambda self: None)
    provisioning_util = JobProvisioningUtil(
        context=context, jobs=[mock_job(job_group=job_group, instance_type='g5.xlarge')]
    )
    provisioning_util.ec2_dry_run_cached()


def test_the_dry_run_refusal_does_not_carry_aws_identifiers(context, monkeypatch):
    # the message reaches the submitter in the web ui, so account ids, arns and resource
    # ids are stripped at the raise site; the scheduler log keeps the original.
    def raise_unauthorized(**_):
        raise ClientError(
            {
                'Error': {
                    'Code': 'UnauthorizedOperation',
                    'Message': 'User: arn:aws:sts::123456789012:assumed-role/idea-role/sess '
                    'is not authorized to perform: ec2:RunInstances on '
                    'subnet-0abc1234def567890',
                }
            },
            'RunInstances',
        )

    monkeypatch.setattr(
        JobProvisioningUtil, 'ec2_dry_run_request', lambda self, instance_type: {}
    )
    context.aws().ec2().run_instances = raise_unauthorized

    provisioning_util = JobProvisioningUtil(
        context=context,
        jobs=[mock_job(job_group=Utils.short_uuid(), instance_type='hpc7g.16xlarge')],
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        provisioning_util.ec2_dry_run()

    message = exc_info.value.message
    assert '123456789012' not in message
    assert 'arn:aws:sts' not in message
    assert 'subnet-0abc1234def567890' not in message
    assert 'hpc7g.16xlarge' in message, 'the useful part of the message survives'
