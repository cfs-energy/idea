/**
 * The scheduler oracle gate.
 *
 * The recorded dev27 template stays the oracle. It is compared against a synthesis driven by the
 * settings it was generated from, and the branch change the recorded template predates is proved
 * as an itemised delta.
 *
 * This suite exists to prove the gate can fail. It states the two branch changes here, applied to
 * the recorded template by hand, rather than reusing the checker's own reverts: if the itemisation
 * in tools/parity/intended-drift.ts and the change the stack actually makes ever disagree, one of
 * these tests and the harness run below cannot both pass.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import { checkIntendedDrift, intendedDriftFor, settingsLookup } from '../../tools/parity/intended-drift.ts';
import { loadTemplate, type Json, type JsonObject } from '../../tools/parity/parity.ts';
import { requireFixtures } from '../support/fixtures.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SYNTH = join(PKG, 'tools', 'parity', 'synth.ts');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-scheduler.json');

const CLUSTER = 'idea-dev27';
const MODULE_ID = 'scheduler';

requireFixtures(
  [CONFIG_FILE, LIVE_TEMPLATE],
  'node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2',
);

// The settings-gated entry on its own, which is what every case below exercises.
const drift = intendedDriftFor(CLUSTER, MODULE_ID);
if (drift === undefined) throw new Error(`tools/parity/intended-drift.ts has no ${CLUSTER} ${MODULE_ID} entry`);

// The whole entry the gate uses, which adds the unconditional retain differences derived from the
// recorded template. `recorded()` is defined below, so this is resolved lazily.
const fullDrift = (): NonNullable<ReturnType<typeof intendedDriftFor>> => {
  const entry = intendedDriftFor(CLUSTER, MODULE_ID, recorded());
  assert.ok(entry !== undefined);
  return entry;
};

const settings = settingsLookup(CONFIG_FILE);
const setting = (key: string): string => {
  const value = settings(key);
  assert.ok(value !== undefined, `the dev27 settings have no ${key}`);
  return value;
};

const isObject = (value: Json | undefined): value is JsonObject =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function resources(template: JsonObject): Record<string, JsonObject> {
  assert.ok(isObject(template.Resources), 'template has no Resources');
  return template.Resources as Record<string, JsonObject>;
}

/** The emitted settings rows of the stack's one settings custom resource. */
function emittedSettings(template: JsonObject): JsonObject {
  const found = Object.values(resources(template)).find((resource) => resource.Type === 'Custom::ClusterSettings');
  assert.ok(found !== undefined && isObject(found.Properties) && isObject(found.Properties.settings), 'no settings rows');
  return found.Properties.settings as JsonObject;
}

/** The statements of the scheduler role's inline policy, the longest document in the stack. */
function schedulerPolicy(template: JsonObject): Json[] {
  const documents = Object.values(resources(template)).flatMap((resource) => {
    if (!isObject(resource.Properties) || !isObject(resource.Properties.PolicyDocument)) return [];
    const statements = (resource.Properties.PolicyDocument as JsonObject).Statement;
    return Array.isArray(statements) ? [statements] : [];
  });
  assert.ok(documents.length > 0, 'no inline policy documents');
  return documents.sort((a, b) => b.length - a.length)[0]!;
}

/** The statement the branch inserts, stated independently of the checker. */
const dnsRecordStatement = (): JsonObject => ({
  Action: 'route53:ChangeResourceRecordSets',
  Effect: 'Allow',
  Resource: `arn:${setting('cluster.aws.partition')}:route53:::hostedzone/*`,
  Sid: 'SchedulerDnsRecord',
});

const recorded = (): JsonObject => loadTemplate(LIVE_TEMPLATE);

/** The recorded template with both branch changes applied, at the index the policy source gives. */
function asCaptured(index = 26): JsonObject {
  const template = recorded();
  emittedSettings(template).private_dns_name = setting('scheduler.hostname');
  schedulerPolicy(template).splice(index, 0, dnsRecordStatement());
  return template;
}

const check = (captured: JsonObject, deployedInputSynth: JsonObject = recorded()): string[] =>
  checkIntendedDrift({ drift, deployed: recorded(), asCaptured: captured, deployedInputSynth, settings });

