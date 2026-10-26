/**
 * The scheduler stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; every test that needs them skips
 * when they are absent so the suite still runs without them.
 *
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { after, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/scheduler.ts';
import { ECS_SHARED_CAPACITY_VALUES, TARGET_GROUP_HASH } from '../support/ecs-harness.ts';
import { requireCapture } from '../support/fixtures.ts';
import { cacheSetup } from '../support/setup-cache.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-demo1');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-demo1-scheduler.json');

const CLUSTER = 'idea-demo1';
const MODULE_ID = 'scheduler';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
/** `shake_256('idea-demo1').hexdigest(5)`, the bootstrap qualifier the synthesizer is built with. */
const QUALIFIER = 'd98389644e';

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE],
  "node tools/parity/capture.ts --live --cluster idea-demo1 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/** Copies the demo1 settings scan, replacing the value of each named key (adding it if absent). */
function configWith(overrides: Record<string, string | number | boolean | string[]>): string {
  const attribute = (value: string | number | boolean | string[]): Json => {
    if (Array.isArray(value)) return { L: value.map((entry) => ({ S: entry })) };
    if (typeof value === 'number') return { N: String(value) };
    return typeof value === 'boolean' ? { BOOL: value } : { S: value };
  };
  overrides = { ...ECS_SHARED_CAPACITY_VALUES, ...overrides };
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = attribute(overrides[key]!);
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({ key: { S: key }, value: attribute(overrides[key]!), version: { N: '1' } });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-scheduler-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface Synthesized {
  template: Json;
  /** The stack's entry in the cloud assembly manifest: none of this reaches the template. */
  artifact: Json;
}

async function synthSchedulerUncached(
  configFile: string = CONFIG_FILE,
  context: Record<string, string> = {},
): Promise<Synthesized> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-scheduler-'));
  const contextFile = join(workdir, 'cdk.context.json');
  cpSync(CONTEXT_FILE, contextFile);
  if (Object.keys(context).length > 0) {
    writeFileSync(contextFile, JSON.stringify({ ...readJson(CONTEXT_FILE), ...context }));
  }
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
        terminationProtection: true,
        configFile: configFile === CONFIG_FILE ? configWith({}) : configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_ID]: async () => buildStack },
    );
    app.synth();
    const manifest = readJson(join(outdir, 'manifest.json'));
    // Registered only once the synth is through: cdk-nag finishes writing its report after a
    // failed build returns, and deleting the directory under it turns the throw into an
    // unhandled ENOENT.
    workdirs.push(workdir);
    return {
      template: readJson(join(outdir, `${CLUSTER}-${MODULE_ID}.template.json`)),
      artifact: manifest.artifacts[`${CLUSTER}-${MODULE_ID}`] as Json,
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

/** Everything but `AWS::CDK::Metadata`, whose Analytics blob differs between the two runtimes. */
function deployedResources(template: Json): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );
}

/** Logical id -> the two policies, present or absent, for every resource in a template. */
function retentionPolicies(template: Json): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(deployedResources(template)).map(([id, resource]) => [
      id,
      {
        DeletionPolicy: (resource as Json).DeletionPolicy ?? null,
        UpdateReplacePolicy: (resource as Json).UpdateReplacePolicy ?? null,
      },
    ]),
  );
}

/** The immutable settings the deployed template was built from. */
let deployedConfigFile: string | undefined;
const deployedConfig = (): string => {
  deployedConfigFile ??= configWith({});
  return deployedConfigFile;
};

const synthCurrentScheduler = cacheSetup(async () => synthSchedulerUncached());
const synthDeployedScheduler = cacheSetup(async () => synthSchedulerUncached(deployedConfig()));

/**
 * Reuses each immutable fixture shape and clones its plain JSON result.
 * Configuration and context branches always synthesize fresh.
 */
async function synthScheduler(
  configFile: string = CONFIG_FILE,
  context: Record<string, string> = {},
): Promise<Synthesized> {
  if (Object.keys(context).length === 0) {
    if (configFile === CONFIG_FILE) return synthCurrentScheduler();
    if (configFile === deployedConfigFile) return synthDeployedScheduler();
  }
  return synthSchedulerUncached(configFile, context);
}

/** One row out of the demo1 settings scan, so no live identifier is written into this file. */
function settingValue(key: string): Json {
  const item = (readJson(CONFIG_FILE).Items as Json[]).find((row) => row.key?.S === key);
  assert.ok(item !== undefined, `demo1 settings have no ${key}`);
  return item.value as Json;
}

