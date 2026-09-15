/**
 * The analytics stack.
 *
 * The template diff lives in `tools/parity`; what is asserted here is what that diff cannot see:
 * the stack-level manifest entry, the deletion policies on every stateful resource, the three IAM
 * documents CDK writes from L2 grants, the two values that are regenerated on every synth, and the
 * four branches that have no captured template (use-existing, service-linked role, and the two
 * GovCloud ones).
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

import { App } from 'aws-cdk-lib';

import { buildApp } from '../../src/cdk/app.ts';
import { makeContext } from '../../src/cdk/constructs/base.ts';
import { CALLER_IDENTITY_KEY, listRolesKey, replaySynthReads } from '../../src/cdk/synth-reads.ts';
import { AnalyticsStack, buildStack, serviceLinkedRolePathPrefixes } from '../../src/cdk/stacks/analytics.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { ideaVersion } from '../../src/version.ts';
import { requireFixtures } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { cacheSetup } from '../support/setup-cache.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.analytics', 'manifest.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-analytics.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const STACK = `${CLUSTER}-analytics`;
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
const DNS_SUFFIX = 'amazonaws.com';

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

function scratch(prefix: string): string {
  const workdir = mkdtempSync(join(tmpdir(), `ideactl-w10-${prefix}-`));
  workdirs.push(workdir);
  return workdir;
}

/** The account id lives only in the gitignored fixture. */
function fixtureAccount(): string {
  return (readJson(SYNTH_READS)[CALLER_IDENTITY_KEY] as Json).account as string;
}

/** Copies the dev27 settings scan, replacing the value of each named key (adding it if absent). */
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
  const file = join(scratch('config'), 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

/** A copy of the replay fixture with the two service-linked-role probes answered differently. */
function synthReadsWithRoles(roles: Json[]): string {
  const reads = readJson(SYNTH_READS);
  for (const pathPrefix of serviceLinkedRolePathPrefixes(DNS_SUFFIX)) {
    reads[listRolesKey(pathPrefix)] = roles;
  }
  const file = join(scratch('reads'), 'synth-reads.json');
  writeFileSync(file, JSON.stringify(reads));
  return file;
}

interface AnalyticsSynthOptions {
  configFile?: string;
  synthReadsFile?: string;
  /** Bypasses setup reuse when an assertion depends on a regenerated value. */
  fresh?: boolean;
}

/** Synthesizes through `app.ts`, the way the parity harness and the CLI do. */
async function synthAnalyticsUncached(
  options: AnalyticsSynthOptions = {},
): Promise<{ template: Json; manifest: Json }> {
  const configFile = options.configFile ?? CONFIG_FILE;
  const synthReadsFile = options.synthReadsFile ?? SYNTH_READS;
  const workdir = scratch('app');
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, 'cdk.out.analytics');

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: 'analytics',
        moduleName: 'analytics',
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile,
        synthReadsFile,
      },
      { analytics: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${STACK}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

const synthDefaultAnalytics = cacheSetup(async () => synthAnalyticsUncached());

/**
 * Reuses the immutable default-fixture synthesis and clones its plain JSON
 * result. Branch inputs and freshness checks always synthesize fresh.
 */
async function synthAnalytics(
  options: AnalyticsSynthOptions = {},
): Promise<{ template: Json; manifest: Json }> {
  if (
    options.configFile === undefined &&
    options.synthReadsFile === undefined &&
    options.fresh !== true
  ) {
    return synthDefaultAnalytics();
  }
  return synthAnalyticsUncached(options);
}

/**
 * Builds the stack directly, for the branches driven by something other than the cluster settings.
 * `awsRegion` is the stack's region (the GovCloud switch); the environment stays in the captured
 * region so the VPC lookup still resolves from `cdk.context.json`.
 */
function synthDirect(
  options: { configFile?: string; awsRegion?: string; serviceLinkedRoleExists?: boolean } = {},
): Json {
  const configFile = options.configFile ?? CONFIG_FILE;
  const outdir = join(scratch('direct'), 'out');
  const app = new App({
    context: { 'aws:cdk:enable-path-metadata': true, ...readJson(CONTEXT_FILE) },
    outdir,
  });
  const ctx = makeContext({
    config: ClusterConfig.fromFile(readFileSync(configFile, 'utf8')),
    awsRegion: options.awsRegion ?? REGION,
    moduleId: 'analytics',
    releaseVersion: ideaVersion(),
    synthReads: replaySynthReads(SYNTH_READS),
  });
  new AnalyticsStack(
    {
      app,
      ctx,
      moduleName: 'analytics',
      deploymentId: DEPLOYMENT_ID,
      terminationProtection: true,
      env: { account: fixtureAccount(), region: REGION },
    },
    options.serviceLinkedRoleExists ?? true,
  );
  return readJson(join(app.synth().directory, `${STACK}.template.json`));
}

const resourcesOf = (template: Json): Json =>
  Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );

const pathOf = (resource: Json): string => resource.Metadata?.['aws:cdk:path'] as string;

describe('analytics stack, dev27 shape', () => {
  test('every deployed resource carries the deletion policies the live stack has', async () => {
    const synth = resourcesOf((await synthAnalytics()).template);
    // The deployed side carries this branch's retain policy, which is the one change to these
    // attributes and is itemised in tools/parity/intended-drift.ts. Everything else still has to
    // match resource for resource.
    const live = resourcesOf(withRetainedStateful(readJson(LIVE_TEMPLATE)));
    const policies = (resources: Json) =>
      Object.fromEntries(
        Object.entries(resources).map(([id, r]) => [
          id,
          { DeletionPolicy: (r as Json).DeletionPolicy, UpdateReplacePolicy: (r as Json).UpdateReplacePolicy },
        ]),
      );
    assert.deepEqual(policies(synth), policies(live));
  });

  test('the stateful resources delete on teardown from config, and never on update-replace', async () => {
    // DeletionPolicy is `removal_policy` driven: dev27 configures DESTROY, and a cluster
    // configured RETAIN must get Retain on the same resources. UpdateReplacePolicy is not
    // configurable and is Retain either way, because an update is never the place to lose data.
    const stateful = [
      'analytics367A4110',
      'analyticskinesisstreamEF4F5950',
      'analyticssearchloggroupF6E87433',
      'analyticsapploggroup7CE00917',
      'analyticsslowindexloggroup2F17F009',
    ];
    const dev27 = resourcesOf((await synthAnalytics()).template);
    for (const id of stateful) {
      assert.deepEqual(
        { id, deletion: dev27[id].DeletionPolicy, update: dev27[id].UpdateReplacePolicy },
        { id, deletion: 'Delete', update: 'Retain' },
      );
    }

    const retained = resourcesOf(
      (
        await synthAnalytics({
          configFile: configWith({
            'analytics.opensearch.removal_policy': { S: 'RETAIN' },
            'analytics.kinesis.removal_policy': { S: 'RETAIN' },
            'analytics.opensearch.logging.search_log_removal_policy': { S: 'RETAIN' },
            'analytics.opensearch.logging.app_log_removal_policy': { S: 'RETAIN' },
            'analytics.opensearch.logging.slow_index_log_removal_policy': { S: 'RETAIN' },
          }),
        })
      ).template,
    );
    for (const id of stateful) {
      assert.deepEqual(
        { id, deletion: retained[id].DeletionPolicy, update: retained[id].UpdateReplacePolicy },
        { id, deletion: 'Retain', update: 'Retain' },
      );
    }
  });

  test('the two IAM roles have no deletion policy and the custom resources are Delete/Delete', async () => {
    const resources = resourcesOf((await synthAnalytics()).template);
    for (const id of ['opensearchprivateipsrole8D8E26D8', 'analyticssinklambdarole33D41AE0']) {
      assert.equal(resources[id].DeletionPolicy, undefined, id);
      assert.equal(resources[id].UpdateReplacePolicy, undefined, id);
    }
    for (const id of ['opensearchprivateips', 'dashboardendpoint', 'ideadev27analyticssettings']) {
      assert.equal(resources[id].DeletionPolicy, 'Delete', id);
      assert.equal(resources[id].UpdateReplacePolicy, 'Delete', id);
    }
  });

  test('the AwsCustomResource provider is the shared singleton at the stack root', async () => {
    const resources = resourcesOf((await synthAnalytics()).template);
    const singleton = resources.AWS679f53fac002430cb0da5b7982bd22872D164C4C;
    assert.equal(singleton.Type, 'AWS::Lambda::Function');
    assert.equal(pathOf(singleton), `${STACK}/AWS679f53fac002430cb0da5b7982bd2287/Resource`);
    assert.equal(singleton.Properties.Handler, 'index.handler');
    assert.equal(singleton.Properties.Timeout, 120);
    // One provider serves both the log-group resource policy and the domain access policy.
    const consumers = Object.values(resources).filter(
      (r) =>
        (r as Json).Properties?.ServiceToken?.['Fn::GetAtt']?.[0] ===
        'AWS679f53fac002430cb0da5b7982bd22872D164C4C',
    );
    assert.equal(consumers.length, 2);
    assert.equal(
      pathOf(resources.AWS679f53fac002430cb0da5b7982bd2287ServiceRoleC1EA0FF2),
      `${STACK}/AWS679f53fac002430cb0da5b7982bd2287/ServiceRole/Resource`,
    );
  });
});

