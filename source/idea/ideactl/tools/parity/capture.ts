// Build a parity fixture set for one cluster:
//
//   node capture.ts --from-raw <rawDir> [--out DIR]                 AWS CLI output already on disk
//   node capture.ts --from-local --cluster C --region R [--out DIR] the local administrator dir only
//   node capture.ts --live --cluster C --region R [--profile P] [--out DIR]
//
// The fixture set parity needs is:
//
//   <out>/cluster-settings.json  <out>/modules.json   DynamoDB scans (--from-raw, --live)
//   <out>/synth-reads.json                            the five synth-time reads (--from-raw, --live)
//   <out>/cdk.context.json  <out>/values.yml  <out>/flat.json
//                                                     copied/derived from the local
//                                                     ~/.idea/clusters/<C>/<R> (--from-local, --live)
//   ../live/<C>-<stack>.json                          deployed templates (--live), written where
//                                                     synth.ts looks for them
//
// The two table dumps keep the DynamoDB attribute-value shape `ClusterConfig.fromFile`
// expects; synth-reads.json is keyed the way `replaySynthReads` looks reads up.
//
// A release bump does not recapture: the reference is the last Python-deployed shape and the
// administrator that produced it is gone. Run `node tools/parity/retag-release.ts <from> <to>` to
// move the reference's release string with the tree.
//
// --live is the only path that touches AWS and is untested here (no credentials in the
// port tree); --from-local is the credential-free half of it. Exit 0 written, 2 usage or
// unreadable input.

import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';
import {
  CALLER_IDENTITY_KEY,
  describeDomainKey,
  listRolesKey,
  listenerKey,
  userPoolKey,
} from '../../src/cdk/synth-reads.ts';
import {
  awsClientOptions,
  formatAwsIdentity,
  type AwsClientOptions,
} from "../../src/cli/aws-client-options.ts";

type JsonObject = Record<string, unknown>;
type Item = JsonObject;

const SERVICE_LINKED_ROLE_SERVICES = ['es', 'opensearchservice'];
const FIXTURES = join(import.meta.dirname, 'fixtures');
const LIVE = join(import.meta.dirname, 'live');
const clusterDirOf = (cluster: string, region: string) => join(homedir(), '.idea/clusters', cluster, region);

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function objectValue(value: unknown, message: string): JsonObject {
  if (!isObject(value)) throw new Error(message);
  return value;
}

function arrayValue(value: unknown, message: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(message);
  return value;
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf8'));
}

function scanItems(scan: unknown, source: string): Item[] {
  const object = objectValue(scan, `${source}: expected a JSON object`);
  const items = arrayValue(object.Items ?? [], `${source}: Items must be an array`);
  return items.map((item, index) => objectValue(item, `${source}: Items[${index}] must be an object`));
}

function itemString(item: Item, key: string): string | undefined {
  const attribute = item[key];
  return isObject(attribute) && typeof attribute.S === 'string' ? attribute.S : undefined;
}

const writeJson = (p: string, value: unknown): void => {
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, `${JSON.stringify(value, null, 2)}\n`);
};

const sortedByKey = (reads: Record<string, unknown>): Record<string, unknown> =>
  Object.fromEntries(Object.entries(reads).sort(([a], [b]) => a.localeCompare(b)));

/** `key` -> string value, for the S-typed settings the reads are derived from. */
function settingsStrings(items: Item[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const item of items) {
    const key = itemString(item, 'key');
    const value = itemString(item, 'value');
    if (typeof key === 'string' && typeof value === 'string' && value.trim() !== '') out.set(key, value);
  }
  return out;
}

const listenerArns = (settings: Map<string, string>): string[] =>
  [...settings].filter(([k]) => k.endsWith('_listener_arn')).map(([, v]) => v);

/** Python strips a `vpc-` prefix before describe_domain (`analytics_stack.py:410-418`). */
const domainNameForDescribe = (name: string): string => (name.startsWith('vpc-') ? name.slice(4) : name);

const serviceLinkedRolePrefixes = (dnsSuffix: string): string[] =>
  SERVICE_LINKED_ROLE_SERVICES.map((service) => `/aws-service-role/${service}.${dnsSuffix}`);

/**
 * The half of the fixture set that lives on this machine rather than in AWS: the CDK
 * context the Python administrator resolved (without it `cdk synth` invents a dummy VPC),
 * the values.yml the config was generated from, and the layer-A oracle flattened out of
 * the generated `config/` by flatten.py.
 */
