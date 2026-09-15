/**
 * The network constructs, built in throwaway stacks named like the captured module stacks and
 * compared resource for resource with the live dev27 templates.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; the whole suite requires them.
 * Branch tests override single keys of the captured settings scan in memory.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

import { makeContext, type IdeaContext } from '../../src/cdk/constructs/base.ts';
import { ExistingSocaCluster } from '../../src/cdk/constructs/existing-resources.ts';
import * as network from '../../src/cdk/constructs/network.ts';
import { replaySynthReads } from '../../src/cdk/synth-reads.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { ideaVersion } from '../../src/version.ts';
import { requireFixtures } from '../support/fixtures.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const LIVE = join(PKG, 'tools', 'parity', 'live');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';

requireFixtures(
  [
    CONFIG_FILE,
    SYNTH_READS,
    CONTEXT_FILE,
    ...['cluster', 'cluster-manager', 'scheduler', 'vdc', 'analytics', 'shared-storage'].map(
      (module) => join(LIVE, `idea-dev27-${module}.json`),
    ),
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;
type Attr = { S: string } | { N: string } | { BOOL: boolean } | { NULL: true } | { L: Attr[] };

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

/** The dev27 settings scan with the named keys replaced (or added). */
function scanWith(overrides: Record<string, Attr>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = overrides[key];
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({ key: { S: key }, value: overrides[key], version: { N: '1' } });
  }
  return JSON.stringify(scan);
}

/** A bare stack with the dev27 stack id, env and context, so paths and lookups match the capture. */
function makeStack(moduleId: string, overrides: Record<string, Attr> = {}): { stack: Stack; ctx: IdeaContext } {
  const config = ClusterConfig.fromFile(scanWith(overrides));
  // the account only lives in the gitignored fixture; the AZ and VPC context keys carry it
  const account = config.getString('cluster.aws.account_id') as string;
  const app = new App({ context: { 'aws:cdk:enable-path-metadata': true, ...readJson(CONTEXT_FILE) } });
  const stack = new Stack(app, `${CLUSTER}-${moduleId}`, { env: { account, region: REGION } });
  const ctx = makeContext({
    config,
    awsRegion: REGION,
    moduleId,
    releaseVersion: ideaVersion(),
    synthReads: replaySynthReads(SYNTH_READS),
  });
  return { stack, ctx };
}

/** Synthesized resources minus `AWS::CDK::Metadata`, whose analytics blob is runtime-specific. */
function resources(stack: Stack): Json {
  return Object.fromEntries(
    Object.entries(Template.fromStack(stack).toJSON().Resources as Json).filter(
      ([, resource]) => (resource as Json).Type !== 'AWS::CDK::Metadata',
    ),
  );
}

const liveResources = (module: string): Json => readJson(join(LIVE, `idea-dev27-${module}.json`)).Resources as Json;

/** Every live resource whose type or path matches. */
function liveIds(live: Json, predicate: (id: string, resource: Json) => boolean): string[] {
  return Object.keys(live).filter((id) => predicate(id, live[id] as Json));
}

const isSecurityGroupResource = (_id: string, resource: Json): boolean =>
  (resource.Type as string).startsWith('AWS::EC2::SecurityGroup');

/** Whole-resource equality (Type, Properties, policies, DependsOn, Metadata) for every id. */
function assertSameAsLive(mine: Json, live: Json, ids: string[]): void {
  assert.deepEqual(Object.keys(mine).sort(), [...ids].sort(), 'resource set');
  for (const id of ids) {
    assert.deepEqual(mine[id], live[id], id);
  }
}

// --- the cluster stack: VPC, flow logs, the four groups, {IndirectPeer} counters, WAF ---------

