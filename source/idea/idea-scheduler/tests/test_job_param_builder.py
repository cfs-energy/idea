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
Test Cases for SocaJobBuilder
"""

from typing import Dict, Optional

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    constants,
    SocaBaseModel,
    JobValidationResult,
    JobValidationResultEntry,
    SocaJob,
    SocaJobParams,
    SocaJobProvisioningOptions,
    HpcQueueProfile,
    SocaQueueManagementParams,
    SocaSpotAllocationStrategy,
)
from ideascheduler.app.scheduler import SocaJobBuilder
from ideascheduler import SchedulerAppContext
from ideasdk.utils import Utils
from pydantic import Field


class BuildAndValidateResult(SocaBaseModel):
    job_params: Optional[SocaJobParams] = Field(default=None)
    validation_result: Optional[JobValidationResult] = Field(default=None)
    provisioning_options: Optional[SocaJobProvisioningOptions] = Field(default=None)
    success: Optional[bool] = Field(default=None)


def build_and_validate(
    context: SchedulerAppContext,
    params: Dict,
    queue_profile: Optional[HpcQueueProfile] = None,
    stack_uuid: str = None,
    job_id: Optional[str] = None,
) -> BuildAndValidateResult:
    print()

    builder = SocaJobBuilder(
        context=context,
        params=params,
        queue_profile=queue_profile,
        stack_uuid=stack_uuid,
        job_id=job_id,
    )

    validation_result = builder.validate()

    job_params = None
    provisioning_options = None

    if validation_result.is_valid():
        job_params, provisioning_options = builder.build()
        mock_job = SocaJob(
            name='mock-job',
            job_id='1',
            job_uid=Utils.short_uuid(),
            params=job_params,
            provisioning_options=provisioning_options,
        )
        print(Utils.to_yaml(mock_job))
    else:
        print(validation_result)

    return BuildAndValidateResult(
        validation_result=validation_result,
        job_params=job_params,
        provisioning_options=provisioning_options,
        success=validation_result.is_valid(),
    )


def get_validation_messages(result: BuildAndValidateResult) -> list:
    return [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]


def test_job_builder_basic(context):
    """
    basic job builder test case
    """
    result = build_and_validate(
        context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'}
    )
    assert result.success is True
    assert result.job_params.base_os is not None
    assert result.job_params.instance_ami is not None
    assert result.job_params.instance_ami == 'ami-mockclustersettings'
    assert result.job_params.instance_profile is not None
    assert result.job_params.instance_types is not None
    assert len(result.job_params.instance_types) == 1


def test_job_builder_basic_invalid_nodes(context):
    """
    test invalid no. of nodes
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': -100,
            'cpus': 2,
            'instance_type': 't3.micro',
            'ht_support': 'true',
        },
    )
    assert result.success is False


def test_job_builder_basic_invalid_cpus(context):
    """
    test invalid no. of cpus
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': -200,
            'instance_type': 't3.micro',
            'ht_support': 'true',
        },
    )
    assert result.success is False


def test_job_builder_basic_invalid_nodes_cpus(context):
    """
    test invalid no. of nodes + cpus
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': -100,
            'cpus': -200,
            'instance_type': 't3.micro',
            'ht_support': 'true',
        },
    )
    assert result.success is False
    assert len(result.validation_result.results) == 2


def test_job_builder_basic_invalid_memory_1(context):
    """
    test invalid memory (negative value)
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'memory': -1024},
    )
    assert result.success is False


def test_job_builder_basic_invalid_memory_2(context):
    """
    test invalid memory (larger than available in instance type)
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'memory': 2048},
    )
    assert result.success is False


def test_job_builder_instance_type_valid(context):
    """
    valid instance type, verify all instance type related attributes are populated
    """
    result = build_and_validate(
        context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}
    )
    assert result.success is True
    assert result.job_params.instance_types is not None
    assert len(result.job_params.instance_types) == 1
    assert result.job_params.instance_types[0] == 'c5.large'
    assert result.provisioning_options.instance_types is not None
    assert len(result.provisioning_options.instance_types) == 1
    assert result.provisioning_options.instance_types[0].name == 'c5.large'


def test_job_builder_instance_type_invalid(context):
    """
    invalid instance type
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'invalid-instance-type'},
    )
    assert result.success is False


def test_job_builder_instance_type_efa_valid(context):
    """
    EFA is enabled and instance type supports EFA
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            'efa_support': 'true',
        },
    )
    assert result.success is True


def test_job_builder_instance_type_efa_invalid(context):
    """
    EFA is enabled, but instance type does not support efa
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'efa_support': 'true',
        },
    )
    assert result.success is False


def test_job_builder_instance_type_efa_default_valid(context):
    """
    EFA is enabled by default in queue profile and instance type supports EFA
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            # Note: no efa_support parameter specified
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(enable_efa_support=True),
        ),
    )
    assert result.success is True


def test_job_builder_instance_type_efa_default_invalid(context):
    """
    EFA is enabled by default in queue profile but instance type does not support EFA
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            # Note: no efa_support parameter specified
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(enable_efa_support=True),
        ),
    )
    assert result.success is False
    assert (
        result.validation_result.results[0].error_code
        == constants.JOB_PARAM_ENABLE_EFA_SUPPORT
    )


def test_job_builder_instance_type_allowed_valid(context):
    """
    allowed instance types are configured in queue profile
    instance type provided is part of allowed instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_types=['c5.large']
            ),
        ),
    )
    assert result.success is True


def test_job_builder_instance_type_allowed_invalid(context):
    """
    allowed instance types are configured in queue profile
    instance type provided is NOT part of allowed instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.xlarge'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_types=['c5.large']
            ),
        ),
    )
    assert result.success is False
    assert (
        result.validation_result.results[0].error_code
        == constants.JOB_PARAM_INSTANCE_TYPES
    )


def test_job_builder_instance_type_excluded_valid(context):
    """
    excluded instance types are configured in queue profile
    instance type provided is NOT part of excluded instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                excluded_instance_types=['t3.micro']
            ),
        ),
    )
    assert result.success is True


def test_job_builder_instance_type_excluded_invalid(context):
    """
    excluded instance types are configured in queue profile
    instance type provided is part of excluded instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                excluded_instance_types=['t3.micro']
            ),
        ),
    )
    assert result.success is False
    assert (
        result.validation_result.results[0].error_code
        == constants.JOB_PARAM_INSTANCE_TYPES
    )


def test_job_builder_instance_type_allowed_excluded_valid(context):
    """
    both allowed and excluded instance types are configured in queue profile
    a valid instance type is provided that is part of allowed instance types and is NOT part of excluded instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_types=['c5.large'],
                excluded_instance_types=['t3.micro'],
            ),
        ),
    )
    assert result.success is True


