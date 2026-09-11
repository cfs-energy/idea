// parity.ts: self-diff of every live template, and one mutation per class.
import { deepStrictEqual, match, ok, rejects, strictEqual } from "node:assert";
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, test } from "node:test";
import { guardOracleDirectory } from "../../tools/parity/oracle-guard.ts";
import { compareTemplates, isParity } from "../../tools/parity/parity.ts";
import { requireFixtures } from '../support/fixtures.ts';

type Json = null | boolean | number | string | Json[] | JsonObject;
type JsonObject = { [key: string]: Json };
type Resource = {
  Type?: string;
  Properties: JsonObject;
  Metadata?: JsonObject;
  DependsOn?: Json;
  Condition?: Json;
  DeletionPolicy?: Json;
  UpdateReplacePolicy?: Json;
  CreationPolicy?: Json;
  UpdatePolicy?: Json;
};
type Template = {
  Description?: string;
  Metadata?: JsonObject;
  Parameters?: JsonObject;
  Rules?: JsonObject;
  Conditions?: JsonObject;
  Mappings?: JsonObject;
  Outputs?: JsonObject;
  Resources: Record<string, Resource>;
};

const PKG = resolve(import.meta.dirname, '../..');
const PARITY = join(PKG, 'tools/parity/parity.ts');
const LIVE = join(PKG, 'tools/parity/live');
const REQUIRED_TEMPLATES = [
  "idea-dev27-analytics.json",
  "idea-dev27-bastion-host.json",
  "idea-dev27-bootstrap.json",
  "idea-dev27-cluster-manager.json",
  "idea-dev27-cluster.json",
  "idea-dev27-directoryservice.json",
  "idea-dev27-identity-provider.json",
  "idea-dev27-metrics.json",
  "idea-dev27-scheduler.json",
  "idea-dev27-shared-storage.json",
  "idea-dev27-vdc.json",
] as const;
requireFixtures(
  REQUIRED_TEMPLATES.map((name) => join(LIVE, name)),
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);
const oracleGuard = guardOracleDirectory(LIVE);

function diff(a: string, b: string, ...flags: string[]): { code: number; out: string } {
  const r = spawnSync(process.execPath, [PARITY, 'diff', ...flags, a, b], { encoding: 'utf8' });
  return { code: r.status ?? -1, out: r.stdout + r.stderr };
}

const tmp = mkdtempSync(join(tmpdir(), 'ideactl-parity-'));
after(async () => {
  try {
    await oracleGuard.verifyAndClose();
  } finally {
    rmSync(tmp, { force: true, recursive: true });
  }
});

if (process.env.IDEACTL_TEST_ORACLE_WRITE === "1") {
  test("oracle guard proof writes and removes a file", () => {
    const path = join(LIVE, ".oracle-guard-proof.json");
    try {
      writeFileSync(path, "{}\n");
    } finally {
      rmSync(path, { force: true });
    }
  });
}

function isObject(value: Json): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function parseTemplate(text: string): Template {
  const value: Json = JSON.parse(text);
  if (!isObject(value) || !isObject(value.Resources)) throw new Error('template has no Resources object');
  for (const resource of Object.values(value.Resources)) {
    if (!isObject(resource) || !isObject(resource.Properties)) throw new Error('template resource has no Properties object');
  }
  return value as Template;
}

function resource(template: Template, id: string): Resource {
  const value = template.Resources[id];
  if (!value) throw new Error(`missing resource ${id}`);
  return value;
}

function objectProperty(resourceValue: Resource, name: string): JsonObject {
  const value = resourceValue.Properties[name];
  if (!isObject(value)) throw new Error(`resource property ${name} is not an object`);
  return value;
}

/** Write a mutated copy of a live template and return its path. */
function mutated(name: string, mutate: (template: Template) => void): string {
  const t = parseTemplate(readFileSync(join(LIVE, name), 'utf8'));
  mutate(t);
  const p = join(tmp, `${name}.${Math.random().toString(36).slice(2)}.json`);
  writeFileSync(p, JSON.stringify(t));
  return p;
}

/**
 * Write a minimal synthetic CloudFormation template for matcher-scope regression checks.
 */
