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

from ideadatamodel import (
    constants,
    SocaBaseModel,
    JobValidationResult,
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
) -> BuildAndValidateResult:
    print()

    builder = SocaJobBuilder(
        context=context,
        params=params,
        queue_profile=queue_profile,
        stack_uuid=stack_uuid,
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


def test_job_builder_base_os_amazonlinux2(context):
    """
    base os (amazonlinux2)
    """
    result = build_and_validate(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'base_os': constants.OS_AMAZONLINUX2,
        },
    )
    assert result.success is True
    assert result.job_params.base_os == constants.OS_AMAZONLINUX2


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
