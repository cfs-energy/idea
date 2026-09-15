/**
 * The cluster-manager stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The parity harness compares the template body, so these tests take the parts a template diff
 * cannot see: the manifest the CDK CLI deploys from (stack name, tags, termination protection,
 * synthesizer roles and asset bucket) and the removal policies, which live outside `Properties`
 * and decide whether an upgrade keeps or destroys a stateful resource.
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

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/cluster-manager.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { withRetirements } from '../support/retirements.ts';
import { NODE_NAG_SUPPRESSION, withNodeHandlers } from '../support/node-handlers.ts';
import { cacheSetup } from '../support/setup-cache.ts';
import { ECS_SHARED_CAPACITY_VALUES, TARGET_GROUP_HASH } from '../support/ecs-harness.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.cluster-manager', 'manifest.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-cluster-manager.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const MODULE = 'cluster-manager';
const STACK = `${CLUSTER}-${MODULE}`;
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

requireFixtures(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

/** `[rule id, reason]` per suppression, for every resource that carries one. */
function nagSuppressions(template: Json): Record<string, [string, string][]> {
  const carried = Object.entries(template.Resources as Json).flatMap(([id, resource]) => {
    const rules = (resource as Json).Metadata?.cdk_nag?.rules_to_suppress as Json[] | undefined;
    return rules === undefined ? [] : [[id, rules.map((rule) => [rule.id, rule.reason])] as const];
  });
  return Object.fromEntries(carried) as Record<string, [string, string][]>;
}

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/** Copies the dev27 settings scan, setting each named value (adding it if absent). */
function configWith(overrides: Record<string, string | number | boolean | string[]>): string {
  const attribute = (value: string | number | boolean | string[]): Json => {
    if (Array.isArray(value)) return { L: value.map((entry) => ({ S: entry })) };
    if (typeof value === "number") return { N: String(value) };
    if (typeof value === "boolean") return { BOOL: value };
    return { S: value };
  };
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    const value = key === undefined ? undefined : overrides[key];
    if (key !== undefined && value !== undefined && remaining.delete(key)) item.value = attribute(value);
  }
  for (const key of remaining) {
    const value = overrides[key];
    if (value === undefined) throw new Error(`Missing override for ${key}`);
    (scan.Items as Json[]).push({ key: { S: key }, value: attribute(value), version: { N: '1' } });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-cm-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface Synthesized {
  template: Json;
  manifest: Json;
  assets: Json;
}

/**
 * `nag: true` leaves the cdk-nag aspect on. Deploys run with it off (idea-admin.sh defaults
 * `IDEA_ADMIN_ENABLE_CDK_NAG_SCAN` to false), which is what makes the unsuppressed
 * `AwsSolutions-SMG4` finding on the JWT signing secret survivable.
 */
async function synthClusterManagerUncached(
  configFile: string = CONFIG_FILE,
  options: { nag?: boolean } = {},
): Promise<Synthesized> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-cm-'));
  workdirs.push(workdir);
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, `cdk.out.${MODULE}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  const previousNag = process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  if (options.nag !== true) process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = 'false';
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
      template: readJson(join(outdir, `${STACK}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
      assets: readJson(join(outdir, `${STACK}.assets.json`)),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
    if (previousNag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
    else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = previousNag;
  }
}

const synthDefaultClusterManager = cacheSetup(async () => synthClusterManagerUncached());

/**
 * Reuses the immutable default-fixture synthesis and clones its plain JSON
 * result. Configuration branches and security scans always synthesize fresh.
 */
async function synthClusterManager(
  configFile: string = CONFIG_FILE,
  options: { nag?: boolean } = {},
): Promise<Synthesized> {
  if (configFile === CONFIG_FILE && options.nag !== true) {
    return synthDefaultClusterManager();
  }
  return synthClusterManagerUncached(configFile, options);
}

/** The `DeletionPolicy` / `UpdateReplacePolicy` pair, `null` where the attribute is absent. */
function removalPolicies(template: Json): Record<string, [unknown, unknown]> {
  const out: Record<string, [unknown, unknown]> = {};
  for (const [id, resource] of Object.entries(template.Resources as Json)) {
    if ((resource as Json).Type === 'AWS::CDK::Metadata') continue;
    out[id] = [(resource as Json).DeletionPolicy ?? null, (resource as Json).UpdateReplacePolicy ?? null];
  }
  return out;
}