function syntheticTemplate(id: string, type: string, properties: JsonObject): Template {
  return {
    Resources: {
      [id]: { Type: type, Properties: properties },
    },
  };
}

/**
 * Persist a synthetic template and return its isolated comparison path.
 */
function writeSyntheticTemplate(name: string, template: Template): string {
  const path = join(tmp, `${name}.${Math.random().toString(36).slice(2)}.json`);
  writeFileSync(path, JSON.stringify(template));
  return path;
}

/**
 * Assert that a volatile class is soft only at its legitimate emission path.
 */
function assertScopedVolatility(
  label: string,
  expectedSoftPrefix: "ASSET" | "VOLATILE",
  expectedHardPath: RegExp,
  softLive: Template,
  softSynth: Template,
  hardLive: Template,
  hardSynth: Template,
): void {
  const soft = diff(
    writeSyntheticTemplate(`${label}-soft-live`, softLive),
    writeSyntheticTemplate(`${label}-soft-synth`, softSynth),
  );
  strictEqual(soft.code, 0, `${label} legitimate path: ${soft.out}`);
  match(soft.out, new RegExp(`^${expectedSoftPrefix} `, "m"));

  const hard = diff(
    writeSyntheticTemplate(`${label}-hard-live`, hardLive),
    writeSyntheticTemplate(`${label}-hard-synth`, hardSynth),
  );
  strictEqual(hard.code, 1, `${label} unrelated path: ${hard.out}`);
  match(hard.out, expectedHardPath);
  match(hard.out, /^MISMATCH  1 live resources, 0 missing, 0 extra, 1 property diffs, 0 soft$/m);
  console.log(`${label}: legitimate=soft unrelated=hard`);
}

test("volatile matchers are limited to their Python emission paths", () => {
  const assetA = `assets/${"a".repeat(64)}.zip`;
  const assetB = `assets/${"b".repeat(64)}.zip`;
  assertScopedVolatility(
    "lambda-asset",
    "ASSET",
    /^DIFF     Resources\.assetemitter\.Properties\.Code\.S3Key$/m,
    syntheticTemplate("assetlambda", "AWS::Lambda::Function", { Code: { S3Key: assetA } }),
    syntheticTemplate("assetlambda", "AWS::Lambda::Function", { Code: { S3Key: assetB } }),
    syntheticTemplate("assetemitter", "Custom::AssetEmitter", { Code: { S3Key: assetA } }),
    syntheticTemplate("assetemitter", "Custom::AssetEmitter", { Code: { S3Key: assetB } }),
  );
  assertScopedVolatility(
    "layer-asset",
    "ASSET",
    /^DIFF     Resources\.layerassetemitter\.Properties\.Code\.S3Key$/m,
    syntheticTemplate("assetlayer", "AWS::Lambda::LayerVersion", { Content: { S3Key: assetA }, Code: { S3Key: assetA } }),
    syntheticTemplate("assetlayer", "AWS::Lambda::LayerVersion", { Content: { S3Key: assetA }, Code: { S3Key: assetB } }),
    syntheticTemplate("layerassetemitter", "Custom::AssetEmitter", { Code: { S3Key: assetA } }),
    syntheticTemplate("layerassetemitter", "Custom::AssetEmitter", { Code: { S3Key: assetB } }),
  );
  assertScopedVolatility(
    "update-token",
    "VOLATILE",
    /^DIFF     Resources\.otherprivateips\.Properties\.UpdateToken$/m,
    syntheticTemplate("opensearchprivateips", "Custom::OpenSearchPrivateIPAddresses", { UpdateToken: "first-token" }),
    syntheticTemplate("opensearchprivateips", "Custom::OpenSearchPrivateIPAddresses", { UpdateToken: "second-token" }),
    syntheticTemplate("otherprivateips", "Custom::OpenSearchPrivateIPAddresses", { UpdateToken: "first-token" }),
    syntheticTemplate("otherprivateips", "Custom::OpenSearchPrivateIPAddresses", { UpdateToken: "second-token" }),
  );
  assertScopedVolatility(
    "deployment-id",
    "VOLATILE",
    /^DIFF     Resources\.unrelatedsettings\.Properties\.settings\.deployment_id$/m,
    syntheticTemplate("settings", "Custom::ClusterSettings", { settings: { deployment_id: "first-deployment-id" } }),
    syntheticTemplate("settings", "Custom::ClusterSettings", { settings: { deployment_id: "second-deployment-id" } }),
    syntheticTemplate("unrelatedsettings", "Custom::OtherSettings", { settings: { deployment_id: "first-deployment-id" } }),
    syntheticTemplate("unrelatedsettings", "Custom::OtherSettings", { settings: { deployment_id: "second-deployment-id" } }),
  );
  assertScopedVolatility(
    "dashboard-name",
    "VOLATILE",
    /^DIFF     Resources\.unrelateddashboardtargetgroup\.Properties\.Name$/m,
    syntheticTemplate("ideatest1dashboardtargetgroup", "AWS::ElasticLoadBalancingV2::TargetGroup", { Name: "idea-test1-dashboard-1234abcd-c7" }),
    syntheticTemplate("ideatest1dashboardtargetgroup", "AWS::ElasticLoadBalancingV2::TargetGroup", { Name: "idea-test1-dashboard-1234abcd-c8" }),
    syntheticTemplate("unrelateddashboardtargetgroup", "AWS::ElasticLoadBalancingV2::TargetGroup", { Name: "idea-test1-dashboard-1234abcd-c7" }),
    syntheticTemplate("unrelateddashboardtargetgroup", "AWS::ElasticLoadBalancingV2::TargetGroup", { Name: "idea-test1-dashboard-1234abcd-c8" }),
  );
});

