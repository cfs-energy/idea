/**
 * The metrics stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored, and this whole file requires them.
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
import { replaySynthReads } from '../../src/cdk/synth-reads.ts';
import { MetricsStack, buildStack } from '../../src/cdk/stacks/metrics.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { ideaVersion } from '../../src/version.ts';
import { requireFixtures } from '../support/fixtures.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-metrics.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

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

/** Copies the dev27 settings scan, setting the `S` value of each named key (adding it if absent). */
function configWith(overrides: Record<string, string>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = { S: overrides[key] };
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({ key: { S: key }, value: { S: overrides[key] }, version: { N: '1' } });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-metrics-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

async function synthMetrics(configFile: string = CONFIG_FILE): Promise<Json> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-metrics-'));
  workdirs.push(workdir);
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, 'cdk.out.metrics');

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: 'metrics',
        moduleName: 'metrics',
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { metrics: async () => buildStack },
    );
    app.synth();
    return readJson(join(outdir, `${CLUSTER}-metrics.template.json`));
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

/**
 * Constructs the stack without synthesizing, for the branches that throw before any template is
 * written. Going through `buildApp` here would leave an un-synthesized `App` behind whose
 * exit-time autosynth fires after the temp directories are gone.
 */
function construct(configFile: string): MetricsStack {
  const config = ClusterConfig.fromFile(readFileSync(configFile, 'utf8'));
  const ctx = makeContext({
    config,
    awsRegion: REGION,
    moduleId: 'metrics',
    releaseVersion: ideaVersion(),
    synthReads: replaySynthReads(SYNTH_READS),
  });
  return new MetricsStack({
    app: new App({ context: readJson(CONTEXT_FILE) }),
    ctx,
    moduleName: 'metrics',
    deploymentId: DEPLOYMENT_ID,
    terminationProtection: true,
    env: { account: '123456789012', region: REGION },
  });
}

/** Everything but `AWS::CDK::Metadata`, whose Analytics blob differs between the two runtimes. */
function deployedResources(template: Json): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );
}

describe('metrics stack, cloudwatch provider', () => {
  test('matches the deployed dev27 template resource for resource', async () => {
    const template = await synthMetrics();
    assert.deepEqual(deployedResources(template), deployedResources(readJson(LIVE_TEMPLATE)));
  });

  test('the dashboard body is empty and the settings carry its arn', async () => {
    const resources = deployedResources(await synthMetrics());
    const dashboard = resources.cloudwatchdashboard84BE33F2;
    assert.equal(dashboard.Type, 'AWS::CloudWatch::Dashboard');
    assert.equal(dashboard.Properties.DashboardBody, '{"widgets":[]}');
    assert.equal(dashboard.Properties.DashboardName, 'idea-dev27_us-east-2');

    const settings = resources.ideadev27metricssettings;
    assert.equal(settings.Type, 'Custom::ClusterSettings');
    assert.equal(settings.Properties.module_id, 'metrics');
    assert.equal(settings.Properties.settings.deployment_id, DEPLOYMENT_ID);
    assert.deepEqual(Object.keys(settings.Properties.settings), ['deployment_id', 'cloudwatch.dashboard_arn']);
    // The account id lives only in the gitignored fixture, so assert the shape, not the value.
    const arn = settings.Properties.settings['cloudwatch.dashboard_arn']['Fn::Join'];
    assert.equal(arn[0], '');
    assert.equal(arn[1][0], 'arn:');
    assert.deepEqual(arn[1][1], { Ref: 'AWS::Partition' });
    assert.match(arn[1][2], /^:cloudwatch::\d{12}:dashboard\/idea-dev27_us-east-2$/);
  });
});

describe('metrics stack, other providers', () => {
  for (const provider of ['prometheus', 'dogstatsd']) {
    test(`${provider} builds only the cluster settings`, async () => {
      const config = configWith({
        'metrics.provider': provider,
        'metrics.prometheus.remote_write.url': 'https://example.invalid/api/v1/remote_write',
        'metrics.prometheus.query.url': 'https://example.invalid/api/v1/query',
      });
      const resources = deployedResources(await synthMetrics(config));
      assert.deepEqual(Object.keys(resources), ['ideadev27metricssettings']);
      assert.deepEqual(resources.ideadev27metricssettings.Properties.settings, {
        deployment_id: DEPLOYMENT_ID,
      });
    });
  }

  test('amazon_managed_prometheus builds a workspace and four settings keys', async () => {
    const config = configWith({
      'metrics.provider': 'amazon_managed_prometheus',
      'metrics.amazon_managed_prometheus.workspace_name': 'sample-workspace',
    });
    const resources = deployedResources(await synthMetrics(config));
    const workspace = resources.prometheusworkspace;
    assert.equal(workspace.Type, 'AWS::APS::Workspace');
    assert.equal(workspace.Properties.Alias, 'sample-workspace');
    assert.equal(workspace.Metadata['aws:cdk:path'], 'idea-dev27-metrics/prometheus-workspace');
    const settings = resources.ideadev27metricssettings.Properties.settings;
    assert.deepEqual(Object.keys(settings), [
      'deployment_id',
      'amazon_managed_prometheus.workspace_id',
      'amazon_managed_prometheus.workspace_arn',
      'prometheus.remote_write.url',
      'prometheus.remote_read.url',
    ]);
    assert.deepEqual(settings['amazon_managed_prometheus.workspace_id'], {
      'Fn::GetAtt': ['prometheusworkspace', 'WorkspaceId'],
    });
    assert.deepEqual(settings['amazon_managed_prometheus.workspace_arn'], {
      'Fn::GetAtt': ['prometheusworkspace', 'Arn'],
    });
    assert.deepEqual(settings['prometheus.remote_write.url'], {
      'Fn::Join': ['', [{ 'Fn::GetAtt': ['prometheusworkspace', 'PrometheusEndpoint'] }, 'api/v1/remote_write']],
    });
    assert.deepEqual(settings['prometheus.remote_read.url'], {
      'Fn::Join': ['', [{ 'Fn::GetAtt': ['prometheusworkspace', 'PrometheusEndpoint'] }, 'api/v1/query']],
    });
  });

  test('prometheus without its urls fails at synth', () => {
    const missingBoth = configWith({ 'metrics.provider': 'prometheus' });
    assert.throws(() => construct(missingBoth), /metrics\.prometheus\.remote_write\.url/);

    const missingQuery = configWith({
      'metrics.provider': 'prometheus',
      'metrics.prometheus.remote_write.url': 'https://example.invalid/api/v1/remote_write',
    });
    assert.throws(() => construct(missingQuery), /metrics\.prometheus\.query\.url/);
  });

  test('an unknown provider fails at synth', () => {
    const config = configWith({ 'metrics.provider': 'graphite' });
    assert.throws(() => construct(config), /metrics provider: graphite not supported/);
  });
});
