/**
 * The app entry point, IdeaBaseStack and the common constructs, checked against the
 * dev27 fixtures and the live dev27 templates.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored, and this whole file requires them.
 */

import assert from 'node:assert/strict';
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { Aspects, type App } from 'aws-cdk-lib';

import { buildApp, asBool, parseCdkAppArgs, readLocalContext, type StackBuilder } from '../../src/cdk/app.ts';
import { IdeaBaseStack, convertCustomTags, type IdeaBaseStackProps } from '../../src/cdk/base-stack.ts';
import { IdeaCodeAsset } from '../../src/cdk/code-asset.ts';
import {
  ExistingSocaCluster,
  lookupBackupRole,
  lookupClusterBackupVault,
  lookupClusterDns,
  lookupClusterS3Bucket,
  lookupEbsKmsKey,
  lookupEc2StateChangeTopic,
  lookupExistingOpensearch,
  lookupKeyPair,
} from '../../src/cdk/constructs/existing-resources.ts';
import {
  CustomResourceProvider,
  LambdaFunction,
  Policy,
  Role,
  SQSQueue,
} from '../../src/cdk/constructs/common.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { NODE_LAMBDA_HANDLER, NODE_RUNTIME } from '../support/node-handlers.ts';
import { ideaVersion } from '../../src/version.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const LIVE = join(PKG, 'tools', 'parity', 'live');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_CDK = join(FIXTURES, 'python', '_cdk');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