test('self-diff is PARITY on every live template', () => {
  const lines: string[] = [];
  for (const name of REQUIRED_TEMPLATES) {
    const { code, out } = diff(join(LIVE, name), join(LIVE, name));
    strictEqual(code, 0, `${name}: ${out}`);
    match(out, /^PARITY /m);
    lines.push(`${name}: ${out.trim()}`);
  }
  console.log(lines.join('\n'));
});

test("oracle guard detects a create and remove cycle", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-oracle-guard-"));
  writeFileSync(join(directory, "required.json"), "{}\n");
  const guard = guardOracleDirectory(directory);
  try {
    const temporary = join(directory, "temporary.json");
    writeFileSync(temporary, "{}\n");
    rmSync(temporary);
    await rejects(guard.verifyAndClose(), /Oracle directory changed during test run:[\s\S]*temporary\.json/u);
  } finally {
    guard.close();
    rmSync(directory, { force: true, recursive: true });
  }
});

test('the four volatile classes are soft, all in one template', () => {
  const name = 'idea-dev27-analytics.json';
  const p = mutated(name, (t) => {
    resource(t, 'opensearchprivateips').Properties.UpdateToken = '00000000-1111-2222-3333-444444444444';
    resource(t, 'ideadev27dashboardtargetgroup').Properties.Name = 'idea-dev27-dashboard-2c13f863-9f';
    objectProperty(resource(t, 'ideadev27analyticssettings'), 'settings').deployment_id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
    objectProperty(resource(t, 'analyticssinklambdaADB37882'), 'Code').S3Key = `cdk/${'0'.repeat(64)}.zip`;
  });
  const { code, out } = diff(join(LIVE, name), p);
  strictEqual(code, 0, out);
  match(out, /^PARITY .*, 0 property diffs, 4 soft$/m);
  match(out, /^VOLATILE Resources\.opensearchprivateips\.Properties\.UpdateToken /m);
  match(out, /^VOLATILE Resources\.ideadev27dashboardtargetgroup\.Properties\.Name /m);
  match(out, /^VOLATILE Resources\.ideadev27analyticssettings\.Properties\.settings\.deployment_id /m);
  match(out, /^ASSET .*Resources\.analyticssinklambdaADB37882\.Properties\.Code\.S3Key /m);
});

test('a dashboard target group renamed to another cluster is a hard diff', () => {
  const p = mutated('idea-dev27-analytics.json', (t) => {
    resource(t, 'ideadev27dashboardtargetgroup').Properties.Name = 'idea-other-dashboard-2c13f863-c7';
  });
  const { code, out } = diff(join(LIVE, 'idea-dev27-analytics.json'), p);
  strictEqual(code, 1, out);
  match(out, /^DIFF     Resources\.ideadev27dashboardtargetgroup\.Properties\.Name$/m);
});