const readSetting = (key: string): string => settingValue(key).S as string;

const readList = (key: string): string[] => (settingValue(key).L as Json[]).map((entry) => entry.S as string);

describe('scheduler stack', () => {

  test('every resource carries the deletion policies the deployed template has', async () => {
    const { template } = await synthScheduler(deployedConfig());
    const live = retentionPolicies(template);

    // Nothing in this stack is retained on a deliberate teardown: a stack delete still takes the
    // queues, the secrets and the Cognito groups with it. Spelled out so a RemovalPolicy.RETAIN
    // cannot land unnoticed. The update-replace side is the one this branch moves.
    for (const id of [
      'schedulerrole9B80A9F3',
      'schedulercomputenoderoleB0A20C8B',
      'schedulerspotfleetrequestroleB0E009D7',
    ]) {
      assert.deepEqual(live[id], { DeletionPolicy: null, UpdateReplacePolicy: null }, id);
    }
    // No teardown policy, and never lost to a replacement.
    for (const id of [
      'ideademo1userpoolschedulerclient2A26EA5A',
      'scheduleradministratorsgroup',
      'schedulerusersgroup',
    ]) {
      assert.deepEqual(live[id], { DeletionPolicy: null, UpdateReplacePolicy: 'Retain' }, id);
    }
    // Custom resources hold nothing of their own and still delete in both directions.
    for (const id of ['externalendpoint', 'internalendpoint', 'ideademo1schedulersettings']) {
      assert.deepEqual(live[id], { DeletionPolicy: 'Delete', UpdateReplacePolicy: 'Delete' }, id);
    }
    for (const id of ['schedulerclientid', 'schedulerclientsecret', 'jobstatusevents0A7392AC', 'jobstatuseventsdlqF3F9DBF4']) {
      assert.deepEqual(live[id], { DeletionPolicy: 'Delete', UpdateReplacePolicy: 'Retain' }, id);
    }
  });

  test('the manifest carries the stack properties the template cannot', async () => {
    const { artifact, template } = await synthScheduler(deployedConfig());
    assert.equal(artifact.type, 'aws:cloudformation:stack');
    assert.equal(artifact.displayName, `${CLUSTER}-${MODULE_ID}`);
    assert.equal(artifact.properties.templateFile, `${CLUSTER}-${MODULE_ID}.template.json`);
    // Off by default in CDK, and a stack property rather than a template one: losing it would
    // silently allow the next deploy to delete the scheduler.
    assert.equal(artifact.properties.terminationProtection, true);
    assert.deepEqual(artifact.properties.tags, {
      'idea:ModuleId': MODULE_ID,
      'idea:ModuleName': MODULE_ID,
      'idea:ModuleVersion': template.Description.split('Version: ')[1],
      'idea:ClusterName': CLUSTER,
    });
    assert.equal(template.Description, readJson(LIVE_TEMPLATE).Description);

    // The synthesizer's qualifier decides which bootstrap stack the deploy reads its roles and
    // asset bucket from. It appears in the template only as the parameter default.
    assert.equal(
      template.Parameters.BootstrapVersion.Default,
      `/cdk-bootstrap/${QUALIFIER}/version`,
    );
    assert.equal(artifact.properties.bootstrapStackVersionSsmParameter, `/cdk-bootstrap/${QUALIFIER}/version`);
    assert.equal(artifact.properties.requiresBootstrapStackVersion, 6);
    for (const role of ['deploy-role', 'cfn-exec-role', 'lookup-role']) {
      const arn = JSON.stringify(artifact.properties);
      assert.ok(arn.includes(`cdk-${QUALIFIER}-${role}-`), role);
    }
    // The template asset goes to the cluster bucket under `cdk/`, not to a CDK-owned bucket.
    assert.match(
      artifact.properties.stackTemplateAssetObjectUrl as string,
      /^s3:\/\/idea-demo1-cluster-us-east-2-\d{12}\/cdk\//,
    );
  });

  test('the user pool client is ordered after the resource server', async () => {
    const { template } = await synthScheduler(deployedConfig());
    // The dependency exists only because Python adds it by hand: CDK infers no edge between them,
    // and without it the client can be created before the scopes it references.
    assert.deepEqual(template.Resources.ideademo1userpoolschedulerclient2A26EA5A.DependsOn, [
      'ideademo1userpoolresourceserverF2A5DA79',
    ]);
  });
});

