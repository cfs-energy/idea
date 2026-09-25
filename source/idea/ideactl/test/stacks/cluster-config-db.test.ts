/**
 * Tests for the ClusterConfigDb write side, run against a real DynamoDB Local.
 *
 * Endpoint resolution, in order: $DDB_LOCAL_ENDPOINT, a `java -jar` of
 * ~/.idea/lib/dynamodb-local/DynamoDBLocal.jar on a free port, or `docker run amazon/dynamodb-local`
 * on a free port. A missing emulator fails before a client is configured.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before, beforeEach, describe } from 'node:test';
import { pathToFileURL } from 'node:url';

import {
  DeleteTableCommand,
  DescribeTableCommand,
  DynamoDBClient,
  ListTablesCommand,
  ScanCommand,
  type AttributeValue,
  type DynamoDBClientConfig,
} from '@aws-sdk/client-dynamodb';

import { ClusterConfigDb, ClusterConfigDbError, type ConfigEntry } from '../../src/config/cluster-config-db.ts';
import { compareUpgradeDrift, type CurrentConfigRow } from '../../src/config/upgrade-drift.ts';
import { optionalFixtures, requiredService } from '../support/fixtures.ts';

const JAR = path.join(os.homedir(), '.idea/lib/dynamodb-local/DynamoDBLocal.jar');
const ORACLE_FILE = path.resolve(
  import.meta.dirname,
  '../../tools/parity/fixtures/w19-oracle/oracle.json',
);
const oracleAvailable = optionalFixtures(
  [ORACLE_FILE],
  'recapture the reference oracle from a live cluster',
);

interface OracleModule {
  id: string;
  name: string;
  type: string;
  [key: string]: unknown;
}

interface OracleTables {
  clusterSettings: Array<Record<string, unknown>>;
  modules: Array<Record<string, unknown>>;
}

interface OracleStep {
  name: string;
  action: 'sync' | 'replace-prefix';
  modules?: OracleModule[];
  entries: ConfigEntry[];
  overwrite?: boolean;
  prefix?: string;
  tables: OracleTables;
}

interface OracleInterruption {
  prefix: string;
  modules: OracleModule[];
  seed: ConfigEntry[];
  replacement: ConfigEntry[];
  fullAddOnly: ConfigEntry[];
  exitCode: number;
  partialTables: OracleTables;
  recoveredTables: OracleTables;
}

interface Oracle {
  formatVersion: number;
  reference: {
    clusterName: string;
    region: string;
    writerSourceSha256: string;
  };
  main: { steps: OracleStep[] };
  interruption: OracleInterruption;
}

/** Require a plain JSON object before reading its fields. */
function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

/** Require a JSON string. */
function asString(value: unknown, label: string): string {
  if (typeof value !== 'string') throw new TypeError(`${label} must be a string`);
  return value;
}

/** Require a JSON boolean. */
function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw new TypeError(`${label} must be a boolean`);
  return value;
}

/** Require a finite JSON number. */
function asNumber(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`${label} must be a finite number`);
  }
  return value;
}

/** Require a JSON array. */
function asArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new TypeError(`${label} must be an array`);
  return value;
}

/** Validate captured configuration entries, including required null values. */
function parseEntries(value: unknown, label: string): ConfigEntry[] {
  return asArray(value, label).map((rawEntry, index) => {
    const entry = asRecord(rawEntry, `${label}[${index}]`);
    if (!Object.hasOwn(entry, 'value')) {
      throw new TypeError(`${label}[${index}].value is required`);
    }
    return {
      key: asString(entry['key'], `${label}[${index}].key`),
      value: entry['value'],
    };
  });
}

/** Validate captured module inputs. */
function parseModules(value: unknown, label: string): OracleModule[] {
  return asArray(value, label).map((rawModule, index) => {
    const module = asRecord(rawModule, `${label}[${index}]`);
    return {
      id: asString(module['id'], `${label}[${index}].id`),
      name: asString(module['name'], `${label}[${index}].name`),
      type: asString(module['type'], `${label}[${index}].type`),
    };
  });
}

/** Validate the two raw table snapshots. */
function parseTables(value: unknown, label: string): OracleTables {
  const tables = asRecord(value, label);
  const parseRows = (rawRows: unknown, rowsLabel: string): Array<Record<string, unknown>> =>
    asArray(rawRows, rowsLabel).map((row, index) =>
      asRecord(row, `${rowsLabel}[${index}]`),
    );
  return {
    clusterSettings: parseRows(
      tables['clusterSettings'],
      `${label}.clusterSettings`,
    ),
    modules: parseRows(tables['modules'], `${label}.modules`),
  };
}

