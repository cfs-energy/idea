/**
 * Security-group rule order and peer kinds determine template shape. CIDR peers, including token
 * CIDRs, are inlined into the
 * group's `SecurityGroupIngress`/`SecurityGroupEgress` lists, while prefix-list and security-group
 * peers become separate `AWS::EC2::SecurityGroupIngress`/`Egress` resources whose construct id
 * embeds the peer - a literal prefix-list id or the peer group's unique id verbatim, a token peer
 * as `{IndirectPeer}` and then `'{IndirectPeer2}'`, `'{IndirectPeer3}'`, counted per group in
 * call order (`SecurityGroupBase.renderPeer`). `allowAllOutbound` is false by default, so the
 * explicit egress list renders instead of the L2's allow-all rule.
 */

import { Fn, RemovalPolicy, Tags } from 'aws-cdk-lib';
import type { Construct, IConstruct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

import { ConfigKeyNotFound, isEmpty } from '../../config/cluster-config.ts';
import type { IdeaContext, IdeaNagSuppression } from './base.ts';
import {
  IDEA_TAG_CLUSTER_NAME,
  IDEA_TAG_NAME,
  addCommonTags,
  addNagSuppression as applyNagSuppression,
  buildServicePrincipal,
  constructId,
  isDsActivedirectory,
  resourceName,
} from './base.ts';
import { LOG_RETENTION_DAYS, type CreateTagsCustomResource } from './common.ts';

/** `RemovalPolicy` lookup uses member names, not enum values. */
function removalPolicyByName(name: string): RemovalPolicy {
  if (!Object.prototype.hasOwnProperty.call(RemovalPolicy, name)) {
    throw new Error(`'${name}' is not a valid RemovalPolicy`);
  }
  return RemovalPolicy[name as keyof typeof RemovalPolicy];
}

/** `config.get_int(key, required=True)`: a missing or NULL value raises. */
function requiredInt(ctx: IdeaContext, key: string): number {
  const value = ctx.config.getInt(key);
  if (value === undefined) throw new ConfigKeyNotFound(`'${key}', key: ${key}`);
  return value;
}

export class ElasticIP extends ec2.CfnEIP {
  constructor(ctx: IdeaContext, name: string, scope: Construct) {
    super(scope, constructId(name));
    addCommonTags(ctx, this, name);
  }
}

// --- VPC --------------------------------------------------------------------------------------

/**
 * The log group and role live at the stack scope and are created before the VPC.
 */
function buildFlowLogs(ctx: IdeaContext, scope: Construct): Record<string, ec2.FlowLogOptions> | undefined {
  if (!ctx.config.getBool('cluster.network.vpc_flow_logs', false)) return undefined;

  const removalPolicy = ctx.config.getString('cluster.network.vpc_flow_logs_removal_policy', 'DESTROY');
  const logGroupName = ctx.config.getString(
    'cluster.network.vpc_flow_logs_group_name',
    `${ctx.clusterName}-vpc-flow-logs`,
  );
  const logGroup = new logs.LogGroup(scope, 'vpc-flow-logs-group', {
    logGroupName,
    removalPolicy: removalPolicyByName(removalPolicy),
  });
  const iamRole = new iam.Role(scope, 'vpc-flow-logs-role', {
    assumedBy: buildServicePrincipal('vpc-flow-logs'),
    description: `IAM Role for VPC Flow Logs, Cluster: ${ctx.clusterName}`,
    roleName: `${ctx.clusterName}-vpc-flow-logs-${ctx.awsRegion}`,
  });
  return {
    'cloud-watch': {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(logGroup, iamRole),
      trafficType: ec2.FlowLogTrafficType.ALL,
    },
  };
}

/** `Vpc.build_subnet_configuration`: public always, private always, isolated only when configured. */
function buildSubnetConfiguration(ctx: IdeaContext): ec2.SubnetConfiguration[] {
  const result: ec2.SubnetConfiguration[] = [
    {
      name: 'public',
      cidrMask: ctx.config.getInt('cluster.network.subnet_config.public.cidr_mask', 26),
      subnetType: ec2.SubnetType.PUBLIC,
    },
    {
      name: 'private',
      cidrMask: ctx.config.getInt('cluster.network.subnet_config.private.cidr_mask', 18),
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    },
  ];
  const isolatedCidrMask = ctx.config.getInt('cluster.network.subnet_config.isolated.cidr_mask');
  if (isolatedCidrMask !== undefined) {
    result.push({ name: 'isolated', cidrMask: isolatedCidrMask, subnetType: ec2.SubnetType.PRIVATE_ISOLATED });
  }
  return result;
}

export class Vpc extends ec2.Vpc {
  readonly ctx: IdeaContext;

  constructor(ctx: IdeaContext, name: string, scope: Construct) {
    super(scope, constructId(name), {
      ipAddresses: ec2.IpAddresses.cidr(ctx.config.getString('cluster.network.vpc_cidr_block') as string),
      natGateways: ctx.config.getInt('cluster.network.nat_gateways'),
      enableDnsSupport: true,
      enableDnsHostnames: true,
      maxAzs: ctx.config.getInt('cluster.network.max_azs'),
      subnetConfiguration: buildSubnetConfiguration(ctx),
      flowLogs: buildFlowLogs(ctx, scope),
    });
    this.ctx = ctx;
    addCommonTags(ctx, this, name);
  }

  /** The `EIP` child of every public subnet that has one (one per NAT gateway). */
  get natGatewayIps(): ec2.CfnEIP[] {
    const result: ec2.CfnEIP[] = [];
    for (const subnet of this.publicSubnets) {
      const eip = subnet.node.tryFindChild('EIP');
      if (eip === undefined) continue;
      result.push(eip as ec2.CfnEIP);
    }
    return result;
  }

  get publicSubnetIds(): string[] {
    return this.publicSubnets.map((subnet) => subnet.subnetId);
  }

  get privateSubnetIds(): string[] {
    return this.privateSubnets.map((subnet) => subnet.subnetId);
  }
}

// --- Security groups --------------------------------------------------------------------------

export class SecurityGroup extends ec2.SecurityGroup {
  readonly ctx: IdeaContext;
  readonly vpc: ec2.IVpc;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    description: string,
    allowAllOutbound = false,
  ) {
    super(scope, constructId(name), {
      securityGroupName: resourceName(ctx, name),
      vpc,
      allowAllOutbound,
      description,
    });
    this.ctx = ctx;
    this.vpc = vpc;
    addCommonTags(ctx, this, name);
    this.addNagSuppression([]);
  }

  /**
   * `AwsSolutions-EC23` is prepended to every suppression. At construction time it applies only
   * to the `Resource` child, so later rules carry no metadata.
   */
  addNagSuppression(suppressions: IdeaNagSuppression[], construct?: IConstruct, applyToChildren = true): void {
    const updated: IdeaNagSuppression[] = [
      { rule_id: 'AwsSolutions-EC23', reason: 'suppress warning: parameter referencing intrinsic function' },
      ...suppressions,
    ];
    applyNagSuppression(construct ?? this, updated, applyToChildren);
  }

  addOutboundTrafficRule(): void {
    this.addEgressRule(ec2.Peer.ipv4('0.0.0.0/0'), ec2.Port.tcpRange(0, 65535), 'Allow all egress for TCP');
    this.addEgressRule(ec2.Peer.ipv6('::/0'), ec2.Port.tcpRange(0, 65535), 'Allow all egress for TCP');
  }

  addApiIngressRule(): void {
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(8443),
      'Allow HTTP traffic from all VPC nodes for API access',
    );
  }

  addLoadbalancerIngressRule(loadbalancerSecurityGroup: ec2.ISecurityGroup): void {
    this.addIngressRule(loadbalancerSecurityGroup, ec2.Port.tcp(8443), 'Allow HTTPs traffic from Load Balancer');
  }

  addBastionHostIngressRule(bastionHostSecurityGroup: ec2.ISecurityGroup): void {
    this.addIngressRule(bastionHostSecurityGroup, ec2.Port.tcp(22), 'Allow SSH from Bastion Host');
  }

  addActiveDirectoryRules(): void {
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.udpRange(0, 1024),
      'Allow UDP Traffic from VPC. Required for Directory Service',
    );
    this.addEgressRule(
      ec2.Peer.ipv4('0.0.0.0/0'),
      ec2.Port.udpRange(0, 1024),
      'Allow UDP Traffic. Required for Directory Service',
    );
    this.addEgressRule(
      ec2.Peer.ipv6('::/0'),
      ec2.Port.udpRange(0, 1024),
      'Allow UDP Traffic. Required for Directory Service',
    );
  }
}