test('a changed property is MISMATCH', () => {
  const p = mutated('idea-dev27-metrics.json', (t) => {
    resource(t, 'ideadev27metricssettings').Properties.module_id = 'metrics2';
  });
  const { code, out } = diff(join(LIVE, 'idea-dev27-metrics.json'), p);
  strictEqual(code, 1, out);
  match(out, /^MISMATCH .*1 property diffs/m);
});

test('aws:cdk:path stays a hard diff', () => {
  const p = mutated('idea-dev27-metrics.json', (t) => {
    const metadata = resource(t, 'ideadev27metricssettings').Metadata;
    if (!metadata) throw new Error('metrics settings has no metadata');
    metadata['aws:cdk:path'] = 'idea-dev27-metrics/moved/Default';
  });
  const { code, out } = diff(join(LIVE, 'idea-dev27-metrics.json'), p);
  strictEqual(code, 1, out);
  match(out, /^DIFF     Resources\.ideadev27metricssettings\.Path$/m);
});

test('Parameters.BootstrapVersion and Rules are compared', () => {
  const noParam = mutated('idea-dev27-metrics.json', (t) => {
    if (!t.Parameters) throw new Error('metrics template has no Parameters');
    delete t.Parameters.BootstrapVersion;
  });
  strictEqual(diff(join(LIVE, 'idea-dev27-metrics.json'), noParam).code, 1);
  const noRule = mutated('idea-dev27-metrics.json', (t) => {
    if (!t.Rules) throw new Error('metrics template has no Rules');
    delete t.Rules.CheckBootstrapVersion;
  });
  const r = diff(join(LIVE, 'idea-dev27-metrics.json'), noRule);
  strictEqual(r.code, 1, r.out);
  match(r.out, /^DIFF     Rules\.CheckBootstrapVersion/m);
});

test('Description is compared; --ignore-version masks the version', () => {
  const p = mutated('idea-dev27-metrics.json', (t) => {
    t.Description = 'ModuleId: metrics, Cluster: idea-dev27, Version: 26.10.0';
    resource(t, 'ideadev27metricssettings').Properties.version = '26.10.0';
  });
  const strict = diff(join(LIVE, 'idea-dev27-metrics.json'), p);
  strictEqual(strict.code, 1, strict.out);
  match(strict.out, /^DIFF     Description$/m);
  const masked = diff(join(LIVE, 'idea-dev27-metrics.json'), p, '--ignore-version');
  strictEqual(masked.code, 0, masked.out);
  // ...but a cluster or module change in the Description still fails with --ignore-version.
  const renamed = mutated('idea-dev27-metrics.json', (t) => {
    t.Description = 'ModuleId: metrics2, Cluster: idea-dev27, Version: 26.09.0';
  });
  strictEqual(diff(join(LIVE, 'idea-dev27-metrics.json'), renamed, '--ignore-version').code, 1);
});

test('a missing resource is MISMATCH and bad usage exits 2', () => {
  const p = mutated('idea-dev27-metrics.json', (t) => delete t.Resources.ideadev27metricssettings);
  const { code, out } = diff(join(LIVE, 'idea-dev27-metrics.json'), p);
  strictEqual(code, 1, out);
  match(out, /^MISSING  ideadev27metricssettings /m);
  const usage = spawnSync(process.execPath, [PARITY, 'diff', 'only-one-arg'], { encoding: 'utf8' });
  strictEqual(usage.status, 2);
  const unreadable = spawnSync(process.execPath, [PARITY, 'diff', '/nope.json', '/nope.json'], { encoding: 'utf8' });
  strictEqual(unreadable.status, 2);
});

test('paths prints one row per non-metadata resource', () => {
  const r = spawnSync(process.execPath, [PARITY, 'paths', join(LIVE, 'idea-dev27-metrics.json')], { encoding: 'utf8' });
  strictEqual(r.status, 0);
  const rows = r.stdout.trim().split('\n');
  strictEqual(rows.length, 2);
  ok(rows.every((row) => row.split('\t').length === 3));
  deepStrictEqual(
    rows.map((row) => row.split('\t')[1]).sort(),
    ['cloudwatchdashboard84BE33F2', 'ideadev27metricssettings'].sort(),
  );
});

