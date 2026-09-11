// Stacks whose recorded reference template predates a gated change that this branch already has.
//
// The reference template stays the oracle. It is compared with no tolerance at all, against a
// synthesis driven by the settings the reference template was generated from: `deployedInput`
// names the rows to put back and why. That comparison proves the port.
//
// The change the branch adds is then proved separately, as a delta rather than as an allowance.
// The gate requires that a synthesis from the settings as captured differs from the reference
// template in exactly the itemised `differences` and in nothing else, and that undoing every
// `cause` turns it back into the deployed-input synthesis property for property. So a ninth
// difference fails, one of the itemised differences changing shape fails, and an entry that has
// stopped applying fails instead of rotting in place.
//
// Nothing here is an ignore list. There is no code path in which a difference is accepted
// without being named, valued and reverted.

import { readFileSync, writeFileSync } from 'node:fs';

import { isStatefulType } from '../../src/cdk/stateful.ts';
import { compareTemplates, formatValue, isParity, type Json, type JsonObject } from './parity.ts';

/** Reads one string setting out of the captured table dump. */
export type SettingsLookup = (key: string) => string | undefined;

export interface IntendedDifference {
  /** The path the plain comparison reports for this difference. */
  path: string;
  /** Which recorded cause produces it. */
  cause: string;
  /** Why this difference exists. */
  reason: string;
  /** The condition under which this entry is removed. */
  removeWhen: string;
}

interface DriftCause {
  id: string;
  /** What the branch adds, in words. */
  change: string;
  /**
   * True when the branch produces this difference from any settings, so both syntheses carry it.
   * An unconditional cause is undone on the deployed-input synthesis as well, before that
   * synthesis is held against the recorded template with no tolerance. A cause without this flag
   * is gated on a settings row and appears only in the as-captured synthesis.
   */
  unconditional?: boolean;
  /**
   * Undoes this cause in the as-captured template, in place. Returns a failure for every way
   * the template does not carry the recorded shape, so a changed value cannot pass by being
   * reverted to whatever it happens to be.
   */
  revert(asCaptured: JsonObject, settings: SettingsLookup): string[];
}

export interface IntendedDrift {
  cluster: string;
  stack: string;
  /** One line naming the branch change the reference template predates. */
  summary: string;
  /** Settings rows put back to the values the reference template was generated from. */
  deployedInput: Record<string, boolean | string>;
  /** Why those rows differ from the capture. */
  deployedInputReason: string;
  /** What has to happen for this whole entry to be deleted. */
  endsWhen: string;
  differences: IntendedDifference[];
  causes: DriftCause[];
}

function isObject(value: Json | undefined): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** The stack's single settings custom resource, whose properties carry the emitted rows. */
function clusterSettings(template: JsonObject): { id: string; settings: JsonObject } | string {
  const resources = isObject(template.Resources) ? template.Resources : {};
  const found = Object.entries(resources).filter(
    ([, resource]) => isObject(resource) && resource.Type === 'Custom::ClusterSettings',
  );
  if (found.length !== 1) return `expected one Custom::ClusterSettings resource, found ${found.length}`;
  const [id, resource] = found[0] as [string, JsonObject];
  const properties = isObject(resource.Properties) ? resource.Properties : undefined;
  const settings = properties !== undefined && isObject(properties.settings) ? properties.settings : undefined;
  if (settings === undefined) return `${id} has no settings properties`;
  return { id, settings };
}

/** Every inline policy document in the template, by logical id. */
function policyStatements(template: JsonObject): Array<{ id: string; statements: Json[] }> {
  const resources = isObject(template.Resources) ? template.Resources : {};
  const out: Array<{ id: string; statements: Json[] }> = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource)) continue;
    const properties = isObject(resource.Properties) ? resource.Properties : undefined;
    const document = properties !== undefined && isObject(properties.PolicyDocument) ? properties.PolicyDocument : undefined;
    if (document === undefined || !Array.isArray(document.Statement)) continue;
    out.push({ id, statements: document.Statement });
  }
  return out;
}

/**
 * The instance attribute the settings row carried before the stable name.
 * Declared rather than read from the other side: reading it would accept any value.
 */
