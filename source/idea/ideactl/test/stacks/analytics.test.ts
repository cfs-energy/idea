/**
 * W5c: `constructs/analytics.ts` (the `OpenSearch` domain) against the live dev27 `analytics`
 * template, including the two L2-generated custom resources the analytics stack inherits from it
 * against the live templates.
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import { RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import type * as opensearch from 'aws-cdk-lib/aws-opensearchservice';

import { OpenSearch } from '../../src/cdk/constructs/analytics.ts';
import { ExistingSocaCluster } from '../../src/cdk/constructs/existing-resources.ts';
import {
  CLUSTER,
  cleanup,
  configWith,
  fixtureAccount,
  harness,
  liveResources,
  requireLiveFixture,
} from '../support/construct-harness.ts';
import type { Json } from '../support/construct-harness.ts';

after(cleanup);

requireLiveFixture('analytics');

const DOMAIN = 'analytics367A4110';
const ACCESS_POLICY = 'analyticsAccessPolicy13397FB5';
const ACCESS_POLICY_IAM = 'analyticsAccessPolicyCustomResourcePolicy9ECE5CA0';
const LOG_POLICY = 'analyticsESLogGroupPolicyc8803cad36e6f020d595fd57136b9a57c0a22068652CA16B1F';
const LOG_POLICY_IAM =
  'analyticsESLogGroupPolicyc8803cad36e6f020d595fd57136b9a57c0a2206865CustomResourcePolicy7D66AE30';
const PROVIDER_ROLE = 'AWS679f53fac002430cb0da5b7982bd2287ServiceRoleC1EA0FF2';
const LOG_GROUPS = [
  'analyticssearchloggroupF6E87433',
  'analyticsapploggroup7CE00917',
  'analyticsslowindexloggroup2F17F009',
];

interface BuildOptions {
  configFile?: string;
  createServiceLinkedRole?: boolean;
  dataNodes?: number;
  removalPolicy?: RemovalPolicy;
}

/** What `analytics_stack.build_opensearch()` does, minus the config reads the stack owns. */
function buildAnalytics(options: BuildOptions = {}): Json {
  const h = harness({ moduleId: 'analytics', moduleName: 'analytics', configFile: options.configFile });
  const config = h.ctx.config;
  const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
  const securityGroup = new ec2.SecurityGroup(h.base.stack, 'analytics-opensearch-security-group', {
    vpc: cluster.vpc,
  });

  const logGroup = (id: string, suffix: string): logs.LogGroup =>
    new logs.LogGroup(h.base.stack, id, {
      logGroupName: `/${CLUSTER}/analytics/${suffix}`,
      removalPolicy: RemovalPolicy.DESTROY,
    });

  const logging: opensearch.LoggingOptions = {
    slowSearchLogEnabled: config.getBool('analytics.opensearch.logging.slow_search_log_enabled', false),
    slowSearchLogGroup: logGroup('analytics-search-log-group', 'search-log'),
    appLogEnabled: config.getBool('analytics.opensearch.logging.app_log_enabled', false),
    appLogGroup: logGroup('analytics-app-log-group', 'app-log'),
    slowIndexLogEnabled: config.getBool('analytics.opensearch.logging.slow_index_log_enabled', false),
    slowIndexLogGroup: logGroup('analytics-slow-index-log-group', 'slow-index-log'),
    auditLogEnabled: false,
  };

  new OpenSearch(h.ctx, 'analytics', h.base.stack, {
    cluster,
    securityGroups: [securityGroup],
    dataNodes: options.dataNodes ?? (config.getInt('analytics.opensearch.data_nodes', 0) as number),
    dataNodeInstanceType: config.getString('analytics.opensearch.data_node_instance_type', '') as string,
    ebsVolumeSize: config.getInt('analytics.opensearch.ebs_volume_size', 0) as number,
    removalPolicy:
      options.removalPolicy ??
      (config.getString('analytics.opensearch.removal_policy') === 'RETAIN'
        ? RemovalPolicy.RETAIN
        : RemovalPolicy.DESTROY),
    nodeToNodeEncryption: config.getBool('analytics.opensearch.node_to_node_encryption', false),
    createServiceLinkedRole: options.createServiceLinkedRole ?? false,
    logging,
  });

  return h.template().Resources as Json;
}

