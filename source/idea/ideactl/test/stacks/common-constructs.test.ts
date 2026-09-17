/**
 * The `constructs/common.ts` classes the nine module stacks instantiate, each rebuilt
 * exactly as its Python call site builds it and compared with the live dev27 resource.
 *
 * The base-stack suite covers the cluster-settings trio, `CustomResourceProvider` and `SQSQueue`; the classes
 * pinned here are the rest: `Role`, `Policy`,
 * `ManagedPolicy`, `InstanceProfile` and `SNSTopic`. The assertions are whole-resource, logical
 * ids included, so a changed construct id shows up as a failure rather than as a replaced
 * resource on the next deploy.
 *
 * Fixtures under `tools/parity/{fixtures,live}` are gitignored; every test requires them.
 */

import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import {
  InstanceProfile,
  ManagedPolicy,
  Policy,
  Role,
  SNSTopic,
} from '../../src/cdk/constructs/common.ts';
import { harness, liveResources, requireLiveFixture } from '../support/construct-harness.ts';
import type { Json } from '../support/construct-harness.ts';

for (const stack of ['bastion-host', 'scheduler', 'vdc', 'cluster']) requireLiveFixture(stack);

/** Asserts that `logicalIds` are present in `template` and equal to the live resources. */
function assertMatchesLive(template: Json, stack: string, logicalIds: string[]): void {
  const live = liveResources(stack);
  const ours = template.Resources as Json;
  for (const logicalId of logicalIds) {
    assert.ok(live[logicalId] !== undefined, `${stack} has no live resource ${logicalId}`);
    assert.ok(ours[logicalId] !== undefined, `synth produced no ${logicalId}; got ${Object.keys(ours).join(', ')}`);
    assert.deepEqual(ours[logicalId], live[logicalId], logicalId);
  }
}

describe('Role, Policy and InstanceProfile: the bastion-host IAM trio', () => {
  // `bastion_host_stack.build_iam_roles()`, which is nothing but these three constructs.
  const build = (): Json => {
    const h = harness({ moduleId: 'bastion-host', moduleName: 'bastion-host' });
    const role = new Role(h.ctx, 'bastion-host-role', h.base.stack, {
      description: 'IAM role assigned to the bastion-host',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: h.base.getEc2InstanceManagedPolicies(),
    });
    role.attachInlinePolicy(
      new Policy(h.ctx, 'bastion-host-policy', h.base.stack, { policyTemplateName: 'bastion-host.yml' }),
    );
    const instanceProfile = new InstanceProfile(h.ctx, 'bastion-host-instance-profile', h.base.stack, [role]);
    instanceProfile.node.addDependency(role);
    return h.template();
  };

  test('role, inline policy and instance profile equal the deployed resources', () => {
    assertMatchesLive(build(), 'bastion-host', [
      'bastionhostrole430C4862',
      'bastionhostpolicy211766EC',
      'bastionhostinstanceprofile',
    ]);
  });

  test('the instance profile carries no hash: it is an L1 child of the stack', () => {
    const profile = (build().Resources as Json).bastionhostinstanceprofile as Json;
    assert.equal(profile.Properties.InstanceProfileName, 'idea-dev27-bastion-host-instance-profile-us-east-2');
    assert.deepEqual(profile.Properties.Roles, [{ Ref: 'bastionhostrole430C4862' }]);
  });

  test('assumedBy renders one CompositePrincipal statement per service, in order', () => {
    const role = (build().Resources as Json).bastionhostrole430C4862 as Json;
    const services = (role.Properties.AssumeRolePolicyDocument.Statement as Json[]).map(
      (statement) => statement.Principal.Service['Fn::Join'][1][0],
    );
    assert.deepEqual(services, ['ssm.', 'ec2.']);
  });
});

describe('InstanceProfile: the two scheduler profiles', () => {
  test('both profiles equal the deployed resources', () => {
    const h = harness({ moduleId: 'scheduler', moduleName: 'scheduler' });
    const config = h.ctx.config;
    // `scheduler_stack.build_iam_roles()`, roles first so the profiles reference them.
    const schedulerRole = new Role(h.ctx, 'scheduler-role', h.base.stack, {
      description: 'IAM role assigned to the scheduler',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: config.getList<string>('scheduler.iam.managed_policy_arns', []),
    });
    new InstanceProfile(h.ctx, 'scheduler-scheduler-instance-profile', h.base.stack, [schedulerRole]);
    const computeNodeRole = new Role(h.ctx, 'scheduler-compute-node-role', h.base.stack, {
      description: 'IAM role assigned to compute nodes',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: config.getList<string>('scheduler.compute_node_iam.managed_policy_arns', []),
    });
    new InstanceProfile(h.ctx, 'scheduler-compute-node-instance-profile', h.base.stack, [computeNodeRole]);

    assertMatchesLive(h.template(), 'scheduler', [
      'schedulerschedulerinstanceprofile',
      'schedulercomputenodeinstanceprofile',
    ]);
  });
});