def test_job_builder_instance_type_allowed_excluded_invalid(context):
    """
    both allowed and excluded instance types are configured in queue profile
    a valid instance type is provided that is NOT part of allowed instance types and is part of excluded instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_types=['c5.large'],
                excluded_instance_types=['t3.micro'],
            ),
        ),
    )
    assert result.success is False


def test_job_builder_instance_type_multiple_ht_disabled(context):
    """
    multiple instance types (hyper-threading disabled)
    when hyper-threading is disabled, no. of cpus should be equal to no. of threads per core for 1st instance type
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large+c5.xlarge'},
    )
    assert result.success is True
    assert result.job_params.instance_types is not None
    assert len(result.job_params.instance_types) == 2
    assert result.job_params.instance_types[0] == 'c5.large'
    assert result.job_params.instance_types[1] == 'c5.xlarge'
    assert result.job_params.enable_ht_support is False
    assert result.provisioning_options.instance_types is not None
    assert len(result.provisioning_options.instance_types) == 2

    instance_type_option1 = result.provisioning_options.instance_types[0]
    instance_type_option2 = result.provisioning_options.instance_types[1]

    assert instance_type_option1.name == 'c5.large'
    assert instance_type_option2.name == 'c5.xlarge'
    assert instance_type_option1.threads_per_core == 1
    assert instance_type_option2.threads_per_core == 1
    assert instance_type_option1.weighted_capacity == 1
    assert instance_type_option2.weighted_capacity == 2


def test_job_builder_instance_type_multiple_ht_enabled(context):
    """
    multiple instance types (hyper-threading enabled)
    when hyper-threading is enabled, no. of cpus should be equal to no. of vCPUs for the 1st instance type.
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 2,
            'instance_type': 'c5.large+c5.xlarge',
            'ht_support': 'true',
        },
    )
    assert result.success is True
    assert result.job_params.instance_types is not None
    assert len(result.job_params.instance_types) == 2
    assert result.job_params.instance_types[0] == 'c5.large'
    assert result.job_params.instance_types[1] == 'c5.xlarge'
    assert result.job_params.enable_ht_support is True
    assert result.provisioning_options.instance_types is not None
    assert len(result.provisioning_options.instance_types) == 2

    instance_type_option1 = result.provisioning_options.instance_types[0]
    instance_type_option2 = result.provisioning_options.instance_types[1]

    assert instance_type_option1.name == 'c5.large'
    assert instance_type_option2.name == 'c5.xlarge'
    assert instance_type_option1.threads_per_core == 2
    assert instance_type_option2.threads_per_core == 2
    assert instance_type_option1.weighted_capacity == 2
    assert instance_type_option2.weighted_capacity == 4


def test_job_builder_ht_support_enabled_valid(context):
    """
    when hyper-threading is enabled, no. of cpus should be equal to no. of vCPUs
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 2,
            'instance_type': 't3.micro',
            'ht_support': 'true',
        },
    )
    assert result.success is True


def test_job_builder_ht_support_disabled(context):
    """
    when hyper-threading is disabled, no. of cpus should be equal to no. of threads per core
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'ht_support': 'false',
        },
    )
    assert result.success is True


def test_job_builder_ht_support_disabled_invalid_cpus(context):
    """
    when hyper-threading is disabled, requesting more no. of cpus than no. of threads per core should fail
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 2,
            'instance_type': 't3.micro',
            'ht_support': 'false',
        },
    )
    assert result.success is False


def test_job_builder_basic_invalid_no_of_cpus(context):
    """
    no. of cpus requested are not available for the given instance type
    """
    result = build_and_validate(
        context=context, params={'nodes': 1, 'cpus': 10, 'instance_type': 't3.micro'}
    )
    assert result.success is False


def test_job_builder_base_os_amazonlinux2_rejected(context):
    """
    base os (amazonlinux2) reached end-of-life and must be rejected
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': constants.OS_AMAZONLINUX2,
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == constants.JOB_PARAM_BASE_OS


def test_job_builder_base_os_amazonlinux2023(context):
    """
    base os (amazonlinux2023)
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': constants.OS_AMAZONLINUX2023,
        },
    )
    assert result.success is True
    assert result.job_params.base_os == constants.OS_AMAZONLINUX2023


def test_job_builder_base_os_invalid(context):
    """
    base os (invalid)
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'invalid-base-os',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == constants.JOB_PARAM_BASE_OS


def test_job_builder_instance_ami_from_queue_profile(context):
    """
    custom instance_ami provided in queue profile.
    must be different from the one configured in cluster settings.
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_ami='ami-amazonlinux2023', base_os=constants.OS_AMAZONLINUX2023
            ),
        ),
    )
    assert result.success is True
    assert result.job_params.instance_ami == 'ami-amazonlinux2023'
    assert result.job_params.base_os == constants.OS_AMAZONLINUX2023


def test_job_builder_instance_ami_from_params(context):
    """
    custom instance_ami provided via job params
    must be different from the one configured in cluster settings and queue profile
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'instance_ami': 'ami-rhel8',
            'base_os': constants.OS_RHEL8,
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_ami='ami-amazonlinux2023', base_os=constants.OS_AMAZONLINUX2023
            ),
        ),
    )
    assert result.success is True
    assert result.job_params.instance_ami == 'ami-rhel8'
    assert result.job_params.base_os == constants.OS_RHEL8


def test_job_builder_force_ri(context):
    """
    force reserved instances
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'force_ri': 'true'},
    )
    assert result.success is True
    assert result.job_params.force_reserved_instances is True


def test_job_builder_spot_price_auto(context):
    """
    spot price provided as auto
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
        },
    )
    assert result.success is True
    assert result.job_params.spot is True
    assert result.job_params.spot_price is None


def test_job_builder_spot_price_zero(context):
    """
    spot price provided as 0 should fail.
    spot price can either be auto or a non-zero/+ve dollar amount
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'spot_price': '0'},
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_price'


def test_job_builder_spot_price_invalid(context):
    """
    spot price provided as invalid value should fail.
    spot price can either be auto or a non-zero/+ve dollar amount
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'invalid-value',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_price'


def test_job_builder_spot_price_negative(context):
    """
    spot price provided as invalid value should fail.
    spot price can either be auto or a non-zero/+ve dollar amount
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': '-123',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_price'