describe('OpenSearch domain', () => {
  const live = liveResources('analytics');

  test('the domain, its log groups and both L2 custom resources match the deployed template', () => {
    const resources = buildAnalytics();
    for (const logicalId of [DOMAIN, ACCESS_POLICY, ACCESS_POLICY_IAM, LOG_POLICY, LOG_POLICY_IAM, ...LOG_GROUPS]) {
      assert.ok(resources[logicalId] !== undefined, `missing logical id: ${logicalId}`);
      assert.deepEqual(resources[logicalId], live[logicalId], `mismatch on ${logicalId}`);
    }
  });

  test('the AwsCustomResource provider role is the shared singleton', () => {
    const resources = buildAnalytics();
    assert.deepEqual(resources[PROVIDER_ROLE], live[PROVIDER_ROLE]);
  });

  test('SnapshotOptions is dropped, because automated_snapshot_start_hour is 0', () => {
    assert.equal(buildAnalytics()[DOMAIN].Properties.SnapshotOptions, undefined);
  });

  test('the access policy is a literal ARN built by ArnBuilder, not a CDK token', () => {
    // the L2 renders the call as an Fn::Join around the domain Ref, so the policy JSON is embedded
    const create = buildAnalytics()[ACCESS_POLICY].Properties.Create['Fn::Join'][1] as unknown[];
    const literal = create.filter((part) => typeof part === 'string').join('');
    assert.ok(literal.includes('\\"Action\\":\\"es:ESHttp*\\"'));
    assert.ok(literal.includes('\\"Principal\\":{\\"AWS\\":\\"*\\"}'));
    const resourceArn = `arn:aws:es:us-east-2:${fixtureAccount()}:domain/${CLUSTER}-analytics/*`;
    assert.ok(literal.includes(`\\"Resource\\":\\"${resourceArn}\\"`), literal);
  });

  test('the two L2-generated IAM policies carry exactly the documented documents', () => {
    const resources = buildAnalytics();
    assert.deepEqual(
      (resources[LOG_POLICY_IAM].Properties.PolicyDocument.Statement as Json[]).map((s) => s.Action),
      ['logs:PutResourcePolicy', 'logs:DeleteResourcePolicy'],
    );
    const accessStatements = resources[ACCESS_POLICY_IAM].Properties.PolicyDocument.Statement as Json[];
    assert.equal(accessStatements.length, 1);
    assert.equal(accessStatements[0].Action, 'es:UpdateDomainConfig');
    assert.deepEqual(accessStatements[0].Resource, { 'Fn::GetAtt': [DOMAIN, 'Arn'] });
  });

  test('a RETAIN removal policy is honoured on the stateful domain', () => {
    const configFile = configWith({ 'analytics.opensearch.removal_policy': 'RETAIN' });
    const domain = buildAnalytics({ configFile })[DOMAIN];
    assert.equal(domain.DeletionPolicy, 'Retain');
    assert.equal(domain.UpdateReplacePolicy, 'Retain');
  });

  test('one data node disables zone awareness and takes one subnet', () => {
    const domain = buildAnalytics({ dataNodes: 1 })[DOMAIN];
    assert.deepEqual(domain.Properties.ClusterConfig, {
      DedicatedMasterEnabled: false,
      InstanceCount: 1,
      InstanceType: 'm7g.large.search',
      ZoneAwarenessEnabled: false,
    });
    assert.equal((domain.Properties.VPCOptions.SubnetIds as string[]).length, 1);
  });

  test('four data nodes cap the availability zone count at three', () => {
    const domain = buildAnalytics({ dataNodes: 4 })[DOMAIN];
    assert.deepEqual(domain.Properties.ClusterConfig.ZoneAwarenessConfig, { AvailabilityZoneCount: 3 });
  });

  test('the service-linked role is a child of the domain and the domain depends on it', () => {
    const resources = buildAnalytics({ createServiceLinkedRole: true });
    const [logicalId, role] = Object.entries(resources).find(
      ([, resource]) => (resource as Json).Type === 'AWS::IAM::ServiceLinkedRole',
    ) as [string, Json];
    assert.equal(role.Properties.AWSServiceName, 'es.amazonaws.com');
    assert.equal(role.Properties.Description, 'Role for ES to access resources in the VPC');
    assert.equal(
      role.Metadata['aws:cdk:path'],
      `idea-dev27-analytics/analytics/${CLUSTER}-es-service-linked-role`,
    );
    assert.ok((resources[DOMAIN].DependsOn as string[]).includes(logicalId));
  });

  test('global-settings.opensearch.aws_service_name overrides the derived service name', () => {
    const configFile = configWith({ 'global-settings.opensearch.aws_service_name': 'opensearchservice.amazonaws.com' });
    const resources = buildAnalytics({ configFile, createServiceLinkedRole: true });
    const role = Object.values(resources).find(
      (resource) => (resource as Json).Type === 'AWS::IAM::ServiceLinkedRole',
    ) as Json;
    assert.equal(role.Properties.AWSServiceName, 'opensearchservice.amazonaws.com');
  });
});