describe('ManagedPolicy', () => {
  test('vdc-host-policy equals the deployed managed policy', () => {
    // `virtual_desktop_controller_stack.py:494-501`.
    const h = harness({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' });
    new ManagedPolicy(h.ctx, 'vdc-host-policy', h.base.stack, {
      managedPolicyName: 'idea-dev27-us-east-2-vdc-host',
      description: 'Permissions assigned to virtual-desktop-host',
      policyTemplateName: 'virtual-desktop-dcv-host.yml',
    });
    assertMatchesLive(h.template(), 'vdc', ['vdchostpolicy897A655B']);
  });
});

describe('ManagedPolicy: the two cluster-stack copies of AWS managed policies', () => {
  test('both equal the deployed managed policies', () => {
    // `cluster_stack.build_iam_policies()`; the names carry the region, so the stack passes them.
    const h = harness({ moduleId: 'cluster', moduleName: 'cluster' });
    new ManagedPolicy(h.ctx, 'amazon-ssm-managed-instance-core', h.base.stack, {
      managedPolicyName: 'idea-dev27-us-east-2-amazon-ssm-managed-instance-core',
      description:
        'The policy for Amazon EC2 Role to enable AWS Systems Manager service core functionality.',
      policyTemplateName: 'amazon-ssm-managed-instance-core.yml',
    });
    new ManagedPolicy(h.ctx, 'cloud-watch-agent-server-policy', h.base.stack, {
      managedPolicyName: 'idea-dev27-us-east-2-cloud-watch-agent-server-policy',
      description: 'Permissions required to use AmazonCloudWatchAgent on servers',
      policyTemplateName: 'cloud-watch-agent-server-policy.yml',
    });
    assertMatchesLive(h.template(), 'cluster', [
      'amazonssmmanagedinstancecore26547C9F',
      'cloudwatchagentserverpolicy953A2E64',
    ]);
  });
});

describe('SNSTopic', () => {
  test('the cluster ec2-state-change topic equals the deployed one', () => {
    // `cluster_stack.build_ec2_notification_module()`.
    const h = harness({ moduleId: 'cluster', moduleName: 'cluster' });
    const topic = new SNSTopic(h.ctx, 'cluster-ec2-state-change-sns-topic', h.base.stack, {
      displayName: 'idea-dev27-cluster-ec2-state-change-sns-topic',
      topicName: 'idea-dev27-cluster-ec2-state-change-sns-topic',
      masterKey: h.ctx.config.getString('cluster.sns.kms_key_id'),
    });
    // The stack re-tags the topic afterwards, and the later `Tags.of` call wins at equal
    // priority, so the deployed `Name` tag is `<cluster>-<moduleId>`, not the topic's own name.
    h.base.addCommonTags(topic);
    assertMatchesLive(h.template(), 'cluster', ['clusterec2statechangesnstopic23ADD75C']);
  });

  test('the vdc ssm-commands topic equals the deployed one', () => {
    // `virtual_desktop_controller_stack.build_controller_ssm_commands_notification_infra()`.
    const h = harness({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' });
    const topic = new SNSTopic(h.ctx, 'virtual-desktop-controller-sns-topic', h.base.stack, {
      topicName: 'idea-dev27-vdc-ssm-commands-sns-topic',
      displayName: 'idea-dev27-vdc-ssm-commands-topic',
      masterKey: h.ctx.config.getString('cluster.sns.kms_key_id'),
    });
    h.base.addCommonTags(topic);
    assertMatchesLive(h.template(), 'vdc', ['virtualdesktopcontrollersnstopicB42DFA0E']);
  });

  test('without a master key the alias/aws/sns managed key is imported', () => {
    const h = harness({ moduleId: 'cluster', moduleName: 'cluster' });
    new SNSTopic(h.ctx, 'sample-topic', h.base.stack);
    const resources = h.template().Resources as Json;
    const topic = Object.values(resources).find((resource) => (resource as Json).Type === 'AWS::SNS::Topic') as Json;
    assert.equal(topic.Properties.TopicName, 'idea-dev27-sample-topic');
    assert.equal(topic.Properties.DisplayName, 'idea-dev27-sample-topic');
    let joined = topic.Properties.KmsMasterKeyId['Fn::Join'][1] as unknown[];
    assert.deepEqual(joined[0], 'arn:');
    assert.deepEqual(joined[1], { Ref: 'AWS::Partition' });
    assert.match(String(joined[2]), /^:kms:us-east-2:\d{12}:alias\/aws\/sns$/);

    // `Utils.is_empty` strips before testing, so a whitespace-only key is still empty and the
    // alias, not a `kms:...:key/   ` ARN, is what the topic gets.
    const blank = harness({ moduleId: 'cluster', moduleName: 'cluster' });
    new SNSTopic(blank.ctx, 'sample-topic', blank.base.stack, { masterKey: '  ', topicName: ' ', displayName: '\t' });
    const blankResources = blank.template().Resources as Json;
    const blankTopic = Object.values(blankResources).find(
      (resource) => (resource as Json).Type === 'AWS::SNS::Topic',
    ) as Json;
    assert.equal(blankTopic.Properties.TopicName, 'idea-dev27-sample-topic');
    assert.equal(blankTopic.Properties.DisplayName, 'idea-dev27-sample-topic');
    joined = blankTopic.Properties.KmsMasterKeyId['Fn::Join'][1] as unknown[];
    assert.match(String(joined[2]), /:alias\/aws\/sns$/);
  });
});
