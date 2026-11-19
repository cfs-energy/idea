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

import logging
import re

import pytest

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
from ideadatamodel.aws import EC2InstanceType, EC2Instance
from ideadatamodel.model_utils import ModelUtils
from ideascheduler.app.provisioning.job_provisioner import (
    cloudformation_stack_builder as stack_builder_module,
)
from ideascheduler.app.scheduler import SocaJobBuilder
from ideasdk.aws import AWSUtil
from ideasdk.utils import Utils
from ideatestutils import MockInstanceTypes

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
        # a uid as the hook makes one: letters digits and dashes (U03 checks it again)
        job_uid=f'{job_name}-{job_id}'.replace('[]', ''),
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


class MultiCardInstanceTypes(MockInstanceTypes):
    """
    Mock instance types reporting multiple network cards, as hpc6id/hpc7a/hpc8a (2 cards) and the
    p4d/p5 families (4+ cards) do. Avoids pinning AWS network card facts into a mock template.
    """

    def __init__(self, network_cards: int, efa_interfaces: int):
        self.network_cards = network_cards
        self.efa_interfaces = efa_interfaces

    def get_instance_type(self, instance_type: str) -> EC2InstanceType:
        ec2_instance_type = super().get_instance_type(instance_type)
        # mock instance types are rendered from template on every call, mutation is not shared
        network_info = ec2_instance_type.instance_type_data()['NetworkInfo']
        network_info['MaximumNetworkCards'] = self.network_cards
        network_info['EfaInfo'] = {'MaximumEfaInterfaces': self.efa_interfaces}
        return ec2_instance_type


def mock_network_cards(monkeypatch, network_cards: int, efa_interfaces: int = None):
    if efa_interfaces is None:
        efa_interfaces = network_cards
    monkeypatch.setattr(
        AWSUtil,
        'get_ec2_instance_type',
        MultiCardInstanceTypes(
            network_cards=network_cards, efa_interfaces=efa_interfaces
        ).get_instance_type,
    )


def build_efa_template(context: AppContext, job_name: str) -> BuildTemplateResult:
    return build_template(
        context=context,
        job_name=job_name,
        params={'nodes': 1, 'cpus': 1, 'efa_support': 'true'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5n.18xlarge']),
        ),
    )


def get_efa_network_interfaces(result: BuildTemplateResult) -> List[Dict]:
    launch_template_data = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData'
    )
    # security groups must be on the interface: EC2 rejects both on the same launch template
    assert launch_template_data.get('SecurityGroupIds', None) is None
    network_interfaces = launch_template_data.get('NetworkInterfaces')
    for network_interface in network_interfaces:
        assert network_interface['InterfaceType'] == 'efa'
        assert network_interface['DeleteOnTermination'] is True
        assert network_interface['Groups'] == ['sg-mock123123123']
    return network_interfaces


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
    interface_tags = None
    spot_instances_request = None
    for aws_tags in aws_tag_specs:
        resource_type = aws_tags['ResourceType']
        if resource_type == 'instance':
            instance_tags = aws_tags['Tags']
        elif resource_type == 'volume':
            volume_tags = aws_tags['Tags']
        elif resource_type == 'network-interface':
            interface_tags = aws_tags['Tags']
        elif resource_type == 'spot-instances-request':
            spot_instances_request = aws_tags['Tags']

    assert instance_tags is not None
    check_tags(instance_tags)

    assert volume_tags is not None
    check_tags(volume_tags)

    # interfaces carry the same tags from launch, as instances and volumes do
    assert interface_tags is not None
    check_tags(interface_tags)

    assert spot_instances_request is not None
    # SP-34: this checked the instance tags a second time
    check_tags(spot_instances_request)

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
        # a single dependency is accepted only if it is one of the two required
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


