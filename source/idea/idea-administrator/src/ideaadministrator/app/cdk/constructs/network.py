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

__all__ = (
    'ElasticIP',
    'Vpc',
    'SecurityGroup',
    'BastionHostSecurityGroup',
    'ExternalLoadBalancerSecurityGroup',
    'InternalLoadBalancerSecurityGroup',
    'SharedStorageSecurityGroup',
    'OpenLDAPServerSecurityGroup',
    'WebPortalSecurityGroup',
    'SchedulerSecurityGroup',
    'ComputeNodeSecurityGroup',
    'VpcEndpointSecurityGroup',
    'VpcGatewayEndpoint',
    'VpcInterfaceEndpoint',
    'OpenSearchSecurityGroup',
    'DefaultClusterSecurityGroup',
    'VirtualDesktopPublicLoadBalancerAccessSecurityGroup',
    'VirtualDesktopBastionAccessSecurityGroup',
    'VirtualDesktopBrokerSecurityGroup',
    'WebAcl',
)

from typing import List, Optional, Dict

import aws_cdk as cdk
import constructs
from aws_cdk import aws_ec2 as ec2, aws_logs as logs, aws_iam as iam, aws_wafv2 as wafv2

from ideaadministrator.app.cdk.constructs import (
    SocaBaseConstruct,
    CreateTagsCustomResource,
    IdeaNagSuppression,
)
from ideaadministrator.app_context import AdministratorContext
from ideadatamodel import constants
from ideasdk.utils import Utils


class ElasticIP(SocaBaseConstruct, ec2.CfnEIP):
    def __init__(
        self, context: AdministratorContext, name: str, scope: constructs.Construct
    ):
        self.context = context
        super().__init__(context, name, scope)


class Vpc(SocaBaseConstruct, ec2.Vpc):
    def __init__(
        self, context: AdministratorContext, name: str, scope: constructs.Construct
    ):
        self.context = context
        self.scope = scope
        super().__init__(
            context,
            name,
            scope,
            ip_addresses=ec2.IpAddresses.cidr(
                context.config().get_string('cluster.network.vpc_cidr_block')
            ),
            nat_gateways=context.config().get_int('cluster.network.nat_gateways'),
            enable_dns_support=True,
            enable_dns_hostnames=True,
            max_azs=context.config().get_int('cluster.network.max_azs'),
            subnet_configuration=self.build_subnet_configuration(),
            flow_logs=self.build_flow_logs(),
        )

    def build_flow_logs(self) -> Optional[Dict[str, ec2.FlowLogOptions]]:
        vpc_flow_logs = self.context.config().get_bool(
            'cluster.network.vpc_flow_logs', False
        )
        if not vpc_flow_logs:
            return None

        vpc_flow_logs_removal_policy = self.context.config().get_string(
            'cluster.network.vpc_flow_logs_removal_policy', 'DESTROY'
        )
        log_group_name = self.context.config().get_string(
            'cluster.network.vpc_flow_logs_group_name',
            f'{self.cluster_name}-vpc-flow-logs',
        )
        log_group = logs.LogGroup(
            self.scope,
            'vpc-flow-logs-group',
            log_group_name=log_group_name,
            removal_policy=cdk.RemovalPolicy(vpc_flow_logs_removal_policy),
        )
        iam_role = iam.Role(
            self.scope,
            'vpc-flow-logs-role',
            assumed_by=self.build_service_principal('vpc-flow-logs'),
            description=f'IAM Role for VPC Flow Logs, Cluster: {self.cluster_name}',
            role_name=f'{self.cluster_name}-vpc-flow-logs-{self.context.aws().aws_region()}',
        )
        return {
            'cloud-watch': ec2.FlowLogOptions(
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                    log_group=log_group, iam_role=iam_role
                ),
                traffic_type=ec2.FlowLogTrafficType.ALL,
            )
        }

    @property
    def nat_gateway_ips(self) -> List[constructs.IConstruct]:
        result = []
        if self.public_subnets is None:
            return result
        for subnet in self.public_subnets:
            eip = subnet.node.try_find_child('EIP')
            if eip is None:
                continue
            result.append(eip)
        return result

    def build_subnet_configuration(self) -> List[ec2.SubnetConfiguration]:
        result = []
        # a public subnet always is required to support cognito access as Cognito does support VPC endpoints.
        public_subnet_config = self.build_public_subnet_config()
        result.append(public_subnet_config)

        # private can be optional
        private_subnet_config = self.build_private_subnet_config()
        if private_subnet_config is not None:
            result.append(private_subnet_config)

        # isolated can be optional
        isolated_subnet_config = self.build_isolated_subnet_config()
        if isolated_subnet_config is not None:
            result.append(isolated_subnet_config)

        return result

    def build_public_subnet_config(self) -> ec2.SubnetConfiguration:
        cidr_mask = self.context.config().get_int(
            'cluster.network.subnet_config.public.cidr_mask', 26
        )
        return ec2.SubnetConfiguration(
            name='public', cidr_mask=cidr_mask, subnet_type=ec2.SubnetType.PUBLIC
        )

    def build_private_subnet_config(self) -> Optional[ec2.SubnetConfiguration]:
        cidr_mask = self.context.config().get_int(
            'cluster.network.subnet_config.private.cidr_mask', 18
        )
        return ec2.SubnetConfiguration(
            name='private',
            cidr_mask=cidr_mask,
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
        )

    def build_isolated_subnet_config(self) -> Optional[ec2.SubnetConfiguration]:
        """
        build isolated subnet only if subnet config is provided.
        do not return any default value.
        :return: isolated subnet configuration
        """

        cidr_mask = self.context.config().get_int(
            'cluster.network.subnet_config.isolated.cidr_mask', None
        )
        if cidr_mask is None:
            return None

        return ec2.SubnetConfiguration(
            name='isolated',
            cidr_mask=cidr_mask,
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
        )

    @property
    def public_subnet_ids(self) -> List[str]:
        result = []
        for subnet in self.public_subnets:
            result.append(subnet.subnet_id)
        return result

    @property
    def private_subnet_ids(self) -> List[str]:
        result = []
        for subnet in self.private_subnets:
            result.append(subnet.subnet_id)
        return result