def test_job_builder_spot_price_amount(context):
    """
    spot fleet - spot price provided as a valid dollar amount
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': '0.5',
        },
    )
    assert result.success is True
    assert result.job_params.spot is True
    assert result.job_params.spot_price is not None
    assert result.job_params.spot_price.amount == 0.5


def test_job_builder_spot_allocation_count_valid_nodes(context):
    """
    spot allocation count - is valid and less than total no. of nodes required to be provisioned
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_count': 1,
        },
    )
    assert result.success is True
    assert result.job_params.spot is True
    assert result.job_params.spot_price is None


def test_job_builder_spot_allocation_count_no_spot_price(context):
    """
    spot allocation count is provided without spot price parameter
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_allocation_count': 1,
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_allocation_count'


def test_job_builder_spot_allocation_count_greater_than_total_nodes(context):
    """
    spot allocation count - is not valid as total no. of nodes required to be provisioned must be greater than spot allocation count
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_count': 1,
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_allocation_count'


def test_job_builder_spot_allocation_strategy_valid(context):
    """
    valid spot allocation strategy and spot params
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_strategy': 'capacity-optimized',
        },
    )
    assert result.success is True
    assert (
        result.job_params.spot_allocation_strategy
        == SocaSpotAllocationStrategy.CAPACITY_OPTIMIZED
    )

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_strategy': 'lowest-price',
        },
    )
    assert result.success is True
    assert (
        result.job_params.spot_allocation_strategy
        == SocaSpotAllocationStrategy.LOWEST_PRICE
    )

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_strategy': 'diversified',
        },
    )
    assert result.success is True
    assert (
        result.job_params.spot_allocation_strategy
        == SocaSpotAllocationStrategy.DIVERSIFIED
    )


def test_job_builder_spot_allocation_strategy_invalid(context):
    """
    invalid spot allocation strategy
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'spot_price': 'auto',
            'spot_allocation_strategy': 'invalid',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'spot_allocation_strategy'


def test_job_builder_subnet_id_valid(context):
    """
    custom subnet id
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'subnet_id': 'subnet-custom1',
        },
    )
    assert result.success is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == 1
    assert result.job_params.subnet_ids[0] == 'subnet-custom1'


def test_job_builder_subnet_id_multiple_valid(context):
    """
    custom subnet id - multiple
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'subnet_id': 'subnet-custom1+subnet-custom2',
        },
    )
    assert result.success is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == 2
    assert result.job_params.subnet_ids[0] == 'subnet-custom1'
    assert result.job_params.subnet_ids[1] == 'subnet-custom2'


def test_job_builder_subnet_id_count_valid(context):
    """
    subnet id is provided as valid integer count
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    max_subnets = len(private_subnets)
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'subnet_id': max_subnets,
        },
    )
    assert result.success is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == max_subnets
    for i, subnet_id in enumerate(private_subnets):
        assert result.job_params.subnet_ids[i] == subnet_id


def test_job_builder_subnet_id_count_invalid_1(context):
    """
    subnet id is provided as an invalid -ve integer count
    allowed max subnets = len(cluster.network.private_subnets)
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'subnet_id': '-123',
        },
    )
    assert result.success is False


def test_job_builder_subnet_id_count_invalid_2(context):
    """
    subnet id is provided as an integer count, but are greater than allowed no. of max subnets configured
    allowed max subnets = len(cluster.network.private_subnets)
    """
    max_subnets = len(context.config().get_list('cluster.network.private_subnets', []))
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'subnet_id': max_subnets + 1,
        },
    )
    assert result.success is False


def test_job_builder_subnet_id_placement_group_enabled(context):
    """
    custom subnet id - placement group enabled
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'subnet_id': 'subnet-custom1',
            'placement_group': 'true',
        },
    )
    assert result.success is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == 1
    assert result.job_params.subnet_ids[0] == 'subnet-custom1'


def test_job_builder_subnet_id_multiple_placement_group_enabled_should_fail(context):
    """
    multiple custom subnet ids are provided by user and placement group enabled
    this should result in validation failure as multiple subnets are not supported when placement groups are enabled
    Note:
        * this might need changes based on placement group strategy values spread and partition.
        * but for HPC, why would any one want to use spread and partition?
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'subnet_id': 'subnet-custom1+subnet-custom2',
            'placement_group': 'true',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'subnet_ids'


def test_job_builder_subnet_id_multiple_efa_enabled_should_fail(context):
    """
    multiple custom subnet ids are provided by user and efa support is enabled
    this should result in validation failure as multiple subnets are not supported when efa support is enabled
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'subnet_id': 'subnet-custom1+subnet-custom2',
            'efa_support': 'true',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'enable_efa_support'


def test_job_builder_subnet_id_lustre_enabled(context):
    """
    user does not provide any custom subnets
    cluster configuration has multiple subnets
    when lustre is provided for scratch storage, first subnet from the cluster configured private subnets should be resolved.
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'fsx_lustre': 's3://lustre-backend-s3-bucket+/export-path+/import-path',
        },
    )
    assert result.success is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == 1
    assert (
        result.job_params.subnet_ids[0]
        == context.config().get_list('cluster.network.private_subnets', [])[0]
    )
    assert result.job_params.fsx_lustre.enabled is True