def test_cfn_stack_builder_efa_single_network_card(context):
    """
    EFA on a single network card instance type - one interface on NetworkCardIndex 0
    """
    result = build_efa_template(context=context, job_name='efa-single-card')

    network_interfaces = get_efa_network_interfaces(result)
    assert len(network_interfaces) == 1
    assert network_interfaces[0]['DeviceIndex'] == 0
    assert network_interfaces[0]['NetworkCardIndex'] == 0


def test_cfn_stack_builder_efa_multi_rail_disabled(context, monkeypatch):
    """
    multi-rail disabled (default) - a multi network card instance type still gets one interface
    """
    mock_network_cards(monkeypatch, network_cards=2)

    result = build_efa_template(context=context, job_name='efa-multi-rail-disabled')

    network_interfaces = get_efa_network_interfaces(result)
    assert len(network_interfaces) == 1
    assert network_interfaces[0]['DeviceIndex'] == 0
    assert network_interfaces[0]['NetworkCardIndex'] == 0


def test_cfn_stack_builder_efa_multi_rail_enabled(context, monkeypatch):
    """
    multi-rail enabled - one EFA interface per network card, hpc6id/hpc7a/hpc8a shape
    """
    mock_network_cards(monkeypatch, network_cards=2)
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    result = build_efa_template(context=context, job_name='efa-multi-rail-enabled')

    network_interfaces = get_efa_network_interfaces(result)
    assert len(network_interfaces) == 2
    assert [
        network_interface['NetworkCardIndex']
        for network_interface in network_interfaces
    ] == [0, 1]
    # AWS shape: DeviceIndex 0 on the first network card, 1 on every additional card
    assert [
        network_interface['DeviceIndex'] for network_interface in network_interfaces
    ] == [0, 1]


def test_cfn_stack_builder_efa_multi_rail_enabled_single_network_card(context):
    """
    multi-rail enabled - instance types with a single network card are unaffected
    """
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    result = build_efa_template(context=context, job_name='efa-multi-rail-single-card')

    network_interfaces = get_efa_network_interfaces(result)
    assert len(network_interfaces) == 1
    assert network_interfaces[0]['NetworkCardIndex'] == 0


def test_cfn_stack_builder_efa_multi_rail_more_efa_than_network_cards(
    context, monkeypatch
):
    """
    multi-rail enabled - interface count cannot exceed the no. of network cards
    """
    mock_network_cards(monkeypatch, network_cards=2, efa_interfaces=4)
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    result = build_efa_template(context=context, job_name='efa-multi-rail-efa-gt-cards')

    assert len(get_efa_network_interfaces(result)) == 2


def test_cfn_stack_builder_efa_multi_rail_max_interfaces(context, monkeypatch):
    """
    multi-rail enabled - interface count is capped by scheduler.efa.max_interfaces (default: 2)
    """
    mock_network_cards(monkeypatch, network_cards=8)
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    result = build_efa_template(context=context, job_name='efa-multi-rail-default-max')
    assert len(get_efa_network_interfaces(result)) == 2

    context.config().put('scheduler.efa.max_interfaces', 4)
    result = build_efa_template(context=context, job_name='efa-multi-rail-custom-max')

    network_interfaces = get_efa_network_interfaces(result)
    assert len(network_interfaces) == 4
    assert [
        network_interface['NetworkCardIndex']
        for network_interface in network_interfaces
    ] == [0, 1, 2, 3]
    assert [
        network_interface['DeviceIndex'] for network_interface in network_interfaces
    ] == [0, 1, 1, 1]


def _launch_root_gb(result: BuildTemplateResult) -> int:
    mappings = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData.BlockDeviceMappings'
    )
    return mappings[0]['Ebs']['VolumeSize']


