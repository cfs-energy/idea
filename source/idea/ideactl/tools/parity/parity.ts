// Parity check: a synthesized CloudFormation template against the live one the
// Python administrator deployed. Exit 1 on any difference that would change a
// deployed resource; the four volatile classes are reported and never fail.
//
// `Metadata.cdk_nag` is compared exactly, on the template and on every resource: the
// security-linter suppressions are part of the deployed template, and a missing one shows up
// as a `cdk diff` on a stack that must not diff. The rest of `Metadata` is synthesis noise:
// `aws:cdk:path` is reported on its own as `Path`, and `aws:asset:*` is volatile.
//
//   node parity.ts diff [--ignore-version] <live.json> <synth.json>   compare
//   node parity.ts paths <template.json>                              logical id -> construct path
//
// Exit 0 PARITY, 1 MISMATCH, 2 usage or unreadable input.
// Runs on node >= 22.18 (type stripping), no dependencies.
//
// `compareTemplates` is the same comparison as the `diff` subcommand, returning the classified
// result instead of printing it, so a caller that needs the differences as data uses one
// classifier rather than a second copy of these rules.

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export type Json = null | boolean | number | string | Json[] | JsonObject;
export type JsonObject = { [key: string]: Json };
export type JsonValue = Json | undefined;
type ResourceContext = { id: string; type: string };

// Volatile classes. Anything else that differs is a real difference.
const ASSET_RE = /(^|\/)[0-9a-f]{64}(\.[a-z.]+)?$/;
// analytics dashboard target group: Python regenerates the uuid tail every synth.
const DASHBOARD_TG_RE = /^(.+)-dashboard-[0-9a-f]{8}-[0-9a-f-]{1,9}$/;
const MASKED = '<masked>';

function isObject(value: JsonValue): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function stringValue(value: JsonValue): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export function loadTemplate(path: string): JsonObject {
  const parsed: Json = JSON.parse(readFileSync(path, 'utf8'));
  if (!isObject(parsed)) throw new Error(`${path}: expected a JSON object`);
  return parsed;
}
const load = loadTemplate;

// Strip what CDK regenerates per synth and what carries no deployed state.
// `Description`, `Parameters` (BootstrapVersion included), `Rules` and the `cdk_nag`
// suppressions are compared.
function normalize(template: JsonObject, ignoreVersion: boolean): JsonObject {
  const out: JsonObject = {
    Description: template.Description ?? null,
    Metadata: {},
    Parameters: {},
    Rules: {},
    Conditions: {},
    Mappings: {},
    Resources: {},
    Outputs: {},
  };
  for (const key of ['Parameters', 'Rules', 'Conditions', 'Mappings', 'Outputs']) {
    if (template[key]) out[key] = structuredClone(template[key]);
  }
  const templateMetadata = isObject(template.Metadata) ? template.Metadata : undefined;
  // The analytics stack suppresses four rules stack-wide; those live here, not on a resource.
  if (templateMetadata?.cdk_nag !== undefined) {
    out.Metadata = { cdk_nag: structuredClone(templateMetadata.cdk_nag) };
  }
  if (ignoreVersion && typeof out.Description === 'string') {
    // Preserve all non-version description text. The Python stack description ends with
    // the release token, but a changed suffix is a real template difference.
    out.Description = out.Description.replace(/(Version:\s*)[^\s,]+/, `$1${MASKED}`);
  }
  const resources = isObject(template.Resources) ? template.Resources : {};
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource)) throw new Error(`Resources.${id}: expected an object`);
    if (resource.Type === 'AWS::CDK::Metadata') continue;
    const normalized: JsonObject = { Type: resource.Type ?? null };
    for (const key of ['Properties', 'DependsOn', 'DeletionPolicy', 'UpdateReplacePolicy', 'Condition', 'CreationPolicy', 'UpdatePolicy']) {
      if (resource[key] !== undefined) normalized[key] = structuredClone(resource[key]);
    }
    if (Array.isArray(normalized.DependsOn)) normalized.DependsOn = [...normalized.DependsOn].sort();
    const properties = isObject(normalized.Properties) ? normalized.Properties : undefined;
    if (ignoreVersion && resource.Type === 'Custom::ClusterSettings' && typeof properties?.version === 'string') {
      properties.version = MASKED;
    }
    // aws:cdk:path is a hard diff: a moved construct with an unchanged logical id
    // is still a tree difference.
    const metadata = isObject(resource.Metadata) ? resource.Metadata : undefined;
    normalized.Path = metadata?.['aws:cdk:path'] ?? null;
    // The suppressions this resource carries, under their real path so the diff line names it.
    // `aws:asset:*` stays dropped: the bundling paths and property names are local to the run.
    if (metadata?.cdk_nag !== undefined) normalized.Metadata = { cdk_nag: structuredClone(metadata.cdk_nag) };
    (out.Resources as JsonObject)[id] = normalized;
  }
  return out;
}

