/**
 * `constructs/storage.ts` against the live `shared-storage` template, plus the FSx for
 * Lustre branch (no live coverage) and the RETAIN removal policy.
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import * as ec2 from 'aws-cdk-lib/aws-ec2';

import { ExistingSocaCluster } from '../../src/cdk/constructs/existing-resources.ts';
import { AmazonEFS, FSxForLustre } from '../../src/cdk/constructs/storage.ts';
import { cleanup, configWith, harness, liveResources, requireLiveFixture } from './harness.ts';
import type { Json } from './harness.ts';

after(cleanup);

requireLiveFixture('shared-storage');

/**
 * The shared-storage stack builds its security group with `SharedStorageSecurityGroup`; a
 * bare `ec2.SecurityGroup` at the same construct id produces the same logical ID, which is all the
 * mount targets reference.
 */
function buildStorage(configFile?: string): { resources: Json; ctx: ReturnType<typeof harness>['ctx'] } {
  const h = harness({ moduleId: 'shared-storage', moduleName: 'shared-storage', configFile });
  const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
  const securityGroup = new ec2.SecurityGroup(h.base.stack, 'shared-storage-security-group', {
    vpc: cluster.vpc,
  });

  for (const name of ['apps', 'data']) {
    new AmazonEFS(h.ctx, `${name}-storage-efs`, h.base.stack, {
      vpc: cluster.vpc,
      securityGroup,
      efsConfig: h.ctx.config.getConfig(`shared-storage.${name}.efs`),
      subnets: cluster.privateSubnets,
    });
  }
  return { resources: h.template().Resources as Json, ctx: h.ctx };
}

describe('AmazonEFS', () => {
  const live = liveResources('shared-storage');
  const efsLogicalIds = Object.entries(live)
    .filter(([, resource]) => String((resource as Json).Type).startsWith('AWS::EFS::'))
    .map(([logicalId]) => logicalId);

  test('every file system and mount target matches the deployed template', () => {
    const { resources } = buildStorage();
    assert.equal(efsLogicalIds.length, 8, 'expected 2 file systems and 6 mount targets');
    for (const logicalId of efsLogicalIds) {
      assert.ok(resources[logicalId] !== undefined, `missing logical id: ${logicalId}`);
      assert.deepEqual(resources[logicalId], live[logicalId], `mismatch on ${logicalId}`);
    }
  });

  test('mount targets are children of the file system, one per private subnet', () => {
    const { resources } = buildStorage();
    const mountTargets = Object.entries(resources).filter(
      ([, resource]) => (resource as Json).Type === 'AWS::EFS::MountTarget',
    );
    const appsPaths = mountTargets
      .map(([, resource]) => (resource as Json).Metadata['aws:cdk:path'] as string)
      .filter((path) => path.includes('apps-storage-efs'))
      .sort();
    assert.deepEqual(appsPaths, [
      'idea-dev27-shared-storage/apps-storage-efs/apps-storage-efs-mount-target-1',
      'idea-dev27-shared-storage/apps-storage-efs/apps-storage-efs-mount-target-2',
      'idea-dev27-shared-storage/apps-storage-efs/apps-storage-efs-mount-target-3',
    ]);
  });

  test('DeletionPolicy is set from the config and no UpdateReplacePolicy is emitted', () => {
    const { resources } = buildStorage();
    assert.equal(resources.appsstorageefs.DeletionPolicy, 'Delete');
    assert.equal(resources.appsstorageefs.UpdateReplacePolicy, undefined);
  });

  test('a RETAIN removal policy is honoured', () => {
    const configFile = configWith({ 'shared-storage.apps.efs.removal_policy': 'RETAIN' });
    const { resources } = buildStorage(configFile);
    assert.equal(resources.appsstorageefs.DeletionPolicy, 'Retain');
    assert.equal(resources.appsstorageefs.UpdateReplacePolicy, undefined);
    // the other file system still follows its own key
    assert.equal(resources.datastorageefs.DeletionPolicy, 'Delete');
  });

  test('an unusable removal policy raises rather than silently emitting none', () => {
    const configFile = configWith({ 'shared-storage.apps.efs.removal_policy': null });
    assert.throws(() => buildStorage(configFile), /is not a valid CfnDeletionPolicy/);
  });

  test('transition_to_ia only emits LifecyclePolicies when it is set', () => {
    const { resources } = buildStorage();
    assert.equal(resources.appsstorageefs.Properties.LifecyclePolicies, undefined);
    assert.deepEqual(resources.datastorageefs.Properties.LifecyclePolicies, [
      { TransitionToIA: 'AFTER_30_DAYS' },
    ]);
  });

  test('cloudwatch_monitoring: true builds nothing (the Python branch is dead code)', () => {
    const { ctx, resources } = buildStorage();
    assert.equal(ctx.config.getBool('shared-storage.apps.efs.cloudwatch_monitoring'), true);
    const types = new Set(Object.values(resources).map((resource) => (resource as Json).Type));
    for (const type of ['AWS::SNS::Topic', 'AWS::CloudWatch::Alarm', 'AWS::Lambda::Function']) {
      assert.ok(!types.has(type), `${type} should not be built`);
    }
  });

  test('encrypted defaults to true and can be turned off', () => {
    const defaulted = configWith({
      'shared-storage.apps.efs.encrypted': null,
      'shared-storage.data.efs.encrypted': null,
    });
    const { resources: defaults } = buildStorage(defaulted);
    assert.equal(defaults.appsstorageefs.Properties.Encrypted, true);
    assert.equal(defaults.datastorageefs.Properties.Encrypted, true);

    const overridden = configWith({
      'shared-storage.apps.efs.encrypted': false,
      'shared-storage.data.efs.encrypted': null,
    });
    const { resources } = buildStorage(overridden);
    assert.equal(resources.appsstorageefs.Properties.Encrypted, false);
    assert.equal(resources.datastorageefs.Properties.Encrypted, true);
  });
});