def _describe_root(context: AppContext, image_id: str, gb: int):
    original = context.aws().ec2().describe_images

    def describe_images(**kwargs):
        result = original(**kwargs)
        for image in result.get('Images', []):
            if image.get('ImageId') == image_id:
                image['RootDeviceName'] = '/dev/xvda'
                image['BlockDeviceMappings'] = [
                    {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': gb}}
                ]
        return result

    context.aws().ec2().describe_images = describe_images


def _queue_launch(context: AppContext, root_gb: int, image_gb: int) -> int:
    image_id = 'ami-root12gb'
    _describe_root(context, image_id, image_gb)
    result = build_template(
        context=context,
        job_name=f'root-{root_gb}',
        params={
            'nodes': 1,
            'cpus': 1,
            'root_storage_size': root_gb,
            'instance_ami': image_id,
        },
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )
    return _launch_root_gb(result)


def test_a_10_gb_queue_with_a_12_gb_image_launches_at_12(context, caplog):
    # the ASG and the spot fleet both launch from this template, and the compute
    # canary submits through the same provisioner
    with caplog.at_level(logging.INFO):
        assert _queue_launch(context, 10, 12) == 12
    raised = [
        record
        for record in caplog.records
        if 'raised from 10 GB to 12 GB' in record.message
    ]
    assert len(raised) == 1


def test_the_launch_guard_never_lowers_a_queue_root(context):
    assert _queue_launch(context, 100, 12) == 100


def test_a_compute_root_is_gp3_at_the_baseline_whatever_the_image_carries(context):
    # an IDEA image's root mapping keeps its builder's 16000 IOPS / 1000 MiB/s; the node
    # gets what an unprovisioned gp3 volume gets
    image_id = 'ami-root12gb'
    _describe_root(context, image_id, 12)
    result = build_template(
        context=context,
        job_name='root-gp3',
        params={'nodes': 1, 'cpus': 1, 'instance_ami': image_id},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )
    ebs = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData.BlockDeviceMappings'
    )[0]['Ebs']
    assert (ebs['VolumeType'], ebs['Iops'], ebs['Throughput']) == ('gp3', 3000, 125)


# CreateStack's stack name pattern and the tag value characters CloudFormation accepts
_STACK_NAME = re.compile(r'[a-zA-Z][-a-zA-Z0-9]{0,127}')
_TAG_VALUE = re.compile(r'[\w\s.:/=+\-@]*')
# a CloudWatch log group name, and the characters a metrics namespace may hold
_LOG_GROUP = re.compile(r'[.\-_/#A-Za-z0-9]{1,512}')
_NAMESPACE = re.compile(r'[.\-_/#:A-Za-z0-9 ]{1,255}')


