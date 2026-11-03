/**
 * The scheduler and desktop controller policies only touch AWS service-linked roles in IAM, and
 * IAM refuses AttachRolePolicy and PutRolePolicy on a service-linked role. Creating the role is
 * the only grant that does anything; the policy-changing actions stay out of both documents.
 * Synthetic settings only, so it runs in a public checkout.
 */

import assert from 'node:assert/strict';
import { it } from 'node:test';

import { renderPolicy } from '../../src/cdk/policy.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';

const ROWS: Record<string, string> = {
  'global-settings.module_sets.default.virtual-desktop-controller.module_id': 'vdc',
  'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager',
  'global-settings.module_sets.default.scheduler.module_id': 'scheduler',
  'cluster.cluster_name': 'example',
  'cluster.aws.region': 'us-east-2',
  'cluster.aws.dns_suffix': 'amazonaws.com',
  'cluster.aws.partition': 'aws',
  'cluster.aws.account_id': '0'.repeat(12),
  'cluster.s3.bucket': 'example',
  'cluster.network.vpc_id': 'vpc-0123456789abcdef0',
  'identity-provider.cognito.user_pool_id': 'example',
};

interface Statement {
  Action: string | string[];
  Resource: string | string[];
}

const config = (): ClusterConfig =>
  ClusterConfig.fromFile(
    JSON.stringify({ Items: Object.entries(ROWS).map(([key, value]) => ({ key: { S: key }, value: { S: value } })) }),
    JSON.stringify({ Items: [] }),
  );

const asList = (value: string | string[]): string[] => (Array.isArray(value) ? value : [value]);

function statements(template: string, moduleId: string, vars: Record<string, string> = {}): Statement[] {
  return (renderPolicy(template, { config: config(), moduleId, vars }) as { Statement: Statement[] }).Statement;
}

function assertNoPolicyWrites(documentStatements: Statement[]): void {
  const writes = documentStatements.flatMap((statement) =>
    asList(statement.Action).filter((action) => action === 'iam:AttachRolePolicy' || action === 'iam:PutRolePolicy'),
  );
  assert.deepEqual(writes, []);
}

function serviceLinkedGrant(documentStatements: Statement[]): Statement {
  const found = documentStatements.find((statement) => asList(statement.Action).includes('iam:CreateServiceLinkedRole'));
  assert.ok(found, 'iam:CreateServiceLinkedRole is still granted');
  for (const arn of asList(found.Resource)) assert.match(arn, /:role\/aws-service-role\//);
  return found;
}

it('the scheduler can create its service-linked roles and change no role policy', () => {
  const document = statements('scheduler.yml', 'scheduler', {
    compute_node_role_arn: 'arn:aws:iam::000000000000:role/compute',
    scheduler_role_arn: 'arn:aws:iam::000000000000:role/scheduler',
    spot_fleet_request_role_arn: 'arn:aws:iam::000000000000:role/spot-fleet',
  });
  assertNoPolicyWrites(document);
  assert.deepEqual(asList(serviceLinkedGrant(document).Action), ['iam:CreateServiceLinkedRole']);
});

it('the desktop controller can create its service-linked role and change no role policy', () => {
  const document = statements('virtual-desktop-controller.yml', 'vdc');
  assertNoPolicyWrites(document);
  serviceLinkedGrant(document);
});