function fromLocal(clusterDir: string, outDir: string): string[] {
  const written: string[] = [];
  const context = join(clusterDir, '_cdk', 'cdk.context.json');
  if (!existsSync(context)) throw new Error(`${context} not found; deploy or synth this cluster with the Python administrator first`);
  mkdirSync(outDir, { recursive: true });
  copyFileSync(context, join(outDir, 'cdk.context.json'));
  written.push('cdk.context.json');

  const values = join(clusterDir, 'values.yml');
  if (existsSync(values)) {
    copyFileSync(values, join(outDir, 'values.yml'));
    written.push('values.yml');
  } else console.error(`NOTE  no ${values}; skipping values.yml`);

  const configDir = join(clusterDir, 'config');
  if (existsSync(join(configDir, 'idea.yml'))) {
    const flat = join(outDir, 'flat.json');
    const r = spawnSync('python3', [join(import.meta.dirname, 'flatten.py'), configDir, '-o', flat], { encoding: 'utf8' });
    if (r.status !== 0) throw new Error(`flatten.py failed (${r.status}): ${r.stderr ?? r.error}`);
    written.push('flat.json');
  } else console.error(`NOTE  no ${configDir}/idea.yml; skipping flat.json`);

  // The Python synth output, in the layout synth.ts --against synth reads. Templates and
  // manifests only; the asset staging next to them is large and parity never looks at it.
  const cdk = join(clusterDir, '_cdk');
  const alreadyThere = resolve(clusterDir) === resolve(join(outDir, 'python'));
  let templates = 0;
  for (const dir of alreadyThere || !existsSync(cdk) ? [] : readdirSync(cdk).filter((d) => d.startsWith('cdk.out.'))) {
    for (const f of readdirSync(join(cdk, dir)).filter((n) => n.endsWith('.template.json') || n === 'manifest.json')) {
      const to = join(outDir, 'python/_cdk', dir, f);
      mkdirSync(dirname(to), { recursive: true });
      copyFileSync(join(cdk, dir, f), to);
      if (f !== 'manifest.json') templates += 1;
    }
  }
  if (templates > 0) written.push(`${templates} python/_cdk templates`);
  return written;
}