def test_job_builder_placement_group_valid(context):
    """
    placement group enabled
    in this scenario, one of the subnet ids configured in cluster settings should be randomly picked, not all.
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'placement_group': 'true',
        },
    )
    assert result.success is True
    assert result.job_params.enable_placement_group is True
    assert result.job_params.subnet_ids is not None
    assert len(result.job_params.subnet_ids) == 1


def test_job_builder_security_groups_from_cluster_settings(context):
    """
    security groups (used from cluster settings)
    * user does not provide any security groups
    * queue profile has no security groups
    """
    result = build_and_validate(
        context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}
    )
    assert result.success is True
    assert result.job_params.security_groups is not None
    assert len(result.job_params.security_groups) == len(
        context.config().get_list('scheduler.compute_node_security_group_ids', [])
    )


def test_job_builder_security_groups_from_queue_profile(context):
    """
    security groups (from queue profile)
    * user does not provide any security groups
    * queue profile has security groups
    """

    custom_security_groups = ['sg-customqueue1', 'sg-customqueue2', 'sg-customqueue3']
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(security_groups=custom_security_groups),
        ),
    )
    assert result.success is True
    assert result.job_params.security_groups is not None
    assert len(result.job_params.security_groups) == len(custom_security_groups)
    for i, security_group_id in enumerate(custom_security_groups):
        assert result.job_params.security_groups[i] == custom_security_groups[i]


def test_job_builder_security_groups_custom_from_params(context):
    """
    security groups
    * user provides security groups in params

    Note: security groups provided by user are considered additional security groups and are added to the security groups resolved either via cluster config or the ones in queue profile.
    """

    security_groups = context.config().get_list(
        'scheduler.compute_node_security_group_ids', []
    )

    custom_security_groups = ['sg-customuser1', 'sg-customuser2', 'sg-customuser3']

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
    )
    assert result.success is True
    assert result.job_params.security_groups is not None
    assert len(result.job_params.security_groups) == len(security_groups) + len(
        custom_security_groups
    )
    for security_group_id in result.job_params.security_groups:
        assert (security_group_id in security_groups) or (
            security_group_id in custom_security_groups
        )


def test_job_builder_security_groups_custom_from_params_invalid(context):
    """
    security groups
    * user provides invalid security groups in params
    """

    custom_security_groups = ['invalid-sg']

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'security_groups'


def test_job_builder_security_groups_custom_from_params_allowed(context):
    """
    user provides security groups in params that is configured as allowed security group in queue profile
    """

    custom_security_groups = ['sg-allowed1', 'sg-allowed2', 'sg-allowed3']

    allowed_security_groups = [
        'sg-allowed1',
        'sg-allowed2',
        'sg-allowed3',
        'sg-allowed4',
    ]

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_security_groups=allowed_security_groups
            ),
        ),
    )
    assert result.success is True


def test_job_builder_security_groups_custom_from_params_not_allowed(context):
    """
    user provides security groups in params that is not configured as allowed security group in queue profile
    """

    custom_security_groups = ['sg-not-allowed1', 'sg-not-allowed2', 'sg-not-allowed3']

    allowed_security_groups = [
        'sg-allowed1',
        'sg-allowed2',
        'sg-allowed3',
        'sg-allowed4',
    ]

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_security_groups=allowed_security_groups
            ),
        ),
    )
    assert result.success is False


def test_job_builder_security_groups_custom_from_params_and_queue_profile(context):
    """
    security groups
    * user provides security groups in params
    * security groups are also configured in queue profile
    """

    queue_profile_security_groups = [
        'sg-customqueue1',
        'sg-customqueue2',
        'sg-customqueue3',
    ]

    custom_security_groups = ['sg-customuser1', 'sg-customuser2', 'sg-customuser3']

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                security_groups=queue_profile_security_groups
            ),
        ),
    )
    assert result.success is True
    assert result.job_params.security_groups is not None
    assert len(result.job_params.security_groups) == len(
        queue_profile_security_groups
    ) + len(custom_security_groups)
    for security_group_id in result.job_params.security_groups:
        assert (security_group_id in queue_profile_security_groups) or (
            security_group_id in custom_security_groups
        )


def test_job_builder_security_groups_custom_from_params_more_than_max_should_fail(
    context,
):
    """
    security groups
    * user provided security groups more than the allowed MAX security groups
    """

    custom_security_groups = []
    for i in range(1, constants.MAX_SECURITY_GROUPS + 2):
        custom_security_groups.append(f'sg-customuser{i}')

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'security_groups': '+'.join(custom_security_groups),
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                security_groups=[
                    'sg-customqueue1',
                    'sg-customqueue2',
                    'sg-customqueue3',
                ]
            ),
        ),
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'security_groups'


def test_job_builder_placement_group_invalid_instance_type(context):
    """
    placement group enabled, but given instance type does not support placement groups
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'placement_group': 'true',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == 'enable_placement_group'


def test_job_builder_restricted_param_instance_type_fail(context):
    """
    instance type is a restricted parameter (IDEA param name configured as restricted param)
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                restricted_parameters=['instance_types']
            ),
        ),
    )
    assert result.success is False


def test_job_builder_restricted_param_instance_type_alt_fail(context):
    """
    instance type is a restricted parameter (scheduler param name configured as restricted param)
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                restricted_parameters=['instance_type']
            ),
        ),
    )
    assert result.success is False


def test_job_builder_restricted_param_instance_type_ok(context):
    """
    instance type is a restricted parameter
    user won't be able to provide it, but instance type will be read from queue profile
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(instance_types=['c5.large']),
            queue_management_params=SocaQueueManagementParams(
                restricted_parameters=['instance_types']
            ),
        ),
    )
    assert result.success is True
    assert result.job_params.instance_types is not None
    assert result.job_params.instance_types[0] == 'c5.large'


def test_job_builder_gpus_non_gpu_instance_invalid(context):
    """
    gpus requested on an instance type with no GPUs
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'gpus': 1},
    )
    assert result.success is False


def test_job_builder_gpus_gpu_instance_valid(context):
    """
    gpus requested on an instance type with sufficient GPUs
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge', 'gpus': 1},
    )
    assert result.success is True
    assert result.job_params.gpus == 1


def test_job_builder_gpus_exceeds_available_invalid(context):
    """
    gpus requested exceeds the GPUs available on the instance type
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge', 'gpus': 2},
    )
    assert result.success is False
    messages = get_validation_messages(result)
    assert any(
        'requested gpus: (2)' in message and 'g4dn.xlarge (1 GPUs)' in message
        for message in messages
    )


def test_job_builder_gpus_queue_default_exceeds_available_invalid(context):
    """
    the queue profile default gpus exceeds the GPUs available on the instance type - jobs
    submitted without gpus are provisioned with that default and must be rejected too
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(gpus=2),
        ),
    )
    assert result.success is False
    messages = get_validation_messages(result)
    assert any(
        'queue profile default requests gpus: (2)' in message
        and 'g4dn.xlarge (1 GPUs)' in message
        for message in messages
    )


def test_job_builder_gpus_queue_default_within_available_valid(context):
    """
    the queue profile default gpus is satisfied by the instance type
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(gpus=1),
        ),
    )
    assert result.success is True
    assert result.job_params.gpus == 1


def test_job_builder_gpus_queue_default_unresolvable_instance_type_skipped(context):
    """
    a queue profile default instance type that cannot be resolved is not marked failed by
    the instance_types checks - the GPU count check reaches it and must not raise
    """
    builder = SocaJobBuilder(
        context=context,
        params={'nodes': 1},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                gpus=2, instance_types=['zz9.nonexistent']
            ),
        ),
    )
    validation_result = builder.validate()
    messages = [
        entry.message
        for entry in validation_result.results
        if entry.message is not None
    ]
    assert not any('requests gpus' in message for message in messages)


