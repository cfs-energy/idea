/** Read cluster configuration from the settings and modules tables. */

const DEFAULT_MODULE_SET = 'default';

export interface ModuleInfo {
  module_id: string;
  name: string;
  type: string;
  status?: string;
  stack_name?: string | null;
  version?: string | null;
  /** joined in memory by `get_cluster_modules`, never stored in the table */
  title?: string;
  /** joined in memory by `get_cluster_modules`, never stored in the table */
  deployment_priority?: number;
  [key: string]: unknown;
}

/** `utils/module_metadata.py` MODULE_METADATA, in file order. */
export const MODULE_METADATA: ReadonlyArray<{
  name: string;
  title: string;
  type: string;
  deployment_priority: number;
}> = [
  { name: 'global-settings', title: 'Global Settings', type: 'config', deployment_priority: 0 },
  { name: 'bootstrap', title: 'Bootstrap', type: 'stack', deployment_priority: 1 },
  { name: 'cluster', title: 'Cluster', type: 'stack', deployment_priority: 2 },
  { name: 'analytics', title: 'Analytics', type: 'stack', deployment_priority: 3 },
  { name: 'metrics', title: 'Metrics & Monitoring', type: 'stack', deployment_priority: 3 },
  { name: 'identity-provider', title: 'Identity Provider', type: 'stack', deployment_priority: 3 },
  { name: 'directoryservice', title: 'Directory Service', type: 'stack', deployment_priority: 3 },
  { name: 'shared-storage', title: 'Shared Storage', type: 'stack', deployment_priority: 4 },
  { name: "ecs", title: "ECS", type: "stack", deployment_priority: 4.5 },
  { name: 'cluster-manager', title: 'Cluster Manager', type: 'app', deployment_priority: 5 },
  { name: 'virtual-desktop-controller', title: 'eVDI', type: 'app', deployment_priority: 6 },
  { name: 'scheduler', title: 'Scale-Out Computing', type: 'app', deployment_priority: 6 },
  { name: 'bastion-host', title: 'Bastion Host', type: 'stack', deployment_priority: 7 },
];

const MODULE_METADATA_BY_NAME = new Map(MODULE_METADATA.map((entry) => [entry.name, entry]));

export interface GetOptions {
  /** raise instead of returning the default when the key is absent */
  required?: boolean;
  /** override the module id the key's module-name prefix maps to */
  moduleId?: string;
}

export interface ClusterConfigOptions {
  moduleSet?: string;
  /** The current module maps its own name to its ID without a module_sets row. */
  moduleId?: string;
}

/** One page of a DynamoDB scan, already unmarshalled by the document client. */
export interface ScanPage {
  Items?: Array<Record<string, unknown>>;
  LastEvaluatedKey?: Record<string, unknown>;
}

/** The one AWS call `fromDynamoDb` makes, as a function so tests can supply their own. */
export type TableScanner = (input: {
  TableName: string;
  ExclusiveStartKey?: Record<string, unknown>;
  ConsistentRead?: boolean;
}) => Promise<ScanPage>;

export interface FromDynamoDbOptions extends ClusterConfigOptions {
  scan?: TableScanner;
}

/** `errorcodes.CONFIG_KEY_NOT_FOUND` */
export class ConfigKeyNotFound extends Error {}
/** `errorcodes.CONFIG_TYPE_ERROR` */
export class ConfigTypeError extends Error {}
/** `exceptions.cluster_config_error` */
export class ClusterConfigError extends Error {}
/** `exceptions.general_exception` */
export class GeneralException extends Error {}

/** `ModelUtils.is_empty` (idea-data-model/model_utils.py:37-56). Numbers and booleans are never empty. */
export function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') return value.trim().length === 0;
  if (Array.isArray(value)) return value.length === 0;
  if (value instanceof Map || value instanceof Set) return value.size === 0;
  if (value instanceof Uint8Array) return value.length === 0;
  if (typeof value === 'object') return Object.keys(value).length === 0;
  return false;
}

/** `soca_config.is_null_value`: an empty list is a real value, every other empty value is null. */
export function isNullValue(value: unknown): boolean {
  return isEmpty(value) && !Array.isArray(value);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** One DynamoDB typed attribute value, as the AWS CLI prints it. */
type AttributeValue = Record<string, unknown>;

/** boto3's Table resource unmarshalling: N becomes a number, everything else its JSON shape. */
export function unmarshallAttribute(attr: AttributeValue): unknown {
  const [type] = Object.keys(attr);
  const raw = attr[type as string];
  switch (type) {
    case 'S':
      return raw as string;
    case 'N':
      return Number(raw as string);
    case 'BOOL':
      return raw as boolean;
    case 'NULL':
      return null;
    case 'L':
      return (raw as AttributeValue[]).map(unmarshallAttribute);
    case 'M':
      return unmarshallItem(raw as Record<string, AttributeValue>);
    case 'SS':
      return raw as string[];
    case 'NS':
      return (raw as string[]).map(Number);
    default:
      throw new ConfigTypeError(`unsupported dynamodb attribute type: ${type}`);
  }
}

function unmarshallItem(item: Record<string, AttributeValue>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, attr] of Object.entries(item)) out[key] = unmarshallAttribute(attr);
  return out;
}