describe('analytics stack, manifest entry', () => {
  test('matches the properties the Python app wrote', async () => {
    const { manifest } = await synthAnalytics();
    const expected = readJson(PYTHON_MANIFEST).artifacts[STACK].properties as Json;
    const actual = manifest.artifacts[STACK].properties as Json;

    // Stack tags never reach the template body; a dropped tag is invisible to a template diff.
    assert.deepEqual(actual.tags, expected.tags);
    assert.equal(actual.terminationProtection, expected.terminationProtection);
    assert.equal(actual.templateFile, expected.templateFile);
    assert.equal(actual.requiresBootstrapStackVersion, expected.requiresBootstrapStackVersion);
    assert.equal(actual.bootstrapStackVersionSsmParameter, expected.bootstrapStackVersionSsmParameter);
    assert.equal(actual.assumeRoleArn, expected.assumeRoleArn);
    assert.equal(actual.cloudFormationExecutionRoleArn, expected.cloudFormationExecutionRoleArn);
    assert.deepEqual(actual.lookupRole, expected.lookupRole);
    assert.equal(manifest.artifacts[STACK].environment, readJson(PYTHON_MANIFEST).artifacts[STACK].environment);
  });

  test('custom tags merge under the idea tags', async () => {
    const configFile = configWith({
      'global-settings.custom_tags': { L: [{ S: 'Key=Env,Value=sample' }, { S: 'Key=idea:ModuleId,Value=nope' }] },
    });
    const { manifest } = await synthAnalytics({ configFile });
    const tags = manifest.artifacts[STACK].properties.tags as Json;
    assert.equal(tags.Env, 'sample');
    assert.equal(tags['idea:ModuleId'], 'analytics');
  });
});

describe('analytics stack, L2-generated IAM documents', () => {
  const generated = {
    'ESLogGroupPolicy CustomResourcePolicy':
      'analyticsESLogGroupPolicyc8803cad36e6f020d595fd57136b9a57c0a2206865CustomResourcePolicy7D66AE30',
    'AccessPolicy CustomResourcePolicy': 'analyticsAccessPolicyCustomResourcePolicy9ECE5CA0',
    'sink lambda role DefaultPolicy': 'analyticssinklambdaroleDefaultPolicy74409E1C',
  };

  test('the documents, logical ids and construct paths match the deployed ones', async () => {
    const synth = resourcesOf((await synthAnalytics()).template);
    const live = resourcesOf(readJson(LIVE_TEMPLATE));
    for (const [label, id] of Object.entries(generated)) {
      assert.ok(synth[id] !== undefined, `${label}: ${id} not synthesized`);
      assert.equal(synth[id].Type, 'AWS::IAM::Policy', label);
      assert.deepEqual(synth[id].Properties, live[id].Properties, label);
      assert.equal(pathOf(synth[id]), pathOf(live[id]), label);
      // CDK names the policy after its own logical id and attaches it to exactly one role.
      assert.equal(synth[id].Properties.PolicyName, id, label);
      assert.equal(synth[id].Properties.Roles.length, 1, label);
    }
  });

  test('the kinesis grant keeps CDK action order', async () => {
    const synth = resourcesOf((await synthAnalytics()).template);
    const statements = synth.analyticssinklambdaroleDefaultPolicy74409E1C.Properties.PolicyDocument.Statement;
    assert.equal(statements.length, 1);
    assert.deepEqual(statements[0].Action, [
      'kinesis:DescribeStreamSummary',
      'kinesis:GetRecords',
      'kinesis:GetShardIterator',
      'kinesis:ListShards',
      'kinesis:SubscribeToShard',
      'kinesis:DescribeStream',
      'kinesis:ListStreams',
      'kinesis:DescribeStreamConsumer',
    ]);
    assert.deepEqual(statements[0].Resource, { 'Fn::GetAtt': ['analyticskinesisstreamEF4F5950', 'Arn'] });
  });
});