def test_job_builder_instance_ami_base_os_mismatch_invalid(context):
    """
    instance_ami is the cluster compute node AMI, registered for the cluster base_os -
    pairing it with a different base_os bootstraps the node for an OS it does not carry
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'rhel9',
            'instance_ami': 'ami-mockclustersettings',
        },
    )
    assert result.success is False
    messages = get_validation_messages(result)
    assert any(
        'ami-mockclustersettings' in message
        and 'registered for base_os: (amazonlinux2023)' in message
        and 'requested base_os: (rhel9)' in message
        for message in messages
    )


def test_job_builder_instance_ami_base_os_match_valid(context):
    """
    instance_ami is the cluster compute node AMI and base_os matches what it was built for
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'amazonlinux2023',
            'instance_ami': 'ami-mockclustersettings',
        },
    )
    assert result.success is True
    assert result.job_params.instance_ami == 'ami-mockclustersettings'


def test_job_builder_instance_ami_unknown_base_os_mismatch_allowed(context):
    """
    an AMI the cluster does not know about carries no base_os the submit path can read -
    absence from the known map is not evidence of a mismatch
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'rhel9',
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is True
    assert result.job_params.base_os == 'rhel9'


def test_job_builder_instance_ami_base_os_from_queue_default_blames_queue_profile(
    context,
):
    """
    the base_os came from the queue profile, not from the submission, so the message has
    to name the queue profile - telling the user they requested it sends them nowhere.
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'instance_ami': 'ami-mockclustersettings',
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(base_os=constants.OS_RHEL9),
        ),
    )
    assert result.success is False
    messages = get_validation_messages(result)
    assert any(
        'queue profile default requests base_os: (rhel9)' in message
        and 'ami-mockclustersettings' in message
        and 'registered for base_os: (amazonlinux2023)' in message
        for message in messages
    )
    assert not any('requested base_os' in message for message in messages)


def test_job_builder_instance_ami_queue_default_base_os_mismatch_invalid(context):
    """
    the queue profile default AMI is registered for the queue profile base_os - requesting
    that AMI with a different base_os is the same mismatch
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'rocky9',
            'instance_ami': 'ami-queueprofile',
        },
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_ami='ami-queueprofile', base_os=constants.OS_RHEL9
            ),
        ),
    )
    assert result.success is False
    messages = get_validation_messages(result)
    assert any(
        'ami-queueprofile' in message
        and 'registered for base_os: (rhel9)' in message
        and 'requested base_os: (rocky9)' in message
        for message in messages
    )


def test_job_builder_base_os_default_ami_mismatch_invalid(context):
    """
    base_os is overridden without an instance_ami - the default AMI is built
    for the default base_os and cannot be used
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', 'base_os': 'rhel9'},
    )
    assert result.success is False


def test_job_builder_base_os_matches_default_os_valid(context):
    """
    base_os matches the cluster default compute node os - the default AMI applies
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'amazonlinux2023',
        },
    )
    assert result.success is True


def test_job_builder_base_os_override_with_ami_valid(context):
    """
    base_os is overridden along with a matching instance_ami
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'rhel9',
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is True
    assert result.job_params.base_os == 'rhel9'
    assert result.job_params.instance_ami == 'ami-0123456789abcdef0'


def test_job_builder_base_os_ubuntu2604_rejected(context):
    """
    ubuntu2604 is not a supported base_os: Amazon DCV, the FSx Lustre client and the
    pinned EFA installer publish nothing for it, so it is absent from ALLOWED_BASEOS
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': 'ubuntu2604',
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is False
    assert result.validation_result.results[0].error_code == constants.JOB_PARAM_BASE_OS


def test_job_builder_efa_unsupported_base_os_invalid(context, monkeypatch):
    """
    EFA is enabled but the base_os has no working EFA bootstrap support.

    rhel9 is not in UNSUPPORTED_BASE_OS_JOB_FEATURES - the entry is injected here to
    exercise the guard itself rather than any particular base_os.
    """
    monkeypatch.setitem(
        constants.UNSUPPORTED_BASE_OS_JOB_FEATURES,
        constants.OS_RHEL9,
        (constants.JOB_FEATURE_EFA,),
    )
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            'efa_support': 'true',
            'base_os': constants.OS_RHEL9,
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any('EFA is not supported' in message for message in messages)


@pytest.mark.parametrize('base_os', [constants.OS_RHEL10, constants.OS_ROCKY10])
def test_job_builder_efa_el10_invalid(context, base_os):
    """
    EFA on rhel10/rocky10 is rejected at submission: the pinned EFA installer does not
    recognize EL10 and exits, so the node would come up without the RDMA stack
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            'efa_support': 'true',
            'base_os': base_os,
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any('EFA is not supported' in message for message in messages)


def test_job_builder_efa_el10_without_efa_valid(context):
    """
    the EFA guard is feature-scoped: an EL10 job that does not ask for EFA still validates
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            'base_os': constants.OS_RHEL10,
            'instance_ami': 'ami-0123456789abcdef0',
        },
    )
    assert result.success is True


def test_job_builder_instance_types_mixed_gpu_invalid(context):
    """
    instance types mix GPU and non-GPU families - the bootstrap is rendered for the first
    instance type only, so a GPU node from this list can come up without drivers
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge+c5.large'},
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'mix GPU and non-GPU instance families' in message for message in messages
    )
    assert any(
        'GPU: [g4dn.xlarge]' in message and 'non-GPU: [c5.large]' in message
        for message in messages
    )


def test_job_builder_instance_types_mixed_gpu_first_non_gpu_invalid(context):
    """
    mixed list is rejected irrespective of the ordering of the instance types
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large+g4dn.xlarge'},
    )
    assert result.success is False


def test_job_builder_instance_types_all_gpu_valid(context):
    """
    all requested instance types are GPU families
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge+g5.xlarge'},
    )
    assert result.success is True
    assert result.job_params.instance_types == ['g4dn.xlarge', 'g5.xlarge']


def test_job_builder_instance_types_all_non_gpu_valid(context):
    """
    all requested instance types are non-GPU families
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large+c5.xlarge+t3.micro'},
    )
    assert result.success is True
    assert result.job_params.instance_types == ['c5.large', 'c5.xlarge', 't3.micro']


def test_job_builder_instance_types_single_gpu_valid(context):
    """
    a single GPU instance type is not a mixed list
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'g4dn.xlarge'},
    )
    assert result.success is True
    assert result.job_params.instance_types == ['g4dn.xlarge']