/** The only group in a public subnet. */
export class BastionHostSecurityGroup extends SecurityGroup {
  readonly clusterPrefixListId: string;

  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc, clusterPrefixListId: string) {
    super(ctx, name, scope, vpc, 'Bastion host security group');
    this.clusterPrefixListId = clusterPrefixListId;
    this.setupIngress();
    this.setupEgress();

    if (isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addIngressRule(
      ec2.Peer.prefixList(this.clusterPrefixListId),
      ec2.Port.tcp(22),
      'Allow SSH access from Cluster Prefix List to Bastion Host',
    );

    // entries are not checked for emptiness here, unlike the external load balancer's
    for (const prefixListId of this.ctx.config.getList<string>('cluster.network.prefix_list_ids', [])) {
      this.addIngressRule(
        ec2.Peer.prefixList(prefixListId),
        ec2.Port.tcp(22),
        'Allow SSH access from Prefix List to Bastion Host',
      );
    }

    this.addIngressRule(ec2.Peer.ipv4(this.vpc.vpcCidrBlock), ec2.Port.tcp(22), 'Allow SSH traffic from all VPC nodes');
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export class ExternalLoadBalancerSecurityGroup extends SecurityGroup {
  readonly clusterPrefixListId: string;
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    clusterPrefixListId: string,
    bastionHostSecurityGroup: ec2.ISecurityGroup,
  ) {
    super(ctx, name, scope, vpc, 'External Application Load Balancer security group');
    this.clusterPrefixListId = clusterPrefixListId;
    this.bastionHostSecurityGroup = bastionHostSecurityGroup;
    this.setupIngress();
    this.setupEgress();
  }

  addPeerIngressRule(peer: ec2.IPeer, peerType: string): void {
    this.addIngressRule(peer, ec2.Port.tcp(443), `Allow HTTPS access from ${peerType} to ALB`);
    this.addIngressRule(peer, ec2.Port.tcp(80), `Allow HTTP access from ${peerType} to ALB`);
  }

  setupIngress(): void {
    // two rules on one token peer: `{IndirectPeer}` for 443, then `'{IndirectPeer2}'` for 80
    this.addPeerIngressRule(ec2.Peer.prefixList(this.clusterPrefixListId), 'Cluster Prefix List');

    for (const prefixListId of this.ctx.config.getList<string>('cluster.network.prefix_list_ids', [])) {
      if (!isEmpty(prefixListId)) {
        this.addPeerIngressRule(ec2.Peer.prefixList(prefixListId), 'Prefix List');
      }
    }

    this.addIngressRule(this.bastionHostSecurityGroup, ec2.Port.tcp(80), 'Allow HTTP from Bastion Host');
    this.addIngressRule(this.bastionHostSecurityGroup, ec2.Port.tcp(443), 'Allow HTTPs from Bastion Host');
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }

  /**
   * One inline `<EIP>/32` rule per NAT EIP, so instances behind the NAT (virtual desktops, private
   * subnets) can reach the web portal and the APIs through the public ALB endpoint.
   */
  addNatGatewayIpsIngressRule(natGatewayIps: ec2.CfnEIP[]): void {
    for (const eip of natGatewayIps) {
      this.addIngressRule(ec2.Peer.ipv4(`${eip.ref}/32`), ec2.Port.tcp(443), 'Allow NAT EIP to communicate to ALB.');
    }
  }
}

/**
 * Attached to EFS and FSx file systems; open to every node in the VPC. Lustre rules are always
 * provisioned because compute nodes can mount FSx Lustre on demand for /scratch.
 */
export class SharedStorageSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'Shared Storage security group for EFS/FSx file systems');
    this.setupIngress();
    this.setupEgress();
  }

  setupIngress(): void {
    const vpcCidr = ec2.Peer.ipv4(this.vpc.vpcCidrBlock);
    // NFS
    this.addIngressRule(vpcCidr, ec2.Port.tcp(2049), 'Allow NFS traffic from all VPC nodes to EFS');
    // FSx for Lustre
    this.addIngressRule(vpcCidr, ec2.Port.tcp(988), 'Allow FSx Lustre traffic from all VPC nodes');
    this.addIngressRule(vpcCidr, ec2.Port.tcpRange(1021, 1023), 'Allow FSx Lustre traffic from all VPC nodes');
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export class OpenLDAPServerSecurityGroup extends SecurityGroup {
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    bastionHostSecurityGroup: ec2.ISecurityGroup,
  ) {
    super(ctx, name, scope, vpc, 'OpenLDAP server security group');
    this.bastionHostSecurityGroup = bastionHostSecurityGroup;
    this.setupIngress();
    this.setupEgress();
  }

  setupIngress(): void {
    this.addIngressRule(ec2.Peer.ipv4(this.vpc.vpcCidrBlock), ec2.Port.tcp(389), 'Allow LDAP traffic from all VPC nodes');
    this.addApiIngressRule();
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

/** The cluster-manager group. */
export class WebPortalSecurityGroup extends SecurityGroup {
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;
  readonly loadbalancerSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    bastionHostSecurityGroup: ec2.ISecurityGroup,
    loadbalancerSecurityGroup: ec2.ISecurityGroup,
  ) {
    super(ctx, name, scope, vpc, 'Web Portal security group');
    this.bastionHostSecurityGroup = bastionHostSecurityGroup;
    this.loadbalancerSecurityGroup = loadbalancerSecurityGroup;
    this.setupIngress();
    this.setupEgress();
    if (isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addApiIngressRule();
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
    this.addLoadbalancerIngressRule(this.loadbalancerSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export class SchedulerSecurityGroup extends SecurityGroup {
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;
  readonly loadbalancerSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    bastionHostSecurityGroup: ec2.ISecurityGroup,
    loadbalancerSecurityGroup: ec2.ISecurityGroup,
  ) {
    super(ctx, name, scope, vpc, 'Scheduler security group');
    this.bastionHostSecurityGroup = bastionHostSecurityGroup;
    this.loadbalancerSecurityGroup = loadbalancerSecurityGroup;
    this.setupIngress();
    this.setupEgress();
    if (isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addApiIngressRule();
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcpRange(0, 65535),
      'Allow all TCP traffic from VPC to scheduler',
    );
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
    this.addLoadbalancerIngressRule(this.loadbalancerSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export class ComputeNodeSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'Compute Node security group');
    this.setupIngress();
    this.setupEgress();
    if (isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcpRange(0, 65535),
      'All TCP traffic from all VPC nodes to compute node',
    );
    // a self peer is never inlined: `from <own uniqueId>:ALL TRAFFIC`
    this.addIngressRule(this, ec2.Port.allTraffic(), 'Allow all traffic between compute nodes and EFA');
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
    this.addEgressRule(this, ec2.Port.allTraffic(), 'Allow all traffic between compute nodes and EFA');
  }
}

export interface VirtualDesktopBastionAccessSecurityGroupProps {
  bastionHostSecurityGroup: ec2.ISecurityGroup;
  description: string;
  directoryServiceAccess: boolean;
  componentName: string;
}

/** Virtual desktop group with bastion access. */
export class VirtualDesktopBastionAccessSecurityGroup extends SecurityGroup {
  readonly componentName: string;
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    props: VirtualDesktopBastionAccessSecurityGroupProps,
  ) {
    super(ctx, name, scope, vpc, props.description);
    this.componentName = props.componentName;
    this.bastionHostSecurityGroup = props.bastionHostSecurityGroup;
    this.setupIngress();
    this.setupEgress();
    if (props.directoryServiceAccess && isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addApiIngressRule();
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.allTraffic(),
      `Allow all Internal traffic TO ${this.componentName}`,
    );
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export interface VirtualDesktopPublicLoadBalancerAccessSecurityGroupProps
  extends VirtualDesktopBastionAccessSecurityGroupProps {
  publicLoadbalancerSecurityGroup: ec2.ISecurityGroup;
}

/** Virtual desktop group with bastion and public load balancer access. */
export class VirtualDesktopPublicLoadBalancerAccessSecurityGroup extends SecurityGroup {
  readonly componentName: string;
  readonly publicLoadbalancerSecurityGroup: ec2.ISecurityGroup;
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    props: VirtualDesktopPublicLoadBalancerAccessSecurityGroupProps,
  ) {
    super(ctx, name, scope, vpc, props.description);
    this.componentName = props.componentName;
    this.publicLoadbalancerSecurityGroup = props.publicLoadbalancerSecurityGroup;
    this.bastionHostSecurityGroup = props.bastionHostSecurityGroup;
    this.setupIngress();
    this.setupEgress();
    if (props.directoryServiceAccess && isDsActivedirectory(ctx)) {
      this.addActiveDirectoryRules();
    }
  }

  setupIngress(): void {
    this.addApiIngressRule();
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.allTraffic(),
      `Allow all Internal traffic TO ${this.componentName}`,
    );
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
    this.addLoadbalancerIngressRule(this.publicLoadbalancerSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

export interface VirtualDesktopBrokerSecurityGroupProps {
  bastionHostSecurityGroup: ec2.ISecurityGroup;
  description: string;
  componentName: string;
  publicLoadbalancerSecurityGroup: ec2.ISecurityGroup;
}

export class VirtualDesktopBrokerSecurityGroup extends SecurityGroup {
  readonly componentName: string;
  readonly publicLoadbalancerSecurityGroup: ec2.ISecurityGroup;
  readonly bastionHostSecurityGroup: ec2.ISecurityGroup;

  constructor(
    ctx: IdeaContext,
    name: string,
    scope: Construct,
    vpc: ec2.IVpc,
    props: VirtualDesktopBrokerSecurityGroupProps,
  ) {
    super(ctx, name, scope, vpc, props.description);
    this.componentName = props.componentName;
    this.publicLoadbalancerSecurityGroup = props.publicLoadbalancerSecurityGroup;
    this.bastionHostSecurityGroup = props.bastionHostSecurityGroup;
    this.setupIngress();
    this.setupEgress();
  }

  setupIngress(): void {
    const brokerClientPort = requiredInt(this.ctx, 'virtual-desktop-controller.dcv_broker.client_communication_port');
    const brokerAgentPort = requiredInt(this.ctx, 'virtual-desktop-controller.dcv_broker.agent_communication_port');
    const brokerGatewayPort = requiredInt(
      this.ctx,
      'virtual-desktop-controller.dcv_broker.gateway_communication_port',
    );

    const brokerPortList = [brokerClientPort, brokerAgentPort, brokerGatewayPort].sort((a, b) => a - b);
    const minPort = brokerPortList[0] as number;
    const maxPort = brokerPortList[brokerPortList.length - 1] as number;
    const vpcCidr = ec2.Peer.ipv4(this.vpc.vpcCidrBlock);

    // one range rule when the three ports are consecutive (`sorted == range(min, max + 1)`)
    if (brokerPortList.every((port, index) => port === minPort + index)) {
      this.addIngressRule(
        vpcCidr,
        ec2.Port.tcpRange(minPort, maxPort),
        `Allow VPC to broker ports ${minPort}-${maxPort}`,
      );
    } else {
      for (const port of brokerPortList) {
        this.addIngressRule(vpcCidr, ec2.Port.tcp(port), `Allow VPC to broker port ${port}`);
      }
    }

    // broker to broker communications; hard-coded in the templates too
    for (const port of [47100, 47200, 47500]) {
      this.addIngressRule(this, ec2.Port.tcp(port), `Allow broker to broker port ${port}`);
    }
    this.addBastionHostIngressRule(this.bastionHostSecurityGroup);
    this.addLoadbalancerIngressRule(this.publicLoadbalancerSecurityGroup);
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

/** The group with `allowAllOutbound` enabled. */
export class VpcEndpointSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'VPC Endpoints Security Group', true);
    this.addIngressRule(ec2.Peer.ipv4(this.vpc.vpcCidrBlock), ec2.Port.tcp(443), 'Allow HTTPS traffic from VPC');
  }
}

export class OpenSearchSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'OpenSearch security group');
    this.setupIngress();
    this.setupEgress();
  }

  setupIngress(): void {
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      'Allow HTTPS traffic from all VPC nodes to OpenSearch',
    );
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

/** No rules at all, so the L2 emits its "Disallow all traffic" placeholder egress. */
export class DefaultClusterSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'Default Cluster Security');
  }
}