export type Diff = { path: string; live: JsonValue; synth: JsonValue; soft: 'asset' | 'volatile' | null };

function isAsset(value: JsonValue): boolean {
  return typeof value === 'string' && ASSET_RE.test(value);
}

function isDashboardTargetGroup(a: JsonValue, b: JsonValue): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const ma = DASHBOARD_TG_RE.exec(a);
  const mb = DASHBOARD_TG_RE.exec(b);
  return ma !== null && mb !== null && ma[1] === mb[1];
}

function isAnalyticsDashboardTargetGroup(resource: ResourceContext, a: JsonValue, b: JsonValue): boolean {
  if (resource.type !== 'AWS::ElasticLoadBalancingV2::TargetGroup' || !isDashboardTargetGroup(a, b) || typeof a !== 'string') {
    return false;
  }
  const match = DASHBOARD_TG_RE.exec(a);
  if (!match) return false;
  return resource.id === `${match[1].replace(/[^A-Za-z0-9]/g, '')}dashboardtargetgroup`;
}

function softness(path: string, a: JsonValue, b: JsonValue, resource: ResourceContext | undefined): Diff['soft'] {
  if (!resource) return null;
  const resourcePath = `Resources.${resource.id}.Properties.`;
  const propertyPath = path.slice(resourcePath.length);
  if (
    (resource.type === 'AWS::Lambda::Function' || resource.type === 'AWS::Lambda::LayerVersion') &&
    propertyPath === 'Code.S3Key' &&
    isAsset(a) &&
    isAsset(b)
  ) {
    return 'asset';
  }
  // Every volatile class needs a string on BOTH sides. A value that is present live and
  // absent (or no longer a plain value) in the synthesized template is a real deployed
  // difference: no UpdateToken means the custom resource stops re-running on every deploy,
  // and a dropped settings.deployment_id is one row the Custom::ClusterSettings CR no
  // longer writes. Those must fail, not be masked by a path match.
  if (typeof a !== 'string' || typeof b !== 'string') return null;
  if (resource.type === 'Custom::ClusterSettings' && propertyPath === 'settings.deployment_id') return 'volatile';
  if (resource.id === 'opensearchprivateips' && resource.type === 'Custom::OpenSearchPrivateIPAddresses' && propertyPath === 'UpdateToken') return 'volatile';
  if (propertyPath === 'Name' && isAnalyticsDashboardTargetGroup(resource, a, b)) {
    return 'volatile';
  }
  return null;
}

/**
 * True when both sides are the same set of managed policy references on a role, in a different
 * order.
 *
 * The reference implementation builds two of its role policy lists with a set-to-list conversion,
 * and the language it is written in randomizes string hashing per process, so the order it emitted
 * was chosen at deploy time rather than by any rule. Across the three captured clusters the two
 * orders occur in both permutations, all from the same released version, and no single hash seed
 * reproduces all three. So there is no order for the port to match, and matching one capture takes
 * the gate red on another.
 *
 * A permutation of this property grants nothing different and replaces no resource, so the
 * comparison is by membership. Membership itself stays strict: a policy added, removed or swapped
 * still falls through to the element comparison and reports.
 */
function isReorderedManagedPolicyList(
  path: string,
  a: JsonValue,
  b: JsonValue,
  resource: ResourceContext | undefined,
): boolean {
  if (resource?.type !== 'AWS::IAM::Role') return false;
  if (path !== `Resources.${resource.id}.Properties.ManagedPolicyArns`) return false;
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const key = (value: JsonValue): string => JSON.stringify(value);
  return JSON.stringify(a.map(key).sort()) === JSON.stringify(b.map(key).sort());
}

function childResource(path: string, a: JsonValue, b: JsonValue, parent: ResourceContext | undefined): ResourceContext | undefined {
  if (parent || !/^Resources\.[^.]+$/.test(path) || !isObject(a) || !isObject(b)) return parent;
  const type = stringValue(a.Type);
  return type !== undefined && type === stringValue(b.Type) ? { id: path.slice('Resources.'.length), type } : undefined;
}

function walk(a: JsonValue, b: JsonValue, path: string, out: Diff[], resource?: ResourceContext): void {
  if (JSON.stringify(a) === JSON.stringify(b)) return;
  const resourceContext = childResource(path, a, b, resource);
  if (isReorderedManagedPolicyList(path, a, b, resourceContext)) return;
  if (Array.isArray(a) && Array.isArray(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const key of [...keys].sort()) {
      const index = Number(key);
      walk(a[index], b[index], `${path}[${key}]`, out, resourceContext);
    }
    return;
  }
  if (!isObject(a) || !isObject(b)) {
    out.push({ path, live: a, synth: b, soft: softness(path, a, b, resourceContext) });
    return;
  }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of [...keys].sort()) {
    walk(a[k], b[k], `${path}.${k}`, out, resourceContext);
  }
}

function resourceSection(template: JsonObject): JsonObject {
  if (!isObject(template.Resources)) throw new Error('normalized template has no Resources object');
  return template.Resources;
}