class SecurityGroup(SocaBaseConstruct, ec2.SecurityGroup):
    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        allow_all_outbound=False,
        description=Optional[str],
    ):
        self.context = context
        super().__init__(
            context,
            name,
            scope,
            security_group_name=self.build_resource_name(name),
            vpc=vpc,
            allow_all_outbound=allow_all_outbound,
            description=description,
        )
        self.vpc = vpc
        self.add_nag_suppression(suppressions=[])

    def add_nag_suppression(
        self,
        suppressions: List[IdeaNagSuppression],
        construct: constructs.IConstruct = None,
        apply_to_children: bool = True,
    ):
        updated_suppressions = [
            # [Warning at /idea-test1-cluster/external-load-balancer-security-group/Resource] CdkNagValidationFailure: 'AwsSolutions-EC23' threw an error during validation. This is generally caused by a parameter referencing an intrinsic function. For more details enable verbose logging.'
            IdeaNagSuppression(
                rule_id='AwsSolutions-EC23',
                reason='suppress warning: parameter referencing intrinsic function',
            )
        ]
        if suppressions:
            updated_suppressions += suppressions
        super().add_nag_suppression(updated_suppressions, construct, apply_to_children)

    def add_outbound_traffic_rule(self):
        self.add_egress_rule(
            ec2.Peer.ipv4('0.0.0.0/0'),
            ec2.Port.tcp_range(0, 65535),
            description='Allow all egress for TCP',
        )
        self.add_egress_rule(
            ec2.Peer.ipv6('::/0'),
            ec2.Port.tcp_range(0, 65535),
            description='Allow all egress for TCP',
        )

    def add_api_ingress_rule(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(8443),
            description='Allow HTTP traffic from all VPC nodes for API access',
        )

    def add_loadbalancer_ingress_rule(
        self, loadbalancer_security_group: ec2.ISecurityGroup
    ):
        self.add_ingress_rule(
            loadbalancer_security_group,
            ec2.Port.tcp(8443),
            description='Allow HTTPs traffic from Load Balancer',
        )

    def add_bastion_host_ingress_rule(
        self, bastion_host_security_group: ec2.ISecurityGroup
    ):
        self.add_ingress_rule(
            bastion_host_security_group,
            ec2.Port.tcp(22),
            description='Allow SSH from Bastion Host',
        )

    def add_active_directory_rules(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.udp_range(0, 1024),
            description='Allow UDP Traffic from VPC. Required for Directory Service',
        )
        self.add_egress_rule(
            ec2.Peer.ipv4('0.0.0.0/0'),
            ec2.Port.udp_range(0, 1024),
            description='Allow UDP Traffic. Required for Directory Service',
        )
        self.add_egress_rule(
            ec2.Peer.ipv6('::/0'),
            ec2.Port.udp_range(0, 1024),
            description='Allow UDP Traffic. Required for Directory Service',
        )