export class InternalLoadBalancerSecurityGroup extends SecurityGroup {
  constructor(ctx: IdeaContext, name: string, scope: Construct, vpc: ec2.IVpc) {
    super(ctx, name, scope, vpc, 'Internal load balancer security group');
    this.setupIngress();
    this.setupEgress();
  }

  setupIngress(): void {
    // The OpenSearch description is part of the rendered rule.
    this.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      'Allow HTTPS traffic from all VPC nodes to OpenSearch',
    );
  }

  setupEgress(): void {
    this.addOutboundTrafficRule();
  }
}

// --- VPC endpoints ----------------------------------------------------------------------------

/**
 * `vpc/<service>-gateway-endpoint` plus a `Custom::EC2CreateTags` of the same name at the stack
 * scope, because tags set through the endpoint L2 never reached the endpoint.
 */
export class VpcGatewayEndpoint {
  readonly ctx: IdeaContext;
  readonly scope: Construct;
  readonly name: string;
  readonly endpoint: ec2.GatewayVpcEndpoint;

  constructor(ctx: IdeaContext, scope: Construct, service: string, vpc: ec2.IVpc, createTags: CreateTagsCustomResource) {
    this.ctx = ctx;
    this.scope = scope;
    this.name = `${service}-gateway-endpoint`;

    this.endpoint = vpc.addGatewayEndpoint(constructId(this.name), {
      service: new ec2.GatewayVpcEndpointAwsService(service),
    });

    createTags.apply(this.name, this.endpoint.vpcEndpointId, {
      [IDEA_TAG_NAME]: this.name,
      [IDEA_TAG_CLUSTER_NAME]: ctx.clusterName,
    });
  }
}

