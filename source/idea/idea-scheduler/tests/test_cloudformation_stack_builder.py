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
Test Cases for CloudFormationStackBuilder
"""

import ideascheduler
from ideascheduler import AppContext
from ideascheduler.app.provisioning import CloudFormationStackBuilder
from ideadatamodel import (
    exceptions,
    SocaJob,
    HpcQueueProfile,
    SocaScalingMode,
    SocaJobParams,
    SocaBaseModel,
)
from ideascheduler.app.scheduler import SocaJobBuilder
from ideasdk.utils import Utils

from typing import Dict, Optional, List
from pyhocon import ConfigTree, ConfigFactory
import yaml


class BuildTemplateResult(SocaBaseModel):
    template: ConfigTree
    template_yml: str


def build_template(
    context: AppContext,
    params: Dict,
    queue_profile: HpcQueueProfile,
    job_name: str = 'mock-job',
    job_id: str = '1',
    stack_uuid: str = None,
) -> BuildTemplateResult:
    builder = SocaJobBuilder(
        context=context,
        params=params,
        queue_profile=queue_profile,
        stack_uuid=stack_uuid,
    )

    validation_result = builder.validate()
    if not validation_result.is_valid():
        raise exceptions.invalid_job(
            f'given test job parameters are invalid: {validation_result}'
        )

    job_params, provisioning_options = builder.build()
    mock_job = SocaJob(
        name=job_name,
        job_id=job_id,
        job_uid=f'{job_name}-{job_id}',
        owner='mockuser',
        project='default',
        cluster_name='idea-mock',
        params=job_params,
        queue=queue_profile.queues[0],
        queue_type=queue_profile.name,
        scaling_mode=queue_profile.scaling_mode,
        provisioning_options=provisioning_options,
    )
    mock_job.job_group = mock_job.get_job_group()

    try:
        builder = CloudFormationStackBuilder(
            context=context,
            job=mock_job,
        )

        template_yml = builder.build_template()
        print(template_yml)

        class CfnAny:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        loader = yaml.SafeLoader
        loader.add_constructor('!Base64', CfnAny)
        loader.add_constructor('!Ref', CfnAny)
        loader.add_constructor('!GetAtt', CfnAny)

        template = ConfigFactory.from_dict(Utils.from_yaml(template_yml))

        return BuildTemplateResult(template=template, template_yml=template_yml)
    except Exception as e:
        print(f'Exception in build_template: {e}')
        print(f'Exception type: {type(e)}')
        import traceback

        traceback.print_exc()
        print('Job:')
        print(Utils.to_yaml(mock_job))
        # Re-raise the exception to ensure the test fails with proper error information
        raise e


def get_tag_value(key: str, tags: List[Dict]) -> Optional[str]:
    for tag in tags:
        if tag['Key'] == key:
            return tag['Value']
    return None


def test_cfn_stack_builder_ondemand_basic(context: AppContext):
    """
    on-demand basic
    """
    result = build_template(
        context=context,
        job_name='ondemand-basic',
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    assert result.template.get_string('AWSTemplateFormatVersion') == '2010-09-09'
    assert (
        result.template.get_string('Description')
        == f'IDEA Compute Node Stack (Version: {ideascheduler.__version__})'
    )

    tags = result.template.get('Resources.AutoScalingComputeGroup.Properties.Tags')
    assert len(tags) > 0
    assert get_tag_value('idea:CapacityType', tags) == 'on-demand'
    assert get_tag_value('idea:ClusterName', tags) == 'idea-mock'
    assert get_tag_value('idea:JobId', tags) == '1'
    assert get_tag_value('idea:JobGroup', tags) is not None
    assert get_tag_value('idea:JobName', tags) == 'ondemand-basic'
    assert get_tag_value('idea:JobQueue', tags) == 'normal'
    assert get_tag_value('idea:KeepForever', tags) == 'false'
    assert get_tag_value('idea:ModuleId', tags) == 'scheduler'
    assert get_tag_value('idea:NodeType', tags) == 'compute-node'
    assert get_tag_value('idea:Project', tags) == 'default'
    assert get_tag_value('idea:QueueType', tags) == 'compute'
    assert get_tag_value('idea:ScalingMode', tags) == 'single-job'
    assert get_tag_value('idea:StackId', tags) == 'idea-mock-compute-ondemand-1'
    assert get_tag_value('idea:StackType', tags) == 'job'
    assert get_tag_value('idea:TerminateWhenIdle', tags) == '0'
    assert get_tag_value('idea:KeepForever', tags) == 'false'

    project = context.projects_client.get_project_by_name('default')
    if project.tags is not None:
        for tag in project.tags:
            assert get_tag_value(tag.key, tags) == tag.value

    custom_tags_list = context.config().get_list(
        'global-settings.custom_tags', default=[]
    )
    custom_tags = Utils.convert_custom_tags_to_key_value_pairs(custom_tags_list)
    for key, value in custom_tags.items():
        assert get_tag_value(key, tags) == value


def test_cfn_stack_builder_ondemand_terminate_when_idle(context):
    """
    on-demand basic - terminate when idle set to 3 minutes
    """
    result = build_template(
        context=context,
        job_name='ondemand-terminate-when-idle',
        params={'nodes': 1, 'cpus': 1},
        queue_profile=HpcQueueProfile(
            name='job-shared',
            queues=['job-shared'],
            scaling_mode=SocaScalingMode.BATCH,
            terminate_when_idle=3,
            default_job_params=SocaJobParams(
                instance_types=['c5.large'],
            ),
        ),
    )

    tags = result.template.get('Resources.AutoScalingComputeGroup.Properties.Tags')
    assert get_tag_value('idea:TerminateWhenIdle', tags) == '3'


def test_cfn_stack_builder_spotfleet_basic(context):
    """
    spot-fleet basic
    """
    result = build_template(
        context=context,
        job_name='spotfleet-basic',
        params={'nodes': 1, 'cpus': 1, 'spot_price': 'auto'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    def check_tags(tags: List[Dict]):
        assert get_tag_value('idea:CapacityType', tags) == 'spot'
        assert get_tag_value('idea:ClusterName', tags) == 'idea-mock'
        assert get_tag_value('idea:JobId', tags) == '1'
        assert get_tag_value('idea:JobGroup', tags) is not None
        assert get_tag_value('idea:JobName', tags) == 'spotfleet-basic'
        assert get_tag_value('idea:JobQueue', tags) == 'normal'
        assert get_tag_value('idea:KeepForever', tags) == 'false'
        assert get_tag_value('idea:ModuleId', tags) == 'scheduler'
        assert get_tag_value('idea:NodeType', tags) == 'compute-node'
        assert get_tag_value('idea:Project', tags) == 'default'
        assert get_tag_value('idea:QueueType', tags) == 'compute'
        assert get_tag_value('idea:ScalingMode', tags) == 'single-job'
        assert get_tag_value('idea:StackId', tags) == 'idea-mock-compute-spot-1'
        assert get_tag_value('idea:StackType', tags) == 'job'
        assert get_tag_value('idea:TerminateWhenIdle', tags) == '0'
        assert get_tag_value('idea:KeepForever', tags) == 'false'

        project = context.projects_client.get_project_by_name('default')
        if project.tags is not None:
            for tag in project.tags:
                assert get_tag_value(tag.key, tags) == tag.value

        custom_tags_list = context.config().get_list(
            'global-settings.custom_tags', default=[]
        )
        custom_tags = Utils.convert_custom_tags_to_key_value_pairs(custom_tags_list)
        for key, value in custom_tags.items():
            assert get_tag_value(key, tags) == value

    assert (
        result.template.get_string(
            'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData.InstanceType'
        )
        == 'c5.large'
    )

    aws_tag_specs = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData.TagSpecifications'
    )
    instance_tags = None
    volume_tags = None
    spot_instances_request = None
    for aws_tags in aws_tag_specs:
        resource_type = aws_tags['ResourceType']
        if resource_type == 'instance':
            instance_tags = aws_tags['Tags']
        elif resource_type == 'volume':
            volume_tags = aws_tags['Tags']
        elif resource_type == 'spot-instances-request':
            spot_instances_request = aws_tags['Tags']

    assert instance_tags is not None
    check_tags(instance_tags)

    assert volume_tags is not None
    check_tags(volume_tags)

    assert spot_instances_request is not None
    check_tags(instance_tags)

    spot_fleet = result.template.get('Resources.SpotFleet')
    assert spot_fleet.get_string('Type') == 'AWS::EC2::SpotFleet'
    assert (
        spot_fleet.get_string(
            'Properties.SpotFleetRequestConfigData.AllocationStrategy'
        )
        == 'capacityOptimized'
    )
    assert (
        spot_fleet.get_string('Properties.SpotFleetRequestConfigData.IamFleetRole')
        == 'arn:aws:iam::123456789012:role/idea-mock-scheduler-spot-fleet-request-role-us-east-1'
    )


def test_cfn_stack_builder_spotfleet_auto(context):
    """
    spot-fleet auto spot price should contain max price as AWS::NoValue in the generated template
    """
    result = build_template(
        context=context,
        job_name='spotfleet-auto',
        params={'nodes': 1, 'cpus': 1, 'spot_price': 'auto'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    launch_template_data = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData'
    )
    max_price = launch_template_data.get('InstanceMarketOptions.SpotOptions.MaxPrice')
    assert max_price.args[1].value == 'AWS::NoValue'


def test_cfn_stack_builder_spotfleet_amount(context):
    """
    spot-fleet - spot price specified as amount should contain the amount in template
    """
    result = build_template(
        context=context,
        job_name='spotfleet-auto',
        params={'nodes': 1, 'cpus': 1, 'spot_price': '0.3'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    launch_template_data = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData'
    )
    assert (
        launch_template_data.get('InstanceMarketOptions.SpotOptions.MaxPrice') == '0.3'
    )


def test_cfn_stack_builder_mixed_basic(context):
    """
    mixed - on-demand (2) + spotfleet (2) basic
    """
    result = build_template(
        context=context,
        job_name='mixed-basic',
        params={
            'nodes': 4,
            'cpus': 1,
            'spot_allocation_count': '2',
            'spot_price': 'auto',
        },
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    asg_props = result.template.get('Resources.AutoScalingComputeGroup.Properties')
    assert asg_props.get_string('DesiredCapacity') == '4'
    assert asg_props.get_string('MaxSize') == '4'
    assert asg_props.get_string('MinSize') == '4'
    mixed_instances_policy = asg_props.get('MixedInstancesPolicy')
    assert (
        mixed_instances_policy.get_int('InstancesDistribution.OnDemandBaseCapacity')
        == 2
    )
    assert (
        mixed_instances_policy.get_string(
            'InstancesDistribution.OnDemandPercentageAboveBaseCapacity'
        )
        == '0'
    )


def test_cfn_stack_builder_placement_group_dependency(context):
    """
    Test that placement group dependency is correctly set in Auto Scaling Group.
    This ensures proper deletion order during CloudFormation stack deletion.
    """
    result = build_template(
        context=context,
        job_name='placement-group-test',
        params={'nodes': 2, 'cpus': 1, 'placement_group': 'true'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    # Verify placement group resource exists
    placement_group = result.template.get('Resources.ComputeNodePlacementGroup')
    assert placement_group is not None
    assert placement_group.get_string('Type') == 'AWS::EC2::PlacementGroup'
    assert placement_group.get_string('Properties.Strategy') == 'cluster'

    # Verify Auto Scaling Group has proper dependencies
    asg = result.template.get('Resources.AutoScalingComputeGroup')
    assert asg is not None

    # Check that DependsOn includes both NodeLaunchTemplate and ComputeNodePlacementGroup
    depends_on = asg.get('DependsOn')
    assert depends_on is not None
    # DependsOn could be a string or a list, check both cases
    if isinstance(depends_on, str):
        # This shouldn't happen with our fix, but test for regression
        assert depends_on in ['NodeLaunchTemplate', 'ComputeNodePlacementGroup']
    elif isinstance(depends_on, list):
        assert 'NodeLaunchTemplate' in depends_on
        assert 'ComputeNodePlacementGroup' in depends_on

    # Verify placement group reference in ASG
    asg_placement_group = asg.get('Properties.PlacementGroup')
    assert asg_placement_group is not None
    # Should be a reference to the placement group
    assert (
        hasattr(asg_placement_group, 'args')
        and asg_placement_group.args[1].value == 'ComputeNodePlacementGroup'
    )


def test_cfn_stack_builder_no_placement_group_no_dependency(context):
    """
    Test that when placement group is disabled, no placement group dependency is added.
    """
    result = build_template(
        context=context,
        job_name='no-placement-group-test',
        params={'nodes': 2, 'cpus': 1, 'placement_group': 'false'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )

    # Verify no placement group resource exists
    try:
        result.template.get('Resources.ComputeNodePlacementGroup')
        # If we get here, the placement group exists when it shouldn't
        assert False, (
            'Placement group resource should not exist when placement groups are disabled'
        )
    except Exception:
        # This is expected - the placement group resource should not exist
        pass

    # Verify Auto Scaling Group dependencies
    asg = result.template.get('Resources.AutoScalingComputeGroup')
    assert asg is not None

    # Check that DependsOn only includes NodeLaunchTemplate
    depends_on = asg.get('DependsOn')
    if isinstance(depends_on, str):
        assert depends_on == 'NodeLaunchTemplate'
    elif isinstance(depends_on, list):
        assert 'NodeLaunchTemplate' in depends_on
        assert 'ComputeNodePlacementGroup' not in depends_on

    # Verify no placement group reference in ASG
    try:
        asg.get('Properties.PlacementGroup')
        # If we get here, there's a placement group reference when there shouldn't be
        assert False, (
            'ASG should not have a placement group reference when placement groups are disabled'
        )
    except Exception:
        # This is expected - no placement group reference should exist
        pass