function fromRaw(rawDir: string, outDir: string): void {
  const raw = (name: string) => join(rawDir, name);
  const settingsScan = readJson(raw('cluster-settings.scan.json'));
  const modulesScan = readJson(raw('modules.scan.json'));
  const items = scanItems(settingsScan, raw('cluster-settings.scan.json'));
  const modules = scanItems(modulesScan, raw('modules.scan.json'));
  writeJson(join(outDir, 'cluster-settings.json'), { Items: items });
  writeJson(join(outDir, 'modules.json'), { Items: modules });

  const settings = settingsStrings(items);
  const reads: Record<string, unknown> = {};

  const identity = objectValue(readJson(raw('sts.get-caller-identity.json')), 'sts.get-caller-identity.json: expected an object');
  const account = identity.Account;
  const arn = identity.Arn;
  if (typeof account !== 'string' || typeof arn !== 'string') throw new Error('sts.get-caller-identity.json: missing Account or Arn');
  reads[CALLER_IDENTITY_KEY] = { account, arn };

  for (const file of readdirSync(rawDir).filter((f) => f.startsWith('elbv2.describe-listeners.'))) {
    const response = objectValue(readJson(join(rawDir, file)), `${file}: expected an object`);
    const listener = objectValue(arrayValue(response.Listeners ?? [], `${file}: Listeners must be an array`)[0], `${file}: no Listeners[0]`);
    const listenerArn = listener.ListenerArn;
    if (typeof listenerArn !== 'string') throw new Error(`${file}: no Listeners[0].ListenerArn`);
    reads[listenerKey(listenerArn)] = listener;
  }
  const captured = new Set(Object.keys(reads));
  for (const arn of listenerArns(settings)) {
    if (!captured.has(listenerKey(arn))) console.error(`WARN  no capture for listener ${arn}`);
  }

  if (existsSync(raw('cognito-idp.describe-user-pool.json'))) {
    const response = objectValue(readJson(raw('cognito-idp.describe-user-pool.json')), 'cognito-idp.describe-user-pool.json: expected an object');
    const userPool = objectValue(response.UserPool, 'cognito-idp.describe-user-pool.json: no UserPool');
    const userPoolId = userPool.Id;
    if (typeof userPoolId !== 'string') throw new Error('cognito-idp.describe-user-pool.json: no UserPool.Id');
    reads[userPoolKey(userPoolId)] = userPool;
  }

  if (existsSync(raw('opensearch.describe-domain.json'))) {
    const response = objectValue(readJson(raw('opensearch.describe-domain.json')), 'opensearch.describe-domain.json: expected an object');
    const domainStatus = objectValue(response.DomainStatus, 'opensearch.describe-domain.json: no DomainStatus');
    const domainName = domainStatus.DomainName;
    if (typeof domainName !== 'string') throw new Error('opensearch.describe-domain.json: no DomainStatus.DomainName');
    reads[describeDomainKey(domainNameForDescribe(domainName))] = domainStatus;
  }

  // `check_service_linked_role_exists` calls ListRoles twice, once per service prefix, and
  // unions the results. A single capture rarely covers both prefixes, so split the roles it
  // did return by their own Path; a prefix with no matching role replays as [], which is what
  // ListRoles returns for an account that has no such service-linked role.
  if (existsSync(raw('iam.list-roles.json'))) {
    const response = objectValue(readJson(raw('iam.list-roles.json')), 'iam.list-roles.json: expected an object');
    const roles = arrayValue(response.Roles ?? [], 'iam.list-roles.json: Roles must be an array');
    const dnsSuffix = settings.get('cluster.aws.dns_suffix');
    if (!dnsSuffix) throw new Error('cluster.aws.dns_suffix missing from cluster-settings.scan.json');
    for (const prefix of serviceLinkedRolePrefixes(dnsSuffix)) {
      const matching = roles.filter((role) => isObject(role) && typeof role.Path === 'string' && role.Path.startsWith(`${prefix}/`));
      if (matching.length === 0) console.error(`NOTE  no captured role under ${prefix}; replaying as []`);
      reads[listRolesKey(prefix)] = matching;
    }
  }

  writeJson(join(outDir, 'synth-reads.json'), sortedByKey(reads));
  console.log(`wrote ${outDir}: ${items.length} settings, ${modules.length} modules, ${Object.keys(reads).length} synth reads`);
}

/**
 * Deployed templates, into `tools/parity/live/<cluster>-<stack>.json`, the path synth.ts
 * diffs against. One stack per non-config module plus the CDK toolkit stack; a module that
 * was never deployed is reported and skipped.
 */
async function captureLiveTemplates(cluster: string, modules: Item[], config: AwsClientOptions): Promise<number> {
  const { CloudFormationClient, GetTemplateCommand } = await import('@aws-sdk/client-cloudformation');
  const cfn = new CloudFormationClient(config);
  const moduleIds = modules.flatMap((module) => {
    const id = itemString(module, 'module_id');
    return itemString(module, 'type') !== 'config' && id !== undefined ? [id] : [];
  });
  const stacks = ['bootstrap', ...moduleIds];
  let written = 0;
  for (const stack of stacks) {
    const stackName = `${cluster}-${stack}`;
    try {
      const { TemplateBody } = await cfn.send(new GetTemplateCommand({ StackName: stackName, TemplateStage: 'Original' }));
      if (typeof TemplateBody !== 'string') throw new Error('GetTemplate returned no string TemplateBody');
      writeJson(join(LIVE, `${stackName}.json`), JSON.parse(TemplateBody));
      written += 1;
    } catch (e) {
      console.error(`NOTE  no deployed template for ${stackName}: ${e}`);
    }
  }
  return written;
}

