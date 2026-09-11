/** Compare rendered policy templates with Jinja2 output. */

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { policyContext, renderPolicy, resourcesDir } from '../../src/cdk/policy.ts';
import { jinjaEnv, renderTemplate } from '../../src/config/jinja.ts';
import { requireFixtures, requiredService } from '../support/fixtures.ts';

const RAW = fileURLToPath(new URL('../../tools/parity/fixtures/idea-dev27/raw/', import.meta.url));
const SCAN = `${RAW}cluster-settings.scan.json`;
const MODULES = `${RAW}modules.scan.json`;
const ORACLE = fileURLToPath(new URL('./jinja2-oracle.py', import.meta.url));
const ARN_BUILDER = join(resourcesDir(), '..', '..', 'idea-sdk', 'src', 'ideasdk', 'context', 'arn_builder.py');
const POLICIES = join(resourcesDir(), 'policies');

const PY_ENV = { ...process.env, PYTHONPATH: [process.env.W3_JINJA_PATH, process.env.PYTHONPATH].filter(Boolean).join(':') };

function haveJinja2(): boolean {
  try {
    execFileSync('python3', ['-c', 'import jinja2, yaml'], { env: PY_ENV, stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

requireFixtures(
  [SCAN, MODULES, ARN_BUILDER],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);
if (!haveJinja2()) requiredService("python3 with jinja2 and PyYAML", "python3 -m pip install --user jinja2 PyYAML");

/** Synthetic feature flags and distinct key IDs. */
const OVERRIDES: Record<string, unknown> = {
  'cluster.kms.key_type': 'customer-managed',
  'cluster.secretsmanager.kms_key_id': '11111111-1111-1111-1111-111111111111',
  'cluster.sqs.kms_key_id': '22222222-2222-2222-2222-222222222222',
  'cluster.sns.kms_key_id': '33333333-3333-3333-3333-333333333333',
  'cluster.dynamodb.kms_key_id': '44444444-4444-4444-4444-444444444444',
  'cluster.ebs.kms_key_id': '55555555-5555-5555-5555-555555555555',
  'cluster.backups.backup_vault.kms_key_id': '66666666-6666-6666-6666-666666666666',
  'analytics.opensearch.kms_key_id': '77777777-7777-7777-7777-777777777777',
  'analytics.kinesis.kms_key_id': '88888888-8888-8888-8888-888888888888',
  'scheduler.use_stable_server_name': true,
};

/** `SocaAnyPayload` members the constructs pass; the values only have to match on both sides. */
const VARS: Record<string, string> = {
  scheduler_role_arn: '<<scheduler-role>>',
  compute_node_role_arn: '<<compute-node-role>>',
  spot_fleet_request_role_arn: '<<spot-fleet-role>>',
  role_arn: '<<component-role>>',
};

function configWithOverrides(): ClusterConfig {
  const scan = JSON.parse(readFileSync(SCAN, 'utf-8')) as { Items: { key: { S: string }; value: unknown }[] };
  for (const [key, value] of Object.entries(OVERRIDES)) {
    const attribute = typeof value === 'boolean' ? { BOOL: value } : { S: String(value) };
    const item = scan.Items.find((row) => row.key.S === key);
    if (item === undefined) scan.Items.push({ key: { S: key }, value: attribute });
    else item.value = attribute;
  }
  return ClusterConfig.fromFile(JSON.stringify(scan), readFileSync(MODULES, 'utf-8'));
}

interface OracleResult {
  text?: string;
  parsed?: unknown;
  error?: string;
}

function runOracle(templates: string[], moduleId: string): Record<string, OracleResult> {
  return runOracleWith(SCAN, OVERRIDES, templates, moduleId);
}

function runOracleWith(
  scan: string,
  overrides: Record<string, unknown>,
  templates: string[],
  moduleId: string,
): Record<string, OracleResult> {
  const job = JSON.stringify({
    scan,
    overrides,
    policies_dir: POLICIES,
    arn_builder: ARN_BUILDER,
    module_id: moduleId,
    vars: VARS,
    templates,
  });
  const out = execFileSync('python3', [ORACLE], { input: job, env: PY_ENV, maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(out.toString('utf-8')) as Record<string, OracleResult>;
}

describe('renderPolicy vs real Jinja2, with the KMS and stable-name flags on', () => {
  const templates = readdirSync(POLICIES).filter((name) => name.endsWith('.yml')).sort();
  const config = configWithOverrides();
  const moduleId = config.moduleId('scheduler');
  const env = jinjaEnv(POLICIES);

  it('renders every policy template byte-identically', () => {
    const oracle = runOracle(templates, moduleId);
    const matched: string[] = [];
    const failed: string[] = [];
    const bothFailed: string[] = [];
    for (const name of templates) {
      const expected = oracle[name] as OracleResult;
      let text: string | undefined;
      let ourError: string | undefined;
      try {
        text = renderTemplate(env, name, { context: policyContext({ config, moduleId, vars: VARS }) });
      } catch (error) {
        ourError = String(error).split('\n')[0];
      }
      if (expected.error !== undefined) {
        if (ourError === undefined) failed.push(`${name}: Jinja2 raised (${expected.error}) and this port did not`);
        else bothFailed.push(`${name} (${expected.error})`);
        continue;
      }
      if (ourError !== undefined) {
        failed.push(`${name}: this port raised ${ourError} and Jinja2 did not`);
        continue;
      }
      if (text !== expected.text) {
        failed.push(`${name}: first difference at ${firstDiff(text as string, expected.text as string)}`);
        continue;
      }
      // and the parse: js-yaml `load` against PyYAML `safe_load` on the same bytes
      assert.deepStrictEqual(renderPolicy(name, { config, moduleId, vars: VARS }), expected.parsed, `${name}: parsed differently`);
      matched.push(name);
    }
    console.log(`jinja2 differential: matched ${matched.length}/${templates.length}; raised on both sides: ${bothFailed.join(', ') || 'none'}`);
    assert.deepStrictEqual(failed, []);
    assert.ok(matched.length >= templates.length - 1, `too few templates rendered: ${matched.length}`);
  });
});

describe('renderPolicy conditional blocks without an external oracle', () => {
  it('rendered the two blocks that no captured cluster turns on', () => {
    const config = configWithOverrides();
    const moduleId = config.moduleId('scheduler');
    const env = jinjaEnv(POLICIES);
    const context = () => policyContext({ config, moduleId, vars: VARS });
    const kms = renderTemplate(env, 'scheduler.yml', { context: context() });
    assert.ok(kms.includes('kms:GenerateDataKey'), 'custom-kms-key.yml did not render');
    // `ArnBuilder.kms_key_arn` lists the eight service keys; the `kms:DescribeKey` block below it
    // adds the dynamodb one again.
    assert.equal((kms.match(/arn:[a-z-]+:kms:/g) ?? []).length, 9, 'expected the eight service keys plus the dynamodb one');
    assert.equal((kms.match(/key\/44444444-4444-4444-4444-444444444444/g) ?? []).length, 2, 'expected the dynamodb key twice');
    assert.ok(kms.includes('route53:ChangeResourceRecordSets'), 'use_stable_server_name did not render');
  });
});

/** `line N: <ours> | <theirs>` for the first line that differs. */
function firstDiff(ours: string, theirs: string): string {
  const a = ours.split('\n');
  const b = theirs.split('\n');
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if (a[i] !== b[i]) return `line ${i + 1}: ${JSON.stringify(a[i])} | ${JSON.stringify(b[i])}`;
  }
  return 'no line differs (trailing bytes)';
}

/**
 * The record-changing permission is granted by the row or by the container flag. One condition,
 * two engines: the port and real Jinja2 have to agree on each combination, not only on the one a
 * captured cluster happens to hold. A membership test can silently evaluate false under one engine
 * with no error, so every case below is rendered on both sides and compared as bytes.
 */
describe("the scheduler record permission under both engines", () => {
  const STATEMENT = "Sid: SchedulerDnsRecord";
  const ROW = "scheduler.use_stable_server_name";
  const FLAG = "ecs.enabled";

  /** The dev27 scan with named rows removed, written where both engines can read it. */
  function scanWithout(removed: readonly string[]): string {
    const scan = JSON.parse(readFileSync(SCAN, "utf-8")) as { Items: { key: { S: string } }[] };
    scan.Items = scan.Items.filter((item) => !removed.includes(item.key.S));
    const file = join(mkdtempSync(join(tmpdir(), "ideactl-scan-")), "cluster-settings.scan.json");
    writeFileSync(file, JSON.stringify(scan));
    return file;
  }

  // The container user sync's account-table grant is gated on the container flag alone, so each
  // case also states whether that statement renders, and both engines are held to the same answer.
  const USER_SYNC = "Sid: ClusterUserSync";

  const cases: {
    name: string;
    removed: string[];
    overrides: Record<string, unknown>;
    granted: boolean;
    userSync: boolean;
  }[] = [
    { name: "the row on its own", removed: [FLAG], overrides: { [ROW]: true }, granted: true, userSync: false },
    { name: "the flag with no row at all", removed: [ROW], overrides: { [FLAG]: true }, granted: true, userSync: true },
    { name: "the flag with the row explicitly false", removed: [], overrides: { [ROW]: false, [FLAG]: true }, granted: true, userSync: true },
    { name: "neither", removed: [ROW, FLAG], overrides: {}, granted: false, userSync: false },
  ];

  for (const testCase of cases) {
    it(`${testCase.name}: grants the permission = ${testCase.granted}, identically on both sides`, () => {
      const scan = scanWithout(testCase.removed);
      const scanJson = readFileSync(scan, "utf-8");
      const config = ClusterConfig.fromFile(
        JSON.stringify(withOverrides(JSON.parse(scanJson) as ScanShape, testCase.overrides)),
        readFileSync(MODULES, "utf-8"),
      );
      const moduleId = config.moduleId("scheduler");
      const ours = renderTemplate(jinjaEnv(POLICIES), "scheduler.yml", {
        context: policyContext({ config, moduleId, vars: VARS }),
      });
      const theirs = runOracleWith(scan, testCase.overrides, ["scheduler.yml"], moduleId)["scheduler.yml"];

      assert.equal(theirs?.error, undefined, `Jinja2 raised: ${theirs?.error}`);
      assert.equal(ours, theirs?.text, "the two engines rendered different bytes");
      assert.equal(ours.includes(STATEMENT), testCase.granted, `${STATEMENT} presence`);
      assert.equal((theirs?.text as string).includes(STATEMENT), testCase.granted, `${STATEMENT} presence, Jinja2`);
      assert.equal(ours.includes(USER_SYNC), testCase.userSync, `${USER_SYNC} presence`);
      assert.equal((theirs?.text as string).includes(USER_SYNC), testCase.userSync, `${USER_SYNC} presence, Jinja2`);
      rmSync(join(scan, ".."), { force: true, recursive: true });
    });
  }
});

interface ScanShape {
  Items: { key: { S: string }; value?: unknown }[];
}

/** Apply full-key overrides to a decoded scan the way the oracle applies them to its tree. */
function withOverrides(scan: ScanShape, overrides: Record<string, unknown>): ScanShape {
  for (const [key, value] of Object.entries(overrides)) {
    const attribute = typeof value === "boolean" ? { BOOL: value } : { S: String(value) };
    const item = scan.Items.find((row) => row.key.S === key);
    if (item === undefined) scan.Items.push({ key: { S: key }, value: attribute });
    else item.value = attribute;
  }
  return scan;
}