/** Read and validate the private reference capture. */
function parseOracle(): Oracle {
  const parsed: unknown = JSON.parse(fs.readFileSync(ORACLE_FILE, 'utf8'));
  const root = asRecord(parsed, 'oracle');
  const reference = asRecord(root['reference'], 'oracle.reference');
  const main = asRecord(root['main'], 'oracle.main');
  const interruption = asRecord(root['interruption'], 'oracle.interruption');

  const steps = asArray(main['steps'], 'oracle.main.steps').map(
    (rawStep, index): OracleStep => {
      const label = `oracle.main.steps[${index}]`;
      const step = asRecord(rawStep, label);
      const action = asString(step['action'], `${label}.action`);
      if (action !== 'sync' && action !== 'replace-prefix') {
        throw new TypeError(`${label}.action is invalid`);
      }
      const modules =
        step['modules'] === undefined
          ? undefined
          : parseModules(step['modules'], `${label}.modules`);
      const overwrite =
        step['overwrite'] === undefined
          ? undefined
          : asBoolean(step['overwrite'], `${label}.overwrite`);
      const prefix =
        step['prefix'] === undefined
          ? undefined
          : asString(step['prefix'], `${label}.prefix`);
      return {
        name: asString(step['name'], `${label}.name`),
        action,
        modules,
        entries: parseEntries(step['entries'], `${label}.entries`),
        overwrite,
        prefix,
        tables: parseTables(step['tables'], `${label}.tables`),
      };
    },
  );

  return {
    formatVersion: asNumber(root['formatVersion'], 'oracle.formatVersion'),
    reference: {
      clusterName: asString(
        reference['clusterName'],
        'oracle.reference.clusterName',
      ),
      region: asString(reference['region'], 'oracle.reference.region'),
      writerSourceSha256: asString(
        reference['writerSourceSha256'],
        'oracle.reference.writerSourceSha256',
      ),
    },
    main: { steps },
    interruption: {
      prefix: asString(interruption['prefix'], 'oracle.interruption.prefix'),
      modules: parseModules(
        interruption['modules'],
        'oracle.interruption.modules',
      ),
      seed: parseEntries(
        interruption['seed'],
        'oracle.interruption.seed',
      ),
      replacement: parseEntries(
        interruption['replacement'],
        'oracle.interruption.replacement',
      ),
      fullAddOnly: parseEntries(
        interruption['fullAddOnly'],
        'oracle.interruption.fullAddOnly',
      ),
      exitCode: asNumber(
        interruption['exitCode'],
        'oracle.interruption.exitCode',
      ),
      partialTables: parseTables(
        interruption['partialTables'],
        'oracle.interruption.partialTables',
      ),
      recoveredTables: parseTables(
        interruption['recoveredTables'],
        'oracle.interruption.recoveredTables',
      ),
    },
  };
}

async function freePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as net.AddressInfo).port;
      server.close(() => resolve(port));
    });
  });
}

/**
 * Wait for a real HTTP response, not just an accepted socket: a container port-forward accepts
 * TCP before the process behind it is listening, and the SDK then sees ECONNRESET.
 */
