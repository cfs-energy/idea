import assert from 'node:assert/strict';
import {it} from 'node:test';
import {ClusterConfig} from '../../src/config/cluster-config.ts';
import {renderPolicy} from '../../src/cdk/policy.ts';

function policy(container: boolean, extra: Record<string, string> = {}, all = false) {
  const rows: Record<string, string> = {
    ...extra,
    'global-settings.module_sets.default.virtual-desktop-controller.module_id': 'vdc',
    'global-settings.module_sets.default.directoryservice.module_id': 'directoryservice',
    'cluster.cluster_name': 'example',
    'cluster.aws.region': 'us-east-1',
    'cluster.aws.dns_suffix': 'amazonaws.com',
    'cluster.aws.partition': 'aws',
    'cluster.aws.account_id': '0'.repeat(12),
    'cluster.s3.bucket': 'example',
    'identity-provider.cognito.user_pool_id': 'example',
    ...(container ? {'ecs.cluster_name': 'configured'} : {}),
  };
  const config = ClusterConfig.fromFile(JSON.stringify({Items: Object.entries(rows).map(([key, value]) => ({key: {S: key}, value: {S: value}}))}), JSON.stringify({Items: []}));
  return (renderPolicy('cluster-manager.yml', {config, moduleId: 'cluster-manager'}) as {Statement: {Sid?: string; Action: string | string[]; Resource: string; Condition?: unknown}[]}).Statement
    .map(s => ({...s, Action: Array.isArray(s.Action) ? s.Action : [s.Action]}))
    .filter(s => all || s.Action.some(action => action.startsWith('ecs:')));
}

it('grants only the five live service reads, scoped to the cluster, in two statements', () => {
  const statements = policy(true);
  assert.deepEqual(statements.flatMap(s => s.Action).sort(), ['ecs:DescribeServices', 'ecs:DescribeTaskDefinition', 'ecs:DescribeTasks', 'ecs:ListServices', 'ecs:ListTasks']);
  assert.equal(statements.length, 2);
  const prefix = `arn:aws:ecs:us-east-1:${'0'.repeat(12)}:`;
  assert.deepEqual(statements[0].Condition, {ArnEquals: {'ecs:cluster': `${prefix}cluster/configured`}});
  assert.deepEqual(statements[1].Action, ['ecs:DescribeTaskDefinition']);
  assert.equal(statements[1].Condition, undefined);
});

it('adds no service permissions without container settings', () => {
  assert.deepEqual(policy(false), []);
});

it('stays within the role inline policy limit with every conditional block enabled', t => {
  const statements = policy(true, {
    'directoryservice.provider': 'aws_managed_activedirectory',
    'directoryservice.directory_id': 'd-0000000000',
    'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager',
    'cluster-manager.bedrock.enabled': 'true',
    'cluster-manager.metrics.cost.enabled': 'true',
    'cluster-manager.accounts.reconcile.okta.api_token_secret_arn': 'arn:aws:secretsmanager:us-east-1:000000000000:secret:example',
  }, true);
  // IAM counts every inline policy on the role against 10240 bytes; the task role also carries a
  // generated default policy of about a kilobyte, and real names are longer than these examples.
  const bytes = JSON.stringify({Statement: statements}).length;
  t.diagnostic(`Rendered task policy: ${bytes} bytes; generated policy reserve: 950 bytes`);
  assert.ok(bytes + 950 <= 10240);
  assert.ok(JSON.stringify({Statement: statements}).length <= 8600, `task policy renders ${JSON.stringify({Statement: statements}).length} bytes`);
});

it('includes the dedicated personal cost table in the existing DynamoDB statement', () => {
  const statements = policy(false, {}, true);
  const statement = statements.find(s => s.Action.includes('dynamodb:UpdateItem'))!;
  const resources = statement.Resource as unknown as string[];
  assert.ok(resources.some(resource => resource.endsWith(':table/example.cluster-manager.personal-costs')));
  assert.ok(statement.Action.includes('dynamodb:CreateTable'));
});