describe('analytics stack, values regenerated every synth', () => {
  test('the dashboard target group name gets a fresh uuid tail on each synth', async () => {
    const first = resourcesOf((await synthAnalytics({ fresh: true })).template).ideadev27dashboardtargetgroup.Properties.Name;
    const second = resourcesOf((await synthAnalytics({ fresh: true })).template).ideadev27dashboardtargetgroup.Properties.Name;
    // Reproduced, not fixed: a stable name would leave the deployed target group behind on the
    // first upgrade, and the endpoints lambda expects to be re-pointed.
    assert.notEqual(first, second);
    for (const name of [first, second]) {
      assert.equal(name.length, 32);
      assert.match(name, /^idea-dev27-dashboard-[0-9a-f]{8}-[0-9a-f]{2}$/);
    }
    assert.equal(first.slice(0, 29), second.slice(0, 29));
  });

  test('the private-ip custom resource gets a fresh update token on each synth', async () => {
    const token = async () =>
      resourcesOf((await synthAnalytics({ fresh: true })).template).opensearchprivateips.Properties.UpdateToken as string;
    const [first, second] = [await token(), await token()];
    assert.match(first, /^[0-9a-f-]{36}$/);
    assert.notEqual(first, second);
  });
});

describe('analytics stack, service-linked role branch', () => {
  test('dev27 has one, so no ServiceLinkedRole is created', async () => {
    const resources = resourcesOf((await synthAnalytics()).template);
    assert.equal(
      Object.values(resources).filter((r) => (r as Json).Type === 'AWS::IAM::ServiceLinkedRole').length,
      0,
    );
  });

  test('an account with none gets one inside the domain construct, and the domain depends on it', async () => {
    const synthReadsFile = synthReadsWithRoles([]);
    const resources = resourcesOf((await synthAnalytics({ synthReadsFile })).template);
    const entries = Object.entries(resources).filter(
      ([, r]) => (r as Json).Type === 'AWS::IAM::ServiceLinkedRole',
    );
    assert.equal(entries.length, 1);
    const [id, role] = entries[0] as [string, Json];
    assert.equal(pathOf(role), `${STACK}/analytics/${CLUSTER}-es-service-linked-role`);
    assert.equal(role.Properties.AWSServiceName, `es.${DNS_SUFFIX}`);
    // DO NOT CHANGE: AWS matches an existing service-linked role on this description.
    assert.equal(role.Properties.Description, 'Role for ES to access resources in the VPC');
    assert.ok((resources.analytics367A4110.DependsOn as string[]).includes(id));
  });

  test('the probe reads both path prefixes and a role under either one counts', async () => {
    assert.deepEqual(serviceLinkedRolePathPrefixes(DNS_SUFFIX), [
      `/aws-service-role/es.${DNS_SUFFIX}`,
      `/aws-service-role/opensearchservice.${DNS_SUFFIX}`,
    ]);
    const reads = readJson(SYNTH_READS);
    // dev27 answers the first prefix with one role and the second with none.
    assert.equal((reads[listRolesKey(`/aws-service-role/es.${DNS_SUFFIX}`)] as Json[]).length, 1);
    assert.equal((reads[listRolesKey(`/aws-service-role/opensearchservice.${DNS_SUFFIX}`)] as Json[]).length, 0);
  });
});