@pytest.mark.parametrize(
    'scaling_mode, terminate_when_idle',
    [(SocaScalingMode.SINGLE_JOB, 0), (SocaScalingMode.BATCH, 3)],
)
def test_an_array_job_builds_a_stack_aws_accepts(
    context, monkeypatch, scaling_mode, terminate_when_idle
):
    # the two queue shapes dev refused an array job in: its own stack, and a shared batch one
    queue_profile = HpcQueueProfile(
        name='test',
        queues=['test'],
        scaling_mode=scaling_mode,
        terminate_when_idle=terminate_when_idle,
        default_job_params=SocaJobParams(instance_types=['c5.large']),
    )
    result = build_template(
        context=context,
        job_name='array',
        job_id='311[]',
        params={'nodes': 1, 'cpus': 1},
        queue_profile=queue_profile,
    )
    tags = result.template.get('Resources.AutoScalingComputeGroup.Properties.Tags')
    for tag in tags:
        assert _TAG_VALUE.fullmatch(str(tag['Value'])), tag
    # the tag reads back as the job PBS knows
    assert (
        EC2Instance(
            data={
                'Tags': [
                    {'Key': 'idea:JobId', 'Value': get_tag_value('idea:JobId', tags)}
                ]
            }
        ).soca_job_id
        == '311[]'
    )

    builder = SocaJobBuilder(
        context=context, params={'nodes': 1, 'cpus': 1}, queue_profile=queue_profile
    )
    job_params, provisioning_options = builder.build()
    job = SocaJob(
        name='array',
        job_id='311[]',
        job_uid='array-311',
        owner='mockuser',
        project='default',
        cluster_name='idea-mock',
        params=job_params,
        queue='test',
        queue_type='test',
        scaling_mode=scaling_mode,
        provisioning_options=provisioning_options,
    )
    job.job_group = job.get_job_group()
    stack_builder = CloudFormationStackBuilder(context=context, job=job)
    assert _STACK_NAME.fullmatch(job.get_compute_stack()), job.get_compute_stack()
    for tag in stack_builder.build_cfn_stack_tags():
        assert _TAG_VALUE.fullmatch(str(tag['Value'])), tag

    context.config().put('cluster.cloudwatch_logs.enabled', True)
    context.config().put('scheduler.cloudwatch_logs.enabled', True)
    captured = {}

    class Captured(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise Captured

    monkeypatch.setattr(
        stack_builder_module.BootstrapUtils,
        'check_and_attach_cloudwatch_logging_and_metrics',
        capture,
    )
    with pytest.raises(Captured):
        stack_builder.build_user_data()
    assert _NAMESPACE.fullmatch(captured['metrics_namespace'])
    assert _LOG_GROUP.fullmatch(captured['log_files'][0].log_group_name)


def test_a_plain_job_id_is_its_own_aws_job_id():
    assert ModelUtils.aws_job_id('42') == '42'
    assert ModelUtils.pbs_job_id('42') == '42'
    assert ModelUtils.pbs_job_id(ModelUtils.aws_job_id('311[]')) == '311[]'
    assert ModelUtils.pbs_job_id(None) is None


@pytest.mark.parametrize('iops, volume_type', [(0, 'gp3'), (3000, 'io1')])
def test_an_ebs_scratch_job_builds_its_launch_template(context, iops, volume_type):
    params = {'nodes': 1, 'cpus': 1, 'scratch_storage_size': 100}
    if iops > 0:
        params['scratch_iops'] = iops
    result = build_template(
        context=context,
        job_name='ebs-scratch',
        params=params,
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5.large']),
        ),
    )
    mappings = result.template.get(
        'Resources.NodeLaunchTemplate.Properties.LaunchTemplateData.BlockDeviceMappings'
    )
    assert len(mappings) == 2
    scratch = mappings[1]
    assert scratch['DeviceName'] == '/dev/xvdbx'
    assert scratch['Ebs']['VolumeSize'] == 100
    assert scratch['Ebs']['VolumeType'] == volume_type
    assert scratch['Ebs']['Encrypted'] is True
    if iops > 0:
        assert scratch['Ebs']['Iops'] == iops


@pytest.mark.parametrize(
    'email, tagged',
    [
        ('first.last+hpc@example.invalid', 'first.last+hpc@example.invalid'),
        ("first.o'last@example.invalid", None),
        ('first last@example.invalid', None),
    ],
)
def test_only_a_plain_owner_email_reaches_the_node_tagging(
    context, monkeypatch, email, tagged
):
    from ideadatamodel import GetUserResult, User

    queue_profile = HpcQueueProfile(
        name='test',
        queues=['test'],
        default_job_params=SocaJobParams(instance_types=['c5.large']),
    )
    job_params, provisioning_options = SocaJobBuilder(
        context=context, params={'nodes': 1, 'cpus': 1}, queue_profile=queue_profile
    ).build()
    job = SocaJob(
        name='tags',
        job_id='42',
        job_uid='tags-42',
        owner='mockuser',
        project='default',
        cluster_name='idea-mock',
        params=job_params,
        queue='test',
        queue_type='test',
        provisioning_options=provisioning_options,
    )
    job.job_group = job.get_job_group()
    monkeypatch.setattr(
        context.accounts_client,
        'get_user',
        lambda _request: GetUserResult(user=User(username='mockuser', email=email)),
    )

    class Captured(Exception):
        pass

    def capture(**_kwargs):
        raise Captured

    monkeypatch.setattr(
        stack_builder_module.BootstrapUtils,
        'check_and_attach_cloudwatch_logging_and_metrics',
        capture,
    )
    with pytest.raises(Captured):
        CloudFormationStackBuilder(context=context, job=job).build_user_data()
    assert job.owner_email == tagged