describe('cluster-manager stack manifest', () => {
  test('carries the stack name, tags and termination protection the deploy reads', async () => {
    const { manifest } = await synthClusterManager();
    const artifact = manifest.artifacts[STACK];

    assert.equal(artifact.type, 'aws:cloudformation:stack');
    assert.equal(artifact.displayName, STACK);
    assert.equal(artifact.properties.templateFile, `${STACK}.template.json`);
    // on by default: a port that drops it makes production stacks deletable
    assert.equal(artifact.properties.terminationProtection, true);
    assert.deepEqual(artifact.properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': MODULE,
      'idea:ModuleName': MODULE,
      'idea:ModuleVersion': artifact.properties.tags['idea:ModuleVersion'],
    });
    assert.match(artifact.properties.tags['idea:ModuleVersion'], /^\d+\.\d+\.\d+/);
    assert.equal(artifact.dependencies?.[0], `${STACK}.assets`);
  });

  test('synthesizes against the cluster qualifier, bucket and cdk/ prefix', async () => {
    const { manifest, assets, template } = await synthClusterManager();
    const artifact = manifest.artifacts[STACK];

    // shake_256('idea-dev27').hexdigest(5): the qualifier every bootstrap role name embeds
    const qualifier = '6f3b37a775';
    assert.equal(artifact.properties.bootstrapStackVersionSsmParameter, `/cdk-bootstrap/${qualifier}/version`);
    assert.equal(artifact.properties.requiresBootstrapStackVersion, 6);
    assert.match(artifact.properties.assumeRoleArn, new RegExp(`role/cdk-${qualifier}-deploy-role-`));
    assert.match(
      artifact.properties.cloudFormationExecutionRoleArn,
      new RegExp(`role/cdk-${qualifier}-cfn-exec-role-`),
    );
    assert.match(artifact.properties.lookupRole.arn, new RegExp(`role/cdk-${qualifier}-lookup-role-`));
    assert.equal(template.Parameters.BootstrapVersion.Default, `/cdk-bootstrap/${qualifier}/version`);
    assert.ok(template.Rules.CheckBootstrapVersion !== undefined);

    // assets go to the cluster bucket under cdk/, not the bootstrap staging bucket
    const bucket = `${CLUSTER}-cluster-${REGION}-`;
    assert.ok(artifact.properties.stackTemplateAssetObjectUrl.startsWith(`s3://${bucket}`));
    assert.ok(artifact.properties.stackTemplateAssetObjectUrl.includes('/cdk/'));
    const destinations = Object.values(assets.files as Json).flatMap((file: Json) =>
      Object.values(file.destinations as Json),
    );
    assert.ok(destinations.length >= 3, 'template plus the two lambda packages');
    for (const destination of destinations as Json[]) {
      assert.ok(destination.bucketName.startsWith(bucket));
      assert.ok(destination.objectKey.startsWith('cdk/'));
    }
  });

  test('matches the manifest the Python administrator wrote', async () => {
    const { manifest } = await synthClusterManager();
    const mine = manifest.artifacts[STACK];
    const python = readJson(PYTHON_MANIFEST).artifacts[STACK];

    assert.equal(mine.environment, python.environment);
    assert.equal(mine.displayName, python.displayName);
    assert.deepEqual(mine.dependencies, python.dependencies);
    for (const property of [
      'templateFile',
      'terminationProtection',
      'tags',
      'validateOnSynth',
      'assumeRoleArn',
      'cloudFormationExecutionRoleArn',
      'requiresBootstrapStackVersion',
      'bootstrapStackVersionSsmParameter',
      'additionalDependencies',
      'lookupRole',
    ]) {
      assert.deepEqual(mine.properties[property], python.properties[property], property);
    }
  });

  test('exports nothing and imports nothing', async () => {
    const { template } = await synthClusterManager();
    assert.equal(template.Outputs, undefined);
    assert.ok(!JSON.stringify(template).includes('Fn::ImportValue'));
  });
});