async function fromLive(cluster: string, region: string, profile: string | undefined, outDir: string, clusterDir: string): Promise<void> {
  const { liveSynthReads } = await import("../../src/cdk/synth-reads.ts");
  const live = liveSynthReads(region, profile);
  const identity = await live.callerIdentity();
  console.log(formatAwsIdentity(identity, profile));

  const config = await awsClientOptions(region, profile);
  const { DynamoDBClient, ScanCommand } = await import('@aws-sdk/client-dynamodb');
  const ddb = new DynamoDBClient(config);
  const scan = async (table: string): Promise<Item[]> => {
    const rows: Item[] = [];
    let start: Record<string, AttributeValue> | undefined;
    do {
      const page = await ddb.send(new ScanCommand({ TableName: table, ExclusiveStartKey: start }));
      rows.push(...scanItems(page, `DynamoDB scan ${table}`));
      start = page.LastEvaluatedKey;
    } while (start);
    return rows;
  };
  const items = await scan(`${cluster}.cluster-settings`);
  const modules = await scan(`${cluster}.modules`);
  writeJson(join(outDir, 'cluster-settings.json'), { Items: items });
  writeJson(join(outDir, 'modules.json'), { Items: modules });

  const settings = settingsStrings(items);
  const reads: Record<string, unknown> = {};
  reads[CALLER_IDENTITY_KEY] = identity;
  for (const arn of listenerArns(settings)) reads[listenerKey(arn)] = await live.describeListener(arn);
  const userPoolId = settings.get('identity-provider.cognito.user_pool_id');
  if (userPoolId) reads[userPoolKey(userPoolId)] = await live.describeUserPool(userPoolId);
  const dnsSuffix = settings.get('cluster.aws.dns_suffix');
  if (dnsSuffix) {
    for (const prefix of serviceLinkedRolePrefixes(dnsSuffix)) reads[listRolesKey(prefix)] = await live.listServiceLinkedRoles(prefix);
  }
  const domainName = settings.get('analytics.opensearch.domain_name');
  if (domainName) {
    const name = domainNameForDescribe(domainName);
    reads[describeDomainKey(name)] = await live.describeDomain(name);
  }
  writeJson(join(outDir, 'synth-reads.json'), sortedByKey(reads));

  const templates = await captureLiveTemplates(cluster, modules, config);
  const local = existsSync(clusterDir) ? fromLocal(clusterDir, outDir) : [];
  if (local.length === 0) console.error(`NOTE  no ${clusterDir}; cdk.context.json/values.yml/flat.json not written (parity cannot synth without cdk.context.json)`);
  console.log(
    `wrote ${outDir}: ${items.length} settings, ${modules.length} modules, ${Object.keys(reads).length} synth reads, ${local.join(', ') || 'no local files'}; ${templates} live templates in ${LIVE}`,
  );
}

function flag(argv: string[], name: string): string | undefined {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
}

const USAGE =
  'usage: capture.ts --from-raw <rawDir> [--cluster-dir DIR] [--out DIR] | --from-local --cluster C --region R [--cluster-dir DIR] [--out DIR] | --live --cluster C --region R [--profile P] [--cluster-dir DIR] [--out DIR]';

async function main(argv: string[]): Promise<number> {
  const rawDir = flag(argv, '--from-raw');
  const out = flag(argv, '--out');
  if (rawDir) {
    const dir = resolve(rawDir);
    const outDir = out ? resolve(out) : dirname(dir);
    fromRaw(dir, outDir);
    // The Python side of a raw capture has the same shape as ~/.idea/clusters/<C>/<R>
    // (values.yml, config/, _cdk/), so the local half comes from there.
    const clusterDir = resolve(flag(argv, '--cluster-dir') ?? join(outDir, 'python'));
    if (existsSync(join(clusterDir, '_cdk', 'cdk.context.json'))) {
      console.log(`wrote ${outDir}: ${fromLocal(clusterDir, outDir).join(', ')} (from ${clusterDir})`);
    } else console.error(`NOTE  no ${clusterDir}/_cdk/cdk.context.json; pass --cluster-dir to write cdk.context.json/values.yml/flat.json`);
    return 0;
  }
  const local = argv.includes('--from-local');
  if (local || argv.includes('--live')) {
    const cluster = flag(argv, '--cluster');
    const region = flag(argv, '--region');
    if (!cluster || !region) {
      console.error(USAGE);
      return 2;
    }
    const outDir = out ? resolve(out) : join(FIXTURES, cluster);
    const clusterDir = resolve(flag(argv, '--cluster-dir') ?? clusterDirOf(cluster, region));
    if (local) {
      const written = fromLocal(clusterDir, outDir);
      console.log(`wrote ${outDir}: ${written.join(', ')} (from ${clusterDir})`);
      return 0;
    }
    await fromLive(cluster, region, flag(argv, '--profile'), outDir, clusterDir);
    return 0;
  }
  console.error(USAGE);
  return 2;
}

try {
  process.exit(await main(process.argv.slice(2)));
} catch (e) {
  console.error(`${e}`);
  process.exit(2);
}
