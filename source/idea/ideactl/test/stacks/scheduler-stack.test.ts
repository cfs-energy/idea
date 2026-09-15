/**
 * The scheduler stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; every test that needs them skips
 * when they are absent so the suite still runs without them.
 *
 * The deployed dev27 template predates `scheduler.use_stable_server_name` being turned on in the
 * cluster settings, so the resource-for-resource comparison runs with that key set back to false,
 * the state the deployed template was built from. A separate test covers the key as the fixture
 * has it today.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { buildApp } from '../../src/cdk/app.ts';
import { bootstrapPackageBasenames, bootstrapPackageUri } from '../../src/cli/bootstrap-package.ts';
import { buildStack, ec2BlockDeviceName } from '../../src/cdk/stacks/scheduler.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { withRetirements } from '../support/retirements.ts';
import { cacheSetup } from '../support/setup-cache.ts';
import { ECS_SHARED_CAPACITY_VALUES, TARGET_GROUP_HASH } from '../support/ecs-harness.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-scheduler.json');

const CLUSTER = 'idea-dev27';
const MODULE_ID = 'scheduler';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
/** `shake_256('idea-dev27').hexdigest(5)`, the bootstrap qualifier the synthesizer is built with. */
const QUALIFIER = '6f3b37a775';

requireFixtures(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/** Copies the dev27 settings scan, replacing the value of each named key (adding it if absent). */
function configWith(overrides: Record<string, string | number | boolean | string[]>): string {
  const attribute = (value: string | number | boolean | string[]): Json => {
    if (Array.isArray(value)) return { L: value.map((entry) => ({ S: entry })) };
    if (typeof value === 'number') return { N: String(value) };
    return typeof value === 'boolean' ? { BOOL: value } : { S: value };
  };
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
        configFile,
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
  deployedConfigFile ??= configWith({ 'scheduler.use_stable_server_name': false });
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

/** One row out of the dev27 settings scan, so no live identifier is written into this file. */
function settingValue(key: string): Json {
  const item = (readJson(CONFIG_FILE).Items as Json[]).find((row) => row.key?.S === key);
  assert.ok(item !== undefined, `dev27 settings have no ${key}`);
  return item.value as Json;
}

const readSetting = (key: string): string => settingValue(key).S as string;

const readList = (key: string): string[] => (settingValue(key).L as Json[]).map((entry) => entry.S as string);

/** The instance's `Fn::Base64(Fn::Sub(...))` bootstrap script. */
const userDataOf = (template: Json): string =>
  template.Resources.schedulerinstance.Properties.UserData['Fn::Base64']['Fn::Sub'] as string;

describe('scheduler stack', () => {
  test('matches the deployed dev27 template resource for resource', async () => {
    const { template } = await synthScheduler(deployedConfig());
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    assert.deepEqual(
      deployedResources(template),
      deployedResources(withRetirements('scheduler', withRetainedStateful(readJson(LIVE_TEMPLATE)))),
    );
  });

  test('every resource carries the deletion policies the deployed template has', async () => {
    const { template } = await synthScheduler(deployedConfig());
    const live = retentionPolicies(withRetirements('scheduler', withRetainedStateful(readJson(LIVE_TEMPLATE))));
    assert.deepEqual(retentionPolicies(template), live);

    // Nothing in this stack is retained on a deliberate teardown: a stack delete still takes the
    // queues, the secrets and the Cognito groups with it. Spelled out so a RemovalPolicy.RETAIN
    // cannot land unnoticed. The update-replace side is the one this branch moves.
    // The instance is here on purpose: an ordinary upgrade replaces it, so it carries no policy in
    // either direction and this branch does not give it one.
    for (const id of [
      'schedulerrole9B80A9F3',
      'schedulercomputenoderoleB0A20C8B',
      'schedulerspotfleetrequestroleB0E009D7',
      'schedulerinstance',
    ]) {
      assert.deepEqual(live[id], { DeletionPolicy: null, UpdateReplacePolicy: null }, id);
    }
    // No teardown policy, and never lost to a replacement.
    for (const id of [
      'ideadev27userpoolschedulerclient02E3170D',
      'scheduleradministratorsgroup',
      'schedulerusersgroup',
    ]) {
      assert.deepEqual(live[id], { DeletionPolicy: null, UpdateReplacePolicy: 'Retain' }, id);
    }
    // Custom resources hold nothing of their own and still delete in both directions.
    for (const id of ['externalendpoint', 'internalendpoint', 'ideadev27schedulersettings']) {
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
      /^s3:\/\/idea-dev27-cluster-us-east-2-\d{12}\/cdk\//,
    );
  });

  test('the user pool client is ordered after the resource server', async () => {
    const { template } = await synthScheduler(deployedConfig());
    // The dependency exists only because Python adds it by hand: CDK infers no edge between them,
    // and without it the client can be created before the scopes it references.
    assert.deepEqual(template.Resources.ideadev27userpoolschedulerclient02E3170D.DependsOn, [
      'ideadev27userpoolresourceserver7B2B7736',
    ]);
  });
});

describe('scheduler stack branches', () => {
  test('use_stable_server_name changes those two properties and nothing else', async () => {
    // The key is on in the dev27 settings today, which is why the deployed template is one deploy
    // behind. Asserting the two properties it changes is not enough: what has to hold is that the
    // rest of the stack is untouched. The flag-off synth is the same one the first suite compares
    // to the deployed template resource for resource, so pinning the delta here pins the whole
    // branch against the deployed template too.
    const { template } = await synthScheduler();
    const { template: deployed } = await synthScheduler(deployedConfig());
    const withFlag = deployedResources(template);
    const withoutFlag = deployedResources(deployed);

    const settings = withFlag.ideadev27schedulersettings.Properties.settings;
    assert.equal(settings.private_dns_name, 'scheduler.idea-dev27.us-east-2.local');
    assert.deepEqual(withoutFlag.ideadev27schedulersettings.Properties.settings.private_dns_name, {
      'Fn::GetAtt': ['schedulerinstance', 'PrivateDnsName'],
    });
    settings.private_dns_name = withoutFlag.ideadev27schedulersettings.Properties.settings.private_dns_name;

    const statements = withFlag.schedulerpolicyFF65A604.Properties.PolicyDocument.Statement as Json[];
    // The statement's index follows the position of the block in `resources/policies/scheduler.yml`:
    // it sits before the directory-service includes, so it shifts the two AD automation statements
    // down one rather than landing at the end.
    const index = statements.findIndex((statement) => statement.Sid === 'SchedulerDnsRecord');
    assert.equal(index, 26);
    assert.deepEqual(statements[index], {
      Action: 'route53:ChangeResourceRecordSets',
      Effect: 'Allow',
      Resource: 'arn:aws:route53:::hostedzone/*',
      Sid: 'SchedulerDnsRecord',
    });
    statements.splice(index, 1);

    assert.deepEqual(withFlag, withoutFlag);
  });

  test('the reconstructed bootstrap package uri is the one the uploader and the deploy use', async () => {
    // The stack rebuilds this string when the CLI passes no `bootstrap_package_uri` context, so it
    // has to stay equal to what `src/cli/bootstrap-package.ts` would have uploaded, and to the
    // literal in the deployed template.
    const { template } = await synthScheduler(deployedConfig());
    const expected = bootstrapPackageUri(
      readSetting('cluster.cluster_s3_bucket'),
      `${bootstrapPackageBasenames(MODULE_ID, DEPLOYMENT_ID).standard}.tar.gz`,
    );
    assert.ok(userDataOf(template).includes(`"${expected}"`), expected);
    assert.ok(userDataOf(readJson(LIVE_TEMPLATE)).includes(`"${expected}"`), 'deployed');
  });

  test('the bootstrap_package_uri context wins over the reconstructed one', async () => {
    const uri = 's3://example-bucket/idea/bootstrap/bootstrap-from-context.tar.gz';
    const { template } = await synthScheduler(deployedConfig(), { bootstrap_package_uri: uri });
    assert.ok(userDataOf(template).includes(`"${uri}"`), uri);
  });

  test('a public scheduler lands in a public subnet and publishes its public ip', async () => {
    const { template } = await synthScheduler(
      configWith({ 'scheduler.use_stable_server_name': false, 'scheduler.public': true }),
    );
    const networkInterface = template.Resources.schedulerinstance.Properties.NetworkInterfaces[0];
    assert.equal(networkInterface.AssociatePublicIpAddress, true);
    assert.equal(networkInterface.SubnetId, readList('cluster.network.public_subnets')[0]);
    assert.deepEqual(template.Resources.ideadev27schedulersettings.Properties.settings.public_ip, {
      'Fn::GetAtt': ['schedulerinstance', 'PublicIp'],
    });
  });

  test('an empty subnet list is refused at synth, not at deploy', async () => {
    await assert.rejects(
      synthScheduler(
        configWith({ 'scheduler.use_stable_server_name': false, 'cluster.network.private_subnets': [] }),
      ),
      /cluster\.network\.private_subnets is empty/,
    );
  });

  test('the non-dev27 ec2 settings reach the launch template and the instance', async () => {
    const kmsKeyId = '11111111-2222-3333-4444-555555555555';
    const proxy = 'http://proxy.example.invalid:3128';
    const { template } = await synthScheduler(
      configWith({
        'scheduler.use_stable_server_name': false,
        'scheduler.base_os': 'rhel9',
        'scheduler.volume_type': 'io2',
        'scheduler.ec2.metadata_http_tokens': 'optional',
        'cluster.ebs.kms_key_id': kmsKeyId,
        'cluster.network.https_proxy': proxy,
        'cluster.network.no_proxy': '169.254.169.254',
      }),
    );
    const launchTemplateData = template.Resources.schedulerltC82E59C0.Properties.LaunchTemplateData;
    const blockDevice = launchTemplateData.BlockDeviceMappings[0];

    assert.equal(blockDevice.DeviceName, '/dev/sda1');
    assert.equal(template.Resources.schedulerinstance.Properties.BlockDeviceMappings[0].DeviceName, '/dev/sda1');
    // The launch template takes the CDK enum, which has no io2 member in this code path, so a
    // volume type that is not gp3 silently becomes gp2 there while the instance keeps the raw
    // string. Both halves are Python's, and a deploy resolves them to the instance's value.
    assert.equal(blockDevice.Ebs.VolumeType, 'gp2');
    assert.equal(template.Resources.schedulerinstance.Properties.BlockDeviceMappings[0].Ebs.VolumeType, 'io2');
    assert.equal(blockDevice.Ebs.Encrypted, true);
    assert.match(JSON.stringify(blockDevice.Ebs.KmsKeyId), new RegExp(`:key/${kmsKeyId}`));
    // `require_imdsv2=False` leaves the block off entirely rather than writing `optional`.
    assert.equal(launchTemplateData.MetadataOptions, undefined);

    const userData = userDataOf(template);
    assert.ok(userData.includes(`http_proxy=${proxy}`), 'http_proxy');
    assert.ok(userData.includes(`https_proxy=${proxy}`), 'https_proxy');
    assert.ok(userData.includes('no_proxy=169.254.169.254'), 'no_proxy');
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
    assert.match(projectRoleArn, /:role\/idea\/idea-dev27\/projects\/\*$/);

    const settings = template.Resources.ideadev27schedulersettings.Properties.settings;
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

  test('retain_dns_record retains the name record and changes nothing else', async () => {
    // The stage the one-phase migration requires before `ecs.enabled`: the record leaves the
    // template one deploy later, and CloudFormation keeps the name because this deploy recorded
    // Retain against it. A retain-only change set has no other effect, which is what the final
    // comparison holds.
    const { template } = await synthScheduler(
      configWith({ 'scheduler.retain_dns_record': true, 'scheduler.use_stable_server_name': false }),
    );
    const { template: deployed } = await synthScheduler(deployedConfig());

    assert.deepEqual(retentionPolicies(template).schedulerdnsrecord4F3D9346, {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
    // Without the flag the record already survives a replacement, because it is a DNS record and
    // every stateful resource carries that. What the flag adds is surviving the stack delete.
    assert.deepEqual(retentionPolicies(deployed).schedulerdnsrecord4F3D9346, {
      DeletionPolicy: null,
      UpdateReplacePolicy: 'Retain',
    });

    const retained = deployedResources(template);
    delete retained.schedulerdnsrecord4F3D9346.DeletionPolicy;
    assert.deepEqual(retained, deployedResources(deployed));
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
      template.Resources.ideadev27schedulersettings.Properties.settings.use_stable_server_name,
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

  test("ecs.retain_existing_hosts routes the endpoints and keeps the scheduler host", async () => {
    // The intermediate state a cutover needs: this stack's own container target groups serve the
    // endpoints while the instance is still there to go back to. The DNS record still leaves the
    // template, because the running task owns that name from ECS_SCHEDULER_READY onward.
    const { template } = await synthScheduler(
      configWith({
        ...ECS_SHARED_CAPACITY_VALUES,
        "ecs.retain_existing_hosts": true,
        "scheduler.use_stable_server_name": true,
      }),
    );
    const resources = template.Resources as Json;

    for (const id of [
      "schedulerltC82E59C0",
      "schedulerinstance",
      "schedulerschedulerinstanceprofile",
      "schedulersecuritygroupfromideadev27schedulerbastionhostsecuritygroupE705486322D1BB57B7",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }
    assert.equal(resources.schedulerdnsrecord4F3D9346, undefined);
    assert.equal(resources.schedulerexternaltargetgroup, undefined);
    assert.equal(resources.schedulerinternaltargetgroup, undefined);

    assert.equal(forwardedTargetGroupName(resources, "externalendpoint"), `${CLUSTER}-sched-ecs-e-${TARGET_GROUP_HASH["scheduler"]}`);
    assert.equal(forwardedTargetGroupName(resources, "internalendpoint"), `${CLUSTER}-sched-ecs-i-${TARGET_GROUP_HASH["scheduler"]}`);
  });

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
      "schedulersecuritygroupfromideadev27schedulerbastionhostsecuritygroupE705486322D1BB57B7",
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
    const settings = resources.ideadev27schedulersettings.Properties.settings as Json;
    assert.equal(settings.private_dns_name, "scheduler.idea-dev27.us-east-2.local");
    // Written, not inherited: the task reads this row to update the record, and the compute node
    // template reads it to build PBS_SERVER from the DNS name instead of a short name.
    assert.equal(settings.use_stable_server_name, true);
    assert.equal(settings.private_ip, undefined);
    assert.equal(settings.instance_id, undefined);
    assert.equal(settings.public_ip, undefined);

    for (const id of [
      "ideadev27userpoolschedulerclient02E3170D",
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
      "ideadev27schedulersettings",
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
describe('block device name', () => {
  test('follows the base os', () => {
    assert.equal(ec2BlockDeviceName('amazonlinux2'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('amazonlinux2023'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('rhel9'), '/dev/sda1');
    assert.equal(ec2BlockDeviceName('ubuntu2204'), '/dev/sda1');
  });
});