describe('analytics stack, GovCloud branches', () => {
  test('the kinesis stream loses StreamModeDetails and the event source becomes an L1', () => {
    const resources = resourcesOf(synthDirect({ awsRegion: 'us-gov-west-1' }));
    assert.equal(resources.analyticskinesisstreamEF4F5950.Properties.StreamModeDetails, undefined);
    assert.equal(resources.analyticskinesisstreamEF4F5950.Properties.ShardCount, 2);

    const mapping = resources.analyticssinklambdaeventsource;
    assert.equal(mapping.Type, 'AWS::Lambda::EventSourceMapping');
    assert.equal(pathOf(mapping), `${STACK}/analytics-sink-lambda-event-source`);
    assert.equal(mapping.DeletionPolicy, 'Delete');
    assert.equal(mapping.UpdateReplacePolicy, 'Delete');
    assert.equal(mapping.Properties.BatchSize, 100);
    assert.equal(mapping.Properties.StartingPosition, 'LATEST');
    // `tags=[]` is passed on both sides and CDK drops an empty list, so `Tags` is absent.
    assert.equal(mapping.Properties.Tags, undefined);
    assert.deepEqual(mapping.DependsOn, ['analyticssinklambdaADB37882']);

    // The L2 event source is skipped, so the grant that writes the role's DefaultPolicy never runs.
    assert.equal(resources.analyticssinklambdaroleDefaultPolicy74409E1C, undefined);
    assert.equal(
      resources.analyticssinklambdaKinesisEventSourceideadev27analyticsanalyticskinesisstreamFC71A7F8D93921B0,
      undefined,
    );
  });

  test('a commercial region keeps the L2 mapping and its generated policy', () => {
    const resources = resourcesOf(synthDirect());
    assert.equal(resources.analyticssinklambdaeventsource, undefined);
    assert.ok(resources.analyticssinklambdaroleDefaultPolicy74409E1C !== undefined);
    assert.equal(resources.analyticskinesisstreamEF4F5950.Properties.StreamModeDetails.StreamMode, 'PROVISIONED');
  });

  test('ON_DEMAND drops ShardCount, and anything else is rejected', () => {
    const onDemand = resourcesOf(
      synthDirect({ configFile: configWith({ 'analytics.kinesis.stream_mode': { S: 'ON_DEMAND' } }) }),
    );
    assert.equal(onDemand.analyticskinesisstreamEF4F5950.Properties.ShardCount, undefined);
    assert.equal(onDemand.analyticskinesisstreamEF4F5950.Properties.StreamModeDetails.StreamMode, 'ON_DEMAND');

    const bad = configWith({ 'analytics.kinesis.stream_mode': { S: 'BURST' } });
    assert.throws(() => synthDirect({ configFile: bad }), /PROVISIONED or ON_DEMAND/);
  });
});

describe('analytics stack, use-existing branch', () => {
  test('imports the domain and drops the eight resources the L2 owns', async () => {
    const configFile = configWith({
      'analytics.opensearch.use_existing': { BOOL: true },
      'analytics.opensearch.domain_vpc_endpoint_url': {
        S: readJson(CONFIG_FILE).Items.find((i: Json) => i.key.S === 'analytics.opensearch.domain_endpoint')
          .value.S,
      },
    });
    const resources = resourcesOf((await synthAnalytics({ configFile })).template);

    for (const id of [
      'analytics367A4110',
      'analyticssearchloggroupF6E87433',
      'analyticsapploggroup7CE00917',
      'analyticsslowindexloggroup2F17F009',
      'analyticsAccessPolicy13397FB5',
      'analyticsAccessPolicyCustomResourcePolicy9ECE5CA0',
      'AWS679f53fac002430cb0da5b7982bd22872D164C4C',
      'AWS679f53fac002430cb0da5b7982bd2287ServiceRoleC1EA0FF2',
    ]) {
      assert.equal(resources[id], undefined, id);
    }

    // The node count comes from the live domain, not from analytics.opensearch.data_nodes.
    assert.equal(resources.ideadev27dashboardtargetgroup.Properties.Targets.length, 2);
    assert.equal(resources.opensearchprivateips.Properties.DomainName, 'idea-dev27-analytics');
    // The security group and the kinesis half of the stack are unchanged.
    assert.ok(resources.analyticsopensearchsecuritygroupC59C8839 !== undefined);
    assert.ok(resources.analyticskinesisstreamEF4F5950 !== undefined);
  });
});