def test_job_builder_instance_types_mixed_gpu_queue_default_invalid(context):
    """
    queue profile default instance types mix GPU and non-GPU families - jobs submitted without
    an instance type are provisioned from that list and must be rejected too
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_types=['c5.large', 'g4dn.xlarge']
            ),
        ),
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'queue profile default instance types' in message for message in messages
    )


def test_job_builder_instance_types_all_gpu_queue_default_valid(context):
    """
    queue profile default instance types are all GPU families
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_types=['g4dn.xlarge', 'g5.xlarge']
            ),
        ),
    )
    assert result.success is True
    assert result.job_params.instance_types == ['g4dn.xlarge', 'g5.xlarge']


def get_nodes_validation_entry(
    result: BuildAndValidateResult,
) -> Optional[JobValidationResultEntry]:
    for entry in result.validation_result.results:
        if entry.error_code == constants.JOB_PARAM_NODES:
            return entry
    return None


def test_job_builder_max_nodes_per_job_default_ok(context):
    """
    node count at the cluster wide max_nodes_per_job default is accepted
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': constants.DEFAULT_MAX_NODES_PER_JOB,
            'cpus': 1,
            'instance_type': 't3.micro',
        },
    )
    assert result.success is True
    assert result.job_params.nodes == constants.DEFAULT_MAX_NODES_PER_JOB


def test_job_builder_max_nodes_per_job_default_fail(context):
    """
    node count above the cluster wide max_nodes_per_job default is rejected
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': constants.DEFAULT_MAX_NODES_PER_JOB + 1,
            'cpus': 1,
            'instance_type': 't3.micro',
        },
    )
    assert result.success is False
    entry = get_nodes_validation_entry(result)
    assert entry is not None
    assert f'{constants.DEFAULT_MAX_NODES_PER_JOB} nodes' in entry.message


def test_job_builder_max_nodes_per_job_cluster_setting(context):
    """
    scheduler.job_provisioning.max_nodes_per_job overrides the built-in default
    """
    context.config().put('scheduler.job_provisioning.max_nodes_per_job', 4)

    result = build_and_validate(
        context=context, params={'nodes': 4, 'cpus': 1, 'instance_type': 't3.micro'}
    )
    assert result.success is True

    result = build_and_validate(
        context=context, params={'nodes': 5, 'cpus': 1, 'instance_type': 't3.micro'}
    )
    assert result.success is False
    assert get_nodes_validation_entry(result) is not None


def test_job_builder_max_nodes_per_job_unlimited(context):
    """
    scheduler.job_provisioning.max_nodes_per_job = 0 implies no limit
    """
    context.config().put('scheduler.job_provisioning.max_nodes_per_job', 0)

    result = build_and_validate(
        context=context, params={'nodes': 5000, 'cpus': 1, 'instance_type': 't3.micro'}
    )
    assert result.success is True


def test_job_builder_max_nodes_per_job_queue_profile(context):
    """
    queue profile max_nodes_per_job takes precedence over the cluster setting
    """
    queue_profile = HpcQueueProfile(
        name='mock-queue-profile',
        queue_management_params=SocaQueueManagementParams(max_nodes_per_job=2),
    )

    result = build_and_validate(
        context=context,
        params={'nodes': 2, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=queue_profile,
    )
    assert result.success is True

    result = build_and_validate(
        context=context,
        params={'nodes': 3, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=queue_profile,
    )
    assert result.success is False
    entry = get_nodes_validation_entry(result)
    assert entry is not None
    assert '2 nodes' in entry.message


def test_job_builder_max_nodes_per_job_queue_profile_zero(context):
    """
    queue profile max_nodes_per_job = 0 falls back to the cluster setting
    """
    context.config().put('scheduler.job_provisioning.max_nodes_per_job', 4)

    result = build_and_validate(
        context=context,
        params={'nodes': 5, 'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(max_nodes_per_job=0),
        ),
    )
    assert result.success is False
    assert get_nodes_validation_entry(result) is not None


def test_job_builder_max_nodes_per_job_default_job_params(context):
    """
    a queue profile default node count above the limit is rejected,
    even when the job does not provide the nodes parameter
    """
    context.config().put('scheduler.job_provisioning.max_nodes_per_job', 4)

    result = build_and_validate(
        context=context,
        params={'cpus': 1, 'instance_type': 't3.micro'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(nodes=8),
        ),
    )
    assert result.success is False
    assert get_nodes_validation_entry(result) is not None


# hpc6a/hpc6id/hpc7a/hpc7g/hpc8a are on-demand only, SMT-disabled, and EFA-capable; these tests
# assert IDEA reads that from instance type data, not a hardcoded family list, so new families just work.


def test_job_builder_hpc_instance_type_ondemand_valid(context):
    """
    on-demand job on an HPC instance type
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 2, 'cpus': 192, 'instance_type': 'hpc7a.96xlarge'},
    )
    assert result.success is True


def test_job_builder_hpc_instance_type_spot_rejected(context):
    """
    HPC instance types are not offered as spot instances
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 192,
            'instance_type': 'hpc7a.96xlarge',
            'spot': 'true',
        },
    )
    assert result.success is False
    assert any(
        entry.error_code == constants.JOB_PARAM_SPOT
        for entry in result.validation_result.results
    )


def test_job_builder_hpc_instance_type_spot_price_rejected(context):
    """
    spot_price implies spot capacity, so it must be rejected for HPC instance types too
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 64,
            'instance_type': 'hpc7g.16xlarge',
            'spot_price': 'auto',
        },
    )
    assert result.success is False


def test_job_builder_hpc_instance_type_spot_queue_default_rejected(context):
    """
    spot enabled by the queue profile default must be validated as well
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 192, 'instance_type': 'hpc7a.96xlarge'},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(spot=True),
        ),
    )
    assert result.success is False


def test_job_builder_spot_supported_instance_type_valid(context):
    """
    instance types that do support spot are unaffected
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 18,
            'instance_type': 'c5n.18xlarge',
            'spot': 'true',
        },
    )
    assert result.success is True


def test_job_builder_hpc_instance_type_efa_valid(context):
    """
    EFA on an HPC family that is not named anywhere in the source tree
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 192,
            'instance_type': 'hpc7a.96xlarge',
            'efa_support': 'true',
        },
    )
    assert result.success is True
    assert result.job_params.enable_efa_support is True