test('a volatile value DROPPED from the synth side is a hard diff', () => {
  // Masks apply only when a volatile value changes, not when it disappears.
  const cases: Array<[string, (template: Template) => void, RegExp]> = [
    [
      'idea-dev27-analytics.json',
      (t) => delete resource(t, 'opensearchprivateips').Properties.UpdateToken,
      /^DIFF {5}Resources\.opensearchprivateips\.Properties\.UpdateToken$/m,
    ],
    [
      'idea-dev27-metrics.json',
      (t) => delete objectProperty(resource(t, 'ideadev27metricssettings'), 'settings').deployment_id,
      /^DIFF {5}Resources\.ideadev27metricssettings\.Properties\.settings\.deployment_id$/m,
    ],
    [
      'idea-dev27-analytics.json',
      (t) => delete resource(t, 'ideadev27dashboardtargetgroup').Properties.Name,
      /^DIFF {5}Resources\.ideadev27dashboardtargetgroup\.Properties\.Name$/m,
    ],
  ];
  for (const [name, mutate, line] of cases) {
    const { code, out } = diff(join(LIVE, name), mutated(name, mutate));
    strictEqual(code, 1, out);
    match(out, line);
    match(out, /^MISMATCH .*, 1 property diffs, 0 soft$/m);
    console.log(out.trim().split('\n').filter((l) => /^(DIFF|MISMATCH)/.test(l)).join(' | '));
  }
});

test('a volatile path whose value is no longer a plain string is a hard diff', () => {
  const name = 'idea-dev27-analytics.json';
  const p = mutated(name, (t) => {
    resource(t, 'opensearchprivateips').Properties.UpdateToken = { Ref: 'AWS::StackId' };
  });
  const { code, out } = diff(join(LIVE, name), p);
  strictEqual(code, 1, out);
  match(out, /^DIFF {5}Resources\.opensearchprivateips\.Properties\.UpdateToken$/m);
});

test('a missing resource does not suppress the diffs of a sibling with the same prefix', () => {
  const name = 'idea-dev27-metrics.json';
  const p = mutated(name, (t) => {
    const id = 'ideadev27metricssettings';
    t.Resources[`${id}extra`] = structuredClone(resource(t, id));
    objectProperty(resource(t, `${id}extra`), 'settings').cluster_name = 'changed';
  });
  // live has neither; the extra pair must both be reported, not swallowed by a prefix match
  const { code, out } = diff(join(LIVE, name), p);
  strictEqual(code, 1, out);
  match(out, /^EXTRA {4}ideadev27metricssettingsextra/m);
  const { out: back } = diff(p, join(LIVE, name));
  match(back, /^MISSING {2}ideadev27metricssettingsextra/m);
  // and the sibling `ideadev27metricssettings` itself still compares clean
  match(back, /, 0 property diffs, /);
});

test('volatile matchers reject similarly shaped values on a dashboard', () => {
  const name = 'idea-dev27-metrics.json';
  const cases: Array<[string, (dashboard: Resource, value: string) => void, string, string, RegExp]> = [
    ['asset-shaped module_id', (dashboard, value) => (dashboard.Properties.module_id = value), 'a'.repeat(64), 'b'.repeat(64), /Properties\.module_id$/m],
    ['UpdateToken', (dashboard, value) => (dashboard.Properties.UpdateToken = value), 'first-token', 'second-token', /Properties\.UpdateToken$/m],
    [
      'deployment_id',
      (dashboard, value) => {
        dashboard.Properties.settings = { deployment_id: value };
      },
      'first-deployment-id',
      'second-deployment-id',
      /Properties\.settings\.deployment_id$/m,
    ],
    [
      'dashboard-shaped Name',
      (dashboard, value) => (dashboard.Properties.Name = value),
      'idea-test1-dashboard-1234abcd-c7',
      'idea-test1-dashboard-1234abcd-c8',
      /Properties\.Name$/m,
    ],
  ];
  for (const [label, mutate, liveValue, synthValue, expectedPath] of cases) {
    const live = mutated(name, (template) => mutate(resource(template, 'cloudwatchdashboard84BE33F2'), liveValue));
    const synth = mutated(name, (template) => mutate(resource(template, 'cloudwatchdashboard84BE33F2'), synthValue));
    const result = diff(live, synth);
    strictEqual(result.code, 1, `${label}: ${result.out}`);
    match(result.out, expectedPath);
    match(result.out, /^MISMATCH .*, 1 property diffs, 0 soft$/m);
  }
});