async function waitForPort(port: number, timeoutMs: number): Promise<boolean> {
  const http = await import('node:http');
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise<boolean>((resolve) => {
      const req = http.request({ host: '127.0.0.1', port, method: 'GET', path: '/', timeout: 2000 }, (res) => {
        res.resume();
        resolve(true);
      });
      req.on('error', () => resolve(false));
      req.on('timeout', () => {
        req.destroy();
        resolve(false);
      });
      req.end();
    });
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

interface Local {
  endpoint: string;
  stop: () => void;
  how: string;
}

async function startDynamoDbLocal(): Promise<Local> {
  if (process.env['DDB_LOCAL_ENDPOINT']) {
    return { endpoint: process.env['DDB_LOCAL_ENDPOINT'], stop: () => {}, how: 'DDB_LOCAL_ENDPOINT' };
  }

  const javaOk = spawnSync('java', ['-version'], { stdio: 'ignore' }).status === 0;
  if (javaOk && fs.existsSync(JAR)) {
    const port = await freePort();
    const { spawn } = await import('node:child_process');
    const child = spawn('java', [`-Djava.library.path=${path.dirname(JAR)}/DynamoDBLocal_lib`, '-jar', JAR, '-inMemory', '-port', String(port)], {
      stdio: 'ignore',
      cwd: path.dirname(JAR),
    });
    if (!(await waitForPort(port, 30_000))) {
      child.kill('SIGKILL');
      return requiredService("local database emulator", `java -jar ${JAR} -inMemory -port ${port}`);
    }
    return { endpoint: `http://127.0.0.1:${port}`, stop: () => child.kill('SIGKILL'), how: 'java -jar' };
  }

  const dockerOk = spawnSync('docker', ['info', '--format', '{{.ServerVersion}}'], { stdio: 'ignore' }).status === 0;
  if (dockerOk) {
    const port = await freePort();
    const name = `ideactl-ddb-${process.pid}-${port}`;
    const run = spawnSync('docker', ['run', '-d', '--rm', '-p', `127.0.0.1:${port}:8000`, '--name', name, 'amazon/dynamodb-local'], {
      encoding: 'utf8',
    });
    if (run.status !== 0) {
      return requiredService("local database emulator", "docker run --rm -p 8000:8000 amazon/dynamodb-local");
    }
    if (!(await waitForPort(port, 60_000))) {
      spawnSync('docker', ['rm', '-f', name], { stdio: 'ignore' });
      return requiredService("local database emulator", "docker run --rm -p 8000:8000 amazon/dynamodb-local");
    }
    return {
      endpoint: `http://127.0.0.1:${port}`,
      stop: () => spawnSync('docker', ['rm', '-f', name], { stdio: 'ignore' }),
      how: 'docker amazon/dynamodb-local',
    };
  }

  return requiredService(
    "local database emulator",
    `java -jar ${JAR} -inMemory -port 8000, or docker run --rm -p 8000:8000 amazon/dynamodb-local`,
  );
}

let local: Local | undefined;
let client: DynamoDBClient;
let clientConfig: DynamoDBClientConfig;

before(async () => {
  const started = await startDynamoDbLocal();
  local = started;
  console.log(`# DynamoDB Local via ${started.how} at ${started.endpoint}`);
  const { NodeHttpHandler } = await import('@smithy/node-http-handler');
  const http = await import('node:http');
  clientConfig = {
    region: 'us-east-1',
    endpoint: started.endpoint,
    credentials: { accessKeyId: 'local', secretAccessKey: 'local' },
    // DynamoDB Local closes idle keep-alive sockets; without this the SDK reuses a dead one
    requestHandler: new NodeHttpHandler({ httpAgent: new http.Agent({ keepAlive: false }) }),
  };
  client = new DynamoDBClient(clientConfig);
});

after(() => local?.stop());

/** Return the synthetic cluster name cleared before each test. */
function nextCluster(): string {
  return 'idea-test1';
}

async function open(cluster: string, kmsKeyId?: string): Promise<ClusterConfigDb> {
  return await ClusterConfigDb.open({
    clusterName: cluster,
    client,
    createDatabase: true,
    dynamodbKmsKeyId: kmsKeyId,
  });
}

/**
 * DynamoDB Local 3.3.1 echoes back neither SSEDescription nor Tags, so those two arguments are
 * checked on the outgoing CreateTable input instead. Everything else is read back off the table.
 */
async function captureCreateTables(
  cluster: string,
  kmsKeyId?: string,
): Promise<Record<string, Record<string, unknown>>> {
  const captured: Record<string, Record<string, unknown>> = {};
  const capturing = new DynamoDBClient(clientConfig);
  capturing.middlewareStack.add(
    (next, context) => async (args) => {
      if (context.commandName === 'CreateTableCommand') {
        const input = args.input as Record<string, unknown>;
        captured[String(input['TableName'])] = input;
      }
      return await next(args);
    },
    { step: 'initialize' },
  );
  await ClusterConfigDb.open({ clusterName: cluster, client: capturing, createDatabase: true, dynamodbKmsKeyId: kmsKeyId });
  capturing.destroy();
  return captured;
}

/** Remove prior synthetic tables when a shared local endpoint is supplied. */
async function clearTables(cluster: string): Promise<void> {
  for (const suffix of ['cluster-settings', 'modules']) {
    try {
      await client.send(
        new DeleteTableCommand({ TableName: `${cluster}.${suffix}` }),
      );
    } catch (error) {
      if (
        !(error instanceof Error) ||
        error.name !== 'ResourceNotFoundException'
      ) {
        throw error;
      }
    }
  }
}

beforeEach(async () => {
  await clearTables('idea-test1');
});

/** raw attribute-value dump of a table, sorted by hash key: the parity artefact */
async function dump(table: string, hashKey: string): Promise<Array<Record<string, AttributeValue>>> {
  const rows: Array<Record<string, AttributeValue>> = [];
  let startKey: Record<string, AttributeValue> | undefined;
  do {
    const result = await client.send(new ScanCommand({ TableName: table, ExclusiveStartKey: startKey }));
    rows.push(...(result.Items ?? []));
    startKey = result.LastEvaluatedKey;
  } while (startKey !== undefined);
  rows.sort((a, b) => String(a[hashKey]?.S).localeCompare(String(b[hashKey]?.S)));
  return rows;
}

/** `{key: value-attribute}` with `version` split out, so values compare exactly and versions as deltas */
async function settings(db: ClusterConfigDb): Promise<{
  values: Record<string, AttributeValue | undefined>;
  versions: Record<string, number>;
}> {
  const values: Record<string, AttributeValue | undefined> = {};
  const versions: Record<string, number> = {};
  for (const row of await dump(db.clusterSettingsTableName, 'key')) {
    const key = String(row['key']?.S);
    values[key] = row['value'];
    versions[key] = Number(row['version']?.N);
  }
  return { values, versions };
}

function versionDeltas(before_: Record<string, number>, after_: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const key of Object.keys(after_)) out[key] = (after_[key] as number) - (before_[key] ?? 0);
  return out;
}

/** Read both port tables without document conversion. */
async function rawTables(db: ClusterConfigDb): Promise<OracleTables> {
  return {
    clusterSettings: await dump(db.clusterSettingsTableName, 'key'),
    modules: await dump(db.modulesTableName, 'module_id'),
  };
}

/** Extract a raw string hash key for an item-level failure message. */
function rowKey(row: Record<string, unknown>, hashKey: string, label: string): string {
  const attribute = asRecord(row[hashKey], `${label}.${hashKey}`);
  return asString(attribute['S'], `${label}.${hashKey}.S`);
}

