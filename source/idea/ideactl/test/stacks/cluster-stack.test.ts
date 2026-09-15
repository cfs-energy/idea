/**
 * The cluster stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The template itself is compared resource for resource by `tools/parity/synth.ts`; what is
 * asserted here is what a template diff cannot see:
 *
 * - the manifest properties (stack name, termination protection, stack tags, the synthesizer's
 *   bootstrap parameter and asset bucket), none of which appear in the template but all of which
 *   change what CloudFormation does with it;
 * - `DeletionPolicy` / `UpdateReplacePolicy` on every resource that carries one, and their absence
 *   on every resource that does not;
 * - the external HTTPS listener's default action, which comes from a synth-time
 *   `elbv2:DescribeListeners`: synthesizing the fixed-response instead would reset the web portal's
 *   target group on the next deploy.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; every test that needs them skips
 * when they are absent so the suite still runs without them.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, describe, test } from 'node:test';

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/cluster.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedCertificates } from '../support/retained-certificates.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { withRetirements } from '../support/retirements.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.cluster', 'manifest.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-cluster.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
const STACK = `${CLUSTER}-cluster`;

requireFixtures(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, PYTHON_MANIFEST, LIVE_TEMPLATE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/** Synthesizes the cluster stack and returns the whole cloud assembly directory's contents. */
async function synthCluster(options: {
  awsRegion?: string;
  configFile?: string;
  context?: Json;
} = {}): Promise<{ template: Json; manifest: Json; assets: Json }> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-cluster-'));
  workdirs.push(workdir);
  const contextPath = join(workdir, 'cdk.context.json');
  cpSync(CONTEXT_FILE, contextPath);
  if (options.context !== undefined) {
    const existing = readJson(contextPath);
    writeFileSync(contextPath, JSON.stringify({ ...existing, ...options.context }));
  }
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, 'cdk.out.cluster');

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: options.awsRegion ?? REGION,
        moduleId: 'cluster',
        moduleName: 'cluster',
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile: options.configFile ?? CONFIG_FILE,
        synthReadsFile: SYNTH_READS,
      },
      { cluster: async () => buildStack },
    );
    app.synth();
    const assetsFile = readdirSync(outdir).find((name) => name.endsWith('.assets.json')) as string;
    return {
      template: readJson(join(outdir, `${STACK}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
      assets: readJson(join(outdir, assetsFile)),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

function fixtureAccount(): string {
  const reads = readJson(SYNTH_READS);
  return (reads['sts:GetCallerIdentity:{}'] as { account: string }).account;
}

function configWith(overrides: Record<string, Json>): string {
  const scan = readJson(CONFIG_FILE);
  const items = [...((scan.Items as Json[]) ?? [])];
  for (const [key, value] of Object.entries(overrides)) {
    const index = items.findIndex((item) => (item.key as Json | undefined)?.S === key);
    const row = { key: { S: key }, value };
    if (index >= 0) items[index] = { ...items[index], ...row };
    else items.push(row);
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-cluster-config-'));
  workdirs.push(workdir);
  const path = join(workdir, 'cluster-settings.json');
  writeFileSync(path, JSON.stringify({ Items: items }));
  return path;
}

describe('cluster stack', () => {
  let synthesized: { template: Json; manifest: Json; assets: Json };
  let live: Json;

  before(async () => {
    synthesized = await synthCluster();
    live = readJson(LIVE_TEMPLATE);
  });

  test('the manifest matches the one the python administrator wrote', () => {
    const properties = synthesized.manifest.artifacts[STACK].properties as Json;
    const expected = readJson(PYTHON_MANIFEST).artifacts[STACK].properties as Json;

    assert.equal(synthesized.manifest.artifacts[STACK].environment, readJson(PYTHON_MANIFEST).artifacts[STACK].environment);
    assert.equal(properties.templateFile, `${STACK}.template.json`);
    assert.equal(properties.terminationProtection, true);
    assert.deepEqual(properties.tags, expected.tags);
    assert.deepEqual(properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': 'cluster',
      'idea:ModuleName': 'cluster',
      'idea:ModuleVersion': expected.tags['idea:ModuleVersion'],
    });
    // the synthesizer: qualifier shake_256(cluster_name)[:5], deploy roles, bootstrap version
    assert.equal(properties.bootstrapStackVersionSsmParameter, expected.bootstrapStackVersionSsmParameter);
    assert.match(properties.bootstrapStackVersionSsmParameter, /^\/cdk-bootstrap\/6f3b37a775\/version$/);
    assert.equal(properties.requiresBootstrapStackVersion, expected.requiresBootstrapStackVersion);
    assert.equal(properties.assumeRoleArn, expected.assumeRoleArn);
    assert.equal(properties.cloudFormationExecutionRoleArn, expected.cloudFormationExecutionRoleArn);
    assert.deepEqual(properties.lookupRole, expected.lookupRole);
    assert.equal(properties.validateOnSynth, expected.validateOnSynth);
  });

  test('lambda assets go to the cluster bucket under cdk/', () => {
    const destinations = Object.values(synthesized.assets.files as Json).flatMap((file: Json) =>
      Object.values(file.destinations as Json),
    ) as Json[];
    assert.ok(destinations.length >= 6, `expected the five lambda assets and the template, got ${destinations.length}`);
    const bucket = readJson(PYTHON_MANIFEST)
      .artifacts[STACK].properties.stackTemplateAssetObjectUrl.replace('s3://', '')
      .split('/')[0] as string;
    for (const destination of destinations) {
      assert.equal(destination.bucketName, bucket);
      assert.match(destination.objectKey as string, /^cdk\//);
    }
  });

  test('deletion policies match the deployed stack, resource for resource', () => {
    const synthResources = synthesized.template.Resources as Json;
    // The deployed side carries this branch's retain policy on its stateful resources, which is
    // the one change to these attributes and is itemised in tools/parity/intended-drift.ts.
    const liveResources = withRetainedCertificates(withRetirements('cluster', withRetainedStateful(live)))
      .Resources as Json;
    const policies = (resources: Json): Json =>
      Object.fromEntries(
        Object.entries(resources)
          .filter(([, resource]) => (resource as Json).Type !== 'AWS::CDK::Metadata')
          .map(([id, resource]) => [
            id,
            {
              DeletionPolicy: (resource as Json).DeletionPolicy ?? null,
              UpdateReplacePolicy: (resource as Json).UpdateReplacePolicy ?? null,
            },
          ]),
      );
    assert.deepEqual(policies(synthResources), policies(liveResources));

    // and by name, so the assertion above cannot pass on an empty set. The settings resource holds
    // no state of its own and still deletes in both directions; the two certificate resources name
    // secrets a load balancer is serving, so both directions retain.
    assert.equal(synthResources.ideadev27clustersettings.DeletionPolicy, 'Delete');
    assert.equal(synthResources.ideadev27clustersettings.UpdateReplacePolicy, 'Delete');
    for (const id of ['ideadev27clusterexternalcert', 'ideadev27clusterinternalcert']) {
      assert.equal(synthResources[id].DeletionPolicy, 'Retain', id);
      assert.equal(synthResources[id].UpdateReplacePolicy, 'Retain', id);
    }
    // The log groups keep the configured teardown behaviour and are never lost to a replacement.
    for (const id of ['vpcflowlogsgroup4676BF4E', 'ideadev27externalalbwafloggroup46833A61']) {
      assert.equal(synthResources[id].DeletionPolicy, 'Delete', id);
      assert.equal(synthResources[id].UpdateReplacePolicy, 'Retain', id);
    }
    // The private hosted zone holds every record in it, so it is retained on a replacement while
    // its teardown behaviour is unchanged.
    assert.equal(synthResources.ideadev27privatehostedzone741B171D.DeletionPolicy, undefined);
    assert.equal(synthResources.ideadev27privatehostedzone741B171D.UpdateReplacePolicy, 'Retain');
    // The network holds nothing, so nothing is added to it.
    for (const id of ['vpcA2121C38', 'vpcpublicSubnet1EIP909BE2D3', 'clusterprefixlist']) {
      assert.equal(synthResources[id].DeletionPolicy, undefined, id);
      assert.equal(synthResources[id].UpdateReplacePolicy, undefined, id);
    }
  });

  test('the external https listener keeps the live default action', () => {
    const listener = (synthesized.template.Resources as Json).ideadev27externalalbhttpslistener792DEC0E;
    const action = listener.Properties.DefaultActions[0] as Json;
    assert.equal(action.Type, 'forward', 'a fixed-response here takes the web portal offline');
    const targetGroupArn = action.ForwardConfig.TargetGroups[0].TargetGroupArn as string;
    assert.match(targetGroupArn, /^arn:aws:elasticloadbalancing:/);
    // the same target group the deployed listener points at, read back at synth
    const liveListener = (live.Resources as Json).ideadev27externalalbhttpslistener792DEC0E;
    assert.equal(targetGroupArn, liveListener.Properties.DefaultActions[0].ForwardConfig.TargetGroups[0].TargetGroupArn);
  });

  test('the dcv broker listeners stay on the fixed response', () => {
    // they read `cluster.external_alb.dcv_broker_*_listener_arn`, which nothing writes; the vdc
    // stack sets the real default action out of band and CloudFormation never reverts it
    for (const id of [
      'ideadev27internalalbdcvbrokerclientlistener7E8DB2BE',
      'ideadev27internalalbdcvbrokeragentlistener7092853F',
      'ideadev27internalalbdcvbrokergatewaylistener24355DB3',
    ]) {
      const action = (synthesized.template.Resources as Json)[id].Properties.DefaultActions[0] as Json;
      assert.equal(action.Type, 'fixed-response', id);
      assert.equal(action.FixedResponseConfig.MessageBody, '{"success":true,"message":"OK"}');
    }
  });

  test('the listener read goes through the app SynthReads, not argv or the environment', async () => {
    // The read must come from the app's SynthReads, so a file named on the command line is a
    // decoy and must not change the template.
    const previousArgv = process.argv;
    const previousReads = process.env.IDEA_SYNTH_READS;
    process.argv = [...previousArgv, '--synth-reads', '/nonexistent-synth-reads.json'];
    process.env.IDEA_SYNTH_READS = '/nonexistent-synth-reads.json';
    try {
      const again = await synthCluster();
      const listener = (again.template.Resources as Json).ideadev27externalalbhttpslistener792DEC0E;
      assert.equal(listener.Properties.DefaultActions[0].Type, 'forward');
    } finally {
      process.argv = previousArgv;
      if (previousReads === undefined) delete process.env.IDEA_SYNTH_READS;
      else process.env.IDEA_SYNTH_READS = previousReads;
    }
  });

  test('a replay that does not carry the listener fails the build', async () => {
    const reads = readJson(SYNTH_READS);
    for (const key of Object.keys(reads)) if (key.startsWith('elbv2:DescribeListeners:')) delete reads[key];
    const workdir = mkdtempSync(join(tmpdir(), 'ideactl-cluster-miss-'));
    workdirs.push(workdir);
    const file = join(workdir, 'synth-reads.json');
    writeFileSync(file, JSON.stringify(reads));
    // No CDK_OUTDIR: the app must not auto-synth at exit into a deleted directory.
    await assert.rejects(
      () =>
        buildApp(
          {
            clusterName: CLUSTER,
            awsRegion: REGION,
            moduleId: 'cluster',
            moduleName: 'cluster',
            deploymentId: DEPLOYMENT_ID,
            terminationProtection: true,
            configFile: CONFIG_FILE,
            synthReadsFile: file,
          },
          { cluster: async () => buildStack },
        ),
      /SynthReadMiss: elbv2:DescribeListeners:/,
    );
  });

  test('the settings map is written in the deployed order', () => {
    const settings = (synthesized.template.Resources as Json).ideadev27clustersettings.Properties.settings as Json;
    const liveSettings = (live.Resources as Json).ideadev27clustersettings.Properties.settings as Json;
    assert.deepEqual(Object.keys(settings), Object.keys(liveSettings));
    assert.equal(settings.deployment_id, DEPLOYMENT_ID);
  });
});

test('use_existing_vpc imports the named VPC and does not emit AWS::EC2::VPC', async () => {
  const account = fixtureAccount();
  const configFile = configWith({
    'cluster.network.use_existing_vpc': { BOOL: true },
    'cluster.network.vpc_id': { S: 'vpc-0123456789abcdef0' },
    'cluster.network.public_subnets': { L: [{ S: 'subnet-aaaaaaaaaaaaaaaaa' }] },
    'cluster.network.private_subnets': { L: [{ S: 'subnet-bbbbbbbbbbbbbbbbb' }] },
  });
  const { template } = await synthCluster({
    configFile,
    context: {
      [`vpc-provider:account=${account}:filter.vpc-id=vpc-0123456789abcdef0:region=${REGION}:returnAsymmetricSubnets=true`]: {
        vpcId: 'vpc-0123456789abcdef0',
        vpcCidrBlock: '192.0.2.0/24',
        ownerAccountId: account,
        availabilityZones: [],
        subnetGroups: [
          {
            name: 'Public',
            type: 'Public',
            subnets: [{ subnetId: 'subnet-aaaaaaaaaaaaaaaaa', cidr: '192.0.2.0/24', availabilityZone: 'us-east-2a', routeTableId: 'rtb-aaaaaaaaaaaaaaaaa' }],
          },
          {
            name: 'Private',
            type: 'Private',
            subnets: [{ subnetId: 'subnet-bbbbbbbbbbbbbbbbb', cidr: '198.51.100.0/24', availabilityZone: 'us-east-2b', routeTableId: 'rtb-bbbbbbbbbbbbbbbbb' }],
          },
        ],
      },
    },
  });
  const types = Object.values(template.Resources as Json).map((resource) => (resource as Json).Type);
  assert.ok(!types.includes('AWS::EC2::VPC'));
});

test('us-gov-west-1 writes a CNAME for the internal ALB instead of a cross-zone alias', async () => {
  const { template } = await synthCluster({ awsRegion: 'us-gov-west-1' });
  const record = Object.values(template.Resources as Json).find(
    (resource) => (resource as Json).Type === 'AWS::Route53::RecordSet'
      && String((resource as Json).Properties?.Name ?? '').startsWith('internal-alb.'),
  ) as Json | undefined;
  assert.ok(record !== undefined);
  assert.equal((record.Properties as Json).Type, 'CNAME');
});