describe('FSxForLustre', () => {
  function buildLustre(fsxLustreConfig: Record<string, unknown>): Json {
    const h = harness({ moduleId: 'shared-storage', moduleName: 'shared-storage' });
    const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
    const securityGroup = new ec2.SecurityGroup(h.base.stack, 'shared-storage-security-group', {
      vpc: cluster.vpc,
    });
    new FSxForLustre(h.ctx, 'scratch-storage-fsx-lustre', h.base.stack, {
      vpc: cluster.vpc,
      securityGroup,
      fsxLustreConfig,
      subnets: cluster.privateSubnets,
    });
    return (h.template().Resources as Json).scratchstoragefsxlustre;
  }

  test('SSD + PERSISTENT_1 carries per-unit throughput and lands in the first subnet only', () => {
    const fileSystem = buildLustre({
      deployment_type: 'PERSISTENT_1',
      storage_type: 'SSD',
      per_unit_storage_throughput: 100,
      storage_capacity: 1200,
    });
    assert.equal(fileSystem.Type, 'AWS::FSx::FileSystem');
    assert.equal(fileSystem.Properties.FileSystemType, 'LUSTRE');
    assert.equal(fileSystem.Properties.StorageCapacity, 1200);
    assert.equal((fileSystem.Properties.SubnetIds as string[]).length, 1);
    assert.deepEqual(fileSystem.Properties.LustreConfiguration, {
      DeploymentType: 'PERSISTENT_1',
      PerUnitStorageThroughput: 100,
    });
    assert.deepEqual(fileSystem.Properties.Tags, [
      { Key: 'idea:BackupPlan', Value: 'idea-dev27-cluster' },
      { Key: 'idea:ClusterName', Value: 'idea-dev27' },
      { Key: 'Name', Value: 'idea-dev27-scratch-storage-fsx-lustre' },
    ]);
    assert.equal(fileSystem.DeletionPolicy, undefined);
  });

  test('SSD + SCRATCH_2 drops per-unit throughput', () => {
    const fileSystem = buildLustre({
      deployment_type: 'SCRATCH_2',
      storage_type: 'SSD',
      per_unit_storage_throughput: 100,
      storage_capacity: 1200,
    });
    assert.deepEqual(fileSystem.Properties.LustreConfiguration, { DeploymentType: 'SCRATCH_2' });
  });

  test('HDD drops drive_cache_type, because Python reads it as an int', () => {
    const fileSystem = buildLustre({
      deployment_type: 'PERSISTENT_1',
      storage_type: 'HDD',
      drive_cache_type: 'READ',
      per_unit_storage_throughput: 12,
      storage_capacity: 6000,
    });
    assert.deepEqual(fileSystem.Properties.LustreConfiguration, { DeploymentType: 'PERSISTENT_1' });
  });
});