class BastionHostSecurityGroup(SecurityGroup):
    """
    Security Group for Bastion Host
    Only instance that will be in Public Subnet. All other instances will be launched in private subnets.
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        cluster_prefix_list_id: str,
    ):
        super().__init__(
            context, name, scope, vpc, description='Bastion host security group'
        )
        self.cluster_prefix_list_id = cluster_prefix_list_id
        self.setup_ingress()
        self.setup_egress()

        if self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        cluster_prefix_list = ec2.Peer.prefix_list(self.cluster_prefix_list_id)
        self.add_ingress_rule(
            cluster_prefix_list,
            ec2.Port.tcp(22),
            description='Allow SSH access from Cluster Prefix List to Bastion Host',
        )

        prefix_list_ids = self.context.config().get_list(
            'cluster.network.prefix_list_ids'
        )
        if Utils.is_not_empty(prefix_list_ids):
            for prefix_list_id in prefix_list_ids:
                prefix_list = ec2.Peer.prefix_list(prefix_list_id)
                self.add_ingress_rule(
                    prefix_list,
                    ec2.Port.tcp(22),
                    description='Allow SSH access from Prefix List to Bastion Host',
                )

        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(22),
            description='Allow SSH traffic from all VPC nodes',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class ExternalLoadBalancerSecurityGroup(SecurityGroup):
    """
    External Load Balancer Security Group
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        cluster_prefix_list_id: str,
        bastion_host_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(
            context,
            name,
            scope,
            vpc,
            description='External Application Load Balancer security group',
        )
        self.cluster_prefix_list_id = cluster_prefix_list_id
        self.bastion_host_security_group = bastion_host_security_group
        self.setup_ingress()
        self.setup_egress()

    def add_peer_ingress_rule(self, peer: ec2.IPeer, peer_type: str):
        self.add_ingress_rule(
            peer,
            ec2.Port.tcp(443),
            description=f'Allow HTTPS access from {peer_type} to ALB',
        )

        self.add_ingress_rule(
            peer,
            ec2.Port.tcp(80),
            description=f'Allow HTTP access from {peer_type} to ALB',
        )

    def setup_ingress(self):
        cluster_prefix_list = ec2.Peer.prefix_list(self.cluster_prefix_list_id)
        self.add_peer_ingress_rule(cluster_prefix_list, 'Cluster Prefix List')

        prefix_list_ids = self.context.config().get_list(
            'cluster.network.prefix_list_ids'
        )
        if Utils.is_not_empty(prefix_list_ids):
            for prefix_list_id in prefix_list_ids:
                if Utils.is_not_empty(prefix_list_id):
                    prefix_list = ec2.Peer.prefix_list(prefix_list_id)
                    self.add_peer_ingress_rule(prefix_list, 'Prefix List')

        self.add_ingress_rule(
            self.bastion_host_security_group,
            ec2.Port.tcp(80),
            description='Allow HTTP from Bastion Host',
        )

        self.add_ingress_rule(
            self.bastion_host_security_group,
            ec2.Port.tcp(443),
            description='Allow HTTPs from Bastion Host',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()

    def add_nat_gateway_ips_ingress_rule(
        self, nat_gateway_ips: List[constructs.IConstruct]
    ):
        # allow NAT EIP to communicate with ALB. This so that virtual desktop instances or instances
        # in private subnets can open the Web Portal or Access the APIs using the ALB endpoint
        for eip in nat_gateway_ips:
            self.add_ingress_rule(
                ec2.Peer.ipv4(f'{eip.ref}/32'),
                ec2.Port.tcp(443),
                description='Allow NAT EIP to communicate to ALB.',
            )


class SharedStorageSecurityGroup(SecurityGroup):
    """
    Shared Storage Security Group
    Attached to EFS and FSx File Systems. Access is open to all nodes from VPC.

    Note for Lustre rules:
    Although Lustre is optional and may not be used for /apps or /data,
    compute nodes can mount FSx Lustre on-demand to /scratch. For this reason, Lustre rules are provisioned during cluster creation.
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context,
            name,
            scope,
            vpc,
            description='Shared Storage security group for EFS/FSx file systems',
        )
        self.setup_ingress()
        self.setup_egress()

    def setup_ingress(self):
        # NFS
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(2049),
            description='Allow NFS traffic from all VPC nodes to EFS',
        )

        # FSx for Lustre
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(988),
            description='Allow FSx Lustre traffic from all VPC nodes',
        )
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp_range(1021, 1023),
            description='Allow FSx Lustre traffic from all VPC nodes',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class OpenLDAPServerSecurityGroup(SecurityGroup):
    """
    OpenLDAP Server Security Group
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(
            context, name, scope, vpc, description='OpenLDAP server security group'
        )
        self.bastion_host_security_group = bastion_host_security_group
        self.setup_ingress()
        self.setup_egress()

    def setup_ingress(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(389),
            description='Allow LDAP traffic from all VPC nodes',
        )
        self.add_api_ingress_rule()
        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class WebPortalSecurityGroup(SecurityGroup):
    """
    Cluster Manager Security Group
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
        loadbalancer_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(
            context, name, scope, vpc, description='Web Portal security group'
        )
        self.bastion_host_security_group = bastion_host_security_group
        self.loadbalancer_security_group = loadbalancer_security_group
        self.setup_ingress()
        self.setup_egress()
        if self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        self.add_api_ingress_rule()
        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)
        self.add_loadbalancer_ingress_rule(self.loadbalancer_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class SchedulerSecurityGroup(SecurityGroup):
    """
    HPC Scheduler Security Group
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
        loadbalancer_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(
            context, name, scope, vpc, description='Scheduler security group'
        )
        self.bastion_host_security_group = bastion_host_security_group
        self.loadbalancer_security_group = loadbalancer_security_group
        self.setup_ingress()
        self.setup_egress()
        if self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        self.add_api_ingress_rule()

        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp_range(0, 65535),
            description='Allow all TCP traffic from VPC to scheduler',
        )

        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)

        self.add_loadbalancer_ingress_rule(self.loadbalancer_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class ComputeNodeSecurityGroup(SecurityGroup):
    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context, name, scope, vpc, description='Compute Node security group'
        )
        self.setup_ingress()
        self.setup_egress()
        if self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp_range(0, 65535),
            description='All TCP traffic from all VPC nodes to compute node',
        )

        self.add_ingress_rule(
            self,
            ec2.Port.all_traffic(),
            description='Allow all traffic between compute nodes and EFA',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()

        self.add_egress_rule(
            self,
            ec2.Port.all_traffic(),
            description='Allow all traffic between compute nodes and EFA',
        )


class VirtualDesktopBastionAccessSecurityGroup(SecurityGroup):
    """
    Virtual Desktop Security Group with Bastion Access
    """

    component_name: str

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
        description: str,
        directory_service_access: bool,
        component_name: str,
    ):
        super().__init__(context, name, scope, vpc, description=description)
        self.component_name = component_name
        self.bastion_host_security_group = bastion_host_security_group
        self.setup_ingress()
        self.setup_egress()
        if directory_service_access and self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        self.add_api_ingress_rule()
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.all_traffic(),
            description=f'Allow all Internal traffic TO {self.component_name}',
        )
        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class VirtualDesktopPublicLoadBalancerAccessSecurityGroup(SecurityGroup):
    """
    Virtual Desktop Security Group with Bastion and Public Loadbalancer Access
    """

    component_name: str

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
        description: str,
        directory_service_access: bool,
        component_name: str,
        public_loadbalancer_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(context, name, scope, vpc, description=description)
        self.component_name = component_name
        self.public_loadbalancer_security_group = public_loadbalancer_security_group
        self.bastion_host_security_group = bastion_host_security_group
        self.setup_ingress()
        self.setup_egress()
        if directory_service_access and self.is_ds_activedirectory():
            self.add_active_directory_rules()

    def setup_ingress(self):
        self.add_api_ingress_rule()
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.all_traffic(),
            description=f'Allow all Internal traffic TO {self.component_name}',
        )
        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)
        self.add_loadbalancer_ingress_rule(self.public_loadbalancer_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class VirtualDesktopBrokerSecurityGroup(SecurityGroup):
    """
    Virtual Desktop Broker Security Group
    """

    component_name: str

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
        bastion_host_security_group: ec2.ISecurityGroup,
        description: str,
        component_name: str,
        public_loadbalancer_security_group: ec2.ISecurityGroup,
    ):
        super().__init__(context, name, scope, vpc, description=description)
        self.component_name = component_name
        self.public_loadbalancer_security_group = public_loadbalancer_security_group
        self.bastion_host_security_group = bastion_host_security_group
        self.setup_ingress()
        self.setup_egress()

    def setup_ingress(self):
        broker_client_port = self.context.config().get_int(
            'virtual-desktop-controller.dcv_broker.client_communication_port',
            required=True,
        )
        broker_agent_port = self.context.config().get_int(
            'virtual-desktop-controller.dcv_broker.agent_communication_port',
            required=True,
        )
        broker_gateway_port = self.context.config().get_int(
            'virtual-desktop-controller.dcv_broker.gateway_communication_port',
            required=True,
        )

        _broker_port_list = sorted(
            [broker_client_port, broker_agent_port, broker_gateway_port]
        )

        # Save SG rule entries if the ports are consecutive and can be expressed as a range
        if _broker_port_list == list(
            range(min(_broker_port_list), max(_broker_port_list) + 1)
        ):
            self.add_ingress_rule(
                ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
                ec2.Port.tcp_range(min(_broker_port_list), max(_broker_port_list)),
                description=f'Allow VPC to broker ports {min(_broker_port_list)}-{max(_broker_port_list)}',
            )
        else:
            # Non-consecutive ports - single rule per port
            for _port in _broker_port_list:
                self.add_ingress_rule(
                    ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
                    ec2.Port.tcp(_port),
                    description=f'Allow VPC to broker port {_port}',
                )

        # Broker to Broker communications
        # TODO - these are hard-coded in templates and need to be pulled up to config knobs
        for _port in [47100, 47200, 47500]:
            self.add_ingress_rule(
                self,
                ec2.Port.tcp(_port),
                description=f'Allow broker to broker port {_port}',
            )
        self.add_bastion_host_ingress_rule(self.bastion_host_security_group)
        self.add_loadbalancer_ingress_rule(self.public_loadbalancer_security_group)

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class VpcEndpointSecurityGroup(SecurityGroup):
    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context,
            name,
            scope,
            vpc,
            allow_all_outbound=True,
            description='VPC Endpoints Security Group',
        )
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            description='Allow HTTPS traffic from VPC',
        )