describe('cluster-manager removal policies', () => {
  test('every resource keeps the deployed DeletionPolicy and UpdateReplacePolicy', async () => {
    const { template } = await synthClusterManager();
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    assert.deepEqual(
      removalPolicies(template),
      removalPolicies(withRetirements('cluster-manager', withRetainedStateful(readJson(LIVE_TEMPLATE)))),
    );
  });

  test('the stateful resources have the policies the upgrade path depends on', async () => {
    const { template } = await synthClusterManager();
    const policies = removalPolicies(template);

    // explicit applyRemovalPolicy(DESTROY) on the OAuth pair, CDK's default on the queues and
    // every custom resource
    // Custom resources hold nothing of their own and still delete in both directions.
    for (const id of [
      'projectroleboundaries',
      'bedrockinvocationloggroup',
      'webportalendpoint',
      'externalendpoint',
      'internalendpoint',
      'ideadev27clustermanagersettings',
    ]) {
      assert.deepEqual(policies[id], ['Delete', 'Delete'], id);
    }

    // The secrets and the queues keep the teardown behaviour the upgrade path depends on and are
    // never lost to a replacement. A replaced secret gets a new name, so the retained one does not
    // hold the name the replacement needs.
    for (const id of [
      'clustermanagerclientid',
      'clustermanagerclientsecret',
      'clustertaskssqsqueue319A7909',
      'clustertaskssqsqueuedlq6F21DEB2',
      'notificationssqsqueue60B47557',
      'notificationssqsqueuedlqDE01C0A5',
    ]) {
      assert.deepEqual(policies[id], ['Delete', 'Retain'], id);
    }

    // no removal policy at all: adding Retain here changes the template and can strand the
    // resource on the next delete. The JWT secret is the trap - its name is reserved for the
    // recovery window, so a Retain then recreate cycle cannot redeploy.
    for (const id of [
      'clustermanagerrole4D8ECACE',
      'clustermanagerpolicyEF9BA73D',
      'projectroleboundaryD13C6967',
      'bedrockinvocationlogging054720CC',
      'clustermanagersecuritygroupDD4A1A52',
      'clustermanagerltProfileFDAFAF8F',
      'clustermanagerlt5189880B',
      'clustermanagerasgASGBD35240A',
      'webportaltargetgroup',
      'clustermanagerexternaltargetgroup',
      'clustermanagerinternaltargetgroup',
    ]) {
      assert.deepEqual(policies[id], [null, null], id);
    }

    // Stateful, and still no DeletionPolicy: the JWT secret's name is reserved for the recovery
    // window, so a Retain then recreate cycle could not redeploy. Only the update-replace side
    // moves, and a replacement of a secret is forced by its name changing, so the retained one
    // never holds the name the replacement wants.
    for (const id of [
      'clustermanagerjwtsigningsecret',
      'ideadev27userpoolresourceserver7B2B7736',
      'ideadev27userpoolclustermanagerclientD645207F',
      'clustermanageradministratorsgroup',
      'clustermanagerusersgroup',
      'clustertaskssqsqueuePolicy751D36E7',
      'notificationssqsqueuePolicy42CF8D41',
    ]) {
      assert.deepEqual(policies[id], [null, 'Retain'], id);
    }
  });
});