/** Numeric lists interleave each number with the original list. */
export function checkAndConvertDecimalValue(value: unknown): unknown {
  if (!Array.isArray(value) || value.length === 0 || typeof value[0] !== 'number') return value;
  const converted: unknown[] = [];
  for (const item of value) {
    if (typeof item !== 'number') {
      throw new ConfigTypeError(
        `invalid literal for int() with base 10: '${pyStr(item)}' (mixed list with a numeric first element)`,
      );
    }
    converted.push(item);
    converted.push(value);
  }
  return converted;
}

/** `SocaConfig.put` + pyhocon `ConfigTree.put`: build the path, null-normalise the leaf. */
function putKey(tree: Record<string, unknown>, key: string, value: unknown): void {
  const parts = key.split('.');
  let node = tree;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i] as string;
    const next = node[part];
    if (isPlainObject(next)) {
      node = next;
    } else {
      const created: Record<string, unknown> = {};
      node[part] = created;
      node = created;
    }
  }
  node[parts[parts.length - 1] as string] = isNullValue(value) ? null : value;
}

/** `int(value)` */
function toInt(value: unknown, key: string): number {
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return Math.trunc(value);
  if (typeof value === 'string' && /^\s*[+-]?\d+\s*$/.test(value)) return Number(value.trim());
  throw new ConfigTypeError(`${key} has type '${pyTypeName(value)}' rather than 'int'`);
}

/** `float(value)` */
function toFloat(value: unknown, key: string): number {
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value);
  }
  throw new ConfigTypeError(`${key} has type '${pyTypeName(value)}' rather than 'float'`);
}

const BOOL_CONVERSIONS: Record<string, boolean> = {
  true: true,
  yes: true,
  on: true,
  false: false,
  no: false,
  off: false,
};

/**
 * pyhocon's `re.match('^[1-9][0-9]*$|0', key)` in `get_list`: either the whole key is a non-zero
 * integer, or it starts with a `0`. `re.match` anchors at the start, hence the second branch.
 */
const NUMERIC_TREE_KEY = /^(?:[1-9][0-9]*$|0)/;

/** One `<cluster>.cluster-settings` row, with the Decimal post-processing Python applies. */
function toConfigEntry(row: Record<string, unknown>): { key: string; value?: unknown } {
  const key = row['key'];
  if (typeof key !== 'string') {
    throw new ConfigTypeError(`cluster-settings row without a string key: ${JSON.stringify(row)}`);
  }
  return { key, value: checkAndConvertDecimalValue(row['value']) };
}

/** One `<cluster>.modules` row. Python does not post-process these. */
function toModuleInfo(row: Record<string, unknown>): ModuleInfo {
  const { module_id: moduleId, name, type } = row;
  if (typeof moduleId !== 'string' || typeof name !== 'string' || typeof type !== 'string') {
    throw new ConfigTypeError(`modules row without module_id/name/type: ${JSON.stringify(row)}`);
  }
  return { ...row, module_id: moduleId, name, type };
}

/** The live scanner. Imported lazily so that reading a fixture never loads the AWS SDK. */
async function defaultTableScanner(region: string): Promise<TableScanner> {
  const { DynamoDBClient } = await import('@aws-sdk/client-dynamodb');
  const { DynamoDBDocumentClient, ScanCommand } = await import('@aws-sdk/lib-dynamodb');
  const { networkTolerantRetryStrategy } = await import('../cli/aws-client-options.ts');
  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region, retryStrategy: networkTolerantRetryStrategy }));
  return (input) => doc.send(new ScanCommand(input));
}

export class ClusterConfig {
  private readonly tree: Record<string, unknown>;
  private readonly moduleList: ModuleInfo[];
  readonly moduleSet: string;
  /** ID of the current module. */
  currentModuleId: string | undefined;
  moduleInfo: ModuleInfo | undefined;

