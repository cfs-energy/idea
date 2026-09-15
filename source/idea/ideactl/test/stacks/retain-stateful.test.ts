/**
 * An update that forces a replacement must never be able to delete state.
 *
 * The list of stateful CloudFormation types is written out here, in full, rather than imported
 * from the source. That is the point of this file: the parity gate derives its expectation from
 * the same predicate the synthesis uses, so a type dropped from that predicate moves both sides
 * together and the gate stays green while the resource loses its protection. These assertions do
 * not move with it.
 *
 * The list is what the three captured clusters actually contain and have never had replaced, plus
 * the two types the toolkit stack contains. Add to it when a stack starts building a new kind of
 * stateful resource.
 */

import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import { App, Aspects, CfnResource, RemovalPolicy, Stack } from 'aws-cdk-lib';

import { RetainStatefulOnUpdateReplace } from '../../src/cdk/app.ts';
import { isStatefulType } from '../../src/cdk/stateful.ts';
import { isStatefulType as guardIsStatefulType } from '../../src/cli/cdk-invoker.ts';
import { intendedDriftFor } from '../../tools/parity/intended-drift.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const LIVE = join(PKG, 'tools', 'parity', 'live');
const CLUSTER = 'idea-test1';

/**
 * Every resource type in the captured clusters that holds state, or that lives in a namespace
 * whose members do. Written by hand from the captures, not generated.
 */
const STATEFUL_TYPES = [
  'AWS::Backup::BackupPlan',
  'AWS::Backup::BackupSelection',
  'AWS::Backup::BackupVault',
  'AWS::Cognito::UserPool',
  'AWS::Cognito::UserPoolClient',
  'AWS::Cognito::UserPoolDomain',
  'AWS::Cognito::UserPoolGroup',
  'AWS::Cognito::UserPoolResourceServer',
  'AWS::DirectoryService::MicrosoftAD',
  'AWS::EFS::FileSystem',
  'AWS::EFS::MountTarget',
  'AWS::FSx::FileSystem',
  'AWS::KMS::Key',
  'AWS::Kinesis::Stream',
  'AWS::Logs::LogGroup',
  'AWS::OpenSearchService::Domain',
  'AWS::Route53::HostedZone',
  'AWS::Route53::RecordSet',
  'AWS::S3::Bucket',
  'AWS::SNS::Subscription',
  'AWS::SNS::Topic',
  'AWS::SNS::TopicPolicy',
  'AWS::SQS::Queue',
  'AWS::SQS::QueuePolicy',
  'AWS::SecretsManager::Secret',
];

/**
 * Types the captures also contain that this set deliberately leaves alone. Most hold nothing at
 * all. The two that are replaced routinely, the compute instance and the target group, are here
 * for a different reason: three years of deployment history on the real clusters shows an upgrade
 * replacing them about twenty times per cluster, so a Retain would leave twenty orphaned instances
 * and their volumes behind and bill for all of them. That is litter rather than protection.
 */
const STATELESS_TYPES = [
  'AWS::AutoScaling::AutoScalingGroup',
  'AWS::EC2::Instance',
  'AWS::EC2::LaunchTemplate',
  'AWS::EC2::SecurityGroup',
  'AWS::EC2::Subnet',
  'AWS::EC2::VPC',
  'AWS::ElasticLoadBalancingV2::Listener',
  'AWS::ElasticLoadBalancingV2::LoadBalancer',
  'AWS::ElasticLoadBalancingV2::TargetGroup',
  'AWS::IAM::Policy',
  'AWS::IAM::Role',
  'AWS::Lambda::Function',
  'AWS::Route53Resolver::ResolverEndpoint',
  'AWS::Route53Resolver::ResolverRule',
  'AWS::SSM::Parameter',
  'AWS::WAFv2::WebACL',
];

type Json = Record<string, any>;

/** One synthesized template holding one resource of each given type, with the aspect applied. */
function synthTypes(types: readonly string[], removalPolicy?: RemovalPolicy): Json {
  const app = new App();
  const stack = new Stack(app, 'test-stack');
  for (const [index, type] of types.entries()) {
    const resource = new CfnResource(stack, `r${index}`, { type });
    if (removalPolicy !== undefined) resource.applyRemovalPolicy(removalPolicy);
  }
  Aspects.of(app).add(new RetainStatefulOnUpdateReplace());
  return app.synth().getStackByName('test-stack').template as Json;
}