def test_job_builder_hpc_graviton_instance_type_efa_valid(context):
    """
    EFA on the arm64 HPC family
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 64,
            'instance_type': 'hpc7g.16xlarge',
            'efa_support': 'true',
            'instance_ami': 'ami-arm64mockimage00',
        },
    )
    assert result.success is True


def test_job_builder_hpc_instance_type_cpu_options_unsupported(context):
    """
    HPC instance types have SMT disabled and no configurable CpuOptions.
    the launch template must not request CpuOptions for them.
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 192, 'instance_type': 'hpc7a.96xlarge'},
    )
    assert result.success is True
    assert result.provisioning_options.instance_types[0].cpu_options_supported is False

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 64,
            'instance_type': 'hpc7g.16xlarge',
            'instance_ami': 'ami-arm64mockimage00',
        },
    )
    assert result.success is True
    assert result.provisioning_options.instance_types[0].cpu_options_supported is False


def test_job_builder_cpu_options_supported_unaffected(context):
    """
    non-HPC instance types keep CpuOptions support
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 36, 'instance_type': 'c5n.18xlarge'},
    )
    assert result.success is True
    assert result.provisioning_options.instance_types[0].cpu_options_supported is True


def test_job_builder_hpc_instance_type_placement_group_valid(context):
    """
    HPC instance types support cluster placement groups, which is the configured default
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 192,
            'instance_type': 'hpc7a.96xlarge',
            'placement_group': 'true',
        },
    )
    assert result.success is True


def test_job_builder_placement_group_validates_configured_strategy(context):
    """
    the strategy validated must be the one CloudFormationStackBuilder will request,
    not a hardcoded 'cluster'.
    """
    context.config().put(
        'scheduler.job_provisioning.placement_group.strategy', 'spread'
    )
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 192,
            'instance_type': 'hpc7a.96xlarge',
            'placement_group': 'true',
        },
    )
    assert result.success is False
    assert any(
        entry.error_code == constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP
        and 'spread' in entry.message
        for entry in result.validation_result.results
    )


def test_job_builder_placement_group_rejects_invalid_configured_strategy(context):
    """
    a mistyped strategy in cluster settings must fail at submit, not at stack create
    """
    context.config().put(
        'scheduler.job_provisioning.placement_group.strategy', 'clustered'
    )
    result = build_and_validate(
        context=context,
        params={
            'nodes': 2,
            'cpus': 36,
            'instance_type': 'c5n.18xlarge',
            'placement_group': 'true',
        },
    )
    assert result.success is False


def test_job_builder_instance_types_mixed_architecture_invalid(context):
    """
    instance types that share no processor architecture cannot be served by one AMI
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'hpc7g.16xlarge+c5.large'},
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'do not share a processor architecture' in message for message in messages
    )
    assert any(
        'hpc7g.16xlarge (arm64)' in message and 'c5.large (x86_64)' in message
        for message in messages
    )


def test_job_builder_instance_types_arm64_with_x86_64_ami_invalid(context):
    """
    an arm64 instance type against the cluster default x86_64 AMI is rejected at submit
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'hpc7g.16xlarge'},
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'run arm64' in message and 'ami-mockclustersettings) is x86_64' in message
        for message in messages
    )


def test_job_builder_instance_types_x86_64_with_arm64_ami_invalid(context):
    """
    the mismatch is rejected in the other direction too
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'instance_ami': 'ami-arm64mockimage00',
        },
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'run x86_64' in message and 'ami-arm64mockimage00) is arm64' in message
        for message in messages
    )


def test_job_builder_instance_types_arm64_with_arm64_ami_valid(context):
    """
    an arm64 instance type with an arm64 AMI is accepted
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'hpc7g.16xlarge',
            'instance_ami': 'ami-arm64mockimage00',
        },
    )
    assert result.success is True
    assert result.job_params.instance_types == ['hpc7g.16xlarge']


def test_job_builder_instance_types_architecture_check_skipped_when_ami_not_described(
    context, monkeypatch
):
    """
    a transient DescribeImages failure leaves the AMI architecture unknown. the check is
    skipped rather than read as a mismatch, so submission is never blocked on an EC2 error.
    """

    def raise_describe_failure(**_):
        raise ClientError(
            {'Error': {'Code': 'RequestLimitExceeded', 'Message': 'boom'}},
            'DescribeImages',
        )

    monkeypatch.setattr(context.aws().ec2(), 'describe_images', raise_describe_failure)

    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'hpc7g.16xlarge',
            'instance_ami': 'ami-describefails000',
        },
    )
    assert result.success is True
    assert result.job_params.instance_types == ['hpc7g.16xlarge']


def test_job_builder_instance_types_mixed_architecture_queue_default_invalid(context):
    """
    a queue profile default list that mixes architectures is rejected for jobs that do not
    request instance types of their own
    """
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_types=['c5.large', 'hpc7g.16xlarge']
            ),
        ),
    )
    assert result.success is False
    messages = [
        entry.message
        for entry in result.validation_result.results
        if entry.message is not None
    ]
    assert any(
        'The queue profile default instance types do not share a processor architecture'
        in message
        for message in messages
    )


PREFERRED_SUBNET_KEY = 'cluster.network.preferred_subnet_id'


def test_job_builder_preferred_subnet_id_not_set_uses_all_private_subnets(context):
    """
    preferred_subnet_id is not set: a single node job gets every private subnet.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    result = build_and_validate(
        context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}
    )
    assert result.success is True
    assert result.job_params.subnet_ids == private_subnets


def test_job_builder_preferred_subnet_id_not_set_multi_node_picks_one(context):
    """
    preferred_subnet_id is not set: a multi node on-demand job still gets a single subnet
    chosen from the cluster private subnets.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    result = build_and_validate(
        context=context, params={'nodes': 2, 'cpus': 1, 'instance_type': 'c5.large'}
    )
    assert result.success is True
    assert len(result.job_params.subnet_ids) == 1
    assert result.job_params.subnet_ids[0] in private_subnets


