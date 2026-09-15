// flatten.py: the layer-A oracle. Quirks first, then the captured fixture counts.
import { deepStrictEqual, match, strictEqual } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { requireFixtures, requiredService } from '../support/fixtures.ts';

const PKG = resolve(import.meta.dirname, '../..');
const FLATTEN = join(PKG, 'tools/parity/flatten.py');
const hasYaml = spawnSync('python3', ['-c', 'import yaml'], { encoding: 'utf8' }).status === 0;
if (!hasYaml) requiredService("python3 with PyYAML", "python3 -m pip install PyYAML");

function flatten(configDir: string, ...args: string[]): { code: number; flat: unknown; err: string } {
  const r = spawnSync('python3', [FLATTEN, configDir, ...args], { encoding: 'utf8' });
  return { code: r.status ?? -1, flat: r.status === 0 ? JSON.parse(r.stdout) : null, err: r.stderr };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function configDir(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-flatten-'));
  for (const [name, body] of Object.entries(files)) {
    mkdirSync(join(dir, name.split('/').slice(0, -1).join('/') || '.'), { recursive: true });
    writeFileSync(join(dir, name), body);
  }
  return dir;
}

const IDEA_YML = `modules:
  - name: cluster
    id: cluster
    type: stack
    config_files:
      - settings.yml
      - logging.yml
`;

test('null normalisation, empty list, and the {} quirk', () => {
  const dir = configDir({
    'idea.yml': IDEA_YML,
    'cluster/settings.yml': [
      'aws:',
      '  region: us-east-2',
      'empty_string: ""',
      'blank_string: "   "',
      'tilde: ~',
      'empty_list: []',
      'empty_dict: {}',
      'zero: 0',
      'flag_false: false',
      'float: 0.08',
      'quoted: "72:00:00"',
      'version_like: 470.256.02',
      'from_settings: a',
      '',
    ].join('\n'),
    'cluster/logging.yml': 'from_logging: b\nfrom_settings: overridden\n',
  });
  const { code, flat, err } = flatten(dir);
  strictEqual(code, 0, err);
  deepStrictEqual(flat, {
    'cluster.aws.region': 'us-east-2',
    'cluster.blank_string': null,
    'cluster.empty_dict': null, // is_null_value runs before the dict check; a {} leaf is NULL
    'cluster.empty_list': [], // ...but [] is a real value
    'cluster.empty_string': null,
    'cluster.float': 0.08,
    'cluster.from_logging': 'b',
    'cluster.from_settings': 'overridden', // shallow merge, later file wins
    'cluster.flag_false': false,
    'cluster.quoted': '72:00:00',
    'cluster.tilde': null,
    'cluster.version_like': '470.256.02',
    'cluster.zero': 0,
  });
  match(err, /13 keys/);
});

test('a key with a dot or colon is rejected, and --key-prefix filters', () => {
  const dotted = configDir({ 'idea.yml': IDEA_YML, 'cluster/settings.yml': 'a.b: 1\n', 'cluster/logging.yml': '{}\n' });
  strictEqual(flatten(dotted).code, 1);
  match(flatten(dotted).err, /Config key name: a\.b under: cluster cannot contain/);

  const coloned = configDir({ 'idea.yml': IDEA_YML, 'cluster/settings.yml': "'a:b': 1\n", 'cluster/logging.yml': '{}\n' });
  const colon = flatten(coloned);
  strictEqual(colon.code, 1, colon.err);
  match(colon.err, /Config key name: a:b under: cluster cannot contain/);

  const dir = configDir({ 'idea.yml': IDEA_YML, 'cluster/settings.yml': 'a: 1\nbb: 2\n', 'cluster/logging.yml': '{}\n' });
  deepStrictEqual(flatten(dir, '--key-prefix', 'cluster.b').flat, { 'cluster.bb': 2 });
});

// The captured oracles the config generator port is measured against. Cluster names
// live in the gitignored fixture tree, never in this file.
const FIXTURES = join(PKG, 'tools/parity/fixtures');
requireFixtures([FIXTURES], "node tools/parity/capture.ts --from-raw SOURCE --out tools/parity/fixtures");
const clusters = readdirSync(FIXTURES).filter((c) => existsSync(join(FIXTURES, c, 'flat.json'))).sort();
if (clusters.length === 0) throw new Error(`Required fixture directory has no flat.json oracles: ${FIXTURES}`);

/**
 * Locate the generated config that produced a captured flat map.
 *
 * The development fixture carries a copied config tree; the other captures retain their
 * generated config under the local cluster directory so real identifiers never enter tests.
 */
function capturedConfigDir(cluster: string): string {
  const candidates = [
    join(FIXTURES, cluster, "python/config"),
    join(homedir(), ".idea/clusters", cluster, "us-east-2/config"),
  ];
  const config = candidates.find((candidate) => existsSync(join(candidate, "idea.yml")));
  if (!config) throw new Error(`${cluster}: no captured generated config directory`);
  return config;
}

/**
 * Return the documented oracle size without embedding non-development fixture names.
 *
 * The only 713-key capture is identified by its data-storage provider. All other currently
 * captured maps, including the development and collaboration captures, contain 722 keys.
 */
function expectedFlatKeyCount(flat: Record<string, unknown>): number {
  return flat["shared-storage.data.provider"] === "fsx_netapp_ontap" ? 713 : 722;
}

test("every captured flat.json exactly matches a regenerated sorted flat map", () => {
  for (const cluster of clusters) {
    const flat: unknown = JSON.parse(readFileSync(join(FIXTURES, cluster, "flat.json"), "utf8"));
    if (!isObject(flat)) throw new Error(`${cluster}: flat.json is not an object`);

    const keys = Object.keys(flat);
    strictEqual(keys.length, expectedFlatKeyCount(flat), `${cluster}: unexpected flat key count`);
    deepStrictEqual(keys, [...keys].sort(), `${cluster}: keys not sorted`);
    deepStrictEqual(
      {
        "analytics.kinesis.removal_policy": flat["analytics.kinesis.removal_policy"],
        "cluster.aws.dns_suffix": flat["cluster.aws.dns_suffix"],
        "cluster.aws.region": flat["cluster.aws.region"],
      },
      {
        "analytics.kinesis.removal_policy": "DESTROY",
        "cluster.aws.dns_suffix": "amazonaws.com",
        "cluster.aws.region": "us-east-2",
      },
      `${cluster}: sampled fixture values changed`,
    );

    const regenerated = flatten(capturedConfigDir(cluster));
    strictEqual(regenerated.code, 0, `${cluster}: ${regenerated.err}`);
    if (!isObject(regenerated.flat)) throw new Error(`${cluster}: flatten.py did not return an object`);
    deepStrictEqual(regenerated.flat, flat, `${cluster}: flatten.py differs from flat.json`);
    console.log(`${cluster}: ${keys.length} keys`);
  }
});