function buildClusterNetwork(overrides: Record<string, Attr> = {}) {
  const { stack, ctx } = makeStack('cluster', overrides);
  const vpc = new network.Vpc(ctx, 'vpc', stack);
  // the cluster stack's prefix list (`CS:426-433`): only its GetAtt token matters here
  const prefixList = new ec2.CfnPrefixList(stack, 'cluster-prefix-list', {
    addressFamily: 'IPv4',
    maxEntries: 10,
    prefixListName: `${CLUSTER}-prefix-list`,
  });
  const defaultGroup = new network.DefaultClusterSecurityGroup(ctx, 'default-security-group', stack, vpc);
  const bastion = new network.BastionHostSecurityGroup(
    ctx,
    'bastion-host-security-group',
    stack,
    vpc,
    prefixList.attrPrefixListId,
  );
  const external = new network.ExternalLoadBalancerSecurityGroup(
    ctx,
    'external-load-balancer-security-group',
    stack,
    vpc,
    prefixList.attrPrefixListId,
    bastion,
  );
  const internal = new network.InternalLoadBalancerSecurityGroup(
    ctx,
    'internal-load-balancer-security-group',
    stack,
    vpc,
  );
  external.addNatGatewayIpsIngressRule(vpc.natGatewayIps);
  return { stack, ctx, vpc, prefixList, defaultGroup, bastion, external, internal };
}

