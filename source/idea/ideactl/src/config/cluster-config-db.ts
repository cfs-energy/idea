/**
 * Write side of the cluster configuration tables.
 *
 * Port of the write half of ideasdk `config/cluster_config_db.py`: table creation (the two tables
 * are made with the SDK, not CloudFormation), the add-only `sync_modules_in_db` /
 * `sync_cluster_settings_in_db`, `set_config_entry` and `delete_config_entries`.
 *
 * Behaviour that is contract, not accident, and is reproduced verbatim:
 *   - `sync_modules_in_db` never updates an existing `module_id` row (the `type` of a module is
 *     frozen once written) and validates `type` only for rows it is about to create;
 *   - `sync_cluster_settings_in_db` skips any key that already exists unless `overwrite`, and
 *     never deletes; deletion is only ever `delete_config_entries(prefix)`, a full scan plus
 *     `startsWith`;
 *   - every write stamps its source and uses `ADD #version :version` with `:version = 1`, so `version`
 *     is a per-key write counter that starts at 1 and increments, not a release version;
 *   - values keep their JSON types on the way in: bool -> BOOL, number -> N (as a string), string
 *     -> S, list -> L (an empty list stays an empty `L`, it is NOT a NULL), null -> NULL, object
 *     -> M. Python converts floats to `Decimal(str(v))` for boto3's sake; the JS document client
 *     already writes `N: String(v)`, which is the same wire value.
 *
 * The read side lives in `cluster-config.ts` and is not duplicated here.
 */

import {
  CreateTableCommand,
  DescribeTableCommand,
  type DynamoDBClient,
  type SSESpecification,
} from '@aws-sdk/client-dynamodb';
import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  ScanCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';

import type { ModuleInfo } from './cluster-config.ts';
import { isEmpty } from './cluster-config.ts';

const IDEA_TAG_PREFIX = 'idea:';
const IDEA_TAG_CLUSTER_NAME = `${IDEA_TAG_PREFIX}ClusterName`;
const IDEA_TAG_BACKUP_PLAN = `${IDEA_TAG_PREFIX}BackupPlan`;
const MODULE_CLUSTER = 'cluster';

export const SUPPORTED_MODULE_TYPES = ['app', 'stack', 'config'];

/** `exceptions.cluster_config_error` / `CLUSTER_CONFIG_NOT_INITIALIZED` / `invalid_params`. */
export class ClusterConfigDbError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ClusterConfigDbError';
  }
}

export interface ConfigEntry {
  key: string;
  value?: unknown;
}

/** One entry of a module set, as `read_modules_from_files` produces it from `idea.yml`. */
export interface ModuleSpec {
  id: string;
  name: string;
  type: string;
  [key: string]: unknown;
}

export interface ClusterConfigDbOptions {
  clusterName: string;
  /** caller owns the client, so tests can point it at DynamoDB Local */
  client: DynamoDBClient;
  /** Region named when the configuration tables are missing. */
  awsRegion?: string;
  /** `cluster.dynamodb.kms_key_id`; SSE is configured only when this is set */
  dynamodbKmsKeyId?: string | null;
  /** only `config update` passes true; everything else fails if the tables are missing */
  createDatabase?: boolean;
  logger?: (message: string) => void;
}

function isResourceNotFound(e: unknown): boolean {
  return (e as { name?: string })?.name === 'ResourceNotFoundException';
}

function configurationTablesNotFound(clusterName: string, awsRegion: string): string {
  if (awsRegion === '') {
    return (
      `Configuration tables not found for cluster ${clusterName}. Create them with ideactl config update ` +
      `--cluster-name ${clusterName} --aws-region <region>, or confirm the cluster was installed in this account and region.`
    );
  }
  return (
    `Configuration tables not found for cluster ${clusterName} in ${awsRegion}. Create them with ideactl config update ` +
    `--cluster-name ${clusterName} --aws-region ${awsRegion}, or confirm the cluster was installed in this account and region.`
  );
}