export class VpcInterfaceEndpoint {
  readonly ctx: IdeaContext;
  readonly scope: Construct;
  readonly name: string;
  readonly endpoint: ec2.InterfaceVpcEndpoint;

  constructor(
    ctx: IdeaContext,
    scope: Construct,
    service: string,
    vpc: ec2.IVpc,
    vpcEndpointSecurityGroup: ec2.ISecurityGroup,
    createTags: CreateTagsCustomResource,
  ) {
    this.ctx = ctx;
    this.scope = scope;
    this.name = `${service}-vpc-endpoint`;

    // Component groups do not exist when the cluster stack deploys, so every VPC node may use
    // the interface endpoints.
    this.endpoint = vpc.addInterfaceEndpoint(constructId(this.name), {
      service: new ec2.InterfaceVpcEndpointAwsService(service),
      open: true,
      // private DNS is always on, GovCloud included, where private hosted zones may be unavailable
      privateDnsEnabled: true,
      lookupSupportedAzs: true,
      securityGroups: [vpcEndpointSecurityGroup],
    });

    createTags.apply(this.name, this.endpoint.vpcEndpointId, {
      [IDEA_TAG_NAME]: this.name,
      [IDEA_TAG_CLUSTER_NAME]: ctx.clusterName,
    });
  }