  constructor(
    entries: Array<{ key: string; value?: unknown }>,
    modules: ModuleInfo[] = [],
    options: ClusterConfigOptions = {},
  ) {
    this.tree = {};
    // Sorted keys let `a.b.c` replace a scalar at `a.b`.
    const sorted = [...entries].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
    for (const entry of sorted) putKey(this.tree, entry.key, entry.value);
    this.moduleList = modules;
    this.moduleSet = isEmpty(options.moduleSet) ? DEFAULT_MODULE_SET : (options.moduleSet as string);
    if (!isEmpty(options.moduleId)) this.setModuleId(options.moduleId as string);
  }

  /** Raw `aws dynamodb scan` output for the two tables, as captured by the parity fixtures. */
  static fromFile(scanJson: string, modulesJson?: string, options: ClusterConfigOptions = {}): ClusterConfig {
    const scan = JSON.parse(scanJson) as { Items?: Array<Record<string, AttributeValue>> };
    const entries = (scan.Items ?? []).map((item) => toConfigEntry(unmarshallItem(item)));
    let modules: ModuleInfo[] = [];
    if (modulesJson !== undefined) {
      const modulesScan = JSON.parse(modulesJson) as { Items?: Array<Record<string, AttributeValue>> };
      modules = (modulesScan.Items ?? []).map((item) => toModuleInfo(unmarshallItem(item)));
    }
    return new ClusterConfig(entries, modules, options);
  }

  /**
   * `ClusterConfigDB.get_config_entries` + `get_cluster_modules`: a full scan of both tables,
   * following `LastEvaluatedKey`. `options.scan` replaces the one AWS call, for tests and for
   * callers that already hold a client.
   */
  static async fromDynamoDb(
    cluster: string,
    region: string,
    options: FromDynamoDbOptions = {},
  ): Promise<ClusterConfig> {
    const scan = options.scan ?? (await defaultTableScanner(region));

    const scanAll = async (tableName: string): Promise<Array<Record<string, unknown>>> => {
      const rows: Array<Record<string, unknown>> = [];
      let startKey: Record<string, unknown> | undefined;
      do {
        const page = await scan({ TableName: tableName, ExclusiveStartKey: startKey });
        rows.push(...(page.Items ?? []));
        startKey = page.LastEvaluatedKey;
      } while (startKey !== undefined);
      return rows;
    };

    try {
      const entries = (await scanAll(`${cluster}.cluster-settings`)).map(toConfigEntry);
      const modules = (await scanAll(`${cluster}.modules`)).map(toModuleInfo);
      return new ClusterConfig(entries, modules, options);
    } catch (error) {
      if ((error as { name?: string }).name === 'ResourceNotFoundException') {
        throw new ClusterConfigError(
          `No configuration tables for cluster ${cluster} in ${region} (looked for ${cluster}.modules and ${cluster}.cluster-settings). ` +
            `Install with ideactl quick-setup, or run ideactl config update --cluster-name ${cluster} --aws-region ${region}. ` +
            'If the cluster already exists, check --aws-region and --aws-profile.',
        );
      }
      throw error;
    }
  }

  /** `ClusterConfigDB.get_cluster_modules`: the table rows, joined with the module metadata. */
  modules(): ModuleInfo[] {
    return this.moduleList.map((module) => {
      const metadata = MODULE_METADATA_BY_NAME.get(module.name);
      if (metadata === undefined) {
        throw new GeneralException(`module not found for name: ${module.name}`);
      }
      return { ...module, title: metadata.title, deployment_priority: metadata.deployment_priority };
    });
  }

  moduleInfoById(moduleId: string): ModuleInfo | undefined {
    return this.moduleList.find((module) => module.module_id === moduleId);
  }

  /**
   * Puts one already-resolved row into the tree. A deploy writes settings to the table and then
   * reads them back in the same process, so a row written mid-run is readable without a re-scan.
   */
  setEntry(key: string, value: unknown): void {
    putKey(this.tree, key, value);
  }

  setModuleId(moduleId: string): void {
    const info = this.moduleInfoById(moduleId);
    if (info === undefined) throw new GeneralException(`module not found for module_id: ${moduleId}`);
    this.currentModuleId = moduleId;
    this.moduleInfo = info;
  }

  /** `ClusterConfig.get_module_id`, required, so a missing module_sets row raises. */
  moduleId(moduleName: string): string {
    const value = this.getString(
      `global-settings.module_sets.${this.moduleSet}.${moduleName}.module_id`,
      undefined,
      { required: true },
    );
    if (value === undefined) {
      throw new ConfigKeyNotFound(
        `'${moduleName}', key: global-settings.module_sets.${this.moduleSet}.${moduleName}.module_id`,
      );
    }
    return value;
  }