def fsx_template(context, params: Dict, fsx_default=None) -> BuildTemplateResult:
    return build_template(
        context=context,
        job_name='fsx-default',
        params={'nodes': 1, 'cpus': 1, 'fsx_lustre': 'true', **params},
        queue_profile=HpcQueueProfile(
            name='normal',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(
                instance_types=['c5.large'], fsx_lustre=fsx_default
            ),
        ),
    )


def fsx_resource_text(template_yml: str) -> str:
    lines, inside = [], False
    for line in template_yml.splitlines(keepends=True):
        if line.startswith('  FSxForLustre:'):
            inside = True
        elif inside and re.match(r'^  [A-Za-z]', line):
            break
        if inside:
            lines.append(line)
    return ''.join(lines)


def test_default_scratch_2_fsx_renders_as_before(context):
    # data/: the FSxForLustre resource rendered for this job at the branch base (1a077ec7)
    import pathlib

    expected = (
        pathlib.Path(__file__).parent
        / 'data'
        / 'fsx_lustre_scratch_2_default_resource.txt'
    ).read_text()
    assert fsx_resource_text(fsx_template(context, {}).template_yml) == expected


@pytest.mark.parametrize(
    'queue_default,job_params,deployment_type,throughput',
    [
        # the queue's per-unit throughput is used, not a fixed 200
        (('persistent_1', 100), {}, 'PERSISTENT_1', 100),
        (('persistent_1', None), {}, 'PERSISTENT_1', 200),
        # PERSISTENT_2 needs a throughput of its own range: 125, 250, 500, 1000
        (('persistent_2', 500), {}, 'PERSISTENT_2', 500),
        (('persistent_2', None), {}, 'PERSISTENT_2', 125),
        # the job picks PERSISTENT_2 on a PERSISTENT_1 queue: its throughput doesn't fit
        (
            ('persistent_1', 200),
            {'fsx_lustre_deployment_type': 'persistent_2'},
            'PERSISTENT_2',
            125,
        ),
        (
            ('persistent_2', 250),
            {'fsx_lustre_per_unit_throughput': '1000'},
            'PERSISTENT_2',
            1000,
        ),
    ],
)
def test_persistent_fsx_gets_a_per_unit_throughput(
    context, queue_default, job_params, deployment_type, throughput
):
    from ideadatamodel import SocaFSxLustreConfig

    deployment, per_unit = queue_default
    result = fsx_template(
        context,
        job_params,
        fsx_default=SocaFSxLustreConfig(
            enabled=True, deployment_type=deployment, per_unit_throughput=per_unit
        ),
    )
    lustre = result.template.get(
        'Resources.FSxForLustre.Properties.LustreConfiguration'
    )
    assert lustre.get('DeploymentType') == deployment_type
    assert lustre.get('PerUnitStorageThroughput') == throughput


def test_a_persistent_2_throughput_from_the_persistent_1_range_is_refused(context):
    with pytest.raises(exceptions.SocaException):
        fsx_template(
            context,
            {
                'fsx_lustre_deployment_type': 'persistent_2',
                'fsx_lustre_per_unit_throughput': '200',
            },
        )