  /** `https://` + the DNS name of the first DNS entry (`<hosted zone id>:<dns name>`). */
  getEndpointUrl(): string {
    const dns = Fn.select(1, Fn.split(':', Fn.select(0, this.endpoint.vpcEndpointDnsEntries)));
    return `https://${dns}`;
  }
}

// --- WAF --------------------------------------------------------------------------------------

/**
 * AWS WAF WebACL for the external ALB: the ACL, and with CloudWatch logs enabled a log group and
 * a logging configuration, all three at the stack scope under `<cluster>-<name>-...` ids. The ACL
 * and the logging configuration are L1s as direct children, so their logical ids carry no hash.
 */
export class WebAcl {
  readonly ctx: IdeaContext;
  readonly name: string;
  readonly scope: Construct;
  readonly createTags: CreateTagsCustomResource | undefined;
  readonly webAcl: wafv2.CfnWebACL;
  logGroup: logs.LogGroup | undefined;
  loggingConfiguration: wafv2.CfnLoggingConfiguration | undefined;

  constructor(ctx: IdeaContext, name: string, scope: Construct, createTags?: CreateTagsCustomResource) {
    this.ctx = ctx;
    this.name = name;
    this.scope = scope;
    this.createTags = createTags;

    const clusterName = ctx.clusterName;
    this.webAcl = new wafv2.CfnWebACL(scope, `${clusterName}-${name}-web-acl`, {
      name: `${clusterName}-${name}`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      description: `WAF WebACL for ${clusterName} ${name}`,
      rules: this.createManagedRules(),
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `${clusterName}-${name}`,
        sampledRequestsEnabled: true,
      },
      tags: [
        { key: 'Name', value: `${clusterName}-${name}` },
        { key: 'idea:ClusterName', value: clusterName },
        { key: 'idea:Module', value: 'cluster' },
      ],
    });