test('a dashboard-shaped Name on another target group is a hard diff', () => {
  const name = 'idea-dev27-metrics.json';
  const addTargetGroup = (template: Template, targetGroupName: string) => {
    template.Resources.unrelateddashboardtargetgroup = {
      Type: 'AWS::ElasticLoadBalancingV2::TargetGroup',
      Properties: { Name: targetGroupName },
    };
  };
  const live = mutated(name, (template) => addTargetGroup(template, 'idea-test1-dashboard-1234abcd-c7'));
  const synth = mutated(name, (template) => addTargetGroup(template, 'idea-test1-dashboard-1234abcd-c8'));
  const result = diff(live, synth);
  strictEqual(result.code, 1, result.out);
  match(result.out, /^DIFF     Resources\.unrelateddashboardtargetgroup\.Properties\.Name$/m);
  match(result.out, /^MISMATCH .*, 1 property diffs, 0 soft$/m);
});

test('--ignore-version preserves a non-version description suffix', () => {
  const p = mutated('idea-dev27-metrics.json', (template) => {
    template.Description = 'ModuleId: metrics, Cluster: idea-dev27, Version: 26.09.0, unexpected suffix';
  });
  const result = diff(join(LIVE, 'idea-dev27-metrics.json'), p, '--ignore-version');
  strictEqual(result.code, 1, result.out);
  match(result.out, /^DIFF     Description$/m);
});

test('cdk_nag suppressions are compared; the rest of Metadata stays ignored', () => {
  const name = 'idea-dev27-identity-provider.json';
  const live = join(LIVE, name);
  const nagOf = (resourceValue: Resource): JsonObject => {
    const nag = resourceValue.Metadata?.cdk_nag ?? null;
    if (!isObject(nag)) throw new Error('resource carries no cdk_nag metadata');
    return nag;
  };

  // One rule dropped from one resource: the deployed user pool would show a Metadata diff.
  const oneRule = mutated(name, (t) => {
    const rules = nagOf(resource(t, 'ideadev27userpoolD5C370B5')).rules_to_suppress;
    if (!Array.isArray(rules)) throw new Error('rules_to_suppress is not an array');
    rules.pop();
  });
  const dropped = diff(live, oneRule);
  strictEqual(dropped.code, 1, dropped.out);
  match(dropped.out, /^DIFF     Resources\.ideadev27userpoolD5C370B5\.Metadata\.cdk_nag\.rules_to_suppress\[1\]$/m);

  // The whole block gone from every resource.
  const noNag = mutated(name, (t) => {
    for (const resourceValue of Object.values(t.Resources)) delete resourceValue.Metadata?.cdk_nag;
  });
  const stripped = diff(live, noNag);
  strictEqual(stripped.code, 1, stripped.out);
  match(stripped.out, /^MISMATCH .*, 5 property diffs, 0 soft$/m);

  // Order is part of the comparison: the duplicated AwsSolutions-L1 pair on every IDEA lambda.
  const reordered = mutated(name, (t) => {
    const rules = nagOf(resource(t, 'idtokenclaim18B64AB5')).rules_to_suppress;
    if (!Array.isArray(rules)) throw new Error('rules_to_suppress is not an array');
    rules.reverse();
  });
  strictEqual(diff(live, reordered).code, 1);

  // ...and the synthesis noise under Metadata still compares clean.
  const assetName = 'idea-dev27-analytics.json';
  const assetMetadata = mutated(assetName, (t) => {
    const metadata = resource(t, 'analyticssinklambdaADB37882').Metadata;
    if (!metadata || metadata['aws:asset:path'] === undefined) throw new Error('no asset metadata to mutate');
    metadata['aws:asset:path'] = 'asset.0000000000000000000000000000000000000000000000000000000000000000';
    metadata['aws:asset:is-bundled'] = true;
  });
  const asset = diff(join(LIVE, assetName), assetMetadata);
  strictEqual(asset.code, 0, asset.out);
});