  isModuleEnabled(moduleName: string): boolean {
    return !isEmpty(
      this.getString(`global-settings.module_sets.${this.moduleSet}.${moduleName}.module_id`),
    );
  }

  /** `ClusterConfig.get_real_key` (cluster_config.py:95-117). */
  getRealKey(key: string, moduleId?: string): string {
    const parts = key.split('.');
    const moduleName = parts[0] as string;
    if (moduleName === 'global-settings') return key;

    let resolved = moduleId;
    if (isEmpty(resolved)) {
      if (this.moduleInfo !== undefined && this.moduleInfo.name === moduleName) {
        resolved = this.moduleInfo.module_id;
      } else {
        resolved = this.rawGetString(
          `global-settings.module_sets.${this.moduleSet}.${moduleName}.module_id`,
        );
      }
    }
    if (isEmpty(resolved)) resolved = moduleName;
    // single-segment keys produce a trailing '.', exactly as Python's '.'.join([]) does
    return `${resolved}.${parts.slice(1).join('.')}`;
  }

  // --- pyhocon layer, on already-rewritten keys ---------------------------------------------

  private rawGet(key: string, required: boolean): unknown {
    // pyhocon parses a key with `re.findall(r'"[^"]+"|[^\.]+', key)`, and `[^\.]+` needs at least
    // one non-dot character, so an empty segment is never a path element. `get_real_key` turns a
    // single-segment key into '<module id>.', which pyhocon reads as ['<module id>'] and resolves
    // to the whole module subtree; 'a..b' collapses the same way.
    const parts = key.split('.').filter((part) => part !== '');
    let node: unknown = this.tree;
    for (const part of parts) {
      if (!isPlainObject(node) || !(part in node)) {
        if (required) throw new ConfigKeyNotFound(`'${part}', key: ${key}`);
        return undefined;
      }
      node = node[part];
    }
    return node;
  }

  private rawGetString(key: string): string | undefined {
    const value = this.rawGet(key, false);
    const asString = stringify(value);
    return isEmpty(asString) ? undefined : asString;
  }

  // --- SocaConfig getters, on module-name keys ----------------------------------------------

  get<T = unknown>(key: string, defaultValue?: T, options: GetOptions = {}): T {
    const value = this.rawGet(this.getRealKey(key, options.moduleId), options.required === true);
    return (isNullValue(value) ? defaultValue : value) as T;
  }

  getString(key: string): string | undefined;
  getString(key: string, defaultValue: string, options?: GetOptions): string;
  getString(key: string, defaultValue?: string, options?: GetOptions): string | undefined;
  getString(key: string, defaultValue?: string, options: GetOptions = {}): string | undefined {
    const value = this.rawGet(this.getRealKey(key, options.moduleId), options.required === true);
    const asString = value === undefined || value === null ? defaultValue : stringify(value);
    return isEmpty(asString) ? defaultValue : asString;
  }

  getBool(key: string): boolean | undefined;
  getBool(key: string, defaultValue: boolean, options?: GetOptions): boolean;
  getBool(key: string, defaultValue?: boolean, options?: GetOptions): boolean | undefined;
  getBool(key: string, defaultValue?: boolean, options: GetOptions = {}): boolean | undefined {
    const realKey = this.getRealKey(key, options.moduleId);
    const value = this.rawGet(realKey, options.required === true);
    if (value === undefined || value === null) return defaultValue;
    const asString = (stringify(value) as string).toLowerCase();
    if (!(asString in BOOL_CONVERSIONS)) {
      throw new ConfigTypeError(`${realKey} does not translate to a Boolean value`);
    }
    return BOOL_CONVERSIONS[asString];
  }

  getInt(key: string): number | undefined;
  getInt(key: string, defaultValue: number, options?: GetOptions): number;
  getInt(key: string, defaultValue?: number, options?: GetOptions): number | undefined;
  getInt(key: string, defaultValue?: number, options: GetOptions = {}): number | undefined {
    const realKey = this.getRealKey(key, options.moduleId);
    const value = this.rawGet(realKey, options.required === true);
    if (value === undefined || value === null) return defaultValue;
    return toInt(value, realKey);
  }

  getFloat(key: string): number | undefined;
  getFloat(key: string, defaultValue: number, options?: GetOptions): number;
  getFloat(key: string, defaultValue?: number, options?: GetOptions): number | undefined;
  getFloat(key: string, defaultValue?: number, options: GetOptions = {}): number | undefined {
    const realKey = this.getRealKey(key, options.moduleId);
    const value = this.rawGet(realKey, options.required === true);
    if (value === undefined || value === null) return defaultValue;
    return toFloat(value, realKey);
  }