def test_job_builder_preferred_subnet_id_first_with_fallback(context):
    """
    preferred_subnet_id set: it leads the list and the remaining private subnets stay
    behind it, so a capacity failure in the preferred zone can still fall back.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    try:
        result = build_and_validate(
            context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.subnet_ids[0] == preferred
    assert sorted(result.job_params.subnet_ids) == sorted(private_subnets)


def test_job_builder_preferred_subnet_id_multi_node_pins_preferred(context):
    """
    a multi node on-demand job keeps all its nodes in one subnet: the preferred one.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    try:
        result = build_and_validate(
            context=context, params={'nodes': 2, 'cpus': 1, 'instance_type': 'c5.large'}
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.subnet_ids == [preferred]


def test_job_builder_preferred_subnet_id_explicit_subnet_id_wins(context):
    """
    a job that passes subnet_id explicitly is not affected by preferred_subnet_id.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    context.config().put(PREFERRED_SUBNET_KEY, private_subnets[-1])
    try:
        result = build_and_validate(
            context=context,
            params={
                'nodes': 1,
                'cpus': 1,
                'instance_type': 'c5.large',
                'subnet_id': 'subnet-custom1',
            },
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.subnet_ids == ['subnet-custom1']


def test_job_builder_preferred_subnet_id_unknown_subnet_ignored(context):
    """
    a preferred subnet that is not one of the cluster private subnets is ignored.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    context.config().put(PREFERRED_SUBNET_KEY, 'subnet-not-in-this-cluster')
    try:
        result = build_and_validate(
            context=context, params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.subnet_ids == private_subnets


def test_job_builder_preferred_subnet_id_placement_group_single_subnet(context):
    """
    placement group still collapses to a single subnet, and it is the preferred one.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    try:
        result = build_and_validate(
            context=context,
            params={
                'nodes': 1,
                'cpus': 1,
                'instance_type': 'c5.large',
                'placement_group': 'true',
            },
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.enable_placement_group is True
    assert result.job_params.subnet_ids == [preferred]


def test_job_builder_preferred_subnet_id_efa_single_subnet(context):
    """
    EFA on a multi node job still collapses to a single subnet, and it is the preferred one.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    try:
        result = build_and_validate(
            context=context,
            params={
                'nodes': 2,
                'cpus': 18,
                'instance_type': 'c5n.18xlarge',
                'spot': 'true',
                'efa_support': 'true',
            },
        )
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    assert result.job_params.enable_efa_support is True
    assert result.job_params.subnet_ids == [preferred]


class FakeJobCacheRetries:
    """job_cache stand-in reporting a fixed provisioning retry count"""

    def __init__(self, retry_count: int):
        self._retry_count = retry_count

    def get_job_provisioning_retry_count(self, _job_id: str) -> int:
        return self._retry_count


def build_with_retries(context, monkeypatch, params, retry_count: int):
    """build a job that has already failed provisioning retry_count times"""
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    assert len(private_subnets) > 1, (
        'this test needs more than one subnet to choose from'
    )
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    monkeypatch.setattr(
        context, 'job_cache', FakeJobCacheRetries(retry_count), raising=False
    )
    try:
        result = build_and_validate(context=context, params=params, job_id='101')
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')
    assert result.success is True
    return result, preferred, private_subnets


MULTI_NODE_JOB = {'nodes': 2, 'cpus': 1, 'instance_type': 'c5.large'}
EFA_JOB = {
    'nodes': 2,
    'cpus': 18,
    'instance_type': 'c5n.18xlarge',
    'spot': 'true',
    'efa_support': 'true',
}
PLACEMENT_GROUP_JOB = {
    'nodes': 1,
    'cpus': 1,
    'instance_type': 'c5.large',
    'placement_group': 'true',
}


def test_job_builder_preferred_subnet_id_first_attempt_pins_preferred(
    context, monkeypatch
):
    """
    a job that has not failed yet gets the preferred subnet, so it lands beside the shared
    filesystem.
    """
    for params in (MULTI_NODE_JOB, EFA_JOB, PLACEMENT_GROUP_JOB):
        result, preferred, _ = build_with_retries(
            context, monkeypatch, params, retry_count=0
        )
        assert result.job_params.subnet_ids == [preferred], params


def test_job_builder_preferred_subnet_id_retry_leaves_the_preferred_zone(
    context, monkeypatch
):
    """
    once provisioning has failed, the retry draws from the other subnets instead. an
    exhausted availability zone would otherwise consume the job's whole retry budget
    without ever trying another one.
    """
    for params in (MULTI_NODE_JOB, EFA_JOB, PLACEMENT_GROUP_JOB):
        # the pick is random, so a single build could miss the preferred subnet by luck
        for _ in range(10):
            result, preferred, private_subnets = build_with_retries(
                context, monkeypatch, params, retry_count=1
            )
            assert len(result.job_params.subnet_ids) == 1, params
            assert result.job_params.subnet_ids != [preferred], params
            assert result.job_params.subnet_ids[0] in private_subnets, params


def test_job_builder_preferred_subnet_id_list_branch_unchanged_on_retry(
    context, monkeypatch
):
    """
    a job that gets the whole subnet list is untouched by the retry count: the list already
    carries its own fallback, so the preferred subnet stays in front on every attempt.
    """
    result, preferred, private_subnets = build_with_retries(
        context, monkeypatch, {'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'}, 3
    )
    assert result.job_params.subnet_ids[0] == preferred
    assert sorted(result.job_params.subnet_ids) == sorted(private_subnets)


def test_job_builder_without_a_job_id_never_reads_the_job_cache(context, monkeypatch):
    """
    the validation and estimate paths build params for a job that does not exist yet. they
    must not look up a retry count, and they keep the preferred subnet.
    """

    class ExplodingJobCache:
        @staticmethod
        def get_job_provisioning_retry_count(_job_id: str) -> int:
            raise AssertionError('the job cache must not be read without a job id')

    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    preferred = private_subnets[-1]
    context.config().put(PREFERRED_SUBNET_KEY, preferred)
    monkeypatch.setattr(context, 'job_cache', ExplodingJobCache(), raising=False)
    try:
        result = build_and_validate(context=context, params=MULTI_NODE_JOB)
    finally:
        context.config().put(PREFERRED_SUBNET_KEY, '')

    assert result.success is True
    assert result.job_params.subnet_ids == [preferred]


def test_job_builder_no_preferred_subnet_still_random_on_every_attempt(
    context, monkeypatch
):
    """
    with no preferred subnet configured the retry count changes nothing: the pick stays
    random.
    """
    private_subnets = context.config().get_list('cluster.network.private_subnets', [])
    monkeypatch.setattr(context, 'job_cache', FakeJobCacheRetries(2), raising=False)
    result = build_and_validate(context=context, params=MULTI_NODE_JOB, job_id='101')
    assert result.success is True
    assert len(result.job_params.subnet_ids) == 1
    assert result.job_params.subnet_ids[0] in private_subnets
