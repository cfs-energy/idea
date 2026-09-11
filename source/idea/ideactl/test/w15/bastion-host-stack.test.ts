/**
 * The bastion-host stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The template itself is covered by the parity harness, so these tests carry what a template
 * diff cannot see: the stack-level manifest properties, the deletion and update-replace policy
 * of every resource, the dependency edges, and the config branches dev27 does not exercise.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; every test that needs them skips
 * when they are absent so the suite still runs without them.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { bootstrapPackageBasenames, bootstrapPackageUri } from '../../src/cli/bootstrap-package.ts';
import { buildStack } from '../../src/cdk/stacks/bastion-host.ts';
import { ec2BlockDeviceName } from '../../src/cdk/stacks/bastion-host.ts';
import { buildApp } from '../../src/cdk/app.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { cacheSetup } from '../support/setup-cache.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.bastion-host', 'manifest.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-bastion-host.json');

const CLUSTER = 'idea-dev27';
const MODULE = 'bastion-host';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

requireFixtures(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/** Copies the dev27 settings scan, replacing the typed value of each named key. */
function configWith(overrides: Record<string, Json>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = overrides[key];
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({ key: { S: key }, value: overrides[key], version: { N: '1' } });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-bastion-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface SynthResult {
  template: Json;
  manifest: Json;
}

async function synthBastionUncached(
  configFile: string = CONFIG_FILE,
  extraContext: Record<string, unknown> = {},
): Promise<SynthResult> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-bastion-'));
  workdirs.push(workdir);
  // Leave bootstrap_package_uri unset unless a test supplies it. The stack
  // then builds the location from the bucket and deployment identifier, so
  // the resource compare includes that field.
  const context = {
    ...readJson(CONTEXT_FILE),
    ...extraContext,
  };
  writeFileSync(join(workdir, 'cdk.context.json'), JSON.stringify(context));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, `cdk.out.${MODULE}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: MODULE,
        moduleName: MODULE,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE]: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${CLUSTER}-${MODULE}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

const synthDefaultBastion = cacheSetup(async () => synthBastionUncached());

/**
 * Reuses the immutable default-fixture synthesis and clones its plain JSON
 * result. Configuration and context branches always synthesize fresh.
 */
async function synthBastion(
  configFile: string = CONFIG_FILE,
  extraContext: Record<string, unknown> = {},
): Promise<SynthResult> {
  if (configFile === CONFIG_FILE && Object.keys(extraContext).length === 0) {
    return synthDefaultBastion();
  }
  return synthBastionUncached(configFile, extraContext);
}

/** Everything but `AWS::CDK::Metadata`, whose Analytics blob differs between the two runtimes. */
function deployedResources(template: Json): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );
}

/** Rebuilds the package location from settings and the uploader naming functions. */
function reconstructedBootstrapPackageUri(): string {
  const item = (readJson(CONFIG_FILE).Items as Json[]).find((row) => row.key?.S === 'cluster.cluster_s3_bucket');
  const bucket = item?.value?.S;
  if (typeof bucket !== 'string' || bucket.length === 0) {
    throw new Error('cluster-settings fixture has no cluster.cluster_s3_bucket');
  }
  return bootstrapPackageUri(bucket, `${bootstrapPackageBasenames(MODULE, DEPLOYMENT_ID).standard}.tar.gz`);
}

/** The recorded user data contains the package location passed to the deployed stack. */
function fixtureBootstrapPackageUri(): string {
  const instance = deployedResources(readJson(LIVE_TEMPLATE)).bastionhostinstance as Json;
  const userData = instance.Properties.UserData['Fn::Base64']['Fn::Sub'];
  if (typeof userData !== "string") {
    throw new Error("bastion-host fixture has no instance user data");
  }
  const match = /download_bootstrap\.sh "([^"]+)"/.exec(userData);
  if (match?.[1] === undefined) {
    throw new Error("bastion-host fixture has no bootstrap package location");
  }
  return match[1];
}

/** `{logicalId: {Type, DeletionPolicy, UpdateReplacePolicy}}`, the part a property diff skips. */
function removalPolicies(template: Json): Json {
  return Object.fromEntries(
    Object.entries(deployedResources(template)).map(([id, resource]) => [
      id,
      {
        Type: (resource as Json).Type,
        DeletionPolicy: (resource as Json).DeletionPolicy ?? null,
        UpdateReplacePolicy: (resource as Json).UpdateReplacePolicy ?? null,
      },
    ]),
  );
}

function dependsOn(template: Json): Json {
  return Object.fromEntries(
    Object.entries(deployedResources(template))
      .filter(([, resource]) => (resource as Json).DependsOn !== undefined)
      .map(([id, resource]) => [id, (resource as Json).DependsOn]),
  );
}

const stackArtifact = (manifest: Json): Json => manifest.artifacts[`${CLUSTER}-${MODULE}`] as Json;

describe('bastion-host stack, dev27 fixtures', () => {
  test('matches the deployed dev27 template resource for resource', async () => {
    const { template } = await synthBastion();
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    const live = withRetainedStateful(readJson(LIVE_TEMPLATE));
    const expectedUri = reconstructedBootstrapPackageUri();
    const instanceUserData = (resource: Json): string =>
      resource.Properties.UserData['Fn::Base64']['Fn::Sub'] as string;
    const downloadLocation = (userData: string, label: string): string => {
      const match = /download_bootstrap\.sh "([^"]+)"/.exec(userData);
      assert.ok(match?.[1], `${label} user data has no bootstrap package location`);
      return match[1];
    };
    assert.equal(
      downloadLocation(instanceUserData(deployedResources(template).bastionhostinstance as Json), 'synth'),
      expectedUri,
    );
    assert.equal(
      downloadLocation(instanceUserData(deployedResources(live).bastionhostinstance as Json), 'live'),
      expectedUri,
    );
    assert.deepEqual(deployedResources(template), deployedResources(live));
  });

  test('every resource carries the deletion policy the live stack has', async () => {
    const { template } = await synthBastion();
    const live = removalPolicies(withRetainedStateful(readJson(LIVE_TEMPLATE)));
    assert.deepEqual(removalPolicies(template), live);

    // Spelled out, so a change to the live capture cannot quietly relax the assertion:
    // the settings custom resource is the only resource with a policy at all.
    assert.deepEqual(live.ideadev27bastionhostsettings, {
      Type: 'Custom::ClusterSettings',
      DeletionPolicy: 'Delete',
      UpdateReplacePolicy: 'Delete',
    });
    const stateful = ['bastionhostdnsrecordDDBD12D3'];
    for (const [id, policies] of Object.entries(live)) {
      if (id === 'ideadev27bastionhostsettings') continue;
      assert.equal((policies as Json).DeletionPolicy, null, `${id} should have no DeletionPolicy`);
      if (stateful.includes(id)) continue;
      assert.equal((policies as Json).UpdateReplacePolicy, null, `${id} should have no UpdateReplacePolicy`);
    }
    // The host itself carries nothing in either direction. Changing bastion-host.instance_ami
    // replaces it, and an ordinary upgrade does that already, so a Retain here would leave an
    // orphaned instance and its volume behind on every one.
    assert.equal(live.bastionhostinstance.DeletionPolicy, null);
    assert.equal(live.bastionhostinstance.UpdateReplacePolicy, null);
    // The DNS record is a different matter: it is never replaced by an upgrade, and a replacement
    // that took it would take the name the host is reached by.
    for (const id of stateful) {
      assert.equal(live[id].DeletionPolicy, null, id);
      assert.equal(live[id].UpdateReplacePolicy, 'Retain', id);
    }
  });

  test('the only dependency edges are the two explicit ones', async () => {
    const { template } = await synthBastion();
    assert.deepEqual(dependsOn(template), {
      bastionhostinstanceprofile: ['bastionhostrole430C4862'],
      bastionhostinstance: ['bastionhostinstanceprofile'],
    });
    assert.deepEqual(dependsOn(template), dependsOn(readJson(LIVE_TEMPLATE)));
  });

  test('the instance takes the instance profile by name, not by reference', async () => {
    const { template } = await synthBastion();
    const instance = deployedResources(template).bastionhostinstance as Json;
    assert.equal(typeof instance.Properties.IamInstanceProfile, 'string');
    assert.equal(instance.Properties.IamInstanceProfile, `${CLUSTER}-${MODULE}-instance-profile-${REGION}`);
  });
});

describe('bastion-host stack manifest', () => {
  test('stack properties the template does not carry', async () => {
    const { manifest } = await synthBastion();
    const artifact = stackArtifact(manifest);
    assert.equal(artifact.type, 'aws:cloudformation:stack');
    assert.equal(artifact.displayName, `${CLUSTER}-${MODULE}`);
    assert.match(artifact.environment as string, /^aws:\/\/\d{12}\/us-east-2$/);

    const properties = artifact.properties as Json;
    assert.equal(properties.templateFile, `${CLUSTER}-${MODULE}.template.json`);
    assert.equal(properties.terminationProtection, true);
    assert.equal(properties.validateOnSynth, false);
    assert.deepEqual(properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': MODULE,
      'idea:ModuleName': MODULE,
      'idea:ModuleVersion': '26.09.0',
    });
    // Synthesizer: the cluster-name qualifier, the cluster bucket and the `cdk/` prefix.
    assert.match(properties.bootstrapStackVersionSsmParameter as string, /^\/cdk-bootstrap\/[0-9a-f]{10}\/version$/);
    assert.equal(properties.requiresBootstrapStackVersion, 6);
    assert.match(properties.stackTemplateAssetObjectUrl as string, /^s3:\/\/[a-z0-9.-]+\/cdk\/[0-9a-f]{64}\.json$/);
    assert.match((properties.lookupRole as Json).arn as string, /:role\/cdk-[0-9a-f]{10}-lookup-role-/);
  });

  test('matches the manifest the python administrator wrote', async () => {
    const { manifest } = await synthBastion();
    const ours = stackArtifact(manifest).properties as Json;
    const theirs = stackArtifact(readJson(PYTHON_MANIFEST)).properties as Json;
    // Everything but the template asset hash, which moves with the template content.
    for (const key of [
      'templateFile',
      'terminationProtection',
      'tags',
      'validateOnSynth',
      'assumeRoleArn',
      'cloudFormationExecutionRoleArn',
      'requiresBootstrapStackVersion',
      'bootstrapStackVersionSsmParameter',
      'lookupRole',
    ]) {
      assert.deepEqual(ours[key], theirs[key], `manifest property ${key}`);
    }
    assert.equal(
      (ours.stackTemplateAssetObjectUrl as string).replace(/[0-9a-f]{64}/, ''),
      (theirs.stackTemplateAssetObjectUrl as string).replace(/[0-9a-f]{64}/, ''),
    );
    assert.equal(
      stackArtifact(manifest).environment,
      stackArtifact(readJson(PYTHON_MANIFEST)).environment,
    );
  });
});

describe('bastion-host stack, config branches dev27 does not deploy', () => {
  test('a private bastion drops the public ip setting and lands in a private subnet', async () => {
    const config = configWith({ 'bastion-host.public': { BOOL: false } });
    const resources = deployedResources((await synthBastion(config)).template);
    const nic = resources.bastionhostinstance.Properties.NetworkInterfaces[0] as Json;
    assert.equal(nic.AssociatePublicIpAddress, false);
    const privateSubnets = readJson(CONFIG_FILE).Items.find(
      (item: Json) => item.key?.S === 'cluster.network.private_subnets',
    );
    assert.equal(nic.SubnetId, (privateSubnets.value.L as Json[])[0].S);
    assert.deepEqual(Object.keys(resources.ideadev27bastionhostsettings.Properties.settings), [
      'deployment_id',
      'private_ip',
      'private_dns_name',
      'instance_id',
      'iam_role_arn',
      'instance_profile_arn',
    ]);
  });

  test('termination protection off adds the EC29 suppression', async () => {
    const config = configWith({ 'bastion-host.ec2.enable_termination_protection': { BOOL: false } });
    const instance = deployedResources((await synthBastion(config)).template).bastionhostinstance as Json;
    assert.equal(instance.Properties.DisableApiTermination, false);
    assert.deepEqual(
      (instance.Metadata.cdk_nag.rules_to_suppress as Json[]).map((rule) => rule.id),
      ['AwsSolutions-EC26', 'AwsSolutions-EC28', 'AwsSolutions-EC29'],
    );
  });

  test('detailed monitoring on drops the EC28 suppression', async () => {
    const config = configWith({ 'bastion-host.ec2.enable_detailed_monitoring': { BOOL: true } });
    const instance = deployedResources((await synthBastion(config)).template).bastionhostinstance as Json;
    assert.equal(instance.Properties.Monitoring, true);
    assert.deepEqual(
      (instance.Metadata.cdk_nag.rules_to_suppress as Json[]).map((rule) => rule.id),
      ['AwsSolutions-EC26'],
    );
  });

  test('a volume type other than gp3 becomes gp2 in the launch template only', async () => {
    const config = configWith({ 'bastion-host.volume_type': { S: 'io2' } });
    const resources = deployedResources((await synthBastion(config)).template);
    assert.equal(
      resources.bastionhostltFD0D5EC2.Properties.LaunchTemplateData.BlockDeviceMappings[0].Ebs.VolumeType,
      'gp2',
    );
    assert.equal(resources.bastionhostinstance.Properties.BlockDeviceMappings[0].Ebs.VolumeType, 'io2');
  });

  test('uses the bootstrap_package_uri context parameter', async () => {
    const uri = 's3://sample-cluster-bucket/idea/bootstrap/bootstrap-sample.tar.gz';
    const resources = deployedResources((await synthBastion(CONFIG_FILE, { bootstrap_package_uri: uri })).template);
    const userData = resources.bastionhostinstance.Properties.UserData['Fn::Base64']['Fn::Sub'] as string;
    assert.match(userData, new RegExp(`download_bootstrap\\.sh "${uri}"`));
  });

  test('the reconstructed bootstrap package uri is the one the uploader and the deploy use', async () => {
    // With no context parameter the stack rebuilds the location, so it has to stay equal to what
    // `src/cli/bootstrap-package.ts` would have uploaded, and to the literal in the deployed
    // template. A literal placeholder here costs a false parity mismatch instead.
    const resources = deployedResources(
      (await synthBastion(CONFIG_FILE, { bootstrap_package_uri: undefined })).template,
    );
    const userData = resources.bastionhostinstance.Properties.UserData['Fn::Base64']['Fn::Sub'] as string;
    assert.match(userData, new RegExp(`download_bootstrap\\.sh "${fixtureBootstrapPackageUri()}"`));
  });
});

describe('ec2BlockDeviceName', () => {
  test('amazon linux gets /dev/xvda and everything else /dev/sda1', () => {
    assert.equal(ec2BlockDeviceName('amazonlinux2'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('amazonlinux2023'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('rhel9'), '/dev/sda1');
    assert.equal(ec2BlockDeviceName('ubuntu2204'), '/dev/sda1');
  });
});