describe('cluster stack network', () => {
  test('vpc, flow logs, security groups and rule resources match the deployed template', () => {
    const { stack, vpc, internal } = buildClusterNetwork();
    // the DCV broker listener rules the cluster stack adds to the internal group (`CS:1261-1337`)
    for (const [port, source] of [
      [8444, 'DCV Clients'],
      [8445, 'DCV Agents'],
      [8446, 'DCV Connection Gateway'],
    ] as const) {
      internal.addIngressRule(
        ec2.Peer.ipv4(vpc.vpcCidrBlock),
        ec2.Port.tcp(port),
        `Allow HTTPS traffic from ${source} to DCV Broker`,
      );
    }

    const live = liveResources('cluster');
    const expected = liveIds(
      live,
      (id, resource) =>
        isSecurityGroupResource(id, resource) ||
        /^idea-dev27-cluster\/(vpc|vpc-flow-logs-group|vpc-flow-logs-role)\//.test(
          resource.Metadata?.['aws:cdk:path'] as string,
        ),
    );
    const mine = resources(stack);
    delete mine.clusterprefixlist; // built here without the stack's tags; not under test
    assertSameAsLive(mine, live, expected);

    // the token peer counter: first `{IndirectPeer}`, then the quoted `'{IndirectPeer2}'`
    assert.equal(
      mine.bastionhostsecuritygroupfromIndirectPeer22CD87C3C1.Metadata['aws:cdk:path'],
      'idea-dev27-cluster/bastion-host-security-group/from {IndirectPeer}:22',
    );
    assert.equal(
      mine.externalloadbalancersecuritygroupfromIndirectPeer280366DFBAA.Metadata['aws:cdk:path'],
      "idea-dev27-cluster/external-load-balancer-security-group/from '{IndirectPeer2}':80",
    );
    // the NAT EIP rule is inline, after nothing: it is the only inline ingress on the external group
    assert.equal(mine.externalloadbalancersecuritygroupFBD9BF3A.Properties.SecurityGroupIngress.length, 1);
    assert.deepEqual(vpc.natGatewayIps.map((eip) => stack.resolve(eip.ref)), [{ Ref: 'vpcpublicSubnet1EIP909BE2D3' }]);
    assert.equal(vpc.publicSubnetIds.length, 3);
    assert.equal(vpc.privateSubnetIds.length, 3);
  });

  test('the WAF trio matches the deployed template', () => {
    const { stack, ctx } = makeStack('cluster');
    const webAcl = new network.WebAcl(ctx, 'external-alb', stack);
    const live = liveResources('cluster');
    const expected = ['ideadev27externalalbwebacl', 'ideadev27externalalbwafloggroup46833A61', 'ideadev27externalalbwafloggingconfig'];
    assertSameAsLive(resources(stack), live, expected);
    assert.deepEqual(stack.resolve(webAcl.webAclArn), { 'Fn::GetAtt': ['ideadev27externalalbwebacl', 'Arn'] });
    assert.deepEqual(stack.resolve(webAcl.webAclId), { 'Fn::GetAtt': ['ideadev27externalalbwebacl', 'Id'] });
  });

  test('extra prefix lists add literal-id rules without touching the token counter', () => {
    const { stack, bastion } = buildClusterNetwork({
      'cluster.network.prefix_list_ids': { L: [{ S: 'pl-11111111111111111' }, { S: '' }, { S: 'pl-22222222222222222' }] },
    });
    const mine = resources(stack);
    const paths = Object.values(mine)
      .map((resource) => (resource as Json).Metadata?.['aws:cdk:path'] as string)
      .filter((path) => path?.includes('security-group/from '));
    assert.deepEqual(paths, [
      'idea-dev27-cluster/bastion-host-security-group/from {IndirectPeer}:22',
      'idea-dev27-cluster/bastion-host-security-group/from pl-11111111111111111:22',
      // the bastion group does not skip an empty entry; the external group does
      'idea-dev27-cluster/bastion-host-security-group/from :22',
      'idea-dev27-cluster/bastion-host-security-group/from pl-22222222222222222:22',
      'idea-dev27-cluster/external-load-balancer-security-group/from {IndirectPeer}:443',
      "idea-dev27-cluster/external-load-balancer-security-group/from '{IndirectPeer2}':80",
      'idea-dev27-cluster/external-load-balancer-security-group/from pl-11111111111111111:443',
      'idea-dev27-cluster/external-load-balancer-security-group/from pl-11111111111111111:80',
      'idea-dev27-cluster/external-load-balancer-security-group/from pl-22222222222222222:443',
      'idea-dev27-cluster/external-load-balancer-security-group/from pl-22222222222222222:80',
      'idea-dev27-cluster/external-load-balancer-security-group/from ideadev27clusterbastionhostsecuritygroup86FC0C14:80',
      'idea-dev27-cluster/external-load-balancer-security-group/from ideadev27clusterbastionhostsecuritygroup86FC0C14:443',
    ]);
    const rule = Object.values(mine).find(
      (resource) => (resource as Json).Metadata?.['aws:cdk:path'] === 'idea-dev27-cluster/bastion-host-security-group/from pl-11111111111111111:22',
    ) as Json;
    assert.equal(rule.Properties.SourcePrefixListId, 'pl-11111111111111111');
    assert.equal(rule.Properties.Description, 'Allow SSH access from Prefix List to Bastion Host');
    assert.deepEqual(stack.resolve(bastion.securityGroupId), { 'Fn::GetAtt': ['bastionhostsecuritygroupA8DBD33A', 'GroupId'] });
  });

  test('without an Active Directory provider the bastion group has no UDP rules', () => {
    const { stack } = buildClusterNetwork({ 'directoryservice.provider': { S: 'openldap' } });
    const bastion = resources(stack).bastionhostsecuritygroupA8DBD33A.Properties;
    assert.deepEqual(
      bastion.SecurityGroupIngress.map((rule: Json) => rule.Description),
      ['Allow SSH traffic from all VPC nodes'],
    );
    assert.deepEqual(
      bastion.SecurityGroupEgress.map((rule: Json) => [rule.IpProtocol, rule.CidrIp ?? rule.CidrIpv6]),
      [['tcp', '0.0.0.0/0'], ['tcp', '::/0']],
    );
  });

  test('vpc without flow logs, with an isolated subnet group', () => {
    const { stack, ctx } = makeStack('cluster', {
      'cluster.network.vpc_flow_logs': { BOOL: false },
      'cluster.network.subnet_config.isolated.cidr_mask': { N: '24' },
      'cluster.network.max_azs': { N: '2' },
    });
    new network.Vpc(ctx, 'vpc', stack);
    const mine = resources(stack);
    const paths = Object.values(mine).map((resource) => (resource as Json).Metadata['aws:cdk:path'] as string);
    assert.ok(!paths.some((path) => path.includes('flow-logs') || path.includes('FlowLog')), 'no flow log resources');
    assert.ok(paths.includes('idea-dev27-cluster/vpc/isolatedSubnet1/Subnet'));
    assert.ok(paths.includes('idea-dev27-cluster/vpc/isolatedSubnet2/Subnet'));
    assert.ok(!paths.includes('idea-dev27-cluster/vpc/isolatedSubnet3/Subnet'));
    assert.ok(!paths.includes('idea-dev27-cluster/vpc/publicSubnet3/Subnet'));
    const isolated = Object.values(mine).find(
      (resource) => (resource as Json).Metadata['aws:cdk:path'] === 'idea-dev27-cluster/vpc/isolatedSubnet1/Subnet',
    ) as Json;
    assert.equal(isolated.Properties.Tags.find((tag: Json) => tag.Key === 'aws-cdk:subnet-type').Value, 'Isolated');
  });

  test('flow log group name and removal policy come from config', () => {
    const { stack, ctx } = makeStack('cluster', {
      'cluster.network.vpc_flow_logs_group_name': { S: 'sample-flow-logs' },
      'cluster.network.vpc_flow_logs_removal_policy': { S: 'RETAIN' },
    });
    new network.Vpc(ctx, 'vpc', stack);
    const group = resources(stack).vpcflowlogsgroup4676BF4E;
    assert.equal(group.Properties.LogGroupName, 'sample-flow-logs');
    assert.equal(group.DeletionPolicy, 'Retain');
    assert.equal(group.UpdateReplacePolicy, 'Retain');
  });

  test('an unknown flow log removal policy is refused, as the Python enum would', () => {
    const { stack, ctx } = makeStack('cluster', {
      'cluster.network.vpc_flow_logs_removal_policy': { S: 'destroy' },
    });
    assert.throws(() => new network.Vpc(ctx, 'vpc', stack), /'destroy' is not a valid RemovalPolicy/);
  });

  test('vpc endpoints: endpoint security group, gateway and interface endpoints, native tags', () => {
    const { stack, ctx, vpc } = buildClusterNetwork({ 'cluster.network.use_vpc_endpoints': { BOOL: true } });
    const endpointGroup = new network.VpcEndpointSecurityGroup(ctx, 'vpc-endpoint-security-group', stack, vpc);
    new network.VpcGatewayEndpoint(ctx, stack, 's3', vpc);
    const ssm = new network.VpcInterfaceEndpoint(ctx, stack, 'ssm', vpc, endpointGroup);

    const mine = resources(stack);
    const byPath = (path: string): Json => {
      const found = Object.values(mine).find((resource) => (resource as Json).Metadata?.['aws:cdk:path'] === path);
      assert.ok(found, path);
      return found as Json;
    };

    // allow_all_outbound=True: the L2's own egress, and the `open=True` 443 rule deduplicates
    const group = byPath('idea-dev27-cluster/vpc-endpoint-security-group/Resource');
    assert.equal(group.Properties.GroupDescription, 'VPC Endpoints Security Group');
    assert.equal(group.Properties.GroupName, 'idea-dev27-vpc-endpoint-security-group');
    assert.deepEqual(group.Properties.SecurityGroupEgress, [
      { CidrIp: '0.0.0.0/0', Description: 'Allow all outbound traffic by default', IpProtocol: '-1' },
    ]);
    assert.deepEqual(group.Properties.SecurityGroupIngress, [
      {
        CidrIp: { 'Fn::GetAtt': ['vpcA2121C38', 'CidrBlock'] },
        Description: 'Allow HTTPS traffic from VPC',
        FromPort: 443,
        IpProtocol: 'tcp',
        ToPort: 443,
      },
    ]);
    assert.deepEqual(group.Metadata.cdk_nag.rules_to_suppress.map((rule: Json) => rule.id), ['AwsSolutions-EC23']);

    const gateway = byPath('idea-dev27-cluster/vpc/s3-gateway-endpoint/Resource');
    assert.equal(gateway.Type, 'AWS::EC2::VPCEndpoint');
    assert.equal(gateway.Properties.VpcEndpointType, 'Gateway');
    assert.equal(gateway.Properties.RouteTableIds.length, 6);

    // the tags ride on the endpoint resource itself; nothing tags it out of band, and the
    // endpoint's own Name wins over the one it inherits from the VPC it hangs off
    assert.deepEqual(gateway.Properties.Tags, [
      { Key: 'idea:ClusterName', Value: CLUSTER },
      { Key: 'Name', Value: 's3-gateway-endpoint' },
    ]);
    assert.deepEqual(
      Object.values(mine).filter((resource: Json) => resource.Type === 'Custom::EC2CreateTags'),
      [],
    );

    const iface = byPath('idea-dev27-cluster/vpc/ssm-vpc-endpoint/Resource');
    assert.equal(iface.Properties.VpcEndpointType, 'Interface');
    assert.equal(iface.Properties.PrivateDnsEnabled, true);
    assert.equal(iface.Properties.ServiceName, 'com.amazonaws.us-east-2.ssm');
    const idByPath = (path: string): string =>
      Object.keys(mine).find((id) => (mine[id] as Json).Metadata?.['aws:cdk:path'] === path) as string;
    const groupId = idByPath('idea-dev27-cluster/vpc-endpoint-security-group/Resource');
    assert.match(groupId, /^vpcendpointsecuritygroup[0-9A-F]{8}$/);
    assert.deepEqual(iface.Properties.SecurityGroupIds, [{ 'Fn::GetAtt': [groupId, 'GroupId'] }]);
    assert.equal(iface.Properties.SubnetIds.length, 3);

    assert.deepEqual(iface.Properties.Tags, [
      { Key: 'idea:ClusterName', Value: CLUSTER },
      { Key: 'Name', Value: 'ssm-vpc-endpoint' },
    ]);
    const endpointId = idByPath('idea-dev27-cluster/vpc/ssm-vpc-endpoint/Resource');
    assert.match(endpointId, /^vpcssmvpcendpoint[0-9A-F]{8}$/);
    assert.deepEqual(stack.resolve(ssm.getEndpointUrl()), {
      'Fn::Join': [
        '',
        [
          'https://',
          {
            'Fn::Select': [
              1,
              { 'Fn::Split': [':', { 'Fn::Select': [0, { 'Fn::GetAtt': [endpointId, 'DnsEntries'] }] }] },
            ],
          },
        ],
      ],
    });
  });

  test('WAF without bot control, without cloudwatch logs', () => {
    const { stack, ctx } = makeStack('cluster', {
      'cluster.load_balancers.external_alb.waf.bot_control.enabled': { BOOL: false },
      'cluster.cloudwatch_logs.enabled': { BOOL: false },
    });
    new network.WebAcl(ctx, 'external-alb', stack);
    const mine = resources(stack);
    assert.deepEqual(Object.keys(mine), ['ideadev27externalalbwebacl']);
    assert.deepEqual(
      mine.ideadev27externalalbwebacl.Properties.Rules.map((rule: Json) => [rule.Name, rule.Priority]),
      [
        ['AWS-AWSManagedRulesAmazonIpReputationList', 0],
        ['AWS-AWSManagedRulesCommonRuleSet', 1],
        ['AWS-AWSManagedRulesKnownBadInputsRuleSet', 2],
      ],
    );
  });

  test('WAF logging: an unknown retention keeps the default, allow actions can be kept', () => {
    const { stack, ctx } = makeStack('cluster', {
      'cluster.cloudwatch_logs.retention_in_days': { N: '45' },
      'cluster.load_balancers.external_alb.waf.logging.drop_allow_actions': { BOOL: false },
    });
    const warnings: string[] = [];
    const originalWarn = console.warn;
    console.warn = (message: string) => warnings.push(message);
    try {
      new network.WebAcl(ctx, 'external-alb', stack);
    } finally {
      console.warn = originalWarn;
    }
    assert.equal(warnings.length, 1);
    assert.match(warnings[0] as string, /^Invalid retention days value: 45\. Valid values are: \[1, 3, 5, 7, 14, 30/);
    const mine = resources(stack);
    assert.equal(mine.ideadev27externalalbwafloggroup46833A61.Properties.RetentionInDays, 731);
    assert.equal(mine.ideadev27externalalbwafloggingconfig.Properties.LoggingFilter, undefined);
    assert.deepEqual(mine.ideadev27externalalbwafloggingconfig.DependsOn, [
      'ideadev27externalalbwafloggroup46833A61',
      'ideadev27externalalbwebacl',
    ]);
  });
});