test('the stack-level suppression list is compared', () => {
  const name = 'idea-dev27-analytics.json';
  const live = join(LIVE, name);
  const withoutStackNag = mutated(name, (t) => {
    if (t.Metadata?.cdk_nag === undefined) throw new Error('analytics has no stack-level cdk_nag');
    delete t.Metadata.cdk_nag;
  });
  const result = diff(live, withoutStackNag);
  strictEqual(result.code, 1, result.out);
  match(result.out, /^DIFF     Metadata\.cdk_nag$/m);
});

/**
 * Builds a template carrying every section and resource key the comparator copies, so one
 * mutation per section proves that section is still walked.
 */
function sectionedTemplate(): Template {
  return {
    Conditions: { IsProduction: { 'Fn::Equals': ['production', 'production'] } },
    Mappings: { Regions: { 'us-east-2': { ami: 'ami-0123456789abcdef0' } } },
    Outputs: { SettingsId: { Value: 'sample-cluster-metrics-settings' } },
    Resources: {
      keeper: {
        Type: 'AWS::S3::Bucket',
        Properties: { BucketName: 'sample-cluster-bucket' },
        DependsOn: ['another', 'other'],
        Condition: 'IsProduction',
        DeletionPolicy: 'Retain',
        UpdateReplacePolicy: 'Retain',
        CreationPolicy: { ResourceSignal: { Count: 1 } },
        UpdatePolicy: { AutoScalingRollingUpdate: { MaxBatchSize: 1 } },
      },
      another: { Type: 'AWS::S3::Bucket', Properties: {} },
      other: { Type: 'AWS::S3::Bucket', Properties: {} },
    },
  };
}

/** The one resource in `sectionedTemplate` that carries the resource-level keys. */
function keeper(template: Template): Resource {
  const value = template.Resources.keeper;
  if (!value) throw new Error('sectioned template has no keeper resource');
  return value;
}

test('every compared section and resource key fails on a change', () => {
  // A section the comparator stops walking cannot report a difference. One case per section that
  // no other test mutates.
  const cases: Array<[string, (template: Template) => void, RegExp]> = [
    [
      'Outputs value',
      (t) => {
        t.Outputs = { SettingsId: { Value: 'sample-cluster-metrics-settings-next' } };
      },
      /^DIFF {5}Outputs\.SettingsId\.Value$/m,
    ],
    [
      'Outputs section dropped',
      (t) => delete t.Outputs,
      /^DIFF {5}Outputs\.SettingsId$/m,
    ],
    [
      'Conditions',
      (t) => {
        t.Conditions = { IsProduction: { 'Fn::Equals': ['production', 'development'] } };
      },
      /^DIFF {5}Conditions\.IsProduction\.Fn::Equals\[1\]$/m,
    ],
    [
      'Mappings',
      (t) => {
        t.Mappings = { Regions: { 'us-east-2': { ami: 'ami-0fedcba9876543210' } } };
      },
      /^DIFF {5}Mappings\.Regions\.us-east-2\.ami$/m,
    ],
    [
      'DeletionPolicy',
      (t) => {
        keeper(t).DeletionPolicy = 'Delete';
      },
      /^DIFF {5}Resources\.keeper\.DeletionPolicy$/m,
    ],
    [
      'UpdateReplacePolicy',
      (t) => {
        keeper(t).UpdateReplacePolicy = 'Delete';
      },
      /^DIFF {5}Resources\.keeper\.UpdateReplacePolicy$/m,
    ],
    [
      'CreationPolicy',
      (t) => {
        keeper(t).CreationPolicy = { ResourceSignal: { Count: 2 } };
      },
      /^DIFF {5}Resources\.keeper\.CreationPolicy\.ResourceSignal\.Count$/m,
    ],
    [
      'UpdatePolicy',
      (t) => {
        keeper(t).UpdatePolicy = { AutoScalingRollingUpdate: { MaxBatchSize: 2 } };
      },
      /^DIFF {5}Resources\.keeper\.UpdatePolicy\.AutoScalingRollingUpdate\.MaxBatchSize$/m,
    ],
    [
      'Condition',
      (t) => {
        keeper(t).Condition = 'IsDevelopment';
      },
      /^DIFF {5}Resources\.keeper\.Condition$/m,
    ],
    [
      'DependsOn',
      (t) => {
        keeper(t).DependsOn = ['changed', 'other'];
      },
      /^DIFF {5}Resources\.keeper\.DependsOn\[0\]$/m,
    ],
  ];

  for (const [label, mutate, line] of cases) {
    const synth = sectionedTemplate();
    mutate(synth);
    const result = diff(
      writeSyntheticTemplate(`section-live-${label}`, sectionedTemplate()),
      writeSyntheticTemplate(`section-synth-${label}`, synth),
    );
    strictEqual(result.code, 1, `${label}: ${result.out}`);
    match(result.out, line);
    match(result.out, /^MISMATCH  3 live resources, 0 missing, 0 extra, 1 property diffs, 0 soft$/m);
  }

  // Positive control: the one list the comparator sorts on purpose still compares clean.
  const reordered = sectionedTemplate();
  keeper(reordered).DependsOn = ['other', 'another'];
  const sorted = diff(
    writeSyntheticTemplate('section-live-depends-order', sectionedTemplate()),
    writeSyntheticTemplate('section-synth-depends-order', reordered),
  );
  strictEqual(sorted.code, 0, sorted.out);
});

