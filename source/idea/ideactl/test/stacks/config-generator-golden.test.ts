/**
 * Golden-file parity for the config generator.
 *
 * Layer A: for every captured fixture, generate from its values.yml into a temp directory,
 * flatten it, and compare to the flat.json the Python generator produced. No normalisation:
 * same key set, same JSON types.
 * Layer D: the runtime-read inventory, expanded to concrete keys and split into the
 * branch that produces each one. Every key of a selected branch has to exist in the generated
 * output and no key of an unselected branch may, so the list cannot be padded with keys the
 * generator happens to emit. It runs against the captured fixtures and against every synthetic
 * case with the full module set, so a public checkout still has the gate.
 *
 * The captured fixtures are gitignored, so the layer A suite and the captured half of layer D
 * require them explicitly. The synthetic suite always runs.
 */

import assert from 'node:assert/strict';
import {
  mkdirSync,
  mkdtempSync,
  existsSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import * as yaml from 'js-yaml';

import {
  flattenConfigDir,
  generateConfig,
  generateConfigFromTemplates,
  readConfigFromFiles,
} from '../../src/config/generator.ts';
import { buildContext, loadValuesFile, type UserValues } from '../../src/config/values.ts';
import { requireCapture } from '../support/fixtures.ts';

const here = fileURLToPath(new URL('.', import.meta.url));
const fixturesDir = fileURLToPath(new URL('../../tools/parity/fixtures', import.meta.url));
const syntheticDir = join(here, 'synthetic');
requireCapture(
  [fixturesDir],
  "node tools/parity/capture.ts --from-raw SOURCE --out tools/parity/fixtures",
);

function tempDir(): string {
  return mkdtempSync(join(tmpdir(), 'ideactl-golden-'));
}

/** A captured fixture: a directory holding the Python values.yml and its flat.json oracle. */
interface Fixture {
  name: string;
  valuesFile: string;
  flatFile: string;
  manifestFile: string;
}

function capturedFixtures(): Fixture[] {
  const fixtures: Fixture[] = [];
  for (const name of readdirSync(fixturesDir).sort()) {
    const dir = join(fixturesDir, name);
    const flatFile = join(dir, 'flat.json');
    const valuesFile = [join(dir, 'values.yml'), join(dir, 'python', 'values.yml')].find((file) =>
      existsSync(file),
    );
    if (existsSync(flatFile) && valuesFile !== undefined) {
      fixtures.push({ name, valuesFile, flatFile, manifestFile: join(dir, 'idea.json') });
    }
  }
  return fixtures;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readJson(file: string): unknown {
  return JSON.parse(readFileSync(file, 'utf-8'));
}

function readJsonRecord(file: string): Record<string, unknown> {
  const value: unknown = readJson(file);
  assert.ok(isRecord(value), `${file} must contain a JSON object`);
  return value;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string');
}

/**
 * The runtime-read inventory, expanded to concrete keys: `all` is required of every
 * generated tree, the other groups only of the branch named by `selectedGroups`. A group that is
 * not selected must produce none of its keys, so the branch mapping is checked in both directions.
 */
type RuntimeKeys = Record<string, string[]>;

const RUNTIME_KEY_GROUPS = [
  'all',
  'newVpc',
  'existingVpc',
  'efsApps',
  'efsData',
  'metricsCloudwatch',
  'metricsDogstatsd',
  'metricsPrometheus',
  'metricsAmp',
  'dsActiveDirectory',
  'dsActiveDirectoryNew',
  'dsOpenldap',
  'opensearchManaged',
];

function readRuntimeKeys(file: string): RuntimeKeys {
  const value: unknown = readJson(file);
  assert.ok(isRecord(value), `${file} must contain an object`);
  assert.deepStrictEqual(
    Object.keys(value).sort(),
    [...RUNTIME_KEY_GROUPS].sort(),
    `${file} must hold exactly the known branch groups`,
  );
  const keys: RuntimeKeys = {};
  for (const group of RUNTIME_KEY_GROUPS) {
    const entry: unknown = value[group];
    assert.ok(isStringArray(entry), `${file}.${group} must be an array of strings`);
    keys[group] = entry;
  }
  return keys;
}

const METRICS_GROUPS: Record<string, string> = {
  cloudwatch: 'metricsCloudwatch',
  dogstatsd: 'metricsDogstatsd',
  prometheus: 'metricsPrometheus',
  amazon_managed_prometheus: 'metricsAmp',
};

/** Which branch groups the generated tree took, read back from the tree itself. */
function selectedGroups(flat: Record<string, unknown>): string[] {
  const groups = ['all'];
  groups.push(flat['cluster.network.use_existing_vpc'] === true ? 'existingVpc' : 'newVpc');
  for (const scope of ['apps', 'data'] as const) {
    const managedEfs =
      flat[`shared-storage.${scope}.provider`] === 'efs' &&
      flat[`shared-storage.${scope}.efs.use_existing_fs`] !== true;
    if (managedEfs) groups.push(scope === 'apps' ? 'efsApps' : 'efsData');
  }
  const provider = flat['metrics.provider'];
  if (typeof provider === 'string' && provider in METRICS_GROUPS) {
    groups.push(METRICS_GROUPS[provider] as string);
  }
  if (flat['directoryservice.provider'] === 'openldap') groups.push('dsOpenldap');
  else {
    groups.push('dsActiveDirectory');
    if (flat['directoryservice.root_credentials_provided'] !== true) {
      groups.push('dsActiveDirectoryNew');
    }
  }
  if (flat['analytics.opensearch.use_existing'] !== true) groups.push('opensearchManaged');
  return groups;
}

/**
 * Every runtime key of the selected branches exists, and no key of an unselected branch does.
 * Keys are scoped to the modules the tree actually contains, so a case with fewer modules is
 * still checked against every key its own modules must produce.
 */
function checkRuntimeKeys(label: string, flat: Record<string, unknown>, keys: RuntimeKeys): void {
  const selected = selectedGroups(flat);
  const required = selected.flatMap((group) => keys[group] as string[]);
  const requiredSet = new Set(required);
  const forbidden = RUNTIME_KEY_GROUPS.filter((group) => !selected.includes(group))
    .flatMap((group) => keys[group] as string[])
    .filter((key) => !requiredSet.has(key));
  const missing = required.filter((key) => !(key in flat));
  const unexpected = forbidden.filter((key) => key in flat);
  console.log(
    `${label}: ${required.length} runtime keys checked ` +
      `(${selected.slice(1).join('+') || 'base'}), ${missing.length} missing, ` +
      `${unexpected.length} from an unselected branch`,
  );
  assert.deepStrictEqual(missing, [], `${label}: runtime keys missing from the generated config`);
  assert.deepStrictEqual(unexpected, [], `${label}: keys produced by an unselected branch`);
}

function readManifest(file: string): Record<string, unknown> {
  const value = loadYamlFile(file);
  assert.ok(Array.isArray(value.modules), `${file} must contain a modules array`);
  return value;
}

/** The first differing keys, with both values, so a failure names the drift instead of a blob. */
function reportDifferences(
  got: Record<string, unknown>,
  want: Record<string, unknown>,
  limit = 10,
): string[] {
  const lines: string[] = [];
  for (const key of Object.keys(want)) {
    if (lines.length >= limit) break;
    if (!(key in got)) lines.push(`${key}: missing (python=${JSON.stringify(want[key])})`);
    else if (JSON.stringify(got[key]) !== JSON.stringify(want[key])) {
      lines.push(`${key}: ts=${JSON.stringify(got[key])} python=${JSON.stringify(want[key])}`);
    }
  }
  for (const key of Object.keys(got)) {
    if (lines.length >= limit) break;
    if (!(key in want)) lines.push(`${key}: unexpected (ts=${JSON.stringify(got[key])})`);
  }
  return lines;
}

function loadYamlFile(file: string): Record<string, unknown> {
  return (yaml.load(readFileSync(file, 'utf-8'), { schema: yaml.CORE_SCHEMA }) ?? {}) as Record<
    string,
    unknown
  >;
}

/** A synthetic case is its own file merged over base.yml, one branch at a time. */
function syntheticValues(caseFile: string): UserValues {
  return { ...loadYamlFile(join(syntheticDir, 'base.yml')), ...loadYamlFile(caseFile) };
}

function syntheticCases(dir: string): string[] {
  return readdirSync(dir)
    .filter((name) => name.endsWith('.yml') && name !== 'base.yml')
    .sort();
}

/**
 * Keys the Python generator emits that this branch deliberately stops generating, each with the
 * reason. Every entry has to still be in the oracle, so one that has stopped applying fails here
 * instead of quietly widening the comparison, and anything not listed is still compared key for
 * key against the oracle.
 */
const RETIRED_KEYS: Record<string, string> = {
  'cluster.solution.enable_solution_metrics':
    'job stacks no longer carry the anonymous metrics resource, so nothing reads the flag',
  'cluster.solution.custom_anonymous_metric_entry':
    'the removed scheduler code was its only reader, and the anonymous metrics it was sent with are gone',
};

/**
 * Keys this branch generates that the Python generator never did, each with the reason. Every
 * entry has to be generated and absent from the oracle, so a key the oracle gains or the
 * template loses fails here instead of quietly widening the comparison.
 */
const ADDED_KEYS: Record<string, string> = Object.fromEntries(
  [
    'cost.enabled', 'cost.interval_hours', 'cost.lookback_days', 'cost.module_tag', 'cost.project_tag',
    'cost.owner_tag', 'cost.by_account', 'storage.enabled', 'storage.interval_minutes', 'storage.verify_tls',
  ].map((key) => [`cluster-manager.metrics.${key}`, 'the cost and storage metrics collectors arrived with 26.09.1']),
);

/** The generated output with the added keys dropped, after proving each one is generated and new. */
function generatedWithoutAddedKeys(
  name: string,
  got: Record<string, unknown>,
  want: Record<string, unknown>,
): Record<string, unknown> {
  const remaining: Record<string, unknown> = { ...got };
  for (const [key, reason] of Object.entries(ADDED_KEYS)) {
    assert.ok(key in got, `${name}: ${key} is not generated, remove it from ADDED_KEYS (${reason})`);
    assert.ok(!(key in want), `${name}: ${key} is in the oracle, remove it from ADDED_KEYS (${reason})`);
    delete remaining[key];
  }
  return remaining;
}

/** The oracle with the retired keys dropped, after proving each one is in it. */
function oracleWithoutRetiredKeys(name: string, want: Record<string, unknown>): Record<string, unknown> {
  const remaining: Record<string, unknown> = { ...want };
  for (const [key, reason] of Object.entries(RETIRED_KEYS)) {
    assert.ok(key in want, `${name}: ${key} is no longer in the oracle, remove it from RETIRED_KEYS (${reason})`);
    delete remaining[key];
  }
  return remaining;
}

describe('layer A: generated config equals the Python generator, key for key', () => {
  const fixtures = capturedFixtures();
  assert.ok(fixtures.length > 0, `Required fixture directory has no values.yml and flat.json oracle pairs: ${fixturesDir}`);
  for (const fixture of fixtures) {
    it(fixture.name, () => {
      const outDir = tempDir();
      generateConfig(fixture.valuesFile, outDir);
      const want = oracleWithoutRetiredKeys(fixture.name, readJsonRecord(fixture.flatFile));
      const got = generatedWithoutAddedKeys(fixture.name, flattenConfigDir(outDir), want);
      const differences = reportDifferences(got, want);
      console.log(
        `${fixture.name}: ${Object.keys(got).length} keys generated, ` +
          `${Object.keys(want).length} in flat.json, ` +
          `${differences.length === 0 ? 'PASS' : 'FAIL'}`,
      );
      if (differences.length > 0) console.log(differences.join('\n'));
      assert.deepStrictEqual(got, want);
      assert.ok(
        existsSync(fixture.manifestFile),
        `${fixture.name}: missing Python-parsed idea.yml oracle at ${fixture.manifestFile}`,
      );
      assert.deepStrictEqual(
        readManifest(join(outDir, 'idea.yml')),
        readJsonRecord(fixture.manifestFile),
      );
    });
  }
});

const runtimeKeys = readRuntimeKeys(join(here, 'runtime-keys.json'));

describe('layer D: every template-produced key the runtime reads is generated', () => {
  it('holds the runtime-read inventory, expanded to concrete keys', () => {
    // The inventory counts key patterns: `<app>.server.port` is one entry there and three keys here, as are
    // `<day>`, `<state>`, `<os>`, `<family>` and the `<module>.module_id` mappings.
    const distinct = new Set(RUNTIME_KEY_GROUPS.flatMap((group) => runtimeKeys[group] as string[]));
    assert.equal(distinct.size, 686);
    // Spot-check expanded key identities.
    for (const key of [
      'global-settings.module_sets.default.virtual-desktop-controller.module_id',
      'global-settings.gpu_settings.nvidia_public_driver_versions.production_version',
      'global-settings.package_config.linux_packages.system',
      'vdc.controller.autoscaling.rolling_update_policy.pause_time_minutes',
      'vdc.dcv_broker.autoscaling.instance_type',
      'vdc.dcv_connection_gateway.autoscaling.metadata_http_tokens',
      'vdc.dcv_session.schedule.sunday.shut_down_time',
      'vdc.dcv_session.working_hours.start_up_time',
      'vdc.vdi_host_backup.backup_plan.rules.default.schedule_expression',
      'cluster.logging.profiles.production.loggers.app.level',
    ]) {
      assert.ok(runtimeKeys.all?.includes(key), `runtime-keys.json is missing ${key}`);
    }
  });
  const fixtures = capturedFixtures();
  assert.ok(fixtures.length > 0, `Required fixture directory has no values.yml and flat.json oracle pairs: ${fixturesDir}`);
  for (const fixture of fixtures) {
    it(fixture.name, () => {
      const outDir = tempDir();
      generateConfig(fixture.valuesFile, outDir);
      checkRuntimeKeys(fixture.name, flattenConfigDir(outDir), runtimeKeys);
    });
  }
});

/** Every module in `values.yml`'s optional set enabled; the layer-D inventory assumes this shape. */
const FULL_MODULE_COUNT = 11;

interface SyntheticExpectation {
  modules: number;
  present: Record<string, unknown>;
  absent: string[];
}

const syntheticExpectations: Record<string, SyntheticExpectation> = {
  'activedirectory-existing.yml': {
    modules: 11,
    present: {
      'cluster.network.use_existing_vpc': true,
      'directoryservice.provider': 'activedirectory',
      'directoryservice.root_credentials_provided': true,
    },
    absent: ['directoryservice.ad_edition'],
  },
  'alb-acm-certificate.yml': {
    modules: 11,
    present: {
      'cluster.load_balancers.external_alb.certificates.provided': true,
      'cluster.load_balancers.external_alb.certificates.custom_dns_name': 'idea.example.invalid',
    },
    absent: [],
  },
  'alb-private.yml': {
    modules: 11,
    present: {
      'cluster.load_balancers.external_alb.public': false,
      'cluster.load_balancers.external_alb.waf.enabled': false,
      'bastion-host.public': false,
    },
    absent: [],
  },
  'base-os-rhel9.yml': {
    modules: 11,
    present: {
      'bastion-host.base_os': 'rhel9',
      'cluster-manager.ec2.autoscaling.base_os': 'rhel9',
      'scheduler.base_os': 'rhel9',
      'vdc.controller.autoscaling.base_os': 'rhel9',
    },
    absent: [],
  },
  'existing-apps-fs.yml': {
    modules: 11,
    present: {
      'cluster.network.use_existing_vpc': true,
      'shared-storage.apps.efs.use_existing_fs': true,
    },
    absent: ['shared-storage.apps.efs.performance_mode'],
  },
  'existing-opensearch.yml': {
    modules: 11,
    present: {
      'analytics.opensearch.use_existing': true,
      'analytics.opensearch.domain_vpc_endpoint_url': 'vpc-idea-test1.us-east-2.es.amazonaws.com',
    },
    absent: ['analytics.opensearch.data_nodes'],
  },
  'fsx-lustre-apps.yml': {
    modules: 11,
    present: {
      'shared-storage.apps.provider': 'fsx_lustre',
      'shared-storage.apps.fsx_lustre.storage_type': 'SSD',
      'shared-storage.apps.fsx_lustre.storage_capacity': 1200,
    },
    absent: ['shared-storage.apps.efs.performance_mode'],
  },
  'gateway-certificate.yml': {
    modules: 11,
    present: {
      'vdc.dcv_connection_gateway.certificate.provided': true,
      'vdc.dcv_connection_gateway.certificate.custom_dns_name': 'gateway.example.invalid',
      'vdc.dcv_session.quic_support': true,
    },
    absent: [],
  },
  'kms-customer-managed.yml': {
    modules: 11,
    present: {
      'cluster.kms.key_type': 'customer-managed',
      'analytics.kinesis.kms_key_id': '00000000-1111-2222-3333-444444444444',
      'shared-storage.data.efs.kms_key_id': '00000000-1111-2222-3333-444444444444',
    },
    absent: [],
  },
  'metrics-amp.yml': {
    modules: 11,
    present: {
      'metrics.provider': 'amazon_managed_prometheus',
      'metrics.amazon_managed_prometheus.workspace_name': 'idea-test1-workspace',
      'metrics.prometheus.remote_write.url': null,
    },
    absent: ['metrics.cloudwatch.dashboard_name'],
  },
  'metrics-dogstatsd.yml': {
    modules: 11,
    present: {
      'metrics.provider': 'dogstatsd',
      'metrics.dogstatsd.url': 'udp://127.0.0.1:8125',
    },
    absent: ['metrics.cloudwatch.dashboard_name'],
  },
  'metrics-prometheus.yml': {
    modules: 11,
    present: {
      'metrics.provider': 'prometheus',
      'metrics.prometheus.remote_write.url': 'https://prometheus.example.invalid/api/v1/write',
      'global-settings.package_config.prometheus.installer.linux.x86_64':
        'https://github.com/prometheus/prometheus/releases/download/v2.53.5/prometheus-2.53.5.linux-amd64.tar.gz',
    },
    absent: ['metrics.cloudwatch.dashboard_name'],
  },
  'no-optional-modules.yml': {
    modules: 7,
    present: { 'cluster.cluster_name': 'idea-test1' },
    absent: ['metrics.provider', 'scheduler.provider', 'vdc.dcv_session.idle_timeout'],
  },
  'openldap.yml': {
    modules: 11,
    present: {
      'directoryservice.provider': 'openldap',
      'directoryservice.hostname': 'openldap.idea-test1.us-east-2.local',
      'directoryservice.ec2.metadata_http_tokens': 'required',
    },
    absent: ['directoryservice.ad_edition'],
  },
  'vpc-endpoints-existing-vpc.yml': {
    modules: 11,
    present: {
      'cluster.network.use_existing_vpc': true,
      'cluster.network.use_vpc_endpoints': true,
      'cluster.network.vpc_gateway_endpoints': [],
      'cluster.network.vpc_interface_endpoints': null,
    },
    absent: ['cluster.network.vpc_cidr_block'],
  },
};

/** The unmodified base overlay, so a case's expectations can be proved branch-specific. */
function baselineFlat(): Record<string, unknown> {
  const outDir = tempDir();
  generateConfigFromTemplates(syntheticValues(join(syntheticDir, 'base.yml')), outDir);
  return flattenConfigDir(outDir);
}

describe('synthetic branches render', () => {
  const baseline = baselineFlat();
  for (const caseFile of syntheticCases(syntheticDir)) {
    it(caseFile, () => {
      const outDir = tempDir();
      const modules = generateConfigFromTemplates(
        syntheticValues(join(syntheticDir, caseFile)),
        outDir,
      );
      const flat = flattenConfigDir(outDir);
      console.log(`${caseFile}: ${modules.length} modules, ${Object.keys(flat).length} keys`);
      const expected = syntheticExpectations[caseFile];
      assert.ok(expected !== undefined, `no expectation for ${caseFile}`);
      assert.equal(modules.length, expected.modules);
      for (const [key, value] of Object.entries(expected.present)) {
        assert.deepStrictEqual(flat[key], value, `${caseFile}: ${key}`);
      }
      for (const key of expected.absent) {
        assert.ok(!(key in flat), `${caseFile}: ${key} must be absent`);
      }
      // A case that only asserts values the baseline already produces asserts nothing about its
      // own branch, so at least one assertion has to disagree with the baseline output.
      const distinguishing = [
        ...Object.entries(expected.present)
          .filter(([key, value]) => JSON.stringify(baseline[key]) !== JSON.stringify(value))
          .map(([key]) => key),
        ...expected.absent.filter((key) => key in baseline),
      ];
      assert.ok(
        distinguishing.length > 0,
        `${caseFile}: every assertion also holds for base.yml - assert a value ` +
          'only this branch produces',
      );
      // The layer-D inventory is scoped to a full module set; the reduced case is covered by its
      // own module count and absence assertions.
      if (expected.modules === FULL_MODULE_COUNT) checkRuntimeKeys(caseFile, flat, runtimeKeys);
    });
  }
});

const VALUES_SOURCE = fileURLToPath(new URL('../../src/config/values.ts', import.meta.url));
const BASE_YAML = readFileSync(join(syntheticDir, 'base.yml'), 'utf-8');

/**
 * Run a values.ts number parser from the product source. The functions are not exported, and
 * `new Function` does not strip types, so only the signature annotations are removed.
 */
function productNumberParser(name: string): (text: string) => number | undefined {
  const source = readFileSync(VALUES_SOURCE, 'utf-8');
  const header = `function ${name}(text: string): number | undefined {`;
  const start = source.indexOf(header);
  assert.notEqual(start, -1, `values.ts must define ${name}`);
  let depth = 0;
  let end = -1;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) {
        end = i + 1;
        break;
      }
    }
  }
  assert.ok(end > start, `values.ts ${name} body must close`);
  const runnable = source.slice(start, end).replace(
    `function ${name}(text: string): number | undefined`,
    `function ${name}(text)`,
  );
  return new Function(`${runnable}; return ${name};`)() as (text: string) => number | undefined;
}

