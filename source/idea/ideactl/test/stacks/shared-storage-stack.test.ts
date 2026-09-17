/**
 * The shared-storage stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The template-for-template comparison is the parity harness's job. What lives here is what a
 * template diff cannot see: the CloudFormation assembly manifest (stack tags, termination
 * protection), the deletion/update-replace policy of every resource (the EFS file systems carry a
 * `DeletionPolicy` and no `UpdateReplacePolicy`, which is what keeps an in-place upgrade from
 * replacing them), and the storage branches dev27 does not exercise: an existing file system,
 * FSx for Lustre, and a provider that provisions nothing.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored, and this whole file requires them.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/shared-storage.ts';
import { requireCapture } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-shared-storage.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.shared-storage', 'manifest.json');

const CLUSTER = 'idea-dev27';
const MODULE_ID = 'shared-storage';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/**
 * Copies the dev27 settings scan, replacing (or adding) each named key with the given typed
 * DynamoDB attribute value, and deleting the ones mapped to `null`.
 */
function configWith(overrides: Record<string, Json | null>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  const items = (scan.Items as Json[]).filter((item) => {
    const key = item.key?.S as string | undefined;
    return key === undefined || overrides[key] !== null;
  });
  for (const item of items) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = overrides[key] as Json;
  }
  for (const key of remaining) {
    if (overrides[key] === null) continue;
    items.push({ key: { S: key }, value: overrides[key] as Json, version: { N: '1' } });
  }
  scan.Items = items;
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-storage-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface Synthesized {
  template: Json;
  manifest: Json;
}