// --- module stacks: groups on the imported VPC with imported peers ----------------------------

describe('module stack security groups', () => {
  test('cluster-manager: WebPortalSecurityGroup', () => {
    const { stack, ctx } = makeStack('cluster-manager');
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.WebPortalSecurityGroup(
      ctx,
      'cluster-manager-security-group',
      stack,
      cluster.vpc,
      cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
      cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
    );
    const live = liveResources('cluster-manager');
    assertSameAsLive(resources(stack), live, liveIds(live, isSecurityGroupResource));
  });

  test('scheduler: SchedulerSecurityGroup and ComputeNodeSecurityGroup (self peers)', () => {
    const { stack, ctx } = makeStack('scheduler');
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.SchedulerSecurityGroup(
      ctx,
      'scheduler-security-group',
      stack,
      cluster.vpc,
      cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
      cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
    );
    new network.ComputeNodeSecurityGroup(ctx, 'scheduler-compute-node-security-group', stack, cluster.vpc);
    const live = liveResources('scheduler');
    assertSameAsLive(resources(stack), live, liveIds(live, isSecurityGroupResource));
  });

  test('vdc: dcv host, broker, controller and gateway groups', () => {
    const { stack, ctx } = makeStack('vdc');
    const cluster = new ExistingSocaCluster(ctx, stack);
    const bastionHostSecurityGroup = cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup;
    const publicLoadbalancerSecurityGroup = cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup;

    new network.VirtualDesktopBastionAccessSecurityGroup(ctx, 'vdc-dcv-host-security-group', stack, cluster.vpc, {
      bastionHostSecurityGroup,
      description: 'Security Group for DCV Host',
      directoryServiceAccess: true,
      componentName: 'DCV Host',
    });
    new network.VirtualDesktopBrokerSecurityGroup(ctx, 'vdc-broker-security-group', stack, cluster.vpc, {
      bastionHostSecurityGroup,
      publicLoadbalancerSecurityGroup,
      description: 'Security Group for Virtual Desktop DCV Broker',
      componentName: 'DCV Broker',
    });
    new network.VirtualDesktopPublicLoadBalancerAccessSecurityGroup(
      ctx,
      'vdc-controller-security-group',
      stack,
      cluster.vpc,
      {
        bastionHostSecurityGroup,
        publicLoadbalancerSecurityGroup,
        description: 'Security Group for Virtual Desktop Controller',
        directoryServiceAccess: true,
        componentName: 'Virtual Desktop Controller',
      },
    );
    const gateway = new network.VirtualDesktopPublicLoadBalancerAccessSecurityGroup(
      ctx,
      'vdc-gateway-security-group',
      stack,
      cluster.vpc,
      {
        bastionHostSecurityGroup,
        publicLoadbalancerSecurityGroup,
        description: 'Security Group for Virtual Desktop DCV Connection Gateway',
        directoryServiceAccess: false,
        componentName: 'DCV Connection Gateway',
      },
    );
    // the two rules the VDC stack adds itself (`VD:1298-1310`): a literal prefix-list peer
    gateway.addIngressRule(
      ec2.Peer.ipv4(cluster.vpc.vpcCidrBlock),
      ec2.Port.tcp(8989),
      'Allow TCP traffic access for HealthCheck to DCV Connection Gateway',
    );
    gateway.addIngressRule(
      ec2.Peer.prefixList(ctx.config.getString('cluster.network.cluster_prefix_list_id') as string),
      ec2.Port.allTraffic(),
      'Allow all Traffic access from Cluster Prefix List to DCV Connection Gateway',
    );

    const live = liveResources('vdc');
    assertSameAsLive(resources(stack), live, liveIds(live, isSecurityGroupResource));
  });

  test('analytics: OpenSearchSecurityGroup', () => {
    const { stack, ctx } = makeStack('analytics');
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.OpenSearchSecurityGroup(ctx, 'analytics-opensearch-security-group', stack, cluster.vpc);
    const live = liveResources('analytics');
    assertSameAsLive(resources(stack), live, liveIds(live, isSecurityGroupResource));
  });

  test('shared-storage: SharedStorageSecurityGroup', () => {
    const { stack, ctx } = makeStack('shared-storage');
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.SharedStorageSecurityGroup(ctx, 'shared-storage-security-group', stack, cluster.vpc);
    const live = liveResources('shared-storage');
    assertSameAsLive(resources(stack), live, liveIds(live, isSecurityGroupResource));
  });

  test('directoryservice: OpenLDAPServerSecurityGroup rule order (no live counterpart)', () => {
    const { stack, ctx } = makeStack('directoryservice', { 'directoryservice.provider': { S: 'openldap' } });
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.OpenLDAPServerSecurityGroup(
      ctx,
      'directoryservice-security-group',
      stack,
      cluster.vpc,
      cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
    );
    const mine = resources(stack);
    const group = Object.values(mine).find((resource) => (resource as Json).Type === 'AWS::EC2::SecurityGroup') as Json;
    assert.equal(group.Metadata['aws:cdk:path'], 'idea-dev27-directoryservice/directoryservice-security-group/Resource');
    assert.equal(group.Properties.GroupDescription, 'OpenLDAP server security group');
    assert.deepEqual(
      group.Properties.SecurityGroupIngress.map((rule: Json) => [rule.FromPort, rule.Description]),
      [
        [389, 'Allow LDAP traffic from all VPC nodes'],
        [8443, 'Allow HTTP traffic from all VPC nodes for API access'],
      ],
    );
    // the bastion rule is the only separate resource: an imported-group peer, after the inline ones
    const rules = Object.values(mine).filter((resource) => (resource as Json).Type === 'AWS::EC2::SecurityGroupIngress') as Json[];
    assert.equal(rules.length, 1);
    assert.match(
      rules[0]?.Metadata['aws:cdk:path'],
      /^idea-dev27-directoryservice\/directoryservice-security-group\/from ideadev27directoryservicebastionhostsecuritygroup[0-9A-F]{8}:22$/,
    );
    assert.equal(rules[0]?.Properties.FromPort, 22);
    assert.equal(rules[0]?.Properties.SourceSecurityGroupId, ctx.config.getString('cluster.network.security_groups.bastion-host'));
  });

  test('non-consecutive broker ports produce one rule per port', () => {
    const { stack, ctx } = makeStack('vdc', {
      'vdc.dcv_broker.client_communication_port': { N: '8444' },
      'vdc.dcv_broker.agent_communication_port': { N: '8448' },
      'vdc.dcv_broker.gateway_communication_port': { N: '8446' },
    });
    const cluster = new ExistingSocaCluster(ctx, stack);
    new network.VirtualDesktopBrokerSecurityGroup(ctx, 'vdc-broker-security-group', stack, cluster.vpc, {
      bastionHostSecurityGroup: cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
      publicLoadbalancerSecurityGroup: cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
      description: 'Security Group for Virtual Desktop DCV Broker',
      componentName: 'DCV Broker',
    });
    const group = resources(stack).vdcbrokersecuritygroupF5DE1FEA;
    assert.deepEqual(
      group.Properties.SecurityGroupIngress.map((rule: Json) => [rule.FromPort, rule.ToPort, rule.Description]),
      [
        [8444, 8444, 'Allow VPC to broker port 8444'],
        [8446, 8446, 'Allow VPC to broker port 8446'],
        [8448, 8448, 'Allow VPC to broker port 8448'],
      ],
    );
  });

  test('a missing broker port is a config error', () => {
    const { stack, ctx } = makeStack('vdc', { 'vdc.dcv_broker.agent_communication_port': { NULL: true } });
    const cluster = new ExistingSocaCluster(ctx, stack);
    assert.throws(
      () =>
        new network.VirtualDesktopBrokerSecurityGroup(ctx, 'vdc-broker-security-group', stack, cluster.vpc, {
          bastionHostSecurityGroup: cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
          publicLoadbalancerSecurityGroup: cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
          description: 'Security Group for Virtual Desktop DCV Broker',
          componentName: 'DCV Broker',
        }),
      /agent_communication_port/,
    );
  });

  test('ElasticIP is an unhashed L1 with the common tags', () => {
    const { stack, ctx } = makeStack('cluster');
    new network.ElasticIP(ctx, 'sample-eip', stack);
    const mine = resources(stack);
    assert.deepEqual(Object.keys(mine), ['sampleeip']);
    assert.deepEqual(mine.sampleeip.Properties.Tags, [
      { Key: 'idea:ClusterName', Value: CLUSTER },
      { Key: 'Name', Value: 'idea-dev27-sample-eip' },
    ]);
  });
});