/**
 * base.yml with the overlay's top-level keys replaced, written out and read back through the real
 * loader - the scalar rules under test live there, so an overlay merged as JavaScript would not
 * exercise them. (js-yaml rejects a duplicated mapping key, so the base entry has to go.)
 */
function valuesFromYaml(overlay: string): UserValues {
  const replaced = new Set([...overlay.matchAll(/^([^\s#][^:]*):/gm)].map((match) => match[1]));
  const kept: string[] = [];
  let dropping = false;
  for (const line of BASE_YAML.split('\n')) {
    const key = /^([^\s#][^:]*):/.exec(line)?.[1];
    if (key !== undefined) dropping = replaced.has(key);
    else if (!/^\s/.test(line)) dropping = false;
    if (!dropping) kept.push(line);
  }
  const file = join(tempDir(), 'values.yml');
  writeFileSync(file, `${kept.join('\n')}\n${overlay}`);
  return loadValuesFile(file);
}

describe('values coercion matches Python', () => {
  it('uses the default for a non-integral boolean scalar', () => {
    const values = { ...syntheticValues(join(syntheticDir, 'base.yml')), use_existing_vpc: 0.5 };
    const context = buildContext(values);
    assert.equal(context.use_existing_vpc, false);
    assert.equal(context.vpc_cidr_block, '203.0.113.0/24');
  });

  it('treats an integral YAML float as a float, not an int', () => {
    // model_utils.get_as_bool: isinstance(1.0, int) is False, so the default wins.
    const context = buildContext(valuesFromYaml('use_existing_vpc: 1.0\n'));
    assert.equal(context.use_existing_vpc, false);
    assert.equal(context.vpc_cidr_block, '203.0.113.0/24');
  });

  it('resolves the YAML 1.1 boolean scalars PyYAML resolves', () => {
    const context = buildContext(
      valuesFromYaml('alb_public: off\ndcv_session_quic_support: on\n'),
    );
    assert.equal(context.alb_public, false);
    assert.equal(context.dcv_session_quic_support, true);
  });

  it('stringifies non-strings with Python str()', () => {
    const context = buildContext(
      valuesFromYaml('administrator_username:\n  - foo\n  - bar\n'),
    );
    assert.equal(context.administrator_username, "['foo', 'bar']");
  });

  it('keeps a boolean as a boolean where Python reads an int', () => {
    // bool is a subclass of int, so get_as_int returns it unchanged.
    const context = buildContext(valuesFromYaml('volume_size: true\n'));
    assert.equal(context.volume_size, true);
  });

  it('rejects a hexadecimal string where int() and float() both raise', () => {
    const context = buildContext(valuesFromYaml('volume_size: "0x10"\n'));
    assert.equal(context.volume_size, 200);
  });

  it('accepts the numeric strings int() and float() accept', () => {
    const pyIntFromString = productNumberParser('pyIntFromString');
    const pyFloatFromString = productNumberParser('pyFloatFromString');
    // int() accepts underscored decimals; float() also accepts "1_000", so the context
    // value alone cannot tell the two parsers apart. Call each parser on the product source.
    assert.equal(pyIntFromString(' 1_000 '), 1000);
    assert.equal(pyIntFromString('1_000.5'), undefined);
    assert.equal(pyFloatFromString(' 1_000 '), 1000);
    assert.equal(pyFloatFromString('1_000.5'), 1000.5);
    assert.equal(pyFloatFromString('12.9'), 12.9);

    const context = buildContext(
      valuesFromYaml(
        'volume_size: " 1_000 "\ndcv_broker_volume_size: "12.9"\ndcv_connection_gateway_volume_size: "1_000.5"\n',
      ),
    );
    assert.equal(context.volume_size, 1000);
    assert.equal(context.dcv_broker_volume_size, 12);
    // "1_000.5" is not an int(); only the float() fallback can produce 1000 here.
    assert.equal(context.dcv_connection_gateway_volume_size, 1000);
  });
});

describe('readConfigFromFiles rejects a settings file that is not a mapping', () => {
  function generateBase(): string {
    const outDir = tempDir();
    generateConfigFromTemplates(syntheticValues(join(syntheticDir, 'base.yml')), outDir);
    return outDir;
  }

  it('an emptied settings file stops the read', () => {
    const outDir = generateBase();
    writeFileSync(join(outDir, 'cluster', 'settings.yml'), '# nothing left\n');
    assert.throws(
      () => readConfigFromFiles(outDir),
      /expected a YAML mapping, got an empty document/,
    );
  });

  it('a settings file holding a list stops the read', () => {
    const outDir = generateBase();
    writeFileSync(join(outDir, 'cluster', 'settings.yml'), '- one\n- two\n');
    assert.throws(() => readConfigFromFiles(outDir), /expected a YAML mapping, got a list/);
  });

  it('an emptied idea.yml stops the read', () => {
    const outDir = generateBase();
    writeFileSync(join(outDir, 'idea.yml'), '\n');
    assert.throws(
      () => readConfigFromFiles(outDir),
      /expected a YAML mapping, got an empty document/,
    );
  });
});

describe('error paths reject the values file', () => {
  const errorsDir = join(syntheticDir, 'errors');
  const expected: Record<string, RegExp> = {
    'eol-base-os.yml': /end-of-life/,
    'el10-with-evdi.yml': /not supported with the virtual-desktop-controller module/,
    'arm64-flat-ami-file.yml': /architecture: arm64 was requested/,
    'prometheus-without-url.yml': /prometheus_remote_write_url is required/,
    'dogstatsd-ecs-without-secret.yml': /datadog_api_key_secret_arn is required/,
    'existing-apps-fs-new-vpc.yml': /use_existing_apps_fs cannot be True/,
  };
  for (const caseFile of syntheticCases(errorsDir)) {
    it(caseFile, () => {
      const pattern = expected[caseFile];
      assert.ok(pattern !== undefined, `no expectation for ${caseFile}`);
      const outDir = join(tempDir(), 'config');
      mkdirSync(outDir, { recursive: true });
      assert.throws(
        () => generateConfigFromTemplates(syntheticValues(join(errorsDir, caseFile)), outDir),
        pattern,
      );
      console.log(`${caseFile}: threw ${pattern}`);
    });
  }
});