/** One line matching, with the whole result in the message so a failure is readable. */
function assertFailure(failures: string[], pattern: RegExp): void {
  assert.ok(
    failures.some((failure) => pattern.test(failure)),
    `no failure matched ${pattern}\n${failures.join('\n') || '(the check passed)'}`,
  );
}

describe('scheduler oracle gate', () => {
  test('the documented harness invocation passes', () => {
    const run = spawnSync(process.execPath, [SYNTH, '--cluster', CLUSTER, '--stack', MODULE_ID, '--ignore-version'], {
      encoding: 'utf8',
    });
    const output = `${run.stdout}${run.stderr}`;
    assert.equal(run.status, 0, output);
    assert.match(output, /^PARITY {2}33 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/m, output);
    const expected = fullDrift();
    assert.match(output, new RegExp(`^DRIFT OK {2}${expected.differences.length} itemised differences`, 'm'), output);
    // The accepted differences are named in the run, not left to be remembered. Both causes: the
    // settings-gated one and the retain policy this branch puts on every stateful resource.
    for (const difference of expected.differences) assert.ok(output.includes(difference.path), difference.path);
    assert.ok(expected.differences.length > drift.differences.length, 'the retain differences are missing from the entry');
  });

  test('the itemised set accepts exactly the recorded drift', () => {
    assert.deepEqual(check(asCaptured()), []);
    assert.equal(drift.differences.length, 8);
  });

  test('a port regression against the recorded template fails', () => {
    // The half of the gate that proves the port: the deployed-input synthesis has no tolerance.
    const regressed = recorded();
    resources(regressed).schedulerinstance!.Properties = {
      ...(resources(regressed).schedulerinstance!.Properties as JsonObject),
      InstanceType: 'm5.24xlarge',
    };
    assertFailure(
      check(asCaptured(), regressed),
      /deployed-input synthesis differs from the recorded template: Resources\.schedulerinstance\.Properties\.InstanceType/,
    );
  });

  test('a ninth difference fails', () => {
    const captured = asCaptured();
    resources(captured).schedulerinstance!.Properties = {
      ...(resources(captured).schedulerinstance!.Properties as JsonObject),
      InstanceType: 'm5.24xlarge',
    };
    assertFailure(check(captured), /unexpected difference, not in the itemised set: Resources\.schedulerinstance\.Properties\.InstanceType/);
  });

  test('one of the eight changing shape fails, with the reported paths unchanged', () => {
    const captured = asCaptured();
    const statement = schedulerPolicy(captured)[26] as JsonObject;
    statement.Resource = `arn:${setting('cluster.aws.partition')}:route53:::hostedzone/*/rrset/*`;
    // The path set is untouched, so only the recorded shape of the cause catches this.
    assert.deepEqual(
      check(captured).filter((failure) => /unexpected difference|no longer produced/.test(failure)),
      [],
    );
    assertFailure(check(captured), /^dns-record-statement: .*statement SchedulerDnsRecord is .*rrset/);
  });

  test('the other change pointing at a different name fails the same way', () => {
    const captured = asCaptured();
    emittedSettings(captured).private_dns_name = 'scheduler.example.invalid';
    assertFailure(check(captured), /^stable-name-setting: .*settings\.private_dns_name is "scheduler\.example\.invalid"/);
  });

  test('an itemised difference that is no longer produced fails', () => {
    const captured = asCaptured();
    schedulerPolicy(captured).splice(26, 1);
    const failures = check(captured);
    for (const path of ['Statement[26].Sid', 'Statement[27].Action', 'Statement[28]']) {
      assertFailure(failures, new RegExp(`itemised difference no longer produced, remove the entry: .*${path.replace(/[[\]]/g, '\\$&')}$`));
    }
  });

  test('moving the inserted statement fails', () => {
    // Splicing it out again reconstructs the deployed-input template whatever index it sat at, so
    // position is held by the recorded index and by the shifted statements the move exposes. The
    // eight declared paths still differ, which is why the recorded index has to be checked too.
    const failures = check(asCaptured(10));
    assertFailure(failures, /^dns-record-statement: .*SchedulerDnsRecord at index 10, expected 26/);
    assertFailure(failures, /unexpected difference, not in the itemised set: .*Statement\[26\]\.Condition/);
  });
});