/** Compare one table in stable hash-key order. */
function compareRows(
  scenario: string,
  table: string,
  hashKey: string,
  actual: Array<Record<string, unknown>>,
  expected: Array<Record<string, unknown>>,
): void {
  assert.equal(
    actual.length,
    expected.length,
    `${scenario} ${table} row count`,
  );
  for (let index = 0; index < expected.length; index += 1) {
    const expectedRow = expected[index];
    const actualRow = actual[index];
    assert.ok(expectedRow, `${scenario} ${table} expected row ${index}`);
    assert.ok(actualRow, `${scenario} ${table} actual row ${index}`);
    const expectedKey = rowKey(
      expectedRow,
      hashKey,
      `${scenario}.${table}.expected[${index}]`,
    );
    assert.equal(
      rowKey(
        actualRow,
        hashKey,
        `${scenario}.${table}.actual[${index}]`,
      ),
      expectedKey,
      `${scenario} ${table} item ${index} key`,
    );
    assert.deepEqual(
      actualRow,
      expectedRow,
      `${scenario} ${table} item ${expectedKey}`,
    );
  }
}

/** Compare both table snapshots and print a compact evidence line. */
function compareTables(
  scenario: string,
  actual: OracleTables,
  expected: OracleTables,
): void {
  compareRows(
    scenario,
    'cluster-settings',
    'key',
    actual.clusterSettings,
    expected.clusterSettings,
  );
  compareRows(
    scenario,
    'modules',
    'module_id',
    actual.modules,
    expected.modules,
  );
  console.log(
    `ORACLE ${scenario} cluster-settings=${actual.clusterSettings.length} modules=${actual.modules.length}`,
  );
}

/** Replay one captured reference action through the port writer. */
async function applyOracleStep(
  db: ClusterConfigDb,
  step: OracleStep,
): Promise<void> {
  if (step.action === 'replace-prefix') {
    if (step.prefix === undefined) {
      throw new TypeError(`${step.name}.prefix is required`);
    }
    await db.deleteConfigEntries(step.prefix);
    await db.syncClusterSettingsInDb(step.entries, true);
    return;
  }
  if (step.modules !== undefined) {
    await db.syncModulesInDb(step.modules);
  }
  await db.syncClusterSettingsInDb(step.entries, step.overwrite ?? false);
}