  getList<T = unknown>(key: string): T[] | undefined;
  getList<T = unknown>(key: string, defaultValue: T[], options?: GetOptions): T[];
  getList<T = unknown>(key: string, defaultValue?: T[], options?: GetOptions): T[] | undefined;
  getList<T = unknown>(key: string, defaultValue?: T[], options: GetOptions = {}): T[] | undefined {
    const realKey = this.getRealKey(key, options.moduleId);
    const value = this.rawGet(realKey, options.required === true);
    if (value === undefined || value === null) return defaultValue;
    // [] is a real value: is_null_value() lets it through where every other empty value falls back
    if (Array.isArray(value)) return value as T[];
    // a tree with none but numeric keys is a list to pyhocon: its values, in sorted key order
    if (isPlainObject(value)) {
      return Object.entries(value)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([entryKey, entryValue]) => {
          if (!NUMERIC_TREE_KEY.test(entryKey)) {
            throw new ConfigTypeError(`${realKey} does not translate to a list`);
          }
          return entryValue as T;
        });
    }
    throw new ConfigTypeError(`${realKey} has type '${pyTypeName(value)}' rather than 'list'`);
  }

  /**
   * `SocaConfig.get_config`: a subtree. A missing, NULL or empty subtree reads as the default;
   * a stored scalar or list is a CONFIG_TYPE_ERROR, not a fallback.
   */
  getConfig(key: string, defaultValue?: Record<string, unknown>, options: GetOptions = {}): Record<string, unknown> | undefined {
    const realKey = this.getRealKey(key, options.moduleId);
    const value = this.rawGet(realKey, options.required === true);
    if (value === undefined || value === null) return defaultValue;
    if (!isPlainObject(value)) {
      throw new ConfigTypeError(`${realKey} has type '${pyTypeName(value)}' rather than 'config'`);
    }
    return isEmpty(value) ? defaultValue : value;
  }

  getClusterExternalEndpoint(): string {
    const clusterModuleId = this.moduleId('cluster');
    const dns =
      this.getString(`${clusterModuleId}.load_balancers.external_alb.certificates.custom_dns_name`) ??
      this.getString(`${clusterModuleId}.load_balancers.external_alb.load_balancer_dns_name`);
    if (isEmpty(dns)) throw new ClusterConfigError('cluster external endpoint not found');
    return `https://${dns}`;
  }

  getClusterInternalEndpoint(): string {
    const clusterModuleId = this.moduleId('cluster');
    const dns =
      this.getString(`${clusterModuleId}.load_balancers.internal_alb.certificates.custom_dns_name`) ??
      // Check the alternate `custom_dns_name` path.
      this.getString(`${clusterModuleId}.load_balancers.internal_alb.custom_dns_name`) ??
      this.getString(`${clusterModuleId}.load_balancers.internal_alb.load_balancer_dns_name`);
    if (isEmpty(dns)) throw new ClusterConfigError('cluster internal endpoint not found');
    return `https://${dns}`;
  }
}

/** The name Python's `type(value).__name__` gives a value read out of the tree. */
function pyTypeName(value: unknown): string {
  if (value === undefined || value === null) return 'NoneType';
  if (typeof value === 'boolean') return 'bool';
  if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'float';
  if (typeof value === 'string') return 'str';
  if (Array.isArray(value)) return 'list';
  return 'ConfigTree';
}

/**
 * Python `repr()` of a string: single quotes, unless the string holds a `'` and no `"`.
 * Backslashes, the quote in use and the C0 controls are escaped; printable non-ASCII is not.
 */
function pyReprString(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = '';
  for (const char of value) {
    if (char === '\\') out += '\\\\';
    else if (char === quote) out += `\\${quote}`;
    else if (char === '\n') out += '\\n';
    else if (char === '\r') out += '\\r';
    else if (char === '\t') out += '\\t';
    else if (char < ' ' || char === '\x7f') out += `\\x${char.charCodeAt(0).toString(16).padStart(2, '0')}`;
    else out += char;
  }
  return `${quote}${out}${quote}`;
}

/** Format configuration values with Python representation rules. */
function pyRepr(value: unknown): string {
  if (value === undefined || value === null) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'string') return pyReprString(value);
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .map(([key, entry]) => `${pyReprString(key)}: ${pyRepr(entry)}`)
    .join(', ');
  return `ConfigTree({${entries}})`;
}

/** Python `str()`: a string is itself, a bool is lowercased by pyhocon, everything else is repr. */
function pyStr(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return pyRepr(value);
}

/** pyhocon `ConfigTree.get_string`: `str(value)`, with booleans lowercased. */
function stringify(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  return pyStr(value);
}