export interface ParityReport {
  liveResources: JsonObject;
  synthResources: JsonObject;
  missing: string[];
  extra: string[];
  hard: Diff[];
  soft: Diff[];
}

/** True when nothing that would change a deployed resource differs. */
export function isParity(report: ParityReport): boolean {
  return report.missing.length + report.extra.length + report.hard.length === 0;
}

/** The `diff` subcommand's comparison, as data. */
export function compareTemplates(liveTemplate: JsonObject, synthTemplate: JsonObject, ignoreVersion = false): ParityReport {
  const live = normalize(liveTemplate, ignoreVersion);
  const synth = normalize(synthTemplate, ignoreVersion);
  const liveResources = resourceSection(live);
  const synthResources = resourceSection(synth);
  const diffs: Diff[] = [];
  for (const section of ['Description', 'Metadata', 'Parameters', 'Rules', 'Conditions', 'Mappings', 'Resources', 'Outputs']) {
    walk(live[section], synth[section], section, diffs);
  }
  const missing = Object.keys(liveResources).filter((id) => !(id in synthResources));
  const extra = Object.keys(synthResources).filter((id) => !(id in liveResources));
  // Suppress the per-property noise inside a resource that only one side has. Match on a
  // whole path segment so a missing `X` does not also swallow the diffs of a sibling `Xyz`.
  const under = (id: string, p: string) => p === `Resources.${id}` || p.startsWith(`Resources.${id}.`);
  const inGoneResource = (d: Diff) => missing.some((id) => under(id, d.path)) || extra.some((id) => under(id, d.path));
  return {
    liveResources,
    synthResources,
    missing,
    extra,
    hard: diffs.filter((d) => d.soft === null && !inGoneResource(d)),
    soft: diffs.filter((d) => d.soft !== null),
  };
}

export const formatValue = (value: JsonValue) => (value === undefined ? '<absent>' : JSON.stringify(value));

function diff(livePath: string, synthPath: string, ignoreVersion: boolean): number {
  const { liveResources, synthResources, missing, extra, hard, soft } = compareTemplates(
    load(livePath),
    load(synthPath),
    ignoreVersion,
  );
  const fmt = formatValue;
  for (const id of missing) {
    const resource = liveResources[id];
    if (!isObject(resource)) throw new Error(`Resources.${id}: expected an object`);
    console.log(`MISSING  ${id}  (${resource.Type}, ${resource.Path})`);
  }
  for (const id of extra) {
    const resource = synthResources[id];
    if (!isObject(resource)) throw new Error(`Resources.${id}: expected an object`);
    console.log(`EXTRA    ${id}  (${resource.Type}, ${resource.Path})`);
  }
  for (const d of hard) console.log(`DIFF     ${d.path}\n  live:  ${fmt(d.live)}\n  synth: ${fmt(d.synth)}`);
  for (const d of soft) console.log(`${d.soft === 'asset' ? 'ASSET   ' : 'VOLATILE'} ${d.path}  ${fmt(d.live)} -> ${fmt(d.synth)}`);
  const n = Object.keys(liveResources).length;
  const bad = missing.length + extra.length + hard.length;
  console.log(
    `${bad === 0 ? 'PARITY' : 'MISMATCH'}  ${n} live resources, ${missing.length} missing, ${extra.length} extra, ${hard.length} property diffs, ${soft.length} soft`,
  );
  return bad === 0 ? 0 : 1;
}

function paths(p: string): void {
  const t = load(p);
  const resources = isObject(t.Resources) ? t.Resources : {};
  const rows = Object.entries(resources)
    .filter(([, resource]) => isObject(resource) && resource.Type !== 'AWS::CDK::Metadata')
    .map(([id, resource]) => {
      const metadata = isObject(resource) && isObject(resource.Metadata) ? resource.Metadata : undefined;
      return [id, stringValue(isObject(resource) ? resource.Type : undefined) ?? '?', stringValue(metadata?.['aws:cdk:path']) ?? '?'];
    });
  for (const [id, type, path] of rows.sort((a, b) => a[2].localeCompare(b[2]))) console.log(`${path}\t${id}\t${type}`);
}

function main(argv: string[]): number {
  const ignoreVersion = argv.includes('--ignore-version');
  const [cmd, ...rest] = argv.filter((a) => a !== '--ignore-version');
  if (cmd === 'diff' && rest.length === 2) return diff(rest[0], rest[1], ignoreVersion);
  if (cmd === 'paths' && rest.length === 1) {
    paths(rest[0]);
    return 0;
  }
  console.error('usage: parity.ts diff [--ignore-version] <live.json> <synth.json> | paths <template.json>');
  return 2;
}

const entryPoint = process.argv[1];
if (entryPoint !== undefined && import.meta.url === pathToFileURL(entryPoint).href) {
  try {
    process.exit(main(process.argv.slice(2)));
  } catch (e) {
    console.error(`${e}`);
    process.exit(2);
  }
}