const INSTANCE_PRIVATE_DNS: Json = { 'Fn::GetAtt': ['schedulerinstance', 'PrivateDnsName'] };

const DNS_RECORD_SID = 'SchedulerDnsRecord';
/** The position the policy source puts the statement at, before the directory service includes. */
const DNS_RECORD_INDEX = 26;

const stableNameSetting: DriftCause = {
  id: 'stable-name-setting',
  change: 'the emitted private_dns_name row is the configured scheduler hostname rather than the instance attribute',
  revert(asCaptured, settings) {
    const found = clusterSettings(asCaptured);
    if (typeof found === 'string') return [found];
    const hostname = settings('scheduler.hostname');
    if (hostname === undefined) return ['the captured settings have no scheduler.hostname row'];
    const actual = found.settings.private_dns_name;
    const failures =
      actual === hostname
        ? []
        : [
            `${found.id} settings.private_dns_name is ${formatValue(actual)}, expected the scheduler.hostname value ${formatValue(hostname)}`,
          ];
    found.settings.private_dns_name = structuredClone(INSTANCE_PRIVATE_DNS);
    return failures;
  },
};

const dnsRecordStatement: DriftCause = {
  id: 'dns-record-statement',
  change: `one ${DNS_RECORD_SID} statement is inserted into the scheduler policy at index ${DNS_RECORD_INDEX}`,
  revert(asCaptured, settings) {
    const partition = settings('cluster.aws.partition');
    if (partition === undefined) return ['the captured settings have no cluster.aws.partition row'];
    const expected: Json = {
      Action: 'route53:ChangeResourceRecordSets',
      Effect: 'Allow',
      Resource: `arn:${partition}:route53:::hostedzone/*`,
      Sid: DNS_RECORD_SID,
    };
    const carriers = policyStatements(asCaptured)
      .map(({ id, statements }) => ({
        id,
        statements,
        indexes: statements.flatMap((statement, index) => (isObject(statement) && statement.Sid === DNS_RECORD_SID ? [index] : [])),
      }))
      .filter((carrier) => carrier.indexes.length > 0);
    if (carriers.length !== 1) return [`expected one policy carrying a ${DNS_RECORD_SID} statement, found ${carriers.length}`];
    const carrier = carriers[0]!;
    if (carrier.indexes.length !== 1) {
      return [`${carrier.id} carries ${carrier.indexes.length} ${DNS_RECORD_SID} statements, expected one`];
    }
    const index = carrier.indexes[0]!;
    const failures: string[] = [];
    if (index !== DNS_RECORD_INDEX) {
      failures.push(`${carrier.id} has ${DNS_RECORD_SID} at index ${index}, expected ${DNS_RECORD_INDEX}`);
    }
    const statement = carrier.statements[index];
    if (JSON.stringify(statement) !== JSON.stringify(expected)) {
      failures.push(`${carrier.id} statement ${DNS_RECORD_SID} is ${formatValue(statement)}, expected ${formatValue(expected)}`);
    }
    carrier.statements.splice(index, 1);
    return failures;
  },
};

const POLICY = 'Resources.schedulerpolicyFF65A604.Properties.PolicyDocument.Statement';
const SHIFTED_BY_INSERTION =
  'the statement it names keeps its content and moves down one index, because the inserted statement sits before the directory service includes in the policy source';
const REMOVE_ON_DEPLOY = 'the scheduler stack is deployed to this cluster, which makes the recorded template current again';

