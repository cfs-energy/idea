import assert from 'node:assert/strict';
import {it} from 'node:test';
import {ClusterConfig} from '../../src/config/cluster-config.ts';
import {renderPolicy} from '../../src/cdk/policy.ts';

it('allows the controller to check instance health before recreating a session', () => {
  const rows: Record<string, string> = {
    'global-settings.module_sets.default.virtual-desktop-controller.module_id': 'vdc',
    'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager',
    'cluster.cluster_name': 'example',
    'cluster.aws.region': 'us-east-2',
    'cluster.aws.dns_suffix': 'amazonaws.com',
    'cluster.aws.partition': 'aws',
    'cluster.aws.account_id': '0'.repeat(12),
    'cluster.s3.bucket': 'example',
    'identity-provider.cognito.user_pool_id': 'example',
  };
  const config = ClusterConfig.fromFile(JSON.stringify({Items: Object.entries(rows).map(([key, value]) => ({key: {S: key}, value: {S: value}}))}), JSON.stringify({Items: []}));
  const document = renderPolicy('virtual-desktop-controller.yml', {config, moduleId: 'vdc'}) as {Statement: {Action: string | string[]; Resource: string; Effect: string}[]};
  const statement = document.Statement.find(s => Array.isArray(s.Action) && s.Action.includes('ec2:DescribeInstanceStatus'));
  assert.ok(statement);
  assert.equal(statement.Effect, 'Allow');
  assert.equal(statement.Resource, '*');
});