describe('the stateful classification', () => {
  test('every stateful type in the captures is classified stateful', () => {
    const missed = STATEFUL_TYPES.filter((type) => !isStatefulType(type));
    assert.deepEqual(missed, [], `these types hold state and the predicate says they do not: ${missed.join(', ')}`);
  });

  test('no stateless type in the captures is classified stateful', () => {
    const over = STATELESS_TYPES.filter((type) => isStatefulType(type));
    assert.deepEqual(over, [], `these types hold nothing and the predicate says they do: ${over.join(', ')}`);
  });

  test('the change-set guard and the synthesis use one predicate', () => {
    // Not "both agree on this sample": the same function, so they cannot come to disagree.
    assert.equal(guardIsStatefulType, isStatefulType);
  });

  test('an unknown type is not stateful', () => {
    assert.equal(isStatefulType('AWS::Fictional::Thing'), false);
    assert.equal(isStatefulType(undefined), false);
  });
});

describe('RetainStatefulOnUpdateReplace', () => {
  test('sets UpdateReplacePolicy Retain on every stateful type', () => {
    const template = synthTypes(STATEFUL_TYPES);
    const wrong = STATEFUL_TYPES.map((type, index) => [type, template.Resources[`r${index}`]] as const)
      .filter(([, resource]) => resource.UpdateReplacePolicy !== 'Retain')
      .map(([type, resource]) => `${type}=${String(resource.UpdateReplacePolicy)}`);
    assert.deepEqual(wrong, [], `not retained on update-replace: ${wrong.join(', ')}`);
  });

  test('leaves stateless types alone', () => {
    const template = synthTypes(STATELESS_TYPES);
    const touched = STATELESS_TYPES.map((type, index) => [type, template.Resources[`r${index}`]] as const)
      .filter(([, resource]) => resource.UpdateReplacePolicy !== undefined)
      .map(([type, resource]) => `${type}=${String(resource.UpdateReplacePolicy)}`);
    assert.deepEqual(touched, [], `these carry a policy they should not: ${touched.join(', ')}`);
  });

  test('does not change DeletionPolicy, so a deliberate teardown still deletes', () => {
    const template = synthTypes(STATEFUL_TYPES, RemovalPolicy.DESTROY);
    const kept = STATEFUL_TYPES.map((type, index) => [type, template.Resources[`r${index}`]] as const)
      .filter(([, resource]) => resource.DeletionPolicy !== 'Delete' || resource.UpdateReplacePolicy !== 'Retain')
      .map(([type, resource]) => `${type}=${String(resource.DeletionPolicy)}/${String(resource.UpdateReplacePolicy)}`);
    assert.deepEqual(kept, [], `expected Delete/Retain, got: ${kept.join(', ')}`);
  });

  test('a resource already retained on teardown stays retained on both', () => {
    const template = synthTypes(['AWS::EFS::FileSystem'], RemovalPolicy.RETAIN);
    assert.deepEqual(
      [template.Resources.r0.DeletionPolicy, template.Resources.r0.UpdateReplacePolicy],
      ['Retain', 'Retain'],
    );
  });
});

describe('the recorded templates', () => {
  const stacks = [
    'analytics',
    'bastion-host',
    'cluster',
    'cluster-manager',
    'directoryservice',
    'identity-provider',
    'metrics',
    'scheduler',
    'shared-storage',
    'vdc',
  ];
  const cluster = 'idea-dev27';
  const files = stacks.map((stack) => [stack, join(LIVE, `${cluster}-${stack}.json`)] as const);
  const present = files.filter(([, file]) => existsSync(file));

  test('every stateful resource in every recorded template is itemised as intended drift', { skip: present.length === 0 && 'no captured templates' }, () => {
    const unnamed: string[] = [];
    for (const [stack, file] of present) {
      const template = JSON.parse(readFileSync(file, 'utf8')) as Json;
      const drift = intendedDriftFor(cluster, stack, template);
      const named = new Set((drift?.differences ?? []).map((difference) => difference.path));
      for (const [id, resource] of Object.entries(template.Resources as Json)) {
        const typed = resource as Json;
        if (!STATEFUL_TYPES.includes(String(typed.Type))) continue;
        if (typed.UpdateReplacePolicy === 'Retain') continue;
        if (!named.has(`Resources.${id}.UpdateReplacePolicy`)) unnamed.push(`${stack}/${id} (${String(typed.Type)})`);
      }
    }
    assert.deepEqual(unnamed, [], `stateful and neither retained nor itemised: ${unnamed.join(', ')}`);
  });
});

describe('a deployed stack', () => {
  test('the attributes are not resource properties, so nothing is handed to the provider', () => {
    // The two attributes live beside Properties, never inside it. That is why adding one to a
    // stack that is already deployed is a change to CloudFormation's bookkeeping and not an
    // update to the resource.
    const template = synthTypes(['AWS::SQS::Queue']);
    const resource = template.Resources.r0 as Json;
    assert.equal(resource.UpdateReplacePolicy, 'Retain');
    assert.equal(resource.Properties?.UpdateReplacePolicy, undefined);
    assert.equal(resource.Properties?.DeletionPolicy, undefined);
  });
});

export { CLUSTER };