const SCHEDULER_DRIFT: IntendedDrift = {
  cluster: 'idea-dev27',
  stack: 'scheduler',
  summary: 'scheduler.use_stable_server_name is on in the captured settings and the recorded template predates it',
  deployedInput: { 'scheduler.use_stable_server_name': false },
  deployedInputReason:
    'the row was pre-staged in this cluster settings table but the scheduler stack was never redeployed, so the recorded template is a generation behind the table it was captured with',
  endsWhen: REMOVE_ON_DEPLOY,
  differences: [
    {
      path: 'Resources.ideadev27schedulersettings.Properties.settings.private_dns_name',
      cause: stableNameSetting.id,
      reason:
        'with the flag on the stack writes the configured scheduler hostname instead of the instance private DNS attribute, so execution hosts keep one server name across a scheduler replacement',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Action`,
      cause: dnsRecordStatement.id,
      reason: 'the inserted statement grants the record change the container scheduler path performs when a replacement task starts',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Resource`,
      cause: dnsRecordStatement.id,
      reason: 'the inserted statement is scoped to hosted zone record changes in this partition',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Sid`,
      cause: dnsRecordStatement.id,
      reason: `the inserted statement is identified as ${DNS_RECORD_SID}`,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Action`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Resource`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Sid`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[28]`,
      cause: dnsRecordStatement.id,
      reason: `${SHIFTED_BY_INSERTION}, and this is the last statement, so the recorded template has no index 28 at all`,
      removeWhen: REMOVE_ON_DEPLOY,
    },
  ],
  causes: [stableNameSetting, dnsRecordStatement],
};

const DRIFT: IntendedDrift[] = [SCHEDULER_DRIFT];

const RETAIN_CAUSE = 'retain-stateful-on-update-replace';

/**
 * Every resource in the recorded template that this branch now marks `UpdateReplacePolicy: Retain`,
 * with the value the recorded template carries at that path.
 *
 * Read from the recorded template, which is the oracle, and never from the synthesis. A resource
 * the synthesis marks and this list does not know about shows up as an unexpected difference, and
 * one this list expects and the synthesis does not mark shows up as a difference no longer
 * produced. Both fail.
 */
function retainedInThisBranch(deployed: JsonObject): Array<[string, Json | undefined]> {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const out: Array<[string, Json | undefined]> = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource) || typeof resource.Type !== 'string') continue;
    if (!isStatefulType(resource.Type)) continue;
    if (resource.UpdateReplacePolicy === 'Retain') continue;
    out.push([id, resource.UpdateReplacePolicy]);
  }
  return out;
}

/** The resource type the recorded template gives a logical id, for the per-difference reason. */
function recordedType(deployed: JsonObject, id: string): string {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const resource = resources[id];
  return isObject(resource) && typeof resource.Type === 'string' ? resource.Type : '<unknown type>';
}

/**
 * The drift every stack carrying a stateful resource has, because this branch sets
 * `UpdateReplacePolicy: Retain` on all of them and no recorded template predates that.
 *
 * Unlike the settings-gated entries above there is no row to put back: the change is
 * unconditional, so it is undone on both syntheses and the recorded template is still held
 * against a synthesis with the change removed, with no tolerance.
 */
function retainStatefulDrift(cluster: string, stack: string, deployed: JsonObject): IntendedDrift | undefined {
  const retained = retainedInThisBranch(deployed);
  if (retained.length === 0) return undefined;
  const endsWhen = `the ${stack} stack is deployed to ${cluster} from this branch, which puts Retain in the recorded template`;
  const cause: DriftCause = {
    id: RETAIN_CAUSE,
    change: `UpdateReplacePolicy is Retain on the ${retained.length} stateful resource(s) this stack creates`,
    unconditional: true,
    revert(template) {
      const resources = isObject(template.Resources) ? template.Resources : {};
      const failures: string[] = [];
      for (const [id, recorded] of retained) {
        const resource = resources[id];
        if (!isObject(resource)) {
          failures.push(`${id} is in the recorded template and not in the synthesized one`);
          continue;
        }
        if (resource.UpdateReplacePolicy !== 'Retain') {
          failures.push(`${id} UpdateReplacePolicy is ${formatValue(resource.UpdateReplacePolicy)}, expected Retain`);
        }
        if (recorded === undefined) delete resource.UpdateReplacePolicy;
        else resource.UpdateReplacePolicy = structuredClone(recorded);
      }
      return failures;
    },
  };
  return {
    cluster,
    stack,
    summary: `this branch sets UpdateReplacePolicy Retain on every stateful resource; the recorded template predates it`,
    deployedInput: {},
    deployedInputReason: 'no settings row gates this change, so the deployed-input synthesis is the as-captured one',
    endsWhen,
    differences: retained.map(([id, recorded]) => ({
      path: `Resources.${id}.UpdateReplacePolicy`,
      cause: RETAIN_CAUSE,
      reason:
        `${recordedType(deployed, id)} holds state that cannot be rebuilt from the template, and the recorded ` +
        `value ${formatValue(recorded)} lets an update that forces a replacement delete it`,
      removeWhen: endsWhen,
    })),
    causes: [cause],
  };
}

/** Both entries for one stack, as one. */
function mergeDrift(first: IntendedDrift, second: IntendedDrift): IntendedDrift {
  return {
    cluster: first.cluster,
    stack: first.stack,
    summary: `${first.summary}; ${second.summary}`,
    deployedInput: { ...first.deployedInput, ...second.deployedInput },
    deployedInputReason: first.deployedInputReason,
    endsWhen: `${first.endsWhen}; ${second.endsWhen}`,
    differences: [...first.differences, ...second.differences],
    causes: [...first.causes, ...second.causes],
  };
}

/**
 * The entry for one comparison, or undefined when the stack has no drift at all.
 *
 * `deployed` is the recorded reference template: the unconditional retain entry is derived from
 * it, so the expectation comes from the oracle rather than from the code that produces the change.
 * Omitting it leaves only the recorded settings-gated entries, which is what a comparison against
 * a substitute app wants: a substitute app is not this app, so this app's unconditional changes
 * are not expected of it.
 */
export function intendedDriftFor(cluster: string, stack: string, deployed?: JsonObject): IntendedDrift | undefined {
  const recorded = DRIFT.find((entry) => entry.cluster === cluster && entry.stack === stack);
  const retain = deployed === undefined ? undefined : retainStatefulDrift(cluster, stack, deployed);
  if (recorded === undefined) return retain;
  if (retain === undefined) return recorded;
  return mergeDrift(recorded, retain);
}

/** A captured table dump and its rows. */
function scanRows(scanFile: string): { scan: JsonObject; items: Json[] } {
  const parsed: Json = JSON.parse(readFileSync(scanFile, 'utf8'));
  if (!isObject(parsed)) throw new Error(`${scanFile}: expected a table scan object`);
  return { scan: parsed, items: Array.isArray(parsed.Items) ? parsed.Items : [] };
}

/** The `key` attribute of one row, when it is a string. */
function rowKey(item: Json): string | undefined {
  return isObject(item) && isObject(item.key) && typeof item.key.S === 'string' ? item.key.S : undefined;
}

/** Every string-valued row of a captured table dump. */
export function settingsLookup(scanFile: string): SettingsLookup {
  const values = new Map<string, string>();
  for (const item of scanRows(scanFile).items) {
    const key = rowKey(item);
    const value = isObject(item) && isObject(item.value) && typeof item.value.S === 'string' ? item.value.S : undefined;
    if (key !== undefined && value !== undefined) values.set(key, value);
  }
  return (key) => values.get(key);
}

/**
 * Writes a copy of the captured table dump with the deployed-input rows put back, so the
 * comparison against the recorded template is driven by the inputs that produced it.
 */
export function writeDeployedInputSettings(scanFile: string, overrides: Record<string, boolean | string>, outFile: string): void {
  const attribute = (value: boolean | string): Json => (typeof value === 'boolean' ? { BOOL: value } : { S: value });
  const { scan, items } = scanRows(scanFile);
  const remaining = new Set(Object.keys(overrides));
  for (const item of items) {
    const key = rowKey(item);
    if (key === undefined || !remaining.delete(key) || !isObject(item)) continue;
    item.value = attribute(overrides[key]!);
  }
  for (const key of remaining) {
    items.push({ key: { S: key }, value: attribute(overrides[key]!), version: { N: '1' } });
  }
  scan.Items = items;
  writeFileSync(outFile, JSON.stringify(scan));
}

export interface IntendedDriftInput {
  drift: IntendedDrift;
  /** The recorded reference template, still the oracle. */
  deployed: JsonObject;
  /** Synthesized from the settings as captured. */
  asCaptured: JsonObject;
  /** Synthesized from the settings the recorded template was generated from. */
  deployedInputSynth: JsonObject;
  settings: SettingsLookup;
  ignoreVersion?: boolean;
}

/**
 * The deployed-input synthesis with every unconditional cause undone: what the recorded template
 * is the oracle for, and therefore what the plain comparison runs against.
 *
 * Every reverted value is checked against its recorded shape on the way, so this cannot turn a
 * changed value into a passing one; it appends to `failures` when it does not find what the
 * recorded template says is there.
 */
export function unconditionalBaseline(
  drift: IntendedDrift,
  deployedInputSynth: JsonObject,
  settings: SettingsLookup,
  failures: string[] = [],
): JsonObject {
  const baseline = structuredClone(deployedInputSynth);
  for (const cause of drift.causes) {
    if (cause.unconditional !== true) continue;
    failures.push(...cause.revert(baseline, settings).map((failure) => `${cause.id} on the deployed-input synthesis: ${failure}`));
  }
  return baseline;
}

function failuresFrom(prefix: string, report: ReturnType<typeof compareTemplates>): string[] {
  return [
    ...report.missing.map((id) => `${prefix}: missing resource ${id}`),
    ...report.extra.map((id) => `${prefix}: extra resource ${id}`),
    ...report.hard.map((d) => `${prefix}: ${d.path} ${formatValue(d.live)} -> ${formatValue(d.synth)}`),
  ];
}

/**
 * Runs all three checks. An empty result is the only pass.
 *
 * 1. The deployed-input synthesis, with every unconditional cause undone, matches the recorded
 *    template with no tolerance.
 * 2. The as-captured synthesis differs from the recorded template in exactly the itemised set.
 * 3. Undoing every cause turns the as-captured synthesis back into that same baseline, with every
 *    reverted value checked against its recorded shape.
 *
 * An unconditional cause is in both syntheses, so it is undone in both. It is still named, valued
 * and reverted exactly like a settings-gated one; the only difference is which templates carry it.
 */
export function checkIntendedDrift(input: IntendedDriftInput): string[] {
  const { drift, deployed, asCaptured, deployedInputSynth, settings, ignoreVersion = false } = input;
  const failures: string[] = [];

  const baseline = unconditionalBaseline(drift, deployedInputSynth, settings, failures);

  const port = compareTemplates(deployed, baseline, ignoreVersion);
  if (!isParity(port)) failures.push(...failuresFrom('deployed-input synthesis differs from the recorded template', port));

  const observed = compareTemplates(deployed, asCaptured, ignoreVersion);
  failures.push(...observed.missing.map((id) => `as-captured synthesis is missing resource ${id}`));
  failures.push(...observed.extra.map((id) => `as-captured synthesis has extra resource ${id}`));
  const declared = new Set(drift.differences.map((difference) => difference.path));
  const seen = new Set(observed.hard.map((difference) => difference.path));
  for (const difference of observed.hard) {
    if (declared.has(difference.path)) continue;
    failures.push(
      `unexpected difference, not in the itemised set: ${difference.path} ${formatValue(difference.live)} -> ${formatValue(difference.synth)}`,
    );
  }
  for (const path of declared) {
    if (!seen.has(path)) failures.push(`itemised difference no longer produced, remove the entry: ${path}`);
  }

  const reverted = structuredClone(asCaptured);
  for (const cause of drift.causes) {
    failures.push(...cause.revert(reverted, settings).map((failure) => `${cause.id}: ${failure}`));
  }
  const residue = compareTemplates(baseline, reverted, ignoreVersion);
  failures.push(...failuresFrom('after undoing every cause the templates still differ', residue));
  failures.push(
    ...residue.soft.map((d) => `after undoing every cause a volatile value still differs: ${d.path} ${formatValue(d.live)} -> ${formatValue(d.synth)}`),
  );

  return failures;
}

/** The itemised expectation, printed so the gate names what it accepted instead of hiding it. */
export function formatIntendedDrift(drift: IntendedDrift): string {
  const lines = [
    `INTENDED DRIFT  ${drift.cluster} ${drift.stack}: ${drift.differences.length} differences from the recorded template`,
    `  ${drift.summary}`,
    `  deployed input: ${Object.entries(drift.deployedInput)
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(', ')}`,
    `    ${drift.deployedInputReason}`,
    `  ends when: ${drift.endsWhen}`,
  ];
  for (const cause of drift.causes) lines.push(`  cause ${cause.id}: ${cause.change}`);
  for (const difference of drift.differences) lines.push(`  ${difference.path}  (${difference.cause})`);
  lines.push('  each difference carries its reason and its removal condition in tools/parity/intended-drift.ts');
  return lines.join('\n');
}