async function synth(
  configFile: string = CONFIG_FILE,
  terminationProtection = true,
): Promise<Synthesized> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-storage-'));
  workdirs.push(workdir);
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, `cdk.out.${MODULE_ID}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: MODULE_ID,
        moduleName: MODULE_ID,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_ID]: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${CLUSTER}-${MODULE_ID}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

/**
 * The branches that throw during stack construction. `CDK_OUTDIR` stays unset here so the `App`
 * registers no exit-time autosynth: the stack never finished, and a deferred synth would fire
 * after the temp directories are gone.
 */
async function synthRejects(configFile: string): Promise<void> {
  const previousOutdir = process.env.CDK_OUTDIR;
  delete process.env.CDK_OUTDIR;
  try {
    await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: MODULE_ID,
        moduleName: MODULE_ID,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_ID]: async () => buildStack },
    );
  } finally {
    if (previousOutdir !== undefined) process.env.CDK_OUTDIR = previousOutdir;
  }
}

const deployed = (template: Json): Array<[string, Json]> =>
  Object.entries((template.Resources ?? {}) as Json).filter(
    ([, resource]) => (resource as Json).Type !== 'AWS::CDK::Metadata',
  );

/** `{logicalId: {Type, Path, EFS properties}}`, `AWS::CDK::Metadata` dropped. */
function inventory(template: Json): Record<string, Json> {
  return Object.fromEntries(
    deployed(template).map(([id, resource]) => [
      id,
      {
        Type: resource.Type as string,
        Path: resource.Metadata?.['aws:cdk:path'] as string | undefined,
        Encrypted: resource.Properties?.Encrypted,
        PerformanceMode: resource.Properties?.PerformanceMode,
        ThroughputMode: resource.Properties?.ThroughputMode,
        FileSystemPolicy: resource.Properties?.FileSystemPolicy,
        FileSystemTags: resource.Properties?.FileSystemTags,
        LifecyclePolicies: resource.Properties?.LifecyclePolicies,
      },
    ]),
  );
}

/** `{logicalId: [DeletionPolicy, UpdateReplacePolicy]}` for every resource, undefined included. */
function policies(template: Json): Record<string, [unknown, unknown]> {
  return Object.fromEntries(
    deployed(template).map(([id, resource]) => [
      id,
      [resource.DeletionPolicy, resource.UpdateReplacePolicy] as [unknown, unknown],
    ]),
  );
}

function settingsOf(template: Json): Json {
  const found = deployed(template).find(([, r]) => r.Type === 'Custom::ClusterSettings');
  assert.ok(found, 'no Custom::ClusterSettings resource');
  return (found[1] as Json).Properties.settings as Json;
}

const typesOf = (template: Json): string[] => deployed(template).map(([, r]) => r.Type as string).sort();

// --- the branch dev27 deployed: two provisioned EFS file systems -------------------------------

describe('shared-storage stack, two EFS file systems', () => {
  test('every resource carries exactly the live deletion policies', async () => {
    const { template } = await synth();
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    assert.deepEqual(policies(template), policies(withRetainedStateful(readJson(LIVE_TEMPLATE))));
  });

  test('both file systems delete with the stack and survive a replacement', async () => {
    // The teardown behaviour is unchanged: a deliberate stack delete still takes them. Changing
    // shared-storage.apps.efs.performance_mode or .encrypted is what forces a replacement, and
    // neither file system may be lost to one.
    const { template } = await synth();
    for (const id of ['appsstorageefs', 'datastorageefs']) {
      const resource = (template.Resources as Json)[id] as Json;
      assert.ok(resource, `${id} missing`);
      assert.equal(resource.Type, 'AWS::EFS::FileSystem');
      assert.equal(resource.DeletionPolicy, 'Delete', `${id} DeletionPolicy`);
      assert.equal(resource.UpdateReplacePolicy, 'Retain', `${id} UpdateReplacePolicy`);
    }
  });

  test('the mount targets and the security group carry no deletion policy at all', async () => {
    const { template } = await synth();
    for (const [id, resource] of deployed(template)) {
      if (resource.Type === 'AWS::EFS::MountTarget' || resource.Type === 'AWS::EC2::SecurityGroup') {
        assert.equal(resource.DeletionPolicy, undefined, `${id} DeletionPolicy`);
      }
      if (resource.Type === 'AWS::EC2::SecurityGroup') {
        assert.equal(resource.UpdateReplacePolicy, undefined, `${id} UpdateReplacePolicy`);
      }
    }
  });

  test('the cluster settings resource is Delete/Delete', async () => {
    const { template } = await synth();
    const resource = (template.Resources as Json).ideadev27sharedstoragesettings as Json;
    assert.deepEqual([resource.DeletionPolicy, resource.UpdateReplacePolicy], ['Delete', 'Delete']);
  });

  test('the logical ids and construct paths match the live template', async () => {
    const { template } = await synth();
    assert.deepEqual(inventory(template), inventory(readJson(LIVE_TEMPLATE)));
  });

  test('the mount targets follow the configured private subnet order', async () => {
    const { template } = await synth();
    const configured = (readJson(CONFIG_FILE).Items as Json[])
      .find((item) => item.key?.S === 'cluster.network.private_subnets')
      ?.value?.L?.map((entry: Json) => entry.S as string) as string[];
    assert.equal(configured.length, 3);

    for (const fileSystem of ['apps', 'data']) {
      const mounted = deployed(template)
        .filter(([, r]) => r.Type === 'AWS::EFS::MountTarget')
        .filter(([, r]) =>
          (r.Metadata['aws:cdk:path'] as string).includes(`/${fileSystem}-storage-efs/`),
        )
        .sort(([, a], [, b]) =>
          (a.Metadata['aws:cdk:path'] as string) < (b.Metadata['aws:cdk:path'] as string) ? -1 : 1,
        )
        .map(([, r]) => r.Properties.SubnetId as string);
      assert.deepEqual(mounted, configured, fileSystem);
    }
  });

  test('the settings payload is deployment id, security group, then dns and id per file system', async () => {
    const { template } = await synth();
    const settings = settingsOf(template);
    assert.deepEqual(Object.keys(settings), [
      'deployment_id',
      'security_group_id',
      'apps.efs.dns',
      'apps.efs.file_system_id',
      'data.efs.dns',
      'data.efs.file_system_id',
    ]);
    assert.equal(settings.deployment_id, DEPLOYMENT_ID);
    assert.deepEqual(settings.security_group_id, {
      'Fn::GetAtt': ['sharedstoragesecuritygroup68537F4D', 'GroupId'],
    });
    assert.deepEqual(settings['apps.efs.file_system_id'], { Ref: 'appsstorageefs' });
    assert.deepEqual(settings['data.efs.dns'], {
      'Fn::Join': ['', [{ Ref: 'datastorageefs' }, `.efs.${REGION}.amazonaws.com`]],
    });
  });

  test('the dns guard reads the module id, not the file system key', async () => {
    // `shared-storage.apps.dns` is what the guard reads if it is ever "fixed"; it must not fire.
    const withKeyDns = await synth(configWith({ 'shared-storage.apps.dns': { S: 'fs-1.efs.invalid' } }));
    assert.ok('apps.efs.dns' in settingsOf(withKeyDns.template));

    // `shared-storage.<module id>.dns` is what it actually reads, so this one does fire.
    const withModuleDns = await synth(
      configWith({ [`shared-storage.${MODULE_ID}.dns`]: { S: 'fs-1.efs.invalid' } }),
    );
    const settings = settingsOf(withModuleDns.template);
    assert.deepEqual(Object.keys(settings), [
      'deployment_id',
      'security_group_id',
      'apps.efs.file_system_id',
      'data.efs.file_system_id',
    ]);
  });
});

// --- assembly manifest --------------------------------------------------------------------------

describe('shared-storage stack manifest', () => {
  test('stack properties equal the ones the python app wrote', async () => {
    const { manifest } = await synth();
    const ours = (manifest.artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties as Json;
    const theirs = (readJson(PYTHON_MANIFEST).artifacts as Json)[`${CLUSTER}-${MODULE_ID}`]
      .properties as Json;
    assert.equal(ours.terminationProtection, true);
    assert.equal(ours.terminationProtection, theirs.terminationProtection);
    assert.deepEqual(ours.tags, theirs.tags);
    assert.equal(ours.validateOnSynth, theirs.validateOnSynth);
    assert.equal(ours.assumeRoleArn, theirs.assumeRoleArn);
    assert.equal(ours.cloudFormationExecutionRoleArn, theirs.cloudFormationExecutionRoleArn);
    assert.equal(ours.requiresBootstrapStackVersion, theirs.requiresBootstrapStackVersion);
    assert.equal(ours.bootstrapStackVersionSsmParameter, theirs.bootstrapStackVersionSsmParameter);
    assert.deepEqual(ours.lookupRole, theirs.lookupRole);
    // The template asset digest moves with the template body; the bucket and prefix must not.
    const bucketPrefix = (url: string): string => url.slice(0, url.lastIndexOf('/') + 1);
    assert.equal(
      bucketPrefix(ours.stackTemplateAssetObjectUrl as string),
      bucketPrefix(theirs.stackTemplateAssetObjectUrl as string),
    );
  });

  test('the stack tags are stack level, so they appear in no resource', async () => {
    const { template, manifest } = await synth();
    const tags = (manifest.artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties.tags as Json;
    assert.deepEqual(Object.keys(tags).sort(), [
      'idea:ClusterName',
      'idea:ModuleId',
      'idea:ModuleName',
      'idea:ModuleVersion',
    ]);
    const body = JSON.stringify(template.Resources);
    assert.ok(!body.includes('idea:ModuleVersion'), 'idea:ModuleVersion leaked into the template');
    assert.ok(!body.includes('idea:ModuleId'), 'idea:ModuleId leaked into the template');
    assert.ok(!body.includes('idea:ModuleName'), 'idea:ModuleName leaked into the template');
  });

  test('termination protection follows the flag', async () => {
    const { manifest } = await synth(CONFIG_FILE, false);
    const properties = (manifest.artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties as Json;
    assert.equal(properties.terminationProtection, false);
  });
});

// --- branches the dev27 capture does not cover -------------------------------------------------

describe('shared-storage stack, other providers', () => {
  test('an existing file system writes only its dns and provisions nothing', async () => {
    const { template } = await synth(
      configWith({
        'shared-storage.data.provider': { S: 'fsx_lustre' },
        'shared-storage.data.fsx_lustre.use_existing_fs': { BOOL: true },
        'shared-storage.data.fsx_lustre.file_system_id': { S: 'fs-01234567890abcdef' },
      }),
    );
    assert.deepEqual(typesOf(template), [
      'AWS::EC2::SecurityGroup',
      'AWS::EFS::FileSystem',
      'AWS::EFS::MountTarget',
      'AWS::EFS::MountTarget',
      'AWS::EFS::MountTarget',
      'Custom::ClusterSettings',
    ]);
    const settings = settingsOf(template);
    assert.equal(settings['data.fsx_lustre.dns'], `fs-01234567890abcdef.fsx.${REGION}.amazonaws.com`);
    assert.ok(!('data.fsx_lustre.file_system_id' in settings), 'existing file systems write no id');
  });

  test('an existing file system without an id fails at synth', async () => {
    const config = configWith({
      'shared-storage.data.provider': { S: 'fsx_lustre' },
      'shared-storage.data.fsx_lustre.use_existing_fs': { BOOL: true },
    });
    await assert.rejects(
      synthRejects(config),
      /shared-storage\.data\.fsx_lustre\.file_system_id is required/,
    );
  });

  test('fsx_lustre provisions one file system in the first private subnet, with no deletion policy', async () => {
    const { template } = await synth(
      configWith({
        'shared-storage.data.provider': { S: 'fsx_lustre' },
        'shared-storage.data.fsx_lustre.deployment_type': { S: 'PERSISTENT_1' },
        'shared-storage.data.fsx_lustre.storage_type': { S: 'SSD' },
        'shared-storage.data.fsx_lustre.per_unit_storage_throughput': { N: '125' },
        'shared-storage.data.fsx_lustre.storage_capacity': { N: '1200' },
      }),
    );
    const found = deployed(template).find(([, r]) => r.Type === 'AWS::FSx::FileSystem');
    assert.ok(found, 'no AWS::FSx::FileSystem');
    const [id, resource] = found;
    assert.equal(id, 'datastoragefsxlustre');
    assert.equal(resource.Metadata['aws:cdk:path'], `${CLUSTER}-${MODULE_ID}/data-storage-fsx-lustre`);
    assert.equal(resource.Properties.FileSystemType, 'LUSTRE');
    assert.equal(resource.Properties.StorageCapacity, 1200);
    assert.deepEqual(resource.Properties.LustreConfiguration, {
      DeploymentType: 'PERSISTENT_1',
      PerUnitStorageThroughput: 125,
    });
    // Python never writes StorageType onto the resource; the config key only picks the Lustre branch.
    assert.equal(resource.Properties.StorageType, undefined);
    const configured = (readJson(CONFIG_FILE).Items as Json[])
      .find((item) => item.key?.S === 'cluster.network.private_subnets')
      ?.value?.L?.map((entry: Json) => entry.S as string) as string[];
    assert.deepEqual(resource.Properties.SubnetIds, [configured[0]]);

    const settings = settingsOf(template);
    assert.deepEqual(settings['data.fsx_lustre.dns'], {
      'Fn::Join': ['', [{ Ref: 'datastoragefsxlustre' }, `.fsx.${REGION}.amazonaws.com`]],
    });
    assert.deepEqual(settings['data.fsx_lustre.file_system_id'], { Ref: 'datastoragefsxlustre' });
    assert.equal(resource.DeletionPolicy, undefined);
    // A managed file system is the only copy of what it holds, so it is never lost to an update
    // that forces a replacement. Its teardown behaviour is unchanged.
    assert.equal(resource.UpdateReplacePolicy, 'Retain');
  });

  test('a provider this stack does not provision builds nothing and writes no settings for it', async () => {
    // A cluster whose /data lives on an ONTAP volume created outside this stack.
    const { template } = await synth(
      configWith({
        'shared-storage.data.provider': { S: 'fsx_netapp_ontap' },
        'shared-storage.data.efs.cloudwatch_monitoring': null,
        'shared-storage.data.efs.dns': null,
        'shared-storage.data.efs.encrypted': null,
        'shared-storage.data.efs.file_system_id': null,
        'shared-storage.data.efs.kms_key_id': null,
        'shared-storage.data.efs.performance_mode': null,
        'shared-storage.data.efs.removal_policy': null,
        'shared-storage.data.efs.throughput_mode': null,
        'shared-storage.data.efs.transition_to_ia': null,
      }),
    );
    assert.deepEqual(typesOf(template), [
      'AWS::EC2::SecurityGroup',
      'AWS::EFS::FileSystem',
      'AWS::EFS::MountTarget',
      'AWS::EFS::MountTarget',
      'AWS::EFS::MountTarget',
      'Custom::ClusterSettings',
    ]);
    assert.deepEqual(Object.keys(settingsOf(template)), [
      'deployment_id',
      'security_group_id',
      'apps.efs.dns',
      'apps.efs.file_system_id',
    ]);
  });

  test('a key without mount_dir is not a file system', async () => {
    const { template } = await synth(
      configWith({
        'shared-storage.scratch.provider': { S: 'efs' },
        'shared-storage.scratch.title': { S: 'Shared Storage - Scratch' },
      }),
    );
    assert.equal(deployed(template).filter(([, r]) => r.Type === 'AWS::EFS::FileSystem').length, 2);
    assert.ok(!('scratch.efs.dns' in settingsOf(template)));
  });

  test('an unsupported provider fails at synth', async () => {
    const config = configWith({ 'shared-storage.data.provider': { S: 'nfs' } });
    await assert.rejects(synthRejects(config), /file system provider: nfs not supported/);
  });

  test('a missing mandatory provider key fails at synth', async () => {
    const config = configWith({
      'shared-storage.data.provider': null,
      'shared-storage.data.mount_dir': null,
    });
    await assert.rejects(synthRejects(config), /'provider', key: shared-storage\.data\.provider/);
  });
});