requireFixtures(
  [
    CONFIG_FILE,
    SYNTH_READS,
    CONTEXT_FILE,
    PYTHON_CDK,
    join(LIVE, 'idea-dev27-metrics.json'),
    join(LIVE, 'idea-dev27-cluster.json'),
    join(LIVE, 'idea-dev27-analytics.json'),
    join(LIVE, 'idea-dev27-cluster-manager.json'),
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

const readJson = (path: string): Record<string, any> => JSON.parse(readFileSync(path, 'utf8'));

const workdirs: string[] = [];

/** Synthesizes one stack through `app.ts` with the dev27 fixtures, in a throwaway cwd. */
async function synth(
  moduleId: string,
  moduleName: string,
  builder: StackBuilder,
  options: { nag?: boolean; configFile?: string } = {},
): Promise<{ manifest: Record<string, any>; template: Record<string, any>; app: App }> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-synth-'));
  workdirs.push(workdir);
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, `cdk.out.${moduleId}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  const previousNag = process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  if (options.nag === false) process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = 'false';
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId,
        moduleName,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile: options.configFile ?? CONFIG_FILE,
        synthReadsFile: SYNTH_READS,
      },
      { [moduleName]: async () => builder },
    );
    app.synth();
    return {
      manifest: readJson(join(outdir, 'manifest.json')),
      template: readJson(join(outdir, `${CLUSTER}-${moduleId}.template.json`)),
      app,
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
    if (previousNag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
    else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = previousNag;
  }
}

after(() => {
  for (const workdir of workdirs) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

/** A stack that does nothing but what IdeaBaseStack does, plus the cluster imports and settings. */
class MinimalStack extends IdeaBaseStack {
  constructor(props: IdeaBaseStackProps) {
    super(props);
    new ExistingSocaCluster(this.context, this.stack);
    this.updateClusterSettings({ deployment_id: this.deploymentId });
  }
}

const buildMinimal: StackBuilder = (props) => {
  new MinimalStack({
    scope: props.app,
    ctx: props.ctx,
    moduleName: props.moduleName,
    deploymentId: props.deploymentId,
    terminationProtection: props.terminationProtection,
    env: props.env,
  });
};

// --- argv, registry, tag helpers (no fixtures needed) -----------------------------------------

describe('app argv contract', () => {
  test('parses the cdk-app arguments', () => {
    const options = parseCdkAppArgs([
      '--cluster-name', CLUSTER,
      '--aws-region', REGION,
      '--module-id', 'vdc',
      '--module-name', 'virtual-desktop-controller',
      '--deployment-id', DEPLOYMENT_ID,
      '--termination-protection', 'true',
      '--aws-profile', 'sample-profile',
      '--config-file', '/tmp/settings.json',
      '--synth-reads', '/tmp/reads.json',
    ]);
    assert.deepEqual(options, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'vdc',
      moduleName: 'virtual-desktop-controller',
      deploymentId: DEPLOYMENT_ID,
      terminationProtection: true,
      awsProfile: 'sample-profile',
      configFile: '/tmp/settings.json',
      synthReadsFile: '/tmp/reads.json',
    });
  });

  test('termination protection defaults to true and accepts false', () => {
    const base = ['--cluster-name', CLUSTER, '--aws-region', REGION, '--module-id', 'metrics', '--module-name', 'metrics', '--deployment-id', DEPLOYMENT_ID];
    assert.equal(parseCdkAppArgs(base).terminationProtection, true);
    assert.equal(parseCdkAppArgs([...base, '--termination-protection', 'false']).terminationProtection, false);
    assert.equal(asBool('no', true), false);
    assert.equal(asBool(undefined, true), true);
  });

  test('a missing required argument is refused', () => {
    assert.throws(
      () => parseCdkAppArgs(['--cluster-name', CLUSTER, '--aws-region', REGION, '--module-id', 'metrics', '--deployment-id', DEPLOYMENT_ID]),
      /--module-name is required/,
    );
  });

  test('an unknown module name is a clear error, not a silent empty app', async () => {
    await assert.rejects(
      buildApp({
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: 'nope',
        moduleName: 'nope',
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
      }),
      /module not supported: 'nope'\. supported modules: /,
    );
  });
});

test('custom tags convert from Key=,Value= strings', () => {
  assert.deepEqual(convertCustomTags(['Key=Env,Value=dev', 'Key=Owner,Value=platform']), {
    Env: 'dev',
    Owner: 'platform',
  });
  // Empty keys and values are dropped.
  assert.deepEqual(convertCustomTags(['Key=,Value=dev', 'Key=A,Value=']), {});
});

test('readLocalContext merges cdk.json context with cdk.context.json', () => {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-synth-ctx-'));
  workdirs.push(workdir);
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  const context = readLocalContext(workdir);
  assert.equal(context['@aws-cdk/core:stackRelativeExports'], 'true');
  assert.ok(Object.keys(context).some((key) => key.startsWith('vpc-provider:')));
});

// --- 2.4 stack-level parity -------------------------------------------------------------------

describe('IdeaBaseStack manifest parity', () => {
  test('stack name, description, terminationProtection, tags, synthesizer qualifier', async () => {
    const { manifest, template } = await synth('metrics', 'metrics', buildMinimal);

    const artifact = manifest.artifacts[`${CLUSTER}-metrics`];
    assert.ok(artifact, 'stack artifact is named <cluster>-<moduleId>');
    assert.equal(artifact.type, 'aws:cloudformation:stack');
    // the manifest carries no stackName property, so the artifact key is the stack name: it must
    // be the only stack in the assembly and named <cluster>-<moduleId>
    assert.deepEqual(
      Object.entries(manifest.artifacts)
        .filter(([, value]) => (value as Record<string, any>).type === 'aws:cloudformation:stack')
        .map(([key]) => key),
      [`${CLUSTER}-metrics`],
    );
    assert.equal(artifact.properties.templateFile, `${CLUSTER}-metrics.template.json`);
    assert.equal(artifact.properties.terminationProtection, true);

    // tags, in the documented order: custom tags (none on dev27), then the four IDEA tags
    assert.deepEqual(Object.keys(artifact.properties.tags), [
      'idea:ClusterName',
      'idea:ModuleId',
      'idea:ModuleName',
      'idea:ModuleVersion',
    ]);
    assert.deepEqual(artifact.properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': 'metrics',
      'idea:ModuleName': 'metrics',
      'idea:ModuleVersion': ideaVersion(),
    });

    assert.equal(template.Description, `ModuleId: metrics, Cluster: ${CLUSTER}, Version: ${ideaVersion()}`);

    // synthesizer: qualifier shake256(cluster,5), bucketPrefix cdk/, cluster bucket
    assert.equal(template.Parameters.BootstrapVersion.Default, '/cdk-bootstrap/6f3b37a775/version');
    assert.equal(artifact.properties.bootstrapStackVersionSsmParameter, '/cdk-bootstrap/6f3b37a775/version');
    assert.match(artifact.properties.assumeRoleArn, /role\/cdk-6f3b37a775-deploy-role-/);
    assert.match(artifact.properties.stackTemplateAssetObjectUrl, /^s3:\/\/idea-dev27-cluster-us-east-2-\d+\/cdk\//);
  });

  test('matches the reference manifest for the same stack', async () => {
    const { manifest } = await synth('metrics', 'metrics', buildMinimal);
    const python = readJson(join(PYTHON_CDK, 'cdk.out.metrics', 'manifest.json'));
    const ours = manifest.artifacts[`${CLUSTER}-metrics`].properties;
    const theirs = python.artifacts[`${CLUSTER}-metrics`].properties;
    for (const field of [
      'terminationProtection',
      'tags',
      'requiresBootstrapStackVersion',
      'bootstrapStackVersionSsmParameter',
      'assumeRoleArn',
      'cloudFormationExecutionRoleArn',
      'templateFile',
    ]) {
      assert.deepEqual(ours[field], theirs[field], `manifest property ${field}`);
    }
    assert.equal(
      manifest.artifacts[`${CLUSTER}-metrics`].environment,
      python.artifacts[`${CLUSTER}-metrics`].environment,
    );
  });

  test('updateClusterSettings reproduces the live Custom::ClusterSettings shape', async () => {
    const { template } = await synth('metrics', 'metrics', buildMinimal);
    const live = readJson(join(LIVE, 'idea-dev27-metrics.json'));
    const logicalId = 'ideadev27metricssettings';

    const ours = template.Resources[logicalId];
    const theirs = live.Resources[logicalId];
    assert.ok(ours, `logical id ${logicalId} is reproduced`);
    assert.equal(ours.Type, 'Custom::ClusterSettings');
    assert.equal(ours.Metadata['aws:cdk:path'], theirs.Metadata['aws:cdk:path']);
    assert.equal(ours.DeletionPolicy, theirs.DeletionPolicy);
    assert.equal(ours.UpdateReplacePolicy, theirs.UpdateReplacePolicy);
    assert.deepEqual(ours.Properties, {
      ServiceToken: theirs.Properties.ServiceToken,
      cluster_name: theirs.Properties.cluster_name,
      module_id: theirs.Properties.module_id,
      version: theirs.Properties.version,
      settings: { deployment_id: theirs.Properties.settings.deployment_id },
    });
  });

  test('cdk-nag runs by default and IDEA_ADMIN_ENABLE_CDK_NAG_SCAN=false switches it off', async () => {
    const { app: withNag } = await synth('metrics', 'metrics', buildMinimal);
    const { app: withoutNag } = await synth('metrics', 'metrics', buildMinimal, { nag: false });
    // The retain policy aspect is unconditional and is always present; the security scan is the
    // one the environment variable switches.
    const names = (app: App): string[] => Aspects.of(app).all.map((aspect) => aspect.constructor.name).sort();
    assert.deepEqual(names(withNag), ['RetainStatefulOnUpdateReplace']);
    assert.deepEqual(names(withoutNag), ['RetainStatefulOnUpdateReplace']);
    // cdk-nag 3 registers as a validation plugin, which has no accessor; `buildApp` marks the app.
    const scan = (app: App): unknown[] => app.node.metadata.filter((entry) => entry.type === 'idea:cdk-nag').map((entry) => entry.data);
    assert.deepEqual(scan(withNag), ['AwsSolutions']);
    assert.deepEqual(scan(withoutNag), []);
  });
});

// --- common constructs ------------------------------------------------------------------------

/** The cluster stack's hand-rolled cluster-settings provider (`cluster_stack.py:630-675`). */
const buildClusterSettingsTrio: StackBuilder = (props) => {
  const stack = new MinimalBareStack({
    scope: props.app,
    ctx: props.ctx,
    moduleName: props.moduleName,
    deploymentId: props.deploymentId,
    terminationProtection: props.terminationProtection,
    env: props.env,
  });

  const ctx = props.ctx;
  const role = new Role(ctx, 'cluster-settings-role', stack.stack, {
    description: `Role for cluster-settings lambda function for Cluster: ${ctx.clusterName}`,
    assumedBy: ['lambda'],
  });
  const policy = new Policy(ctx, 'cluster-settings-policy', stack.stack, {
    policyTemplateName: 'custom-resource-update-cluster-settings.yml',
  });
  const fn = new LambdaFunction(ctx, 'cluster-settings', stack.stack, {
    ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_update_cluster_settings'),
    description: 'Update cluster settings during cluster module deployment',
    timeoutSeconds: 180,
    role,
  });
  role.attachInlinePolicy(policy);
  fn.node.addDependency(policy);
  fn.node.addDependency(role);

  // CustomResourceProvider's own trio, checked against the analytics stack's live one
  new CustomResourceProvider(ctx, 'opensearch-private-ips', stack.stack, {
    ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_opensearch_private_ips'),
    policyTemplateName: 'custom-resource-opensearch-private-ips.yml',
  });

  new SQSQueue(ctx, 'cluster-tasks-sqs-queue', stack.stack, {});
};

class MinimalBareStack extends IdeaBaseStack {}

describe('common constructs', () => {
  test('cluster-settings trio: live logical ids and DependsOn', async () => {
    const { template } = await synth('cluster', 'cluster', buildClusterSettingsTrio, { nag: false });
    const live = readJson(join(LIVE, 'idea-dev27-cluster.json')).Resources;

    for (const logicalId of ['clustersettingsrole7B76C95D', 'clustersettingspolicyCB373F35', 'clustersettingsBECB5478']) {
      assert.ok(template.Resources[logicalId], `logical id ${logicalId} reproduced`);
      assert.equal(
        template.Resources[logicalId].Metadata['aws:cdk:path'],
        live[logicalId].Metadata['aws:cdk:path'],
        `path for ${logicalId}`,
      );
      assert.equal(template.Resources[logicalId].Type, live[logicalId].Type);
    }

    assert.deepEqual(
      template.Resources.clustersettingsBECB5478.DependsOn,
      live.clustersettingsBECB5478.DependsOn,
    );

    const role = template.Resources.clustersettingsrole7B76C95D.Properties;
    const liveRole = live.clustersettingsrole7B76C95D.Properties;
    assert.equal(role.RoleName, liveRole.RoleName);
    assert.equal(role.Description, liveRole.Description);
    assert.deepEqual(role.AssumeRolePolicyDocument, liveRole.AssumeRolePolicyDocument);
    assert.deepEqual(role.Tags, liveRole.Tags);

    const fn = template.Resources.clustersettingsBECB5478.Properties;
    const liveFn = live.clustersettingsBECB5478.Properties;
    for (const field of ['FunctionName', 'MemorySize', 'Timeout', 'Description', 'Tags']) {
      assert.deepEqual(fn[field], liveFn[field], `lambda property ${field}`);
    }
    // The handler is the TypeScript port, which is an in-place code update: the function name is
    // unchanged above, and the runtime move is itemised in tools/parity/intended-drift.ts.
    assert.equal(fn.Handler, NODE_LAMBDA_HANDLER);
    assert.equal(fn.Runtime, NODE_RUNTIME);
    assert.equal(liveFn.Handler, 'idea_custom_resource_update_cluster_settings.handler.handler');
  });

  test('CustomResourceProvider reproduces the policy/role/lambda trio ids and DependsOn', async () => {
    const { template } = await synth('cluster', 'cluster', buildClusterSettingsTrio, { nag: false });
    const liveAnalytics = readJson(join(LIVE, 'idea-dev27-analytics.json')).Resources;

    const ids = {
      policy: 'opensearchprivateipslambdapolicy2C1A5D98',
      role: 'opensearchprivateipsrole8D8E26D8',
      lambda: 'opensearchprivateipslambda3D078D54',
    };
    for (const logicalId of Object.values(ids)) {
      assert.ok(template.Resources[logicalId], `logical id ${logicalId} reproduced`);
    }
    assert.equal(
      template.Resources[ids.lambda].Metadata['aws:cdk:path'],
      `${CLUSTER}-cluster/opensearch-private-ips-lambda/Resource`,
    );
    assert.deepEqual(template.Resources[ids.lambda].DependsOn, liveAnalytics[ids.lambda].DependsOn);
    assert.deepEqual(template.Resources[ids.lambda].DependsOn, [ids.policy, ids.role]);
    assert.equal(
      template.Resources[ids.role].Properties.Description,
      'Role for Custom::OpensearchPrivateIps for Cluster: idea-dev27',
    );
  });

  test('SQSQueue emits the AlwaysEncrypted policy at <id>/Policy/Resource', async () => {
    const { template } = await synth('cluster', 'cluster', buildClusterSettingsTrio, { nag: false });
    const policies = Object.entries(template.Resources).filter(
      ([, resource]) => (resource as Record<string, any>).Type === 'AWS::SQS::QueuePolicy',
    );
    assert.equal(policies.length, 1);
    const [, queuePolicy] = policies[0] as [string, Record<string, any>];
    assert.equal(
      queuePolicy.Metadata['aws:cdk:path'],
      `${CLUSTER}-cluster/cluster-tasks-sqs-queue/Policy/Resource`,
    );
    const statement = queuePolicy.Properties.PolicyDocument.Statement[0];
    assert.equal(statement.Sid, 'AlwaysEncrypted');
    assert.equal(statement.Effect, 'Deny');
    assert.deepEqual(statement.Action, 'sqs:*');
    assert.deepEqual(statement.Condition, { Bool: { 'aws:SecureTransport': 'false' } });
    assert.deepEqual(statement.Principal, { AWS: '*' });

    const queue = Object.values(template.Resources).find(
      (resource) => (resource as Record<string, any>).Type === 'AWS::SQS::Queue',
    ) as Record<string, any>;
    assert.equal(queue.Properties.KmsMasterKeyId, 'alias/aws/sqs');
  });
});

// --- base stack members -----------------------------------------------------------------------

describe('lookupUserPool / buildAccessControlGroups / getEc2InstanceManagedPolicies', () => {
  test('the cluster-manager groups and managed policies equal the live ones', async () => {
    let userPoolId: string | undefined;
    let userPoolNodeId: string | undefined;
    let managedPolicies: string[] | undefined;
    const builder: StackBuilder = (props) => {
      const stack = new MinimalBareStack({
        scope: props.app,
        ctx: props.ctx,
        moduleName: props.moduleName,
        deploymentId: props.deploymentId,
        terminationProtection: props.terminationProtection,
        env: props.env,
      });
      const userPool = stack.lookupUserPool();
      userPoolId = userPool.userPoolId;
      userPoolNodeId = userPool.node.id;
      stack.buildAccessControlGroups(userPool);
      managedPolicies = stack.getEc2InstanceManagedPolicies();
    };

    const { template } = await synth('cluster-manager', 'cluster-manager', builder, { nag: false });
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    const live = withRetainedStateful(readJson(join(LIVE, 'idea-dev27-cluster-manager.json'))).Resources;

    // the user pool is imported, not created: the two groups are the only resources
    assert.equal(userPoolNodeId, `${CLUSTER}-user-pool`);
    assert.deepEqual(Object.keys(template.Resources).sort(), [
      'clustermanageradministratorsgroup',
      'clustermanagerusersgroup',
    ]);
    for (const logicalId of ['clustermanageradministratorsgroup', 'clustermanagerusersgroup']) {
      assert.deepEqual(template.Resources[logicalId], live[logicalId], `resource ${logicalId}`);
    }
    assert.equal(template.Resources.clustermanageradministratorsgroup.Properties.UserPoolId, userPoolId);

    // Policy order: SSM core, CloudWatch agent, optional Prometheus, then the config list.
    assert.deepEqual(managedPolicies, live.clustermanagerrole4D8ECACE.Properties.ManagedPolicyArns);
  });
});

// --- existing resources -----------------------------------------------------------------------

describe('ExistingSocaCluster', () => {
  test('imports the vpc, roles and security groups under the documented ids', async () => {
    let imported: ExistingSocaCluster | undefined;
    const builder: StackBuilder = (props) => {
      const stack = new MinimalBareStack({
        scope: props.app,
        ctx: props.ctx,
        moduleName: props.moduleName,
        deploymentId: props.deploymentId,
        terminationProtection: props.terminationProtection,
        env: props.env,
      });
      imported = new ExistingSocaCluster(props.ctx, stack.stack);
    };
    const { template } = await synth('metrics', 'metrics', builder);

    // imported constructs create nothing
    assert.deepEqual(Object.keys(template.Resources ?? {}), []);

    assert.ok(imported);
    assert.deepEqual(Object.keys(imported.roles), ['log-retention']);
    assert.ok(imported.getSecurityGroup('cluster'), 'cluster security group imported');
    assert.ok(imported.getSecurityGroup('external-load-balancer'));
    assert.equal(imported.existingVpc.vpcId, imported.vpc.vpcId);

    // subnets come back in the order of the config lists, not the lookup's
    assert.deepEqual(
      imported.privateSubnets.map((subnet) => subnet.subnetId),
      imported.existingVpc.getPrivateSubnetIds(),
    );
    assert.deepEqual(
      imported.publicSubnets.map((subnet) => subnet.subnetId),
      imported.existingVpc.getPublicSubnetIds(),
    );
  });
});

// --- per-stack imports --------------------------------------------------------------------------

/** The dev27 settings plus the rows dev27 does not carry, so every lookup can be exercised. */
function configFileWith(extra: Record<string, string>): string {
  const scan = readJson(CONFIG_FILE) as { Items: Array<Record<string, any>> };
  for (const [key, value] of Object.entries(extra)) {
    scan.Items.push({ key: { S: key }, value: { S: value }, version: { N: '1' } });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-synth-cfg-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

describe('per-stack imports keep their pinned construct ids', () => {
  test('every lookup imports under its documented id and creates no resource', async () => {
    const configFile = configFileWith({
      'cluster.route53.private_hosted_zone_id': 'Z0123456789ABCDEFGHIJ',
      'cluster.ebs.kms_key_id': '11111111-2222-3333-4444-555555555555',
      'cluster.ec2.state_change_notifications_sns_topic_arn':
        'arn:aws:sns:us-east-2:123456789012:idea-test1-cluster-ec2-state-change',
      'cluster.backups.role_arn': 'arn:aws:iam::123456789012:role/idea-test1-backup-role',
      'cluster.backups.backup_vault.arn':
        'arn:aws:backup:us-east-2:123456789012:backup-vault:idea-test1-cluster-backup-vault',
      'analytics.opensearch.domain_vpc_endpoint_url': 'vpc-analytics.us-east-2.es.example.invalid',
    });

    const ids: string[] = [];
    const builder: StackBuilder = (props) => {
      const stack = new MinimalBareStack({
        scope: props.app,
        ctx: props.ctx,
        moduleName: props.moduleName,
        deploymentId: props.deploymentId,
        terminationProtection: props.terminationProtection,
        env: props.env,
      });
      const scope = stack.stack;
      const ctx = props.ctx;
      ids.push(
        lookupClusterDns(ctx, scope).node.id,
        lookupEbsKmsKey(ctx, scope).node.id,
        lookupEbsKmsKey(ctx, scope, 'dcv-host').node.id,
        lookupKeyPair(ctx, scope).node.id,
        lookupKeyPair(ctx, scope, 'dcv-host-key-pair').node.id,
        lookupClusterS3Bucket(ctx, scope).node.id,
        lookupExistingOpensearch(ctx, scope).node.id,
        lookupEc2StateChangeTopic(ctx, scope).node.id,
        lookupBackupRole(ctx, scope).node.id,
        lookupClusterBackupVault(ctx, scope).node.id,
      );
    };

    const { template } = await synth('vdc', 'virtual-desktop-controller', builder, {
      nag: false,
      configFile,
    });

    assert.deepEqual(ids, [
      'cluster-dns',
      'ebs-kms-key',
      'dcv-host-ebs-kms-key',
      'vdc-key-pair',
      'dcv-host-key-pair',
      'cluster-s3-bucket',
      'existing-opensearch',
      `${CLUSTER}-vdc-ec2-state-change-topic`,
      'backup-role',
      'cluster-backup-vault',
    ]);
    assert.deepEqual(Object.keys(template.Resources ?? {}), []);
  });

  test('without cluster.ebs.kms_key_id the alias is imported as ebs-kms-key-default', async () => {
    let id: string | undefined;
    const builder: StackBuilder = (props) => {
      const stack = new MinimalBareStack({
        scope: props.app,
        ctx: props.ctx,
        moduleName: props.moduleName,
        deploymentId: props.deploymentId,
        terminationProtection: props.terminationProtection,
        env: props.env,
      });
      id = lookupEbsKmsKey(props.ctx, stack.stack).node.id;
    };
    await synth('metrics', 'metrics', builder, { nag: false });
    assert.equal(id, 'ebs-kms-key-default');
  });
});

// --- code asset -------------------------------------------------------------------------------

describe('IdeaCodeAsset', () => {
  test('handler name and asset path resolution', () => {
    const asset = new IdeaCodeAsset('idea_custom_resource_update_cluster_settings');
    assert.equal(asset.lambdaHandler, NODE_LAMBDA_HANDLER);
    assert.equal(asset.runtime.name, NODE_RUNTIME);
    const path = asset.assetPath();
    assert.ok(existsSync(path));
    // The asset root is zipped by `lambda.Code.fromAsset`, and the runtime imports `index`, so
    // the one bundled module sits at the root of it.
    assert.ok(existsSync(join(path, 'index.mjs')), `asset root ${path} has no index.mjs`);
  });

  test('an unknown package fails loudly', () => {
    assert.throws(() => new IdeaCodeAsset('idea_not_a_package').assetPath(), /lambda package not found/);
  });
});

// --- asynchronous stack builders -------------------------------------------------------------

describe('buildApp and asynchronous builders', () => {
  test('awaits a builder that reads before it builds', async () => {
    // Every SynthReads method is async. Unawaited, app.synth() runs first and writes an assembly
    // with no stack in it, so this test fails at the missing template rather than on an assertion.
    const builder: StackBuilder = async (props) => {
      await props.ctx.synthReads.callerIdentity();
      await new Promise((resolve) => setTimeout(resolve, 0));
      buildMinimal(props);
    };
    const { manifest, template } = await synth('metrics', 'metrics', builder, { nag: false });
    assert.ok(manifest.artifacts[`${CLUSTER}-metrics`], 'the stack is missing from the assembly');
    assert.ok(
      Object.values(template.Resources as Record<string, any>).some(
        (resource) => resource.Type === 'Custom::ClusterSettings',
      ),
    );
  });
});