describe('ClusterConfigDb write side', () => {
  test('create-tables: key schema, billing, stream, tags, no SSE without a kms key', async () => {
    const cluster = nextCluster();
    const captured = await captureCreateTables(cluster);
    const db = await open(cluster);

    const tables = await client.send(new ListTablesCommand({}));
    assert.ok(tables.TableNames?.includes(`${cluster}.modules`));
    assert.ok(tables.TableNames?.includes(`${cluster}.cluster-settings`));

    assert.deepEqual(captured[`${cluster}.modules`]?.['Tags'], [
      { Key: 'idea:ClusterName', Value: cluster },
      { Key: 'idea:BackupPlan', Value: `${cluster}-cluster` },
    ]);
    assert.deepEqual(captured[`${cluster}.cluster-settings`]?.['Tags'], [
      { Key: 'idea:ClusterName', Value: cluster },
    ]);
    assert.equal(captured[`${cluster}.modules`]?.['SSESpecification'], undefined);
    assert.equal(captured[`${cluster}.cluster-settings`]?.['SSESpecification'], undefined);

    const modules = (
      await client.send(
        new DescribeTableCommand({ TableName: db.modulesTableName }),
      )
    ).Table;
    assert.ok(modules);
    assert.deepEqual(modules.KeySchema, [{ AttributeName: 'module_id', KeyType: 'HASH' }]);
    assert.deepEqual(modules.AttributeDefinitions, [{ AttributeName: 'module_id', AttributeType: 'S' }]);
    assert.equal(modules.BillingModeSummary?.BillingMode, 'PAY_PER_REQUEST');
    assert.equal(modules.StreamSpecification, undefined);

    const cs = (
      await client.send(
        new DescribeTableCommand({ TableName: db.clusterSettingsTableName }),
      )
    ).Table;
    assert.ok(cs);
    assert.deepEqual(cs.KeySchema, [{ AttributeName: 'key', KeyType: 'HASH' }]);
    assert.deepEqual(cs.AttributeDefinitions, [{ AttributeName: 'key', AttributeType: 'S' }]);
    assert.equal(cs.BillingModeSummary?.BillingMode, 'PAY_PER_REQUEST');
    assert.deepEqual(cs.StreamSpecification, { StreamEnabled: true, StreamViewType: 'NEW_AND_OLD_IMAGES' });
  });

  test('create-tables: tags, and SSE-KMS only when cluster.dynamodb.kms_key_id is set', async () => {
    const plain = nextCluster();
    const withoutKms = await captureCreateTables(plain);
    assert.deepEqual(withoutKms[`${plain}.modules`]?.['Tags'], [
      { Key: 'idea:ClusterName', Value: plain },
      { Key: 'idea:BackupPlan', Value: `${plain}-cluster` },
    ]);
    assert.deepEqual(withoutKms[`${plain}.cluster-settings`]?.['Tags'], [{ Key: 'idea:ClusterName', Value: plain }]);
    assert.equal(withoutKms[`${plain}.modules`]?.['SSESpecification'], undefined);
    assert.equal(withoutKms[`${plain}.cluster-settings`]?.['SSESpecification'], undefined);

    await clearTables(plain);
    const kms = nextCluster();
    const withKms = await captureCreateTables(kms, 'alias/idea-test-key');
    const expected = { Enabled: true, SSEType: 'KMS', KMSMasterKeyId: 'alias/idea-test-key' };
    assert.deepEqual(withKms[`${kms}.modules`]?.['SSESpecification'], expected);
    assert.deepEqual(withKms[`${kms}.cluster-settings`]?.['SSESpecification'], expected);
  });

  test('open without createDatabase fails when the tables are missing', async () => {
    await assert.rejects(
      () => ClusterConfigDb.open({ clusterName: nextCluster(), client }),
      (e: Error) => e instanceof ClusterConfigDbError && /Configuration tables not found/.test(e.message),
    );
  });

  test('cluster_name validation: empty and > 11 characters', async () => {
    await assert.rejects(
      () => ClusterConfigDb.open({ clusterName: '', client, createDatabase: true }),
      /cluster_name is required/,
    );
    await assert.rejects(
      () => ClusterConfigDb.open({ clusterName: 'sample-cluster', client, createDatabase: true }),
      /cannot be more than 11 characters. current: 14/,
    );
  });

  test('typing: bool -> BOOL, int/float -> N string, str -> S, list -> L, null -> NULL, dict -> M', async () => {
    const db = await open(nextCluster());
    const entries: ConfigEntry[] = [
      { key: 'a.bool_true', value: true },
      { key: 'a.bool_false', value: false },
      { key: 'a.int', value: 8443 },
      { key: 'a.float', value: 0.5 },
      { key: 'a.float_repeating', value: 1.1 },
      { key: 'a.negative_float', value: -2.75 },
      { key: 'a.str', value: 'en_US.UTF-8' },
      { key: 'a.str_true', value: 'true' },
      { key: 'a.empty_list', value: [] },
      { key: 'a.str_list', value: ['a', 'b'] },
      { key: 'a.null', value: null },
      { key: 'a.nested', value: { x: 1, y: [true], z: null } },
    ];
    await db.syncClusterSettingsInDb(entries);

    const { values, versions } = await settings(db);
    assert.deepEqual(values['a.bool_true'], { BOOL: true });
    assert.deepEqual(values['a.bool_false'], { BOOL: false });
    assert.deepEqual(values['a.int'], { N: '8443' });
    assert.deepEqual(values['a.float'], { N: '0.5' });
    assert.deepEqual(values['a.float_repeating'], { N: '1.1' });
    assert.deepEqual(values['a.negative_float'], { N: '-2.75' });
    assert.deepEqual(values['a.str'], { S: 'en_US.UTF-8' });
    assert.deepEqual(values['a.str_true'], { S: 'true' });
    // [] is an empty L, NOT a NULL: readers must see [] rather than the default
    assert.deepEqual(values['a.empty_list'], { L: [] });
    assert.deepEqual(values['a.str_list'], { L: [{ S: 'a' }, { S: 'b' }] });
    assert.deepEqual(values['a.null'], { NULL: true });
    assert.deepEqual(values['a.nested'], { M: { x: { N: '1' }, y: { L: [{ BOOL: true }] }, z: { NULL: true } } });

    // every key is on its first write
    assert.deepEqual(
      new Set(Object.values(versions)),
      new Set([1]),
    );
  });

  test('sync is add-only without overwrite, and version increments only on a real write', async () => {
    const db = await open(nextCluster());
    const original: ConfigEntry[] = [
      { key: 'cluster.locale', value: 'en_US.UTF-8' },
      { key: 'cluster.iam.ec2_managed_policy_arns', value: [] },
      { key: 'vdc.enabled', value: true },
    ];
    await db.syncClusterSettingsInDb(original);
    const first = await settings(db);

    // operator edit, then a re-run of `config update` carrying the template defaults
    await db.setConfigEntry('cluster.locale', 'de_DE.UTF-8');
    const edited = await settings(db);

    await db.syncClusterSettingsInDb([...original, { key: 'cluster.new_key', value: 42 }]);
    const after = await settings(db);

    assert.deepEqual(after.values['cluster.locale'], { S: 'de_DE.UTF-8' }, 'operator edit survives add-only sync');
    assert.deepEqual(after.values['cluster.new_key'], { N: '42' }, 'new key added');
    assert.deepEqual(versionDeltas(first.versions, edited.versions), {
      'cluster.locale': 1,
      'cluster.iam.ec2_managed_policy_arns': 0,
      'vdc.enabled': 0,
    });
    assert.deepEqual(versionDeltas(edited.versions, after.versions), {
      'cluster.locale': 0,
      'cluster.iam.ec2_managed_policy_arns': 0,
      'vdc.enabled': 0,
      'cluster.new_key': 1,
    });
  });

  test('sync with overwrite rewrites every entry and bumps every version', async () => {
    const db = await open(nextCluster());
    const entries: ConfigEntry[] = [
      { key: 'global-settings.package_config.dcv.version', value: '2024.0' },
      { key: 'global-settings.custom_tags', value: ['Key=k,Value=v'] },
    ];
    await db.syncClusterSettingsInDb(entries);
    const first = await settings(db);

    const updated: ConfigEntry[] = [
      { key: 'global-settings.package_config.dcv.version', value: '2025.0' },
      { key: 'global-settings.custom_tags', value: [] },
    ];
    await db.syncClusterSettingsInDb(updated, true);
    const after = await settings(db);

    assert.deepEqual(after.values['global-settings.package_config.dcv.version'], { S: '2025.0' });
    assert.deepEqual(after.values['global-settings.custom_tags'], { L: [] });
    assert.deepEqual(versionDeltas(first.versions, after.versions), {
      'global-settings.package_config.dcv.version': 1,
      'global-settings.custom_tags': 1,
    });
  });

  test('deleteConfigEntries removes exactly the key prefix, and requires one', async () => {
    const db = await open(nextCluster());
    await db.syncClusterSettingsInDb([
      { key: 'global-settings.a', value: 1 },
      { key: 'global-settings.b.c', value: 2 },
      { key: 'global-settings2.a', value: 3 },
      { key: 'cluster.a', value: 4 },
    ]);

    await db.deleteConfigEntries('global-settings.');
    assert.deepEqual(Object.keys((await settings(db)).values).sort(), ['cluster.a', 'global-settings2.a']);

    // a prefix matching nothing is a no-op, not an error
    await db.deleteConfigEntries('nothing.');
    assert.deepEqual(Object.keys((await settings(db)).values).sort(), ['cluster.a', 'global-settings2.a']);

    await assert.rejects(() => db.deleteConfigEntries(''), /config_key_prefix is required/);
    await assert.rejects(() => db.deleteConfigEntries('   '), /config_key_prefix is required/);
  });

  test('upgrade sequence: global-settings delete + overwrite, then a full add-only sync', async () => {
    const db = await open(nextCluster());

    // state before the upgrade: template rows plus operator edits
    await db.syncClusterSettingsInDb([
      { key: 'global-settings.package_config.dcv.version', value: '2024.0' },
      { key: 'global-settings.package_config.gone_in_new_release', value: 'stale' },
      { key: 'global-settings.custom_tags', value: ['Key=Owner,Value=ops'] },
      { key: 'cluster.locale', value: 'en_US.UTF-8' },
      { key: 'vdc.server.usb_remotization', value: null },
    ]);
    await db.setConfigEntry('cluster.locale', 'de_DE.UTF-8');
    const before_ = await settings(db);

    // the regenerated config/ for the new release
    const regenerated: ConfigEntry[] = [
      { key: 'global-settings.package_config.dcv.version', value: '2025.0' },
      { key: 'global-settings.custom_tags', value: [] },
      { key: 'global-settings.package_config.new_thing', value: 'added' },
      { key: 'cluster.locale', value: 'en_US.UTF-8' },
      { key: 'vdc.server.usb_remotization', value: [] },
      { key: 'vdc.brand_new_key', value: 3.5 },
    ];

    // phase 2 step 4
    await db.deleteConfigEntries('global-settings.');
    await db.syncClusterSettingsInDb(
      regenerated.filter((e) => e.key.startsWith('global-settings')),
      true,
    );
    // phase 2b: full flatten, add-only
    await db.syncClusterSettingsInDb(regenerated, false);

    const after = await settings(db);

    assert.deepEqual(Object.keys(after.values).sort(), [
      'cluster.locale',
      'global-settings.custom_tags',
      'global-settings.package_config.dcv.version',
      'global-settings.package_config.new_thing',
      'vdc.brand_new_key',
      'vdc.server.usb_remotization',
    ]);
    // global-settings replaced wholesale: the stale key is gone, custom_tags lost the operator value
    assert.equal(after.values['global-settings.package_config.gone_in_new_release'], undefined);
    assert.deepEqual(after.values['global-settings.custom_tags'], { L: [] });
    assert.deepEqual(after.values['global-settings.package_config.dcv.version'], { S: '2025.0' });
    // outside global-settings nothing existing moved
    assert.deepEqual(after.values['cluster.locale'], { S: 'de_DE.UTF-8' });
    // A NULL row stays NULL when the template value is [].
    assert.deepEqual(after.values['vdc.server.usb_remotization'], { NULL: true });
    assert.deepEqual(after.values['vdc.brand_new_key'], { N: '3.5' });

    // versions: deleted-and-rewritten global-settings rows restart at 1 (delete drops the counter),
    // untouched rows do not move
    assert.deepEqual(after.versions['global-settings.package_config.dcv.version'], 1);
    assert.deepEqual(after.versions['global-settings.custom_tags'], 1);
    assert.deepEqual(after.versions['global-settings.package_config.new_thing'], 1);
    assert.deepEqual(versionDeltas(before_.versions, after.versions), {
      'cluster.locale': 0,
      'vdc.server.usb_remotization': 0,
      'global-settings.package_config.dcv.version': 0,
      'global-settings.custom_tags': 0,
      'global-settings.package_config.new_thing': 1,
      'vdc.brand_new_key': 1,
    });
  });

  test('syncModules writes status/stack_name/version once and never updates', async () => {
    const db = await open(nextCluster());
    await db.syncModulesInDb([
      { id: 'global-settings', name: 'global-settings', type: 'config' },
      { id: 'cluster', name: 'cluster', type: 'stack' },
      { id: 'vdc', name: 'virtual-desktop-controller', type: 'app' },
    ]);

    const first = await dump(db.modulesTableName, 'module_id');
    assert.deepEqual(first, [
      { module_id: { S: 'cluster' }, name: { S: 'cluster' }, type: { S: 'stack' }, status: { S: 'not-deployed' }, stack_name: { NULL: true }, version: { NULL: true } },
      { module_id: { S: 'global-settings' }, name: { S: 'global-settings' }, type: { S: 'config' }, status: { S: 'deployed' }, stack_name: { NULL: true }, version: { NULL: true } },
      { module_id: { S: 'vdc' }, name: { S: 'virtual-desktop-controller' }, type: { S: 'app' }, status: { S: 'not-deployed' }, stack_name: { NULL: true }, version: { NULL: true } },
    ]);

    // a deployed module, as the Custom::ClusterSettings lambda leaves it
    await db.syncModulesInDb([{ id: 'vdc', name: 'virtual-desktop-controller', type: 'app' }]);
    const { DynamoDBDocumentClient, UpdateCommand } = await import('@aws-sdk/lib-dynamodb');
    const doc = DynamoDBDocumentClient.from(client);
    await doc.send(
      new UpdateCommand({
        TableName: db.modulesTableName,
        Key: { module_id: 'vdc' },
        UpdateExpression: 'SET #s=:s, #sn=:sn, #v=:v',
        ExpressionAttributeNames: { '#s': 'status', '#sn': 'stack_name', '#v': 'version' },
        ExpressionAttributeValues: { ':s': 'deployed', ':sn': 'idea-test1-vdc', ':v': '26.09.0' },
      }),
    );

    // a re-run with a DIFFERENT type and name must not touch the existing row
    await db.syncModulesInDb([
      { id: 'vdc', name: 'renamed-module', type: 'stack' },
      { id: 'metrics', name: 'metrics', type: 'app' },
    ]);

    const after = await dump(db.modulesTableName, 'module_id');
    const vdc = after.find((r) => r['module_id']?.S === 'vdc');
    assert.deepEqual(vdc, {
      module_id: { S: 'vdc' },
      name: { S: 'virtual-desktop-controller' },
      type: { S: 'app' },
      status: { S: 'deployed' },
      stack_name: { S: 'idea-test1-vdc' },
      version: { S: '26.09.0' },
    });
    assert.deepEqual(after.map((r) => r['module_id']?.S), ['cluster', 'global-settings', 'metrics', 'vdc']);
  });

  test('syncModules rejects an unsupported type on a new module before writing anything', async () => {
    const db = await open(nextCluster());
    await assert.rejects(
      () =>
        db.syncModulesInDb([
          { id: 'cluster', name: 'cluster', type: 'stack' },
          { id: 'weird', name: 'weird', type: 'lambda' },
        ]),
      /invalid type: lambda for module_id: weird/,
    );
    assert.deepEqual(await dump(db.modulesTableName, 'module_id'), []);
  });

  test(
    'reference oracle matches every synchronization scenario item by item',
    { skip: !oracleAvailable },
    async () => {
      const oracle = parseOracle();
      assert.equal(oracle.formatVersion, 1);
      assert.match(oracle.reference.writerSourceSha256, /^[0-9a-f]{64}$/);

      const db = await open(oracle.reference.clusterName);
      for (const step of oracle.main.steps) {
        await applyOracleStep(db, step);
        compareTables(step.name, await rawTables(db), step.tables);
      }
    },
  );

  test(
    'a run interrupted after one prefix write is repaired by the next run',
    { skip: !oracleAvailable },
    async () => {
      const oracle = parseOracle();
      const interruption = oracle.interruption;
      const clusterName = 'idea-dev27';
      const db = await open(clusterName);

      await db.syncModulesInDb(interruption.modules);
      await db.syncClusterSettingsInDb(interruption.seed, false);
      await db.deleteConfigEntries(interruption.prefix);

      const sourceUrl = pathToFileURL(
        path.resolve(
          import.meta.dirname,
          '../../src/config/cluster-config-db.ts',
        ),
      ).href;
      const childScript = [
        `import { DynamoDBClient } from ${JSON.stringify('@aws-sdk/client-dynamodb')};`,
        `import { ClusterConfigDb } from ${JSON.stringify(sourceUrl)};`,
        'const entries = JSON.parse(process.env["W19_ENTRIES"] ?? "[]");',
        'const client = new DynamoDBClient({',
        '  region: process.env["W19_REGION"],',
        '  endpoint: process.env["W19_ENDPOINT"],',
        '  credentials: { accessKeyId: "local", secretAccessKey: "local" },',
        '});',
        'const db = await ClusterConfigDb.open({',
        '  clusterName: process.env["W19_CLUSTER"],',
        '  client,',
        '  createDatabase: false,',
        '});',
        'const originalSet = db.setConfigEntry.bind(db);',
        'let writes = 0;',
        'db.setConfigEntry = async (key, value) => {',
        '  await originalSet(key, value);',
        '  writes += 1;',
        '  if (writes === 1) process.exit(Number(process.env["W19_EXIT_CODE"]));',
        '};',
        'await db.syncClusterSettingsInDb(entries, true);',
        'throw new Error("interruption did not occur");',
      ].join('\n');
      const interrupted = spawnSync(
        process.execPath,
        ['--input-type=module', '--eval', childScript],
        {
          cwd: path.resolve(import.meta.dirname, '../..'),
          encoding: 'utf8',
          env: {
            ...process.env,
            W19_CLUSTER: clusterName,
            W19_ENDPOINT: local?.endpoint ?? '',
            W19_ENTRIES: JSON.stringify(interruption.replacement),
            W19_EXIT_CODE: String(interruption.exitCode),
            W19_REGION: oracle.reference.region,
          },
        },
      );
      assert.equal(
        interrupted.status,
        interruption.exitCode,
        interrupted.stderr,
      );
      compareTables(
        'interruption-partial',
        await rawTables(db),
        interruption.partialTables,
      );

      await db.deleteConfigEntries(interruption.prefix);
      await db.syncClusterSettingsInDb(interruption.replacement, true);
      await db.syncClusterSettingsInDb(interruption.fullAddOnly, false);
      compareTables(
        'interruption-recovered',
        await rawTables(db),
        interruption.recoveredTables,
      );
    },
  );
});