describe('cluster-manager branches', () => {
  test('the ASG registers its target groups web-portal, internal, external', async () => {
    const { template } = await synthClusterManager();
    assert.deepEqual(template.Resources.clustermanagerasgASGBD35240A.Properties.TargetGroupARNs, [
      { Ref: 'webportaltargetgroup' },
      { Ref: 'clustermanagerinternaltargetgroup' },
      { Ref: 'clustermanagerexternaltargetgroup' },
    ]);
  });

  test('the bedrock delivery role carries the confused-deputy condition on statement 0', async () => {
    const { template } = await synthClusterManager();
    const statements =
      template.Resources.bedrockinvocationlogging054720CC.Properties.AssumeRolePolicyDocument.Statement;
    assert.equal(statements.length, 1);
    assert.deepEqual(statements[0].Condition, {
      StringEquals: { 'aws:SourceAccount': { Ref: 'AWS::AccountId' } },
    });
  });

  test('RetentionInDays is a string, and is omitted when the value is not a valid retention', async () => {
    const { template } = await synthClusterManager();
    assert.equal(template.Resources.bedrockinvocationloggroup.Properties.RetentionInDays, '30');

    const invalid = await synthClusterManager(
      configWith({ 'cluster-manager.bedrock.invocation_logging.log_retention_in_days': '45' }),
    );
    const properties = invalid.template.Resources.bedrockinvocationloggroup.Properties;
    assert.deepEqual(Object.keys(properties).sort(), ['LogGroupName', 'ServiceToken']);
  });

  test('bedrock disabled drops its nine resources and its three settings', async () => {
    const { template } = await synthClusterManager(
      configWith({ 'cluster-manager.bedrock.enabled': 'false' }),
    );
    for (const id of [
      'projectroleboundaryD13C6967',
      'detachprojectboundarieslambdapolicy941B0FC6',
      'detachprojectboundariesrole7673BE61',
      'detachprojectboundarieslambdaBF006DAF',
      'projectroleboundaries',
      'ensurebedrockloggrouplambdapolicy4449C6A0',
      'ensurebedrockloggrouprole894DBCE5',
      'ensurebedrockloggrouplambda051BC7B4',
      'bedrockinvocationloggroup',
      'bedrockinvocationlogging054720CC',
      'bedrockinvocationloggingpolicyD7BA525E',
    ]) {
      assert.equal(template.Resources[id], undefined, id);
    }
    const settings = template.Resources.ideadev27clustermanagersettings.Properties.settings;
    assert.equal(settings['bedrock.invocation_log_group_name'], undefined);
    assert.equal(settings['bedrock.invocation_log_role_arn'], undefined);
    assert.ok(settings['jwt_signing_secret_arn'] !== undefined);
  });

  test('the cdk-nag suppressions are the deployed ones, resource for resource', async () => {
    // Every suppression is part of the deployed template, so a lost one and an invented one are
    // both a `cdk diff` on a stack that must not diff. Driven off the live template, so a
    // suppression removed from the stack fails here even if this list is never updated.
    const { template } = await synthClusterManager(CONFIG_FILE, { nag: true });
    const live = nagSuppressions(withNodeHandlers(readJson(LIVE_TEMPLATE)));
    assert.deepEqual(nagSuppressions(template), live);

    // Spelled out, so a change to both sides at once still fails.
    assert.deepEqual(live.clustermanagerclientid, [
      ['AwsSolutions-SMG4', 'Secret rotation not applicable for OAuth 2.0 ClientId/Secret'],
    ]);
    assert.deepEqual(live.clustermanagerclientsecret, live.clustermanagerclientid);
    // A ported handler carries the Node suppression in place of the Python pair.
    assert.deepEqual(live.detachprojectboundarieslambdaBF006DAF, [NODE_NAG_SUPPRESSION]);

    // cdk-nag raises AwsSolutions-SMG4 on the JWT signing secret (a CfnSecret with no rotation
    // schedule) and neither administrator suppresses it: idea-admin.sh defaults
    // IDEA_ADMIN_ENABLE_CDK_NAG_SCAN to false, so no deploy has ever run the scan. A suppression
    // here would add a Metadata block the deployed stack does not have.
    assert.equal(live.clustermanagerjwtsigningsecret, undefined);
    assert.equal(template.Resources.clustermanagerjwtsigningsecret.Metadata?.cdk_nag, undefined);
  });

  /** The name of the target group an endpoint's forward action points at. */
  function forwardedTargetGroupName(resources: Json, endpointId: string): unknown {
    const actions = (resources[endpointId] as Json).Properties.actions as Json[];
    const arn = actions[0]?.TargetGroupArn as Json;
    const referenced = arn?.Ref as string | undefined;
    if (referenced === undefined) return arn;
    return (resources[referenced] as Json | undefined)?.Properties?.Name;
  }

  test("ecs.retain_existing_hosts routes the endpoints and keeps the host shape", async () => {
    // The intermediate state a cutover needs: this stack's own container target groups serve the
    // endpoints while the host group is still there to go back to. The group registers with
    // nothing, because the container target groups take IP targets.
    const { template } = await synthClusterManager(
      configWith({ ...ECS_SHARED_CAPACITY_VALUES, "ecs.retain_existing_hosts": true }),
    );
    const resources = template.Resources as Json;

    for (const id of [
      "clustermanagerltProfileFDAFAF8F",
      "clustermanagerlt5189880B",
      "clustermanagerasgASGBD35240A",
      "clustermanagerasgScalingPolicycpuutilizationscalingpolicyF99F846C",
      "clustermanagersecuritygroupfromideadev27clustermanagerbastionhostsecuritygroup4AA69B082298FB155E",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }
    assert.equal(resources.clustermanagerasgASGBD35240A.Properties.TargetGroupArns, undefined);

    assert.equal(forwardedTargetGroupName(resources, "webportalendpoint"), `${CLUSTER}-cm-ecs-w-${TARGET_GROUP_HASH["cluster-manager"]}`);
    assert.equal(forwardedTargetGroupName(resources, "externalendpoint"), `${CLUSTER}-cm-ecs-e-${TARGET_GROUP_HASH["cluster-manager"]}`);
    assert.equal(forwardedTargetGroupName(resources, "internalendpoint"), `${CLUSTER}-cm-ecs-i-${TARGET_GROUP_HASH["cluster-manager"]}`);
  });

  test("ecs.enabled removes the host shape and repoints the retained endpoints", async () => {
    const { template } = await synthClusterManager(configWith({ ...ECS_SHARED_CAPACITY_VALUES }));
    const resources = template.Resources as Json;

    for (const id of [
      "clustermanagerltProfileFDAFAF8F",
      "clustermanagerlt5189880B",
      "clustermanagerasgASGBD35240A",
      "clustermanagerasgScalingPolicycpuutilizationscalingpolicyF99F846C",
      "webportaltargetgroup",
      "clustermanagerexternaltargetgroup",
      "clustermanagerinternaltargetgroup",
      "clustermanagersecuritygroupfromideadev27clustermanagerbastionhostsecuritygroup4AA69B082298FB155E",
    ]) {
      assert.equal(resources[id], undefined, id);
    }

    assert.equal(forwardedTargetGroupName(resources, "webportalendpoint"), `${CLUSTER}-cm-ecs-w-${TARGET_GROUP_HASH["cluster-manager"]}`);
    assert.equal(forwardedTargetGroupName(resources, "externalendpoint"), `${CLUSTER}-cm-ecs-e-${TARGET_GROUP_HASH["cluster-manager"]}`);
    assert.equal(forwardedTargetGroupName(resources, "internalendpoint"), `${CLUSTER}-cm-ecs-i-${TARGET_GROUP_HASH["cluster-manager"]}`);
    // The task runs as an identity this stack creates, so the host role never trusts the task
    // service. Two sets of roles coexist through the transition.
    assert.doesNotMatch(
      JSON.stringify(resources.clustermanagerrole4D8ECACE.Properties.AssumeRolePolicyDocument),
      /ecs-tasks\./,
    );

    // The published identity is the service name spelled out, not a reference to the service: the
    // service has to start after this resource, because the application reads its rows at boot.
    const settings = resources.ideadev27clustermanagersettings.Properties.settings;
    assert.equal(settings.asg_name, `${CLUSTER}-cluster-manager`);
    assert.match(
      JSON.stringify(settings.asg_arn),
      new RegExp(`service/${CLUSTER}-ecs/${CLUSTER}-cluster-manager`),
    );

    for (const id of [
      "ideadev27userpoolclustermanagerclientD645207F",
      "clustermanagerjwtsigningsecret",
      "clustertaskssqsqueue319A7909",
      "notificationssqsqueue60B47557",
      "clustermanagerrole4D8ECACE",
      "clustermanagerpolicyEF9BA73D",
      "clustermanagersecuritygroupDD4A1A52",
      "clustermanagersecuritygroupfromideadev27clustermanagerexternalloadbalancersecuritygroupEFB1EA8784431FF11675",
      "bedrockinvocationloggroup",
      "ideadev27clustermanagersettings",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }

    // The task itself: one definition, one service, and the three target groups its endpoints
    // forward to.
    const ofType = (type: string): string[] =>
      Object.entries(resources).filter(([, resource]) => (resource as Json).Type === type).map(([id]) => id);
    assert.equal(ofType("AWS::ECS::TaskDefinition").length, 1, "one task definition");
    assert.equal(ofType("AWS::ECS::Service").length, 1, "one service");
    assert.equal(ofType("AWS::ElasticLoadBalancingV2::TargetGroup").length, 3, "three target groups");
  });
});