async function dynamoRegion(client: DynamoDBClient): Promise<string | undefined> {
  const configured = client.config.region;
  if (typeof configured === 'string' && configured !== '') return configured;
  if (typeof configured === 'function') {
    const value = await configured();
    if (typeof value === 'string' && value !== '') return value;
  }
  return undefined;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

export class ClusterConfigDb {
  readonly clusterName: string;
  readonly awsRegion: string;
  readonly dynamodbKmsKeyId: string | null;
  readonly createDatabase: boolean;
  private readonly client: DynamoDBClient;
  private readonly doc: DynamoDBDocumentClient;
  private readonly logger: (message: string) => void;

  private constructor(options: ClusterConfigDbOptions, awsRegion: string) {
    this.clusterName = options.clusterName;
    this.awsRegion = awsRegion;
    this.client = options.client;
    this.doc = DynamoDBDocumentClient.from(options.client);
    this.dynamodbKmsKeyId = isEmpty(options.dynamodbKmsKeyId) ? null : (options.dynamodbKmsKeyId as string);
    this.createDatabase = options.createDatabase === true;
    this.logger = options.logger ?? (() => {});
  }

  /**
   * `ClusterConfigDB.__init__`: validate, then either assert both tables exist or create them.
   * A constructor cannot await, so the async half of `__init__` lives here.
   */
  static async open(options: ClusterConfigDbOptions): Promise<ClusterConfigDb> {
    if (isEmpty(options.clusterName)) {
      throw new ClusterConfigDbError('cluster_name is required');
    }
    if (options.clusterName.length > 11) {
      throw new ClusterConfigDbError(
        `cluster_name: ${options.clusterName} cannot be more than 11 characters. current: ${options.clusterName.length}`,
      );
    }
    const awsRegion = options.awsRegion ?? (await dynamoRegion(options.client)) ?? '';
    const db = new ClusterConfigDb(options, awsRegion);
    if (!db.createDatabase) await db.checkTableCreated();
    await db.getOrCreateModulesTable();
    await db.getOrCreateClusterSettingsTable();
    return db;
  }

  get modulesTableName(): string {
    return `${this.clusterName}.modules`;
  }

  get clusterSettingsTableName(): string {
    return `${this.clusterName}.cluster-settings`;
  }

  async checkTableCreated(): Promise<boolean> {
    try {
      await this.client.send(new DescribeTableCommand({ TableName: this.modulesTableName }));
      await this.client.send(new DescribeTableCommand({ TableName: this.clusterSettingsTableName }));
      return true;
    } catch (e) {
      if (isResourceNotFound(e)) {
        throw new ClusterConfigDbError(configurationTablesNotFound(this.clusterName, this.awsRegion));
      }
      throw e;
    }
  }

  /** `describe_table` until ACTIVE, creating the table on the first ResourceNotFoundException. */
  private async waitOrCreate(tableName: string, create: () => Promise<void>): Promise<void> {
    for (;;) {
      try {
        const result = await this.client.send(new DescribeTableCommand({ TableName: tableName }));
        if (result.Table?.TableStatus === 'ACTIVE') return;
        await sleep(2000);
      } catch (e) {
        if (!isResourceNotFound(e) || !this.createDatabase) throw e;
        await create();
      }
    }
  }

  private sseSpecification(): SSESpecification | undefined {
    if (this.dynamodbKmsKeyId === null) return undefined;
    return { Enabled: true, SSEType: 'KMS', KMSMasterKeyId: this.dynamodbKmsKeyId };
  }

  private async getOrCreateModulesTable(): Promise<void> {
    await this.waitOrCreate(this.modulesTableName, async () => {
      if (this.dynamodbKmsKeyId !== null) {
        this.logger(`detected cluster.dynamodb.kms_key_id is set to: ${this.dynamodbKmsKeyId}`);
      }
      this.logger(`creating cluster config dynamodb table: ${this.modulesTableName}`);
      await this.client.send(
        new CreateTableCommand({
          TableName: this.modulesTableName,
          AttributeDefinitions: [{ AttributeName: 'module_id', AttributeType: 'S' }],
          KeySchema: [{ AttributeName: 'module_id', KeyType: 'HASH' }],
          BillingMode: 'PAY_PER_REQUEST',
          SSESpecification: this.sseSpecification(),
          Tags: [
            { Key: IDEA_TAG_CLUSTER_NAME, Value: this.clusterName },
            { Key: IDEA_TAG_BACKUP_PLAN, Value: `${this.clusterName}-${MODULE_CLUSTER}` },
          ],
        }),
      );
    });
  }

  private async getOrCreateClusterSettingsTable(): Promise<void> {
    await this.waitOrCreate(this.clusterSettingsTableName, async () => {
      this.logger(`creating cluster config dynamodb table: ${this.clusterSettingsTableName}`);
      await this.client.send(
        new CreateTableCommand({
          TableName: this.clusterSettingsTableName,
          AttributeDefinitions: [{ AttributeName: 'key', AttributeType: 'S' }],
          KeySchema: [{ AttributeName: 'key', KeyType: 'HASH' }],
          BillingMode: 'PAY_PER_REQUEST',
          StreamSpecification: { StreamEnabled: true, StreamViewType: 'NEW_AND_OLD_IMAGES' },
          SSESpecification: this.sseSpecification(),
          Tags: [{ Key: IDEA_TAG_CLUSTER_NAME, Value: this.clusterName }],
        }),
      );
    });
  }

  async getConfigEntry(key: string): Promise<Record<string, unknown> | undefined> {
    const result = await this.doc.send(
      new GetCommand({ TableName: this.clusterSettingsTableName, Key: { key } }),
    );
    return result.Item;
  }

  async getModuleInfo(moduleId: string): Promise<ModuleInfo | undefined> {
    const result = await this.doc.send(
      new GetCommand({ TableName: this.modulesTableName, Key: { module_id: moduleId } }),
    );
    return result.Item as ModuleInfo | undefined;
  }

  /** Write value and last writer atomically; version counts writes. */
  async setConfigEntry(key: string, value: unknown, source: 'cli' | 'template' = 'cli'): Promise<void> {
    this.logger(`updating config: ${key} = ${String(value)}`);
    await this.doc.send(
      new UpdateCommand({
        TableName: this.clusterSettingsTableName,
        Key: { key },
        UpdateExpression: 'SET #value=:value, #source=:source ADD #version :version',
        ExpressionAttributeNames: { '#value': 'value', '#version': 'version', '#source': 'source' },
        // Python has no `undefined`; a missing value is Python's None, i.e. NULL.
        ExpressionAttributeValues: { ':value': value === undefined ? null : value, ':version': 1, ':source': source },
      }),
    );
  }

  /** Add-only unless `overwrite`. Never deletes, never touches keys absent from `entries`. */
  async syncClusterSettingsInDb(entries: ConfigEntry[], overwrite = false, source: 'cli' | 'template' = 'template'): Promise<void> {
    this.logger(`sync config entries to db. overwrite: ${overwrite}`);
    for (const entry of entries) {
      if (!overwrite) {
        const existing = await this.getConfigEntry(entry.key);
        if (existing !== undefined) {
          this.logger(`entry already exists for key: ${entry.key}, skip.`);
          continue;
        }
      }
      await this.setConfigEntry(entry.key, entry.value, source);
    }
  }

  /**
   * Add-only, with no update path at all: an existing `module_id` keeps its `type`, `status`,
   * `stack_name` and `version`. Python validates the type in a first pass over every module and
   * writes in a second pass, so an invalid type on a NEW module aborts before anything is written
   * while an invalid type on an EXISTING module is never noticed.
   */
  async syncModulesInDb(modules: ModuleSpec[]): Promise<void> {
    this.logger('sync modules in db ...');

    const modulesToCreate: ModuleSpec[] = [];
    for (const module of modules) {
      const existing = await this.getModuleInfo(module.id);
      if (existing !== undefined) {
        this.logger(`module: ${module.id}, name: ${module.name} already exists. skip.`);
        continue;
      }
      if (!SUPPORTED_MODULE_TYPES.includes(module.type)) {
        throw new ClusterConfigDbError(
          `invalid type: ${module.type} for module_id: ${module.id}. ` +
            `supported module types: ${SUPPORTED_MODULE_TYPES.join(', ')}`,
        );
      }
      modulesToCreate.push(module);
    }

    for (const module of modulesToCreate) {
      const status = module.type === 'config' ? 'deployed' : 'not-deployed';
      this.logger(`creating module entry for module: ${module.name}, module_id: ${module.id}`);
      await this.doc.send(
        new UpdateCommand({
          TableName: this.modulesTableName,
          Key: { module_id: module.id },
          UpdateExpression:
            'SET #name=:name, #status=:status, #stack_name=:stack_name, #version=:version, #type=:type',
          ExpressionAttributeNames: {
            '#name': 'name',
            '#status': 'status',
            '#stack_name': 'stack_name',
            '#version': 'version',
            '#type': 'type',
          },
          ExpressionAttributeValues: {
            ':name': module.name,
            ':type': module.type,
            ':status': status,
            ':stack_name': null,
            ':version': null,
          },
        }),
      );
    }
  }

  /** Full scan + `startsWith`, then a delete per match. `config delete <prefix>`. */
  async deleteConfigEntries(configKeyPrefix: string): Promise<void> {
    if (isEmpty(configKeyPrefix)) {
      throw new ClusterConfigDbError('config_key_prefix is required');
    }

    this.logger(`searching for config entries with prefix: ${configKeyPrefix}`);
    const toDelete: Array<Record<string, unknown>> = [];
    let startKey: Record<string, unknown> | undefined;
    do {
      const result = await this.doc.send(
        new ScanCommand({ TableName: this.clusterSettingsTableName, ExclusiveStartKey: startKey }),
      );
      for (const item of result.Items ?? []) {
        if (String(item['key'] ?? '').startsWith(configKeyPrefix)) toDelete.push(item);
      }
      startKey = result.LastEvaluatedKey;
    } while (startKey !== undefined);

    if (toDelete.length === 0) {
      this.logger(`no config entries found matching config prefix: ${configKeyPrefix}`);
      return;
    }
    this.logger(`found ${toDelete.length} config entries matching: ${configKeyPrefix}`);
    for (const item of toDelete) {
      this.logger(`deleting config entry - ${String(item['key'])} = ${String(item['value'])}`);
      await this.doc.send(
        new DeleteCommand({
          TableName: this.clusterSettingsTableName,
          Key: { key: item['key'] },
        }),
      );
    }
    this.logger(`deleted ${toDelete.length} config entries`);
  }
}
