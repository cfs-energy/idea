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
Test Cases for JobProvisioningUtil
"""

from ideadatamodel import SocaJob, SocaJobParams
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideasdk.aws import AWSUtil
from ideatestutils import MockInstanceTypes


def build_job(enable_efa_support: bool) -> SocaJob:
    return SocaJob(
        name='mock-job',
        job_id='1',
        job_uid='mock-job-uid',
        params=SocaJobParams(
            nodes=2,
            base_os='amazonlinux2',
            instance_ami='ami-mock',
            instance_types=['hpc7a.96xlarge'],
            subnet_ids=['subnet-mock1', 'subnet-mock2'],
            security_groups=['sg-mock'],
            enable_efa_support=enable_efa_support,
        ),
    )


def test_ec2_dry_run_request_without_efa(context):
    """
    without EFA the dry run keeps the top level subnet / security group shape
    """
    util = JobProvisioningUtil(context=context, jobs=[build_job(False)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert request['DryRun'] is True
    assert request['InstanceType'] == 'hpc7a.96xlarge'
    assert request['SubnetId'] == 'subnet-mock1'
    assert request['SecurityGroupIds'] == ['sg-mock']
    assert 'NetworkInterfaces' not in request


def test_ec2_dry_run_request_with_efa(context):
    """
    with EFA the dry run must mirror the launch template: an InterfaceType='efa' network
    interface instead of top level SubnetId / SecurityGroupIds, which RunInstances rejects
    alongside NetworkInterfaces.
    """
    util = JobProvisioningUtil(context=context, jobs=[build_job(True)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert 'SubnetId' not in request
    assert 'SecurityGroupIds' not in request

    interfaces = request['NetworkInterfaces']
    assert len(interfaces) == 1
    assert interfaces[0]['InterfaceType'] == 'efa'
    assert interfaces[0]['DeviceIndex'] == 0
    assert interfaces[0]['NetworkCardIndex'] == 0
    assert interfaces[0]['SubnetId'] == 'subnet-mock1'
    assert interfaces[0]['Groups'] == ['sg-mock']


def test_ec2_dry_run_request_with_efa_multi_rail(context, monkeypatch):
    """
    with scheduler.efa.multi_rail_enabled the dry run models the same interface list the
    launch template attaches (build_efa_network_interface_shapes) - one EFA interface per
    network card, subnet on every interface - so a multi-ENI rejection is caught at dry run.
    """

    def get_multi_card_instance_type(_aws_util, instance_type: str):
        ec2_instance_type = MockInstanceTypes().get_instance_type(instance_type)
        network_info = ec2_instance_type.instance_type_data()['NetworkInfo']
        network_info['MaximumNetworkCards'] = 2
        network_info['EfaInfo'] = {'MaximumEfaInterfaces': 2}
        return ec2_instance_type

    monkeypatch.setattr(AWSUtil, 'get_ec2_instance_type', get_multi_card_instance_type)
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    util = JobProvisioningUtil(context=context, jobs=[build_job(True)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert 'SubnetId' not in request
    assert 'SecurityGroupIds' not in request

    interfaces = request['NetworkInterfaces']
    assert len(interfaces) == 2
    assert [interface['NetworkCardIndex'] for interface in interfaces] == [0, 1]
    # AWS shape: DeviceIndex 0 on the first network card, 1 on every additional card
    assert [interface['DeviceIndex'] for interface in interfaces] == [0, 1]
    for interface in interfaces:
        assert interface['InterfaceType'] == 'efa'
        assert interface['SubnetId'] == 'subnet-mock1'
        assert interface['Groups'] == ['sg-mock']