// The reference implementation emits two role policy lists through a set-to-list conversion in a
// language that randomizes string hashing per process, so the deployed order is a permutation
// picked at deploy time. All three captured clusters are the same released version and their orders
// disagree, and no hash seed reproduces all three. A permutation grants nothing different and
// replaces nothing, so the gate compares this one property by membership. Membership stays strict.
const roleTemplate = (policies: readonly string[]): JsonObject => ({
  Resources: {
    schedulerrole: {
      Type: "AWS::IAM::Role",
      Properties: {
        ManagedPolicyArns: [...policies],
        RoleName: "sample-scheduler-role",
      },
    },
  },
});

const AGENT = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy";
const SSM = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore";
const READONLY = "arn:aws:iam::aws:policy/ReadOnlyAccess";

test("a managed policy list in a different order is parity", () => {
  const report = compareTemplates(roleTemplate([AGENT, SSM]), roleTemplate([SSM, AGENT]));
  deepStrictEqual(report.hard, []);
  deepStrictEqual(report.soft, []);
  strictEqual(isParity(report), true);
});

test("a managed policy swapped for a different one is not parity", () => {
  const report = compareTemplates(roleTemplate([AGENT, SSM]), roleTemplate([AGENT, READONLY]));
  strictEqual(isParity(report), false);
  ok(report.hard.length > 0, JSON.stringify(report.hard));
});

test("a managed policy added is not parity", () => {
  const report = compareTemplates(roleTemplate([AGENT, SSM]), roleTemplate([AGENT, SSM, READONLY]));
  strictEqual(isParity(report), false);
});

test("a managed policy removed is not parity", () => {
  const report = compareTemplates(roleTemplate([AGENT, SSM]), roleTemplate([AGENT]));
  strictEqual(isParity(report), false);
});

test("the membership exemption does not leak to other properties of the same role", () => {
  const live = roleTemplate([AGENT, SSM]);
  const synth = roleTemplate([SSM, AGENT]);
  // Same policy set, different role name: the name must still fail.
  const synthRoles = synth.Resources as Record<string, { Properties: Record<string, string> }>;
  synthRoles.schedulerrole.Properties.RoleName = "sample-scheduler-role-renamed";
  const report = compareTemplates(live, synth);
  strictEqual(isParity(report), false);
  ok(
    report.hard.some((d) => d.path.endsWith(".RoleName")),
    JSON.stringify(report.hard),
  );
});

test("the membership exemption does not apply to a resource that is not a role", () => {
  const live: JsonObject = { Resources: { thing: { Type: "AWS::S3::Bucket", Properties: { ManagedPolicyArns: [AGENT, SSM] } } } };
  const synth: JsonObject = { Resources: { thing: { Type: "AWS::S3::Bucket", Properties: { ManagedPolicyArns: [SSM, AGENT] } } } };
  strictEqual(isParity(compareTemplates(live, synth)), false);
});
