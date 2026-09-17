// capture.ts --from-raw: the fixture set replaySynthReads and ClusterConfig.fromFile read.
import { deepStrictEqual, match, ok, strictEqual } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { CALLER_IDENTITY_KEY, listRolesKey, replaySynthReads } from '../../src/cdk/synth-reads.ts';
import { requireCapture } from '../support/fixtures.ts';

type DynamoItem = Record<string, unknown>;
type Table = { Items: DynamoItem[] };

const PKG = resolve(import.meta.dirname, '../..');
const RAW = join(PKG, 'tools/parity/fixtures/idea-dev27/raw');
requireCapture([RAW], "node tools/parity/capture.ts --from-raw SOURCE --out tools/parity/fixtures/idea-dev27");

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function attributeString(item: DynamoItem, key: string): string | undefined {
  const attribute = item[key];
  return isRecord(attribute) && typeof attribute.S === 'string' ? attribute.S : undefined;
}

function readTable(path: string): Table {
  const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
  if (!isRecord(parsed) || !Array.isArray(parsed.Items) || !parsed.Items.every(isRecord)) {
    throw new Error(`${path}: expected an Items array of objects`);
  }
  return { Items: parsed.Items };
}

function readObject(path: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
  if (!isRecord(parsed)) throw new Error(`${path}: expected a JSON object`);
  return parsed;
}

test('--from-raw builds the table dumps and the synth reads', async () => {
  const out = mkdtempSync(join(tmpdir(), 'ideactl-capture-'));
  const r = spawnSync(process.execPath, [join(PKG, 'tools/parity/capture.ts'), '--from-raw', RAW, '--out', out], { encoding: 'utf8' });
  strictEqual(r.status, 0, r.stdout + r.stderr);
  match(r.stdout, /858 settings, 11 modules, 10 synth reads/);

  const settings = readTable(join(out, 'cluster-settings.json'));
  strictEqual(settings.Items.length, 858);
  // the DynamoDB attribute-value shape ClusterConfig.fromFile expects
  ok(settings.Items.every((item) => attributeString(item, 'key') !== undefined));
  const modules = readTable(join(out, 'modules.json'));
  strictEqual(modules.Items.length, 11);

  const reads = readObject(join(out, 'synth-reads.json'));
  deepStrictEqual(Object.keys(reads).sort(), Object.keys(reads), 'keys are written sorted');
  strictEqual(Object.keys(reads).filter((k) => k.startsWith('elbv2:DescribeListeners:')).length, 5);
  ok(CALLER_IDENTITY_KEY in reads);
  // both ListRoles prefixes are present: check_service_linked_role_exists calls each once.
  deepStrictEqual(reads[listRolesKey('/aws-service-role/opensearchservice.amazonaws.com')], []);
  const esRoles = reads[listRolesKey('/aws-service-role/es.amazonaws.com')];
  if (!Array.isArray(esRoles)) throw new Error('es ListRoles replay is not an array');
  strictEqual(esRoles.length, 1);

  // and the file it wrote is one replaySynthReads can read back.
  const domainKey = Object.keys(reads).find((key) => key.startsWith('opensearch:DescribeDomain:'));
  if (!domainKey) throw new Error('no opensearch replay key');
  const params: unknown = JSON.parse(domainKey.slice('opensearch:DescribeDomain:'.length));
  if (!isRecord(params) || typeof params.DomainName !== 'string') throw new Error('invalid opensearch replay key');
  const replay = replaySynthReads(join(out, 'synth-reads.json'));
  strictEqual((await replay.describeDomain(params.DomainName)).DomainName, params.DomainName);
});

test('no mode is a usage error', () => {
  const r = spawnSync(process.execPath, [join(PKG, 'tools/parity/capture.ts')], { encoding: 'utf8' });
  strictEqual(r.status, 2);
  match(r.stderr, /usage: capture\.ts --from-raw/);
});

/** A minimal ~/.idea/clusters/<C>/<R>: what --from-local reads. No account data. */
function fakeClusterDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-cluster-'));
  mkdirSync(join(dir, '_cdk/cdk.out.cluster'), { recursive: true });
  mkdirSync(join(dir, 'config/cluster'), { recursive: true });
  writeFileSync(join(dir, '_cdk/cdk.context.json'), JSON.stringify({ 'vpc-provider:account=111111111111:region=us-east-2': { vpcId: 'vpc-0abc' } }));
  writeFileSync(join(dir, '_cdk/cdk.out.cluster/idea-test1-cluster.template.json'), JSON.stringify({ Resources: {} }));
  writeFileSync(join(dir, '_cdk/cdk.out.cluster/manifest.json'), '{}');
  writeFileSync(join(dir, 'values.yml'), 'cluster_name: idea-test1\n');
  writeFileSync(join(dir, 'config/idea.yml'), 'modules:\n  - id: cluster\n    config_files: [settings.yml]\n');
  writeFileSync(join(dir, 'config/cluster/settings.yml'), 'cluster_name: idea-test1\naws:\n  region: us-east-2\n');
  return dir;
}

test('--from-local writes the files AWS does not hold', () => {
  const out = mkdtempSync(join(tmpdir(), 'ideactl-local-'));
  const r = spawnSync(
    process.execPath,
    [join(PKG, 'tools/parity/capture.ts'), '--from-local', '--cluster', 'idea-test1', '--region', 'us-east-2', '--cluster-dir', fakeClusterDir(), '--out', out],
    { encoding: 'utf8' },
  );
  strictEqual(r.status, 0, r.stdout + r.stderr);
  // cdk.context.json is the one synth.ts refuses to run without.
  ok(existsSync(join(out, 'cdk.context.json')), 'cdk.context.json');
  ok(existsSync(join(out, 'values.yml')), 'values.yml');
  deepStrictEqual(JSON.parse(readFileSync(join(out, 'flat.json'), 'utf8')), { 'cluster.cluster_name': 'idea-test1', 'cluster.aws.region': 'us-east-2' });
  // and the Python synth output, where synth.ts --against synth looks for it
  ok(existsSync(join(out, 'python/_cdk/cdk.out.cluster/idea-test1-cluster.template.json')), 'python template');
  match(r.stdout, /cdk\.context\.json, values\.yml, flat\.json, 1 python\/_cdk templates/);
});

test('the default --out is the fixtures dir next to capture.ts, whatever the cwd', () => {
  const out = join(PKG, 'tools/parity/fixtures/idea-test1');
  rmSync(out, { recursive: true, force: true });
  const r = spawnSync(
    process.execPath,
    [join(PKG, 'tools/parity/capture.ts'), '--from-local', '--cluster', 'idea-test1', '--region', 'us-east-2', '--cluster-dir', fakeClusterDir()],
    { cwd: tmpdir(), encoding: 'utf8' },
  );
  strictEqual(r.status, 0, r.stdout + r.stderr);
  ok(existsSync(join(out, 'cdk.context.json')), `${out}/cdk.context.json, synth.ts looks nowhere else`);
  rmSync(out, { recursive: true, force: true });
});

test('--from-local without a cdk.context.json fails loudly', () => {
  const r = spawnSync(
    process.execPath,
    [join(PKG, 'tools/parity/capture.ts'), '--from-local', '--cluster', 'idea-test1', '--region', 'us-east-2', '--cluster-dir', mkdtempSync(join(tmpdir(), 'ideactl-empty-'))],
    { encoding: 'utf8' },
  );
  strictEqual(r.status, 2);
  match(r.stderr, /cdk\.context\.json not found/);
});