class VpcGatewayEndpoint(SocaBaseConstruct):
    def __init__(
        self,
        context: AdministratorContext,
        scope: constructs.Construct,
        service: str,
        vpc: ec2.IVpc,
        create_tags: CreateTagsCustomResource,
    ):
        super().__init__(context, f'{service}-gateway-endpoint')
        self.scope = scope

        self.endpoint = vpc.add_gateway_endpoint(
            self.construct_id, service=ec2.GatewayVpcEndpointAwsService(name=service)
        )

        create_tags.apply(
            name=self.name,
            resource_id=self.endpoint.vpc_endpoint_id,
            tags={
                constants.IDEA_TAG_NAME: self.name,
                constants.IDEA_TAG_CLUSTER_NAME: self.cluster_name,
            },
        )


class VpcInterfaceEndpoint(SocaBaseConstruct):
    def __init__(
        self,
        context: AdministratorContext,
        scope: constructs.Construct,
        service: str,
        vpc: ec2.IVpc,
        vpc_endpoint_security_group: ec2.ISecurityGroup,
        create_tags: CreateTagsCustomResource,
    ):
        super().__init__(context, f'{service}-vpc-endpoint')
        self.scope = scope

        # this is a change from 2.x behaviour, where access to VPC endpoints was restricted by security group.
        # in 3.x, since security groups for individual components do not exist during cluster/network creation,
        # all VPC traffic can communicate with VPC Interface endpoints.
        self.endpoint = vpc.add_interface_endpoint(
            self.construct_id,
            service=ec2.InterfaceVpcEndpointAwsService(name=service),
            open=True,
            # setting private_dns_enabled = True can be problem in GovCloud where Route53 and in turn Private Hosted Zones is not supported.
            private_dns_enabled=True,
            lookup_supported_azs=True,
            security_groups=[vpc_endpoint_security_group],
        )

        create_tags.apply(
            name=self.name,
            resource_id=self.endpoint.vpc_endpoint_id,
            tags={
                constants.IDEA_TAG_NAME: self.name,
                constants.IDEA_TAG_CLUSTER_NAME: self.cluster_name,
            },
        )

    def get_endpoint_url(self) -> str:
        dns = cdk.Fn.select(
            1,
            cdk.Fn.split(':', cdk.Fn.select(0, self.endpoint.vpc_endpoint_dns_entries)),
        )
        return f'https://{dns}'