    if (ctx.config.getBool('cluster.cloudwatch_logs.enabled', false)) {
      this.setupCloudwatchLogging(scope, name);
    }
  }

  private createManagedRules(): wafv2.CfnWebACL.RuleProperty[] {
    const rules: wafv2.CfnWebACL.RuleProperty[] = [];

    // blocks requests from IP addresses known to be malicious
    rules.push({
      name: 'AWS-AWSManagedRulesAmazonIpReputationList',
      priority: 0,
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesAmazonIpReputationList',
        },
      },
      overrideAction: { none: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'AWS-AWSManagedRulesAmazonIpReputationList',
        sampledRequestsEnabled: true,
      },
    });

    // OWASP top 10; three rules excluded to avoid false positives
    rules.push({
      name: 'AWS-AWSManagedRulesCommonRuleSet',
      priority: 1,
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesCommonRuleSet',
          version: 'Version_1.18',
          excludedRules: [
            { name: 'SizeRestrictions_BODY' },
            { name: 'CrossSiteScripting_BODY' },
            { name: 'RestrictedExtensions_QUERYARGUMENTS' },
          ],
        },
      },
      overrideAction: { none: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'AWS-AWSManagedRulesCommonRuleSet',
        sampledRequestsEnabled: true,
      },
    });

    // request patterns known to be malicious
    rules.push({
      name: 'AWS-AWSManagedRulesKnownBadInputsRuleSet',
      priority: 2,
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          version: 'Version_1.22',
        },
      },
      overrideAction: { none: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'AWS-AWSManagedRulesKnownBadInputsRuleSet',
        sampledRequestsEnabled: true,
      },
    });

    // optional bot control at the COMMON inspection level (billed per month and per request)
    const botControlEnabled = this.ctx.config.getBool(
      'cluster.load_balancers.external_alb.waf.bot_control.enabled',
      false,
    );
    if (botControlEnabled) {
      rules.push({
        name: 'AWS-AWSManagedRulesBotControlRuleSet',
        priority: 3,
        statement: {
          managedRuleGroupStatement: {
            vendorName: 'AWS',
            name: 'AWSManagedRulesBotControlRuleSet',
            version: 'Version_3.2',
            excludedRules: [{ name: 'CategoryHttpLibrary' }, { name: 'SignalNonBrowserUserAgent' }],
            managedRuleGroupConfigs: [
              { awsManagedRulesBotControlRuleSet: { inspectionLevel: 'COMMON' } },
            ],
          },
        },
        overrideAction: { none: {} },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: 'AWS-AWSManagedRulesBotControlRuleSet',
          sampledRequestsEnabled: true,
        },
      });
    }

    return rules;
  }

  private setupCloudwatchLogging(scope: Construct, name: string): void {
    const clusterName = this.ctx.clusterName;
    // WAF requires the log group name to start with `aws-waf-logs-`
    const logGroupName = `aws-waf-logs-${clusterName}-cluster-waf-${name}`;

    // an unknown retention value keeps the CDK default (two years), with a warning
    let retention: logs.RetentionDays | undefined;
    const retentionDays = this.ctx.config.getInt('cluster.cloudwatch_logs.retention_in_days');
    if (retentionDays !== undefined) {
      retention = LOG_RETENTION_DAYS[retentionDays];
      if (retention === undefined) {
        console.warn(
          `Invalid retention days value: ${retentionDays}. ` +
            `Valid values are: [${Object.keys(LOG_RETENTION_DAYS).join(', ')}]. ` +
            'Using the CDK default retention (two years).',
        );
      }
    }

    this.logGroup = new logs.LogGroup(scope, `${clusterName}-${name}-waf-log-group`, {
      logGroupName,
      removalPolicy: RemovalPolicy.DESTROY,
      retention,
    });

    Tags.of(this.logGroup).add('Name', `${clusterName}-${name}-waf-logs`);
    Tags.of(this.logGroup).add('idea:ClusterName', clusterName);
    Tags.of(this.logGroup).add('idea:Module', 'cluster');

    // drop ALLOW actions from the log by default; the filter is a raw CloudFormation dict
    const dropAllowLogs = this.ctx.config.getBool(
      'cluster.load_balancers.external_alb.waf.logging.drop_allow_actions',
      true,
    );
    const loggingFilter = dropAllowLogs
      ? {
          DefaultBehavior: 'KEEP',
          Filters: [
            {
              Behavior: 'DROP',
              Requirement: 'MEETS_ANY',
              Conditions: [{ ActionCondition: { Action: 'ALLOW' } }],
            },
          ],
        }
      : undefined;

    this.loggingConfiguration = new wafv2.CfnLoggingConfiguration(
      scope,
      `${clusterName}-${name}-waf-logging-config`,
      {
        logDestinationConfigs: [this.logGroup.logGroupArn],
        resourceArn: this.webAcl.attrArn,
        loggingFilter,
      },
    );

    this.loggingConfiguration.node.addDependency(this.webAcl);
    this.loggingConfiguration.node.addDependency(this.logGroup);
  }

  get webAclArn(): string {
    return this.webAcl.attrArn;
  }

  get webAclId(): string {
    return this.webAcl.attrId;
  }
}