@pytest.mark.parametrize('keep_forever', [False, True])
def test_a_persistent_pool_records_its_instance_profile(context, keep_forever):
    # SCH-03: the hook checks a keep-forever job against the pool's profile
    queue_profile = HpcQueueProfile(
        name='pool',
        queues=['pool'],
        scaling_mode=SocaScalingMode.BATCH if not keep_forever else None,
        keep_forever=keep_forever,
        terminate_when_idle=3 if not keep_forever else 0,
        stack_uuid='pool-stack',
        default_job_params=SocaJobParams(instance_types=['c5.large']),
    )
    builder = SocaJobBuilder(
        context=context, params={'nodes': 1, 'cpus': 1}, queue_profile=queue_profile
    )
    job_params, provisioning_options = builder.build()
    job = SocaJob(
        name='pool',
        job_id='1',
        job_uid='pool-1',
        owner='mockuser',
        project='default',
        cluster_name='idea-mock',
        params=job_params,
        queue='pool',
        queue_type='pool',
        provisioning_options=provisioning_options,
    )
    job.job_group = job.get_job_group()
    tags = CloudFormationStackBuilder(context=context, job=job).build_cfn_stack_tags()
    value = get_tag_value('idea:InstanceProfile', tags)
    assert value == (job_params.instance_profile if keep_forever else None)


def test_the_scheduler_creates_the_job_log_group_with_its_retention(
    context, monkeypatch
):
    # H04: retention is set by the scheduler, not by the compute node role
    from unittest.mock import Mock

    logs = Mock()
    logs.exceptions.ResourceAlreadyExistsException = type('Exists', (Exception,), {})
    monkeypatch.setattr(type(context.aws()), 'logs', lambda self: logs)
    context.config().put('cluster.cloudwatch_logs.enabled', True)
    context.config().put('scheduler.cloudwatch_logs.enabled', True)
    context.config().put('cluster.cloudwatch_logs.retention_in_days', 30)
    fsx_template(context, {'fsx_lustre': 'false'})
    [created] = logs.create_log_group.call_args_list
    group = created.kwargs['logGroupName']
    assert group.startswith(f'/{context.cluster_name()}/') and '/job-' in group
    logs.put_retention_policy.assert_called_once_with(
        logGroupName=group, retentionInDays=30
    )


def test_a_project_tag_cannot_replace_the_schedulers_own_tags(context, monkeypatch):
    # NM-01: a project tag with a scheduler key named another stack, and clean-up acted on it
    from ideadatamodel import SocaKeyValue

    project = context.projects_client.get_project_by_name('default')
    real_get_project = type(context.projects_client).get_project

    def get_project(self, request):
        result = real_get_project(self, request)
        result.project.tags = (result.project.tags or []) + [
            SocaKeyValue(key='idea:StackId', value='someone-elses-stack'),
            SocaKeyValue(key='idea:ClusterName', value='other-cluster'),
            SocaKeyValue(key='team', value='cfd'),
        ]
        return result

    monkeypatch.setattr(type(context.projects_client), 'get_project', get_project)
    result = fsx_template(context, {'fsx_lustre': 'false'})
    tags = result.template.get('Resources.AutoScalingComputeGroup.Properties.Tags')
    assert get_tag_value('idea:StackId', tags) != 'someone-elses-stack'
    assert get_tag_value('idea:ClusterName', tags) == 'idea-mock'
    assert get_tag_value('team', tags) == 'cfd'
    assert project is not None


def test_an_efa_spot_fleet_is_given_the_jobs_subnet(context):
    # SP-19: the EFA overrides carried no subnet, while the dry run used the job's
    result = build_template(
        context=context,
        job_name='efa-spot',
        params={'nodes': 1, 'cpus': 1, 'efa_support': 'true', 'spot_price': 'auto'},
        queue_profile=HpcQueueProfile(
            name='compute',
            queues=['normal'],
            scaling_mode=SocaScalingMode.SINGLE_JOB,
            default_job_params=SocaJobParams(instance_types=['c5n.18xlarge']),
        ),
    )
    configs = result.template.get(
        'Resources.SpotFleet.Properties.SpotFleetRequestConfigData.LaunchTemplateConfigs'
    )
    overrides = configs[0]['Overrides']
    subnet = context.config().get_list('cluster.network.private_subnets')[0]
    assert overrides and all(o['SubnetId'] == subnet for o in overrides)