class OpenSearchSecurityGroup(SecurityGroup):
    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context, name, scope, vpc, description='OpenSearch security group'
        )
        self.setup_ingress()
        self.setup_egress()

    def setup_ingress(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            description='Allow HTTPS traffic from all VPC nodes to OpenSearch',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class DefaultClusterSecurityGroup(SecurityGroup):
    """
    Default Cluster Security Group with no inbound or outbound rules.
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context, name, scope, vpc, description='Default Cluster Security'
        )


class InternalLoadBalancerSecurityGroup(SecurityGroup):
    """
    Internal Load Balancer Security Group
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        vpc: ec2.IVpc,
    ):
        super().__init__(
            context,
            name,
            scope,
            vpc,
            description='Internal load balancer security group',
        )
        self.setup_ingress()
        self.setup_egress()

    def setup_ingress(self):
        self.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            description='Allow HTTPS traffic from all VPC nodes to OpenSearch',
        )

    def setup_egress(self):
        self.add_outbound_traffic_rule()


class WebAcl(SocaBaseConstruct):
    """
    AWS WAF WebACL for Application Load Balancer protection
    """

    def __init__(
        self,
        context: AdministratorContext,
        name: str,
        scope: constructs.Construct,
        create_tags: Optional[CreateTagsCustomResource] = None,
    ):
        super().__init__(context, name)
        self.scope = scope
        self.create_tags = create_tags

        self.web_acl = wafv2.CfnWebACL(
            scope,
            f'{self.cluster_name}-{name}-web-acl',
            name=f'{self.cluster_name}-{name}',
            scope='REGIONAL',
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            description=f'WAF WebACL for {self.cluster_name} {name}',
            rules=self._create_managed_rules(),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f'{self.cluster_name}-{name}',
                sampled_requests_enabled=True,
            ),
            tags=[
                cdk.CfnTag(key='Name', value=f'{self.cluster_name}-{name}'),
                cdk.CfnTag(key='idea:ClusterName', value=self.cluster_name),
                cdk.CfnTag(key='idea:Module', value='cluster'),
            ],
        )

        # Configure CloudWatch Logs for WAF (if enabled)
        cloudwatch_logs_enabled = context.config().get_bool(
            'cluster.cloudwatch_logs.enabled', default=False
        )
        if cloudwatch_logs_enabled:
            self._setup_cloudwatch_logging(scope, name)

    def _create_managed_rules(self) -> List[wafv2.CfnWebACL.RuleProperty]:
        """
        Create AWS managed rule groups for common web application protection
        """
        rules = []

        # AWS Managed Rules - Amazon IP Reputation List
        # Blocks requests from IP addresses known to be malicious
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name='AWS-AWSManagedRulesAmazonIpReputationList',
                priority=0,
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name='AWS',
                        name='AWSManagedRulesAmazonIpReputationList',
                    )
                ),
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name='AWS-AWSManagedRulesAmazonIpReputationList',
                    sampled_requests_enabled=True,
                ),
            )
        )

        # AWS Managed Rules - Common Rule Set
        # Provides protection against common application vulnerabilities (OWASP Top 10)
        # Exclude SizeRestrictions_BODY, CrossSiteScripting_BODY, and RestrictedExtensions_QUERYARGUMENTS rules to prevent false positives
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name='AWS-AWSManagedRulesCommonRuleSet',
                priority=1,
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name='AWS',
                        name='AWSManagedRulesCommonRuleSet',
                        version='Version_1.18',
                        excluded_rules=[
                            wafv2.CfnWebACL.ExcludedRuleProperty(
                                name='SizeRestrictions_BODY'
                            ),
                            wafv2.CfnWebACL.ExcludedRuleProperty(
                                name='CrossSiteScripting_BODY'
                            ),
                            wafv2.CfnWebACL.ExcludedRuleProperty(
                                name='RestrictedExtensions_QUERYARGUMENTS'
                            ),
                        ],
                    )
                ),
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name='AWS-AWSManagedRulesCommonRuleSet',
                    sampled_requests_enabled=True,
                ),
            )
        )

        # AWS Managed Rules - Known Bad Inputs Rule Set
        # Blocks requests containing patterns known to be malicious
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name='AWS-AWSManagedRulesKnownBadInputsRuleSet',
                priority=2,
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name='AWS',
                        name='AWSManagedRulesKnownBadInputsRuleSet',
                        version='Version_1.22',
                    )
                ),
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name='AWS-AWSManagedRulesKnownBadInputsRuleSet',
                    sampled_requests_enabled=True,
                ),
            )
        )

        # AWS Managed Rules - Bot Control Rule Set (Optional)
        # Provides bot detection and mitigation capabilities
        # Pricing: $10/month (prorated hourly) + $1 per million requests after first 10M free
        # Common inspection level provides basic bot detection
        # Exclude CategoryHttpLibrary and SignalNonBrowserUserAgent rules to prevent false positives
        bot_control_enabled = self.context.config().get_bool(
            'cluster.load_balancers.external_alb.waf.bot_control.enabled', default=False
        )

        if bot_control_enabled:
            rules.append(
                wafv2.CfnWebACL.RuleProperty(
                    name='AWS-AWSManagedRulesBotControlRuleSet',
                    priority=3,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name='AWS',
                            name='AWSManagedRulesBotControlRuleSet',
                            version='Version_3.2',
                            excluded_rules=[
                                wafv2.CfnWebACL.ExcludedRuleProperty(
                                    name='CategoryHttpLibrary'
                                ),
                                wafv2.CfnWebACL.ExcludedRuleProperty(
                                    name='SignalNonBrowserUserAgent'
                                ),
                            ],
                            managed_rule_group_configs=[
                                wafv2.CfnWebACL.ManagedRuleGroupConfigProperty(
                                    aws_managed_rules_bot_control_rule_set=wafv2.CfnWebACL.AWSManagedRulesBotControlRuleSetProperty(
                                        inspection_level='COMMON'
                                    )
                                )
                            ],
                        )
                    ),
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name='AWS-AWSManagedRulesBotControlRuleSet',
                        sampled_requests_enabled=True,
                    ),
                )
            )

        return rules

    def _setup_cloudwatch_logging(self, scope: constructs.Construct, name: str):
        """
        Configure CloudWatch Logs for WAF WebACL following IDEA patterns
        """
        # WAF requires log group names to start with 'aws-waf-logs-'
        # Format: aws-waf-logs-{cluster-name}-{module}-waf-{name}
        log_group_name = f'aws-waf-logs-{self.cluster_name}-cluster-waf-{name}'

        # Get retention setting from cluster config
        retention_days = self.context.config().get_int(
            'cluster.cloudwatch_logs.retention_in_days', None
        )

        # Create CloudWatch Log Group following IDEA pattern
        log_group_params = {
            'log_group_name': log_group_name,
            'removal_policy': cdk.RemovalPolicy.DESTROY,
        }

        # Apply retention if configured
        if retention_days is not None:
            # Map retention days to CDK enum values
            retention_mapping = {
                1: logs.RetentionDays.ONE_DAY,
                3: logs.RetentionDays.THREE_DAYS,
                5: logs.RetentionDays.FIVE_DAYS,
                7: logs.RetentionDays.ONE_WEEK,
                14: logs.RetentionDays.TWO_WEEKS,
                30: logs.RetentionDays.ONE_MONTH,
                60: logs.RetentionDays.TWO_MONTHS,
                90: logs.RetentionDays.THREE_MONTHS,
                120: logs.RetentionDays.FOUR_MONTHS,
                150: logs.RetentionDays.FIVE_MONTHS,
                180: logs.RetentionDays.SIX_MONTHS,
                365: logs.RetentionDays.ONE_YEAR,
                400: logs.RetentionDays.THIRTEEN_MONTHS,
                545: logs.RetentionDays.EIGHTEEN_MONTHS,
                731: logs.RetentionDays.TWO_YEARS,
                1827: logs.RetentionDays.FIVE_YEARS,
                3653: logs.RetentionDays.TEN_YEARS,
            }

            if retention_days in retention_mapping:
                log_group_params['retention'] = retention_mapping[retention_days]
            else:
                self.context.logger().warning(
                    f'Invalid retention days value: {retention_days}. '
                    f'Valid values are: {list(retention_mapping.keys())}. '
                    f'Using default retention (never expire).'
                )

        self.log_group = logs.LogGroup(
            scope, f'{self.cluster_name}-{name}-waf-log-group', **log_group_params
        )

        # Add IDEA standard tags to log group
        cdk.Tags.of(self.log_group).add('Name', f'{self.cluster_name}-{name}-waf-logs')
        cdk.Tags.of(self.log_group).add('idea:ClusterName', self.cluster_name)
        cdk.Tags.of(self.log_group).add('idea:Module', 'cluster')

        # Create WAF Logging Configuration with filters
        logging_config_params = {
            'log_destination_configs': [self.log_group.log_group_arn],
            'resource_arn': self.web_acl.attr_arn,
        }

        # Configure log filters to drop ALLOW actions for cost optimization
        # This can be controlled via configuration
        drop_allow_logs = self.context.config().get_bool(
            'cluster.load_balancers.external_alb.waf.logging.drop_allow_actions',
            default=True,
        )

        if drop_allow_logs:
            logging_config_params['logging_filter'] = {
                'DefaultBehavior': 'KEEP',
                'Filters': [
                    {
                        'Behavior': 'DROP',
                        'Requirement': 'MEETS_ANY',
                        'Conditions': [{'ActionCondition': {'Action': 'ALLOW'}}],
                    }
                ],
            }

        self.logging_configuration = wafv2.CfnLoggingConfiguration(
            scope,
            f'{self.cluster_name}-{name}-waf-logging-config',
            **logging_config_params,
        )

        # Ensure logging configuration is created after both WebACL and log group
        self.logging_configuration.node.add_dependency(self.web_acl)
        self.logging_configuration.node.add_dependency(self.log_group)

    @property
    def web_acl_arn(self) -> str:
        """Get the ARN of the Web ACL"""
        return self.web_acl.attr_arn

    @property
    def web_acl_id(self) -> str:
        """Get the ID of the Web ACL"""
        return self.web_acl.attr_id