describe('cost settings synchronization', () => {
  for (const region of ['us-east-2', 'us-west-2', 'us-gov-west-1', 'us-gov-east-1']) {
    test(region, async () => {
      const db = await open(nextCluster());
      const defaults: ConfigEntry[] = [
        { key: 'cluster.aws.region', value: region },
        { key: 'cluster-manager.metrics.cost.enabled', value: false },
        { key: 'cluster-manager.metrics.cost.lookback_days', value: 3 },
        { key: 'cluster-manager.metrics.cost.interval_hours', value: 6 },
        { key: 'cluster-manager.web_portal.cost_ticker.enabled', value: false },
        { key: 'scheduler.cost_estimation.ebs_gp3_storage', value: 0.08 },
        { key: 'scheduler.cost_estimation.ebs_io1_storage', value: 0.125 },
        { key: 'scheduler.cost_estimation.provisioned_iops', value: 0.065 },
        { key: 'scheduler.cost_estimation.default_fsx_lustre_size', value: 1200 },
        { key: 'scheduler.cost_estimation.ec2_boot_penalty_seconds', value: 300 },
        { key: 'scheduler.cost_estimation.fsx_lustre', value: 0.000194 },
      ];
      await db.syncClusterSettingsInDb(defaults.slice(0, -1));
      await db.setConfigEntry('scheduler.cost_estimation.provisioned_iops', 0.075);
      await db.setConfigEntry('cluster-manager.metrics.cost.enabled', true);
      const before = await settings(db);
      await db.syncClusterSettingsInDb(defaults);
      const added = await settings(db);
      assert.deepEqual(added.values['scheduler.cost_estimation.provisioned_iops'], { N: '0.075' });
      assert.deepEqual(added.values['cluster-manager.metrics.cost.enabled'], { BOOL: true });
      assert.deepEqual(added.values['scheduler.cost_estimation.fsx_lustre'], { N: '0.000194' });
      for (const key of Object.keys(before.values)) {
        assert.deepEqual(added.values[key], before.values[key], key);
        assert.equal(added.versions[key], before.versions[key], key);
      }
      await db.syncClusterSettingsInDb(defaults, true);
      const overwritten = await settings(db);
      assert.deepEqual(overwritten.values['scheduler.cost_estimation.provisioned_iops'], { N: '0.065' });
      assert.deepEqual(overwritten.values['cluster-manager.metrics.cost.enabled'], { BOOL: false });
      for (const { key } of defaults) assert.equal(overwritten.versions[key], added.versions[key]! + 1, key);
    });
  }
});

for (const source of ['cli', 'template'] as const) {
  test(`sync stamps explicit ${source} provenance`, async () => {
    const db = await open(nextCluster());
    const key = 'global-settings.gpu_settings.fail_on_missing_driver';
    await db.syncClusterSettingsInDb([{key, value: false}], true, source);
    const row = await db.getConfigEntry(key) as unknown as CurrentConfigRow;
    assert.equal(row.source, source);
    const report = compareUpgradeDrift({current: [row], generated: [{key, value: true}], replaceGlobalSettings: true});
    assert.deepEqual(report.changedRowsDifferingFromGenerated, source === 'cli' ? [key] : []);
  });
}
