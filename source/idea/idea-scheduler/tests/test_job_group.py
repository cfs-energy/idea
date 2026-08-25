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
Test Cases for SocaJob.get_job_group / get_capacity_signature and the
shared capacity mismatch guard
"""

import pytest

from ideadatamodel import (
    constants,
    errorcodes,
    exceptions,
    SocaJob,
    SocaJobParams,
    SocaFSxLustreConfig,
    SocaMemory,
    SocaMemoryUnit,
    SocaScalingMode,
)
from ideadatamodel.aws.cloudformation_stack import CloudFormationStack

from ideascheduler import AppContext
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import ProvisionJobs


def make_job(**params_overrides) -> SocaJob:
    params = dict(
        nodes=2,
        cpus=4,
        instance_types=['c5.large'],
        instance_ami='ami-0123456789abcdef0',
        base_os='amazonlinux2',
        enable_ht_support=False,
    )
    params.update(params_overrides)
    return SocaJob(
        job_id='1',
        job_uid='mock-job-1',
        owner='mockuser',
        cluster_name='idea-mock',
        queue_type='compute',
        queue='normal',
        scaling_mode=SocaScalingMode.SINGLE_JOB,
        params=SocaJobParams(**params),
    )


def test_job_group_golden_ondemand():
    """
    hash stability: a fixed on-demand job must always produce the same group.
    a golden value change means every job group in a live cluster is re-keyed.
    """
    assert make_job().get_job_group() == 'g0f8c3136'
    assert make_job().get_capacity_signature() == 's0f8c3136dd6251f5'


def test_job_group_golden_spot():
    """
    hash stability for spot capacity
    """
    job = make_job(spot=True)
    assert job.get_job_group() == 'ga1fd2599'


def test_job_group_base_os_divergence():
    """
    jobs differing only in base_os must never share a job group
    """
    job_al2 = make_job(base_os='amazonlinux2')
    job_rhel = make_job(base_os='rhel8')
    assert job_al2.get_job_group() != job_rhel.get_job_group()


@pytest.mark.parametrize(
    'overrides',
    [
        {'enable_efa_support': True},
        {'enable_instance_store': True},
        {'enable_placement_group': True},
        {'enable_system_metrics': True},
        {'enable_anonymous_metrics': True},
        {
            'enable_scratch': True,
            'scratch_storage_size': SocaMemory(value=600, unit=SocaMemoryUnit.GB),
        },
        {
            'enable_scratch': True,
            'scratch_storage_size': SocaMemory(value=600, unit=SocaMemoryUnit.GB),
            'scratch_storage_iops': 3000,
        },
        {'fsx_lustre': SocaFSxLustreConfig(enabled=True, existing_fsx='fs-012345')},
        {'root_storage_size': SocaMemory(value=100, unit=SocaMemoryUnit.GB)},
        {'keep_ebs_volumes': True},
        {'instance_profile': 'arn:aws:iam::123456789012:instance-profile/custom'},
        {'security_groups': ['sg-0123456789abcdef0']},
        {'subnet_ids': ['subnet-0123456789abcdef0']},
        {'instance_ami': 'ami-0aaaaaaaaaaaaaaa0'},
        {'enable_ht_support': True},
        {'instance_types': ['c5.xlarge']},
    ],
)
def test_job_group_bootstrap_param_divergence(overrides):
    """
    any bootstrap or launch relevant parameter must partition capacity
    """
    assert make_job().get_job_group() != make_job(**overrides).get_job_group()


def test_job_group_missing_base_os_raises():
    job = make_job(base_os=None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        job.get_job_group()
    assert exc_info.value.error_code == errorcodes.SOCA_SCHEDULER_INVALID_JOB


def test_job_group_custom_override_wins():
    job = make_job()
    job.job_group = 'custom-mygroup'
    assert job.get_job_group() == 'custom-mygroup'


def test_capacity_signature_ignores_custom_job_group():
    """
    a shared custom job_group must not mask differing provisioning params
    """
    job_a = make_job(base_os='amazonlinux2')
    job_b = make_job(base_os='rhel8')
    job_a.job_group = 'custom-mygroup'
    job_b.job_group = 'custom-mygroup'
    assert job_a.get_job_group() == job_b.get_job_group()
    assert job_a.get_capacity_signature() != job_b.get_capacity_signature()


def test_capacity_signature_stable_for_equal_params():
    assert make_job().get_capacity_signature() == make_job().get_capacity_signature()


def mock_stack(job_group: str, job_queue: str, capacity_signature: str = None):
    tags = [
        {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': job_group},
        {'Key': constants.IDEA_TAG_JOB_QUEUE, 'Value': job_queue},
    ]
    if capacity_signature is not None:
        tags.append(
            {
                'Key': constants.IDEA_TAG_CAPACITY_SIGNATURE,
                'Value': capacity_signature,
            }
        )
    return CloudFormationStack(
        entry={
            'StackId': 'mock-stack-id',
            'StackStatus': 'CREATE_COMPLETE',
            'Tags': tags,
        }
    )


def make_provisioner(context: AppContext, job: SocaJob, stack) -> ProvisionJobs:
    provisioner = ProvisionJobs(context=context, jobs=[job], logger=context.logger())
    provisioner.provisioning_util._stack = stack
    return provisioner


def test_shared_capacity_guard_signature_mismatch(context: AppContext):
    """
    same job group with a different capacity signature must be rejected
    """
    job = make_job(base_os='rhel8')
    job.job_group = 'custom-mygroup'
    other = make_job(base_os='amazonlinux2')
    stack = mock_stack(
        job_group='custom-mygroup',
        job_queue='normal',
        capacity_signature=other.get_capacity_signature(),
    )
    provisioner = make_provisioner(context, job, stack)
    with pytest.raises(exceptions.SocaException) as exc_info:
        provisioner.provision_job_on_shared_capacity()
    assert exc_info.value.error_code == errorcodes.SHARED_CAPACITY_MISMATCH


def test_shared_capacity_guard_job_group_mismatch(context: AppContext):
    job = make_job()
    stack = mock_stack(job_group='g00000000', job_queue='normal')
    provisioner = make_provisioner(context, job, stack)
    with pytest.raises(exceptions.SocaException) as exc_info:
        provisioner.provision_job_on_shared_capacity()
    assert exc_info.value.error_code == errorcodes.SHARED_CAPACITY_MISMATCH


def test_shared_capacity_guard_signature_match_passes(context: AppContext):
    """
    matching group and signature must pass the mismatch guard.
    the mismatching queue tag proves the guard was cleared.
    """
    job = make_job()
    stack = mock_stack(
        job_group=job.get_job_group(),
        job_queue='other-queue',
        capacity_signature=job.get_capacity_signature(),
    )
    provisioner = make_provisioner(context, job, stack)
    with pytest.raises(exceptions.SocaException) as exc_info:
        provisioner.provision_job_on_shared_capacity()
    assert exc_info.value.error_code == errorcodes.SHARED_CAPACITY_INVALID_QUEUE


def test_shared_capacity_guard_legacy_stack_without_signature(context: AppContext):
    """
    stacks created before the signature tag existed are matched by job group alone
    """
    job = make_job()
    stack = mock_stack(job_group=job.get_job_group(), job_queue='other-queue')
    provisioner = make_provisioner(context, job, stack)
    with pytest.raises(exceptions.SocaException) as exc_info:
        provisioner.provision_job_on_shared_capacity()
    assert exc_info.value.error_code == errorcodes.SHARED_CAPACITY_INVALID_QUEUE