describe('scheduler stack branches', () => {
  test('the scheduler client may create the validation user and project', async () => {
    const { template } = await synthScheduler();
    const client = Object.values(template.Resources as Json).find(
      (resource) => resource.Type === 'AWS::Cognito::UserPoolClient',
    ) as Json;
    // compute image validation provisions its identity when no desktop controller does
    assert.deepEqual(client.Properties.AllowedOAuthScopes, [
      'scheduler/read',
      'scheduler/write',
      'cluster-manager/read',
      'cluster-manager/write',
    ]);
  });

  test('the configured hostname agrees across the task and published settings', async () => {
    const hostname = 'batch.idea-demo1.us-east-2.local';
    const { template } = await synthScheduler(configWith({
      ...ECS_SHARED_CAPACITY_VALUES,
      'scheduler.hostname': hostname,
    }));
    const task = Object.values(template.Resources as Json).find(
      (resource) => resource.Type === 'AWS::ECS::TaskDefinition',
    ) as Json;
    const containers = task.Properties.ContainerDefinitions as Json[];
    const scheduler = containers.find((container) =>
      container.Environment?.some((entry: Json) => entry.Name === 'IDEA_SCHEDULER_DNS_NAME'),
    );
    assert.equal(scheduler?.Environment.find((entry: Json) => entry.Name === 'IDEA_SCHEDULER_DNS_NAME').Value, hostname);
    assert.equal(template.Resources.ideademo1schedulersettings.Properties.settings.private_dns_name, hostname);

    const sidecar = containers.find((container) => container.Name === 'scheduler-openpbs-logs');
    assert.ok(sidecar);
    const script = sidecar.Command[0] as string;
    for (const directory of ['server_logs', 'sched_logs', 'server_priv/accounting']) {
      assert.ok(script.includes(`${directory}'/*`), directory);
    }
    assert.match(script, /tail -n "\$from_line" -F/);
    assert.match(script, /sleep 30/);
    assert.doesNotMatch(script, /\*\.log|exec tail/);

  });

  test('bedrock for jobs adds the queue grant, the pass-role grant and the setting', async () => {
    const { template } = await synthScheduler(
      configWith({
        'scheduler.use_stable_server_name': false,
        'scheduler.bedrock.enabled': true,
        'cluster-manager.bedrock.enabled': true,
      }),
    );
    const queuePolicy = template.Resources.jobstatuseventsPolicy9878DDAE.Properties.PolicyDocument
      .Statement as Json[];
    assert.deepEqual(
      queuePolicy.map((statement) => statement.Sid),
      ['AlwaysEncrypted', 'ProjectRoleJobStatusEvents'],
    );
    const projectRoleArn = queuePolicy[1]!.Condition.ArnLike['aws:PrincipalArn'] as string;
    assert.match(projectRoleArn, /:role\/idea\/idea-demo1\/projects\/\*$/);

    const settings = template.Resources.ideademo1schedulersettings.Properties.settings;
    assert.equal(settings['bedrock.project_pass_role_arn'], projectRoleArn);

    const statements = template.Resources.schedulerpolicyFF65A604.Properties.PolicyDocument
      .Statement as Json[];
    assert.ok(statements.some((statement) => statement.Sid === 'PassProjectRoles'));
  });

  test('the dlq is a dead letter queue with no queue of its own', async () => {
    const { template } = await synthScheduler(deployedConfig());
    const dlq = template.Resources.jobstatuseventsdlqF3F9DBF4;
    assert.equal(dlq.Properties.RedrivePolicy, undefined);
    assert.deepEqual(dlq.Metadata.cdk_nag.rules_to_suppress, [
      { reason: 'Dead letter queue', id: 'AwsSolutions-SQS3' },
    ]);
    assert.equal(
      template.Resources.jobstatusevents0A7392AC.Properties.RedrivePolicy.maxReceiveCount,
      10,
    );
  });

  test('ecs.enabled needs no prior write of use_stable_server_name', async () => {
    // The policy template grants route53:ChangeResourceRecordSets on the row or the flag, and the
    // stack writes the row itself, so a cluster whose row is still false gets the permission on this
    // deploy and the row after it. No operator step, no ordering between a write and a render.
    const { template } = await synthScheduler(
      configWith({ ...ECS_SHARED_CAPACITY_VALUES, 'scheduler.use_stable_server_name': false }),
    );
    const statements = template.Resources.schedulerpolicyFF65A604.Properties.PolicyDocument
      .Statement as Json[];
    assert.ok(
      statements.some((statement) => statement.Sid === 'SchedulerDnsRecord'),
      'the task can update its own record on the deploy that turns the flag on',
    );
    assert.equal(
      template.Resources.ideademo1schedulersettings.Properties.settings.use_stable_server_name,
      true,
    );
  });

  /** The name of the target group an endpoint's forward action points at. */
  function forwardedTargetGroupName(resources: Json, endpointId: string): unknown {
    const actions = (resources[endpointId] as Json).Properties.actions as Json[];
    const arn = actions[0]?.TargetGroupArn as Json;
    const referenced = arn?.Ref as string | undefined;
    if (referenced === undefined) return arn;
    return (resources[referenced] as Json | undefined)?.Properties?.Name;
  }

  test("ecs.enabled removes the scheduler host and keeps the service-facing resources", async () => {
    const { template } = await synthScheduler(
      configWith({ ...ECS_SHARED_CAPACITY_VALUES, "scheduler.use_stable_server_name": true }),
    );
    const resources = template.Resources as Json;

    // The record leaves the template here. It survives in the hosted zone because the retain-only
    // stage above recorded Retain against it, and the task upserts the same name.
    for (const id of [
      "schedulerltC82E59C0",
      "schedulerinstance",
      "schedulerschedulerinstanceprofile",
      "schedulerdnsrecord4F3D9346",
      "schedulerexternaltargetgroup",
      "schedulerinternaltargetgroup",
      "schedulersecuritygroupfromideademo1schedulerbastionhostsecuritygroup5BD8E476220CA81B06",
    ]) {
      assert.equal(resources[id], undefined, id);
    }

    assert.equal(forwardedTargetGroupName(resources, "externalendpoint"), `${CLUSTER}-sched-ecs-e-${TARGET_GROUP_HASH["scheduler"]}`);
    assert.equal(forwardedTargetGroupName(resources, "internalendpoint"), `${CLUSTER}-sched-ecs-i-${TARGET_GROUP_HASH["scheduler"]}`);
    // The endpoint handler also reads the singular property, which stays in step with the action.
    assert.deepEqual(
      resources.externalendpoint.Properties.target_group_arn,
      (resources.externalendpoint.Properties.actions as Json[])[0]?.TargetGroupArn,
    );
    assert.deepEqual(
      resources.internalendpoint.Properties.target_group_arn,
      (resources.internalendpoint.Properties.actions as Json[])[0]?.TargetGroupArn,
    );

    // The task runs as an identity this stack creates, so the host role never trusts the task
    // service. Two sets of roles coexist through the transition.
    assert.doesNotMatch(
      JSON.stringify(resources.schedulerrole9B80A9F3.Properties.AssumeRolePolicyDocument),
      /ecs-tasks\./,
    );
    const settings = resources.ideademo1schedulersettings.Properties.settings as Json;
    assert.equal(settings.private_dns_name, "scheduler.idea-demo1.us-east-2.local");
    // Written, not inherited: the task reads this row to update the record, and the compute node
    // template reads it to build PBS_SERVER from the DNS name instead of a short name.
    assert.equal(settings.use_stable_server_name, true);
    assert.equal(settings.private_ip, undefined);
    assert.equal(settings.instance_id, undefined);
    assert.equal(settings.public_ip, undefined);

    for (const id of [
      "ideademo1userpoolschedulerclient2A26EA5A",
      "scheduleradministratorsgroup",
      "schedulerusersgroup",
      "schedulerrole9B80A9F3",
      "schedulerpolicyFF65A604",
      "schedulersecuritygroupFC4F1A4A",
      "schedulercomputenodesecuritygroup98545405",
      "schedulercomputenoderoleB0A20C8B",
      "schedulercomputenodeinstanceprofile",
      "schedulerspotfleetrequestroleB0E009D7",
      "jobstatusevents0A7392AC",
      "externalendpoint",
      "internalendpoint",
      "ideademo1schedulersettings",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }

    // The task itself, its two target groups, and the file system that carries PBS state across a
    // replacement. The instance is gone; the state is not.
    const ofType = (type: string): string[] =>
      Object.entries(resources).filter(([, resource]) => (resource as Json).Type === type).map(([id]) => id);
    assert.equal(ofType("AWS::ECS::TaskDefinition").length, 1, "one task definition");
    assert.equal(ofType("AWS::ECS::Service").length, 1, "one service");
    assert.equal(ofType("AWS::ElasticLoadBalancingV2::TargetGroup").length, 2, "two target groups");
    assert.equal(ofType("AWS::EFS::FileSystem").length, 1, "the PBS state file system");
    assert.equal(ofType("AWS::EFS::AccessPoint").length, 1, "the PBS access point");
  });
});

// The only part of the stack that needs no fixture, so the one thing in this file that runs in CI.
