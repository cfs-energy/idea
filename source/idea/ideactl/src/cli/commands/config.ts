/**
 * Port of the `config` command group (`app_main.py:158-978`).
 *
 * `generate` renders the local `config/` tree from `values.yml`; `update` pushes it into the two
 * DynamoDB tables; the rest read, write or export single entries. Nothing here talks to AWS
 * directly: every effect arrives through `Deps`.
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import * as yaml from 'js-yaml';
import { Command, Option } from 'commander';

import {
  checkAndConvertDecimalValue,
  ClusterConfigError,
  GeneralException,
  isEmpty,
  type ModuleInfo,
} from '../../config/cluster-config.ts';
import { convertConfigToKeyValuePairs, generateConfigFromTemplates, readConfigFromFiles, readModulesFromFiles, type ConfigEntry } from '../../config/generator.ts';
import type { UpgradeDriftInput, UpgradeDriftReport } from "../../config/upgrade-drift.ts";
import { loadValuesFile } from '../../config/values.ts';
import {
  collectInstallerValues,
  type InstallerChoiceProvider,
  type InstallerIdentity,
} from '../installer-params.ts';
import { TerminalPromptDriver, type PromptDriver } from '../prompts.ts';
import {
  clusterConfigDir,
  clusterRegionDir,
  ExitWithCode,
  valuesFilePath,
  VALUES_FILE_S3_KEY,
  type Deps,
} from '../cdk-invoker.ts';

/** One `<cluster>.cluster-settings` row as the CLI prints it. */
export interface SettingsRow {
  key: string;
  value?: unknown;
  version?: number;
  source?: string;
}

// ---------------------------------------------------------------------------------------------
// small shared helpers
// ---------------------------------------------------------------------------------------------

/** `PrettyTable` with `align = 'l'`: a fixed-width ASCII table, no dependency needed. */
export function renderTable(headers: readonly string[], rows: ReadonlyArray<readonly string[]>): string {
  const widths = headers.map((header, index) =>
    Math.max(header.length, ...rows.map((row) => (row[index] ?? '').split('\n')[0]?.length ?? 0)),
  );
  const line = `+${widths.map((width) => '-'.repeat(width + 2)).join('+')}+`;
  const format = (cells: readonly string[]): string =>
    `|${cells.map((cell, index) => ` ${(cell ?? '').padEnd(widths[index] ?? 0)} `).join('|')}|`;
  return [line, format(headers), line, ...rows.map(format), line].join('\n');
}

/** `Utils.get_value_as_string(..., '-')`: Python's `str()` for the shapes cluster settings hold. */
export function pyStr(value: unknown, defaultValue = '-'): string {
  if (value === undefined || value === null) return defaultValue;
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (Array.isArray(value)) return `[${value.map((item) => (typeof item === 'string' ? `'${item}'` : pyStr(item, 'None'))).join(', ')}]`;
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** `Utils.to_yaml`: `yaml.dump(sort_keys=False, width=140)`. */
export function toYaml(value: unknown): string {
  return yaml.dump(value, { sortKeys: false, lineWidth: 140, noRefs: true });
}

/** Nests flat `a.b.c` keys, longest-last so `a.b.c` can replace a scalar written at `a.b`. */
export function buildTree(entries: ReadonlyArray<{ key: string; value?: unknown }>): Record<string, unknown> {
  const tree: Record<string, unknown> = {};
  const sorted = [...entries].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  for (const entry of sorted) {
    const parts = entry.key.split('.');
    let node = tree;
    for (const part of parts.slice(0, -1)) {
      const child = node[part];
      if (typeof child !== 'object' || child === null || Array.isArray(child)) node[part] = {};
      node = node[part] as Record<string, unknown>;
    }
    const leaf = parts[parts.length - 1];
    if (leaf !== undefined) node[leaf] = entry.value ?? null;
  }
  return tree;
}

/**
 * `ClusterConfigDB.get_config_entries`: a full scan, `re.match` (start-anchored) against `query`,
 * sorted by key. `version` survives, which is why this does not go through `ClusterConfig`.
 */
export async function scanSettings(deps: Deps, clusterName: string, query?: string): Promise<SettingsRow[]> {
  let pattern: RegExp | undefined;
  if (!isEmpty(query)) {
    try {
      pattern = new RegExp(query as string);
    } catch (error) {
      throw new ClusterConfigError(`invalid search regex: ${query} - ${(error as Error).message}`);
    }
  }
  const rows: SettingsRow[] = [];
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: `${clusterName}.cluster-settings`, ExclusiveStartKey: startKey });
    for (const item of page.Items ?? []) {
      const key = item['key'];
      if (typeof key !== 'string') continue;
      if (pattern !== undefined && pattern.exec(key)?.index !== 0) continue;
      rows.push({
        key,
        value: checkAndConvertDecimalValue(item['value']),
        version: typeof item['version'] === 'number' ? item['version'] : 0,
        source: typeof item['source'] === 'string' ? item['source'] : undefined,
      });
    }
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  rows.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  return rows;
}

export async function scanModules(deps: Deps, clusterName: string): Promise<ModuleInfo[]> {
  const modules: ModuleInfo[] = [];
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: `${clusterName}.modules`, ExclusiveStartKey: startKey });
    for (const item of page.Items ?? []) modules.push(item as unknown as ModuleInfo);
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  return modules;
}

/**
 * `get_bucket_name`: the recorded bucket, else the conventional name. The fallback is what lets
 * `save-values` work on a cluster whose settings table was lost.
 */
export async function clusterBucketName(deps: Deps, clusterName: string, awsRegion: string): Promise<string> {
  const rows = await scanSettings(deps, clusterName, '^cluster\\.cluster_s3_bucket$');
  const recorded = rows[0]?.value;
  if (typeof recorded === 'string' && recorded !== '') return recorded;
  return `${clusterName}-cluster-${awsRegion}-${await deps.accountId()}`;
}

function hasConfig(dir: string): boolean {
  if (!existsSync(dir) || !statSync(dir).isDirectory()) return false;
  return readdirSync(dir).some((entry) => entry !== '.DS_Store');
}

function cleanupClusterRegionDir(dir: string, preserveValuesFile: boolean): void {
  for (const entry of readdirSync(dir)) {
    if (preserveValuesFile && entry === 'values.yml') continue;
    rmSync(join(dir, entry), { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------------------------
// commands
// ---------------------------------------------------------------------------------------------

export interface GenerateOptions {
  valuesFile?: string;
  configDir?: string;
  force?: boolean;
  existingResources?: boolean;
  regenerate?: boolean;
  /** Optional driver for test and replay callers. */
  installerDriver?: PromptDriver;
  /** Optional identity resolver for test and replay callers. */
  installerIdentity?: (values: Readonly<Record<string, unknown>>) => Promise<InstallerIdentity>;
  /** Resource-backed choices and validations for the installer. */
  installerChoices?: InstallerChoiceProvider;
}

/**
 * `config generate`. Returns the values map, because `quick-setup` deploys what this returned.
 */
export async function configGenerate(deps: Deps, options: GenerateOptions): Promise<Record<string, unknown>> {
  if (options.configDir !== undefined && !existsSync(options.configDir)) {
    deps.err(`${options.configDir} not found or is not a valid directory.`);
    throw new ExitWithCode(1);
  }
  const values = isEmpty(options.valuesFile)
    ? await collectInstallerValues({
        driver: options.installerDriver ?? new TerminalPromptDriver(),
        identity: options.installerIdentity ?? (async (answers) => {
          const region = answers["aws_region"];
          return installerIdentity(await deps.accountId(), typeof region === "string" ? region : undefined);
        }),
        existingResources: options.existingResources,
        regenerate: options.regenerate,
        choices: options.installerChoices,
      })
    : loadPreparedValues(options.valuesFile as string);

  requireContainerShapeDecision(deps, values, options.regenerate === true);

  const clusterName = String(values['cluster_name'] ?? '');
  const awsRegion = String(values['aws_region'] ?? '');
  if (clusterName === '' || awsRegion === '') {
    deps.err('Cluster name and AWS region are required');
    throw new ExitWithCode(1);
  }

  const regionDir = options.configDir ?? clusterRegionDir(clusterName, awsRegion);
  const valuesFileCopy = join(regionDir, 'values.yml');
  const preserveValuesFile = options.valuesFile === valuesFileCopy;

  if (options.force !== true && hasConfig(regionDir)) {
    const confirm = await deps.prompt({
      message: `Config directory: ${regionDir} is not empty, would you like to overwrite it?`,
      default: true,
    });
    if (confirm !== true && confirm !== 'Yes') {
      deps.out('Aborted!');
      throw new ExitWithCode(0);
    }
    cleanupClusterRegionDir(regionDir, preserveValuesFile);
  }

  mkdirSync(regionDir, { recursive: true });
  deps.out(`saving values to: ${valuesFileCopy}`);
  writeFileSync(valuesFileCopy, toYaml(values));

  deps.out('generating config from templates ...');
  generateConfigFromTemplates(values, join(regionDir, 'config'));
  return values;
}

/**
 * Stops a new cluster whose values file says nothing about the control-plane shape.
 *
 * The installer writes `enable_ecs`, so a file with no key came from somewhere else: written by
 * hand, carried over from before the container control plane, or copied from another cluster.
 * Generating from it produces a module set with no container module, which is the host shape, and
 * the host shape no longer builds because the per-module release archives a control-plane host
 * downloads are not produced any more. The failure would land partway through a deploy with
 * nothing pointing at the missing key, so it is named here instead, before anything is created.
 *
 * `--regenerate` is the existing-cluster path. That cluster's shape is whatever it already has, so
 * a missing key there is the correct answer and not a question. An explicit `false` is a recorded
 * decision rather than an omission, and is left alone: the migration stages exactly that value.
 */
function requireContainerShapeDecision(
  deps: Deps,
  values: Readonly<Record<string, unknown>>,
  regenerate: boolean,
): void {
  if (regenerate || Object.hasOwn(values, 'enable_ecs')) return;
  deps.err(
    'enable_ecs is missing from the values file. A new cluster runs its control plane as container ' +
      'tasks, and that is the only supported shape: without this key the generated module set has no ' +
      'container module, and a control plane on instances cannot be built because its per-module ' +
      'release archives are no longer produced.',
  );
  deps.err('Add `enable_ecs: true` to the values file, or run `config generate` with no --values-file to let the installer write it.');
  deps.err('Regenerating the configuration of a cluster that already exists is a different command: pass --regenerate.');
  throw new ExitWithCode(1);
}

/** Loads a supplied values file without involving the interactive installer flow. */
function loadPreparedValues(valuesFile: string): Record<string, unknown> {
  if (!existsSync(valuesFile)) throw new ClusterConfigError(`file not found: ${valuesFile}`);
  return loadValuesFile(valuesFile);
}

/**
 * The Python callback derives partition and DNS suffix from the AWS session after the account
 * section. The region mapping is the same endpoint partition boundary for supported regions.
 */
function installerIdentity(accountId: string, region: string | undefined): InstallerIdentity {
  if (!/^\d{12}$/.test(accountId)) throw new ClusterConfigError("sts:GetCallerIdentity returned an invalid account");
  if (region?.startsWith("cn-") === true) {
    return { accountId, partition: "aws-cn", dnsSuffix: "amazonaws.com.cn" };
  }
  if (region?.startsWith("us-gov-") === true) {
    return { accountId, partition: "aws-us-gov", dnsSuffix: "amazonaws.com" };
  }
  if (region?.startsWith("us-iso-") === true) {
    return { accountId, partition: "aws-iso", dnsSuffix: "c2s.ic.gov" };
  }
  if (region?.startsWith("us-isob-") === true) {
    return { accountId, partition: "aws-iso-b", dnsSuffix: "sc2s.sgov.gov" };
  }
  return { accountId, partition: "aws", dnsSuffix: "amazonaws.com" };
}

export interface UpdateOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  moduleSet: string;
  force?: boolean;
  overwrite?: boolean;
  keyPrefix?: string;
  configDir?: string;
}

/** `config update`: the local `config/` tree becomes the cluster settings table. */
export async function configUpdate(deps: Deps, options: UpdateOptions): Promise<void> {
  let configDir: string;
  if (!isEmpty(options.configDir)) {
    configDir = join(options.configDir as string, 'config');
    if (!existsSync(configDir)) {
      deps.err(`${configDir} does not exist`);
      throw new ExitWithCode(1);
    }
  } else {
    configDir = clusterConfigDir(options.clusterName, options.awsRegion);
  }

  // A --config-dir pointing at another cluster's tree would push that cluster's settings here.
  const localConfig = readConfigFromFiles(configDir);
  const clusterModuleId = requireLocal(
    localConfig,
    `global-settings.module_sets.${options.moduleSet}.cluster.module_id`,
  );
  const localClusterName = requireLocal(localConfig, `${clusterModuleId}.cluster_name`);
  if (localClusterName !== options.clusterName) {
    throw new ClusterConfigError(
      `local configuration in ${configDir} does not match the given cluster name: ${options.clusterName}`,
    );
  }
  const localAwsRegion = requireLocal(localConfig, `${clusterModuleId}.aws.region`);
  if (localAwsRegion !== options.awsRegion) {
    throw new ClusterConfigError(
      `local configuration in ${configDir} does not match the given aws region: ${options.awsRegion}`,
    );
  }

  const readEntries = (): ConfigEntry[] => {
    deps.out(`reading cluster settings from ${configDir} ...`);
    const entries = convertConfigToKeyValuePairs(configDir, options.keyPrefix);
    deps.out(renderTable(['Key', 'Value'], entries.map((entry) => [entry.key, pyStr(entry.value)])));
    return entries;
  };

  let entries = readEntries();
  if (options.force !== true) {
    for (;;) {
      const result = await deps.prompt({
        message:
          'Are you sure you want to update cluster settings db with above configuration from local file system?',
        choices: ['Yes', 'Reload Changes', 'Exit'],
        default: 'Yes',
      });
      if (result === 'Exit') {
        deps.out('Aborted!');
        throw new ExitWithCode(0);
      }
      if (result === 'Reload Changes') {
        entries = readEntries();
        continue;
      }
      break;
    }
  }

  const writer = await deps.configWriter({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
    dynamodbKmsKeyId: lookupLocal(localConfig, `${clusterModuleId}.dynamodb.kms_key_id`) as string | null,
    createDatabase: true,
  });
  await writer.syncModulesInDb(
    readModulesFromFiles(configDir).map((module) => ({ ...module, id: module.id, name: module.name, type: module.type })),
  );
  await writer.syncClusterSettingsInDb(entries, options.overwrite === true);
}

function lookupLocal(config: Record<string, unknown>, key: string): unknown {
  let node: unknown = config;
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

function requireLocal(config: Record<string, unknown>, key: string): string {
  const value = lookupLocal(config, key);
  if (typeof value !== 'string' || value === '') {
    throw new ClusterConfigError(`config key not found: ${key}`);
  }
  return value;
}

/** `config set`: `Key=K,Type=T,Value=V` into typed entries. Throws on the first bad entry. */
export function parseSetEntries(entries: readonly string[]): Array<{ key: string; value: unknown }> {
  return entries.map((entry, index) => {
    const tokens = splitN(entry, ',', 3);
    const key = afterPrefix(tokens[0], 'Key=', index, 'Key');
    const rawType = afterPrefix(tokens[1], 'Type=', index, 'Type');
    const rawValue = afterPrefix(tokens[2], 'Value=', index, 'Value');

    if (key.includes(',') || key.includes(':')) {
      throw new ClusterConfigError(
        `[${index}] Invalid Key: ${key}. comma(,) and colon(:) are not allowed in key names.`,
      );
    }

    const { dataType, isList } = normalizeType(rawType, index);
    if (isList) {
      const items = rawValue
        .split(',')
        .map((token) => token.trim())
        .filter((token) => token !== '');
      if (dataType === 'int') {
        for (const item of items) {
          if (!/^[+-]?\d+$/.test(item)) {
            throw new ClusterConfigError(`[${index}] Value: ${rawValue} is not a valid list<${dataType}>`);
          }
        }
        return { key, value: items.map((item) => Number.parseInt(item, 10)) };
      }
      if (dataType === 'float') {
        for (const item of items) {
          if (!Number.isFinite(Number(item))) {
            throw new ClusterConfigError(`[${index}] Value: ${rawValue} is not a valid list<${dataType}>`);
          }
        }
        return { key, value: items.map((item) => Number(item)) };
      }
      // Python's list<bool> branch is dead code (a duplicate `int` test), so a list<bool> stays
      // a list of strings, as it does today.
      return { key, value: items };
    }

    if (dataType === 'int') {
      if (!/^[+-]?\d+$/.test(rawValue)) {
        throw new ClusterConfigError(`[${index}] Value: ${rawValue} is not a valid ${dataType}`);
      }
      return { key, value: Number.parseInt(rawValue, 10) };
    }
    if (dataType === 'float') {
      if (!Number.isFinite(Number(rawValue))) {
        throw new ClusterConfigError(`[${index}] Value: ${rawValue} is not a valid ${dataType}`);
      }
      return { key, value: Number(rawValue) };
    }
    if (dataType === 'bool') {
      return { key, value: ['true', 'yes', 'y', '1', 'on'].includes(rawValue.toLowerCase()) };
    }
    return { key, value: rawValue };
  });
}

function splitN(value: string, separator: string, limit: number): string[] {
  const parts: string[] = [];
  let rest = value;
  while (parts.length < limit - 1) {
    const index = rest.indexOf(separator);
    if (index === -1) break;
    parts.push(rest.slice(0, index));
    rest = rest.slice(index + separator.length);
  }
  parts.push(rest);
  return parts;
}

function afterPrefix(token: string | undefined, prefix: string, index: number, name: string): string {
  const parts = (token ?? '').split(prefix);
  const value = parts[1]?.trim() ?? '';
  if (value === '') throw new ClusterConfigError(`[${index}] ${name} is required`);
  return value;
}

function normalizeType(rawType: string, index: number): { dataType: string; isList: boolean } {
  const table: Record<string, { dataType: string; isList: boolean }> = {
    str: { dataType: 'str', isList: false },
    string: { dataType: 'str', isList: false },
    int: { dataType: 'int', isList: false },
    integer: { dataType: 'int', isList: false },
    bool: { dataType: 'bool', isList: false },
    boolean: { dataType: 'bool', isList: false },
    float: { dataType: 'float', isList: false },
    decimal: { dataType: 'float', isList: false },
    'list<str>': { dataType: 'str', isList: true },
    'list<string>': { dataType: 'str', isList: true },
    'list<int>': { dataType: 'int', isList: true },
    'list<integer>': { dataType: 'int', isList: true },
    'list<bool>': { dataType: 'bool', isList: true },
    'list<boolean>': { dataType: 'bool', isList: true },
    'list<float>': { dataType: 'float', isList: true },
    'list<decimal>': { dataType: 'float', isList: true },
  };
  const resolved = table[rawType];
  if (resolved === undefined) throw new ClusterConfigError(`[${index}] Type: ${rawType} not supported`);
  return resolved;
}

/** `config export`: the DB back into a `config/` tree. Refuses a non-empty directory. */
export async function configExport(
  deps: Deps,
  options: { clusterName: string; awsRegion: string; awsProfile?: string; exportDir?: string },
): Promise<void> {
  const exportDir =
    options.exportDir ?? join(clusterRegionDir(options.clusterName, options.awsRegion), 'config');
  if (hasConfig(exportDir)) {
    throw new GeneralException(
      `export directory: ${exportDir} already exists and can cause merge conflicts. ` +
        'backup your existing configuration to another directory and try again.',
    );
  }
  const entries = await scanSettings(deps, options.clusterName);
  const modules = await scanModules(deps, options.clusterName);
  deps.out(`exporting config from db to ${exportDir} ...`);
  mkdirSync(exportDir, { recursive: true });

  const tree = buildTree(entries);
  const ideaConfig: { modules: Array<Record<string, unknown>> } = { modules: [] };
  for (const module of modules) {
    const moduleDir = join(exportDir, module.module_id);
    mkdirSync(moduleDir, { recursive: true });
    writeFileSync(join(moduleDir, 'settings.yml'), toYaml(tree[module.module_id] ?? {}));
    ideaConfig.modules.push({
      name: module.name,
      id: module.module_id,
      type: module.type,
      config_files: ['settings.yml'],
    });
  }
  writeFileSync(join(exportDir, 'idea.yml'), toYaml(ideaConfig));
}

/** `config diff`: local `config/` against the DB, as MODIFIED / DELETED / ADDED. */
export async function configDiff(
  deps: Deps,
  options: { clusterName: string; awsRegion: string; configDir?: string },
): Promise<Array<[string, string, string, string]>> {
  const configDir = options.configDir ?? clusterConfigDir(options.clusterName, options.awsRegion);
  const dbEntries = new Map<string, string>();
  for (const row of await scanSettings(deps, options.clusterName)) dbEntries.set(row.key, pyStr(row.value));
  const localEntries = new Map<string, string>();
  for (const entry of convertConfigToKeyValuePairs(configDir)) localEntries.set(entry.key, pyStr(entry.value));

  const rows: Array<[string, string, string, string]> = [];
  for (const [key, value] of dbEntries) {
    if (localEntries.get(key) === value) continue;
    if (localEntries.has(key)) rows.push([key, value, localEntries.get(key) as string, 'MODIFIED']);
    else rows.push([key, value, 'n/a', 'DELETED']);
  }
  for (const [key, value] of localEntries) {
    if (dbEntries.has(key)) continue;
    rows.push([key, 'n/a', value, 'ADDED']);
  }
  rows.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return rows;
}

/** Options shared by the standalone preview and the upgrade pre-write step. */
export interface ConfigUpgradePreviewOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  baseOs?: string;
  valuesFile?: string;
  skipGlobalSettingsUpdate?: boolean;
  modules?: readonly string[];
}

/**
 * Tests and replay callers can supply the complete comparison input. The live
 * command prepares the same input through the upgrade command's read adapters.
 */
export interface ConfigDriftPreviewDeps extends Deps {
  loadUpgradeDriftInput?(
    options: ConfigUpgradePreviewOptions,
  ): Promise<UpgradeDriftInput>;
}

/** Render the same preview used by `upgrade-cluster`, without changing cluster state. */
export async function configUpgradePreview(
  deps: ConfigDriftPreviewDeps,
  options: ConfigUpgradePreviewOptions,
): Promise<UpgradeDriftReport> {
  const { compareUpgradeDrift, renderUpgradeDrift } = await import("../../config/upgrade-drift.ts");
  const input =
    deps.loadUpgradeDriftInput === undefined
      ? await (async () => {
          const { createLiveUpgradeDeps, prepareUpgradeDriftInput } = await import("./upgrade.ts");
          return prepareUpgradeDriftInput(createLiveUpgradeDeps(deps), options);
        })()
      : await deps.loadUpgradeDriftInput(options);
  const report = compareUpgradeDrift(input);
  deps.out(renderUpgradeDrift(report));
  return report;
}

// ---------------------------------------------------------------------------------------------
// commander wiring
// ---------------------------------------------------------------------------------------------

const clusterOptions = (command: Command): Command =>
  command
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region');

export function registerConfigCommands(program: Command, deps: Deps): Command {
  const config = program.command('config').description('configuration options');

  config
    .command('generate')
    .description('generate configuration')
    .option('--values-file <values-file>', 'path to values.yml file')
    .option('--config-dir <config-dir>', 'path to where to create config directory')
    .option('--force', 'Skip all confirmation prompts.')
    .option('--existing-resources', 'Generate configuration using existing resources')
    .option(
      '--regenerate',
      'Regenerate configuration for an existing cluster. Enables skipping validations such as existing cluster name and CIDR block.',
    )
    .action(async (options: GenerateOptions) => {
      await configGenerate(deps, options);
    });

  clusterOptions(config.command('update'))
    .description('update configuration from local file system to cluster settings db')
    .option('--force', 'Skip all confirmation prompts.')
    .option('--overwrite', 'Overwrite existing db config entries. Default behavior is to skip if the config entry exists.')
    .option('--key-prefix <key-prefix>', 'Update configuration for the keys matching the given key prefix.')
    .option('--config-dir <config-dir>', 'Path to Config Directory; Uses default location if not provided')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .action(async (options: UpdateOptions) => {
      await configUpdate(deps, options);
    });

  clusterOptions(config.command('set'))
    .description('set config entries')
    .option('--force', 'Skip confirmation prompts')
    .argument('<entries...>', 'Key=KEY_NAME,Type=[str|int|float|bool|list<str>|list<int>|list<float>|list<bool>],Value=VALUE')
    .action(async (entries: string[], options: { clusterName: string; awsRegion: string; awsProfile?: string; force?: boolean }) => {
      const parsed = parseSetEntries(entries);
      deps.out(renderTable(['Key', 'Value'], parsed.map((entry) => [entry.key, pyStr(entry.value)])));
      if (options.force !== true) {
        const confirm = await deps.prompt({ message: 'Are you sure you want to update above config entries?' });
        if (confirm !== true && confirm !== 'Yes') {
          deps.out('Abort!');
          throw new ExitWithCode(0);
        }
      }
      const writer = await deps.configWriter({
        clusterName: options.clusterName,
        awsRegion: options.awsRegion,
        awsProfile: options.awsProfile,
      });
      for (const entry of parsed) await writer.setConfigEntry(entry.key, entry.value);
    });

  clusterOptions(config.command('show'))
    .description('show configuration for a cluster as yaml')
    .option('-q, --query <query>', 'Search Query for configuration entries. Accepts a regular expression.')
    .addOption(new Option('--format <format>', 'Output format. Default: table').choices(['table', 'yaml', 'raw']))
    .action(async (options: { clusterName: string; query?: string; format?: string }) => {
      const rows = await scanSettings(deps, options.clusterName, options.query);
      if (options.format === 'yaml') {
        deps.out(toYaml(buildTree(rows)));
      } else if (options.format === 'raw') {
        for (const row of rows) if (row.value !== undefined && row.value !== null) deps.out(pyStr(row.value));
      } else {
        deps.out(
          renderTable(
            ['Key', 'Value', 'Version'],
            rows.map((row) => [row.key, pyStr(row.value), String(row.version ?? 0)]),
          ),
        );
      }
    });

  clusterOptions(config.command('export'))
    .description('export configuration')
    .option('--export-dir <export-dir>', 'Export Directory. Defaults to the cluster config directory.')
    .action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string; exportDir?: string }) => {
      await configExport(deps, options);
    });

  clusterOptions(config.command('delete'))
    .description('delete all configuration entries for a given config key prefix')
    .argument('<config-key-prefixes...>', 'config key prefixes')
    .action(async (prefixes: string[], options: { clusterName: string; awsRegion: string; awsProfile?: string }) => {
      const writer = await deps.configWriter({
        clusterName: options.clusterName,
        awsRegion: options.awsRegion,
        awsProfile: options.awsProfile,
      });
      for (const prefix of prefixes) await writer.deleteConfigEntries(prefix.trim());
    });

  clusterOptions(config.command('diff'))
    .description('diff configuration files between the latest config and the config in the db')
    .option('--config-dir <config-dir>', 'Path to local config folder; default location will be used if none provided')
    .action(async (options: { clusterName: string; awsRegion: string; configDir?: string }) => {
      const rows = await configDiff(deps, options);
      deps.out(renderTable(['Key', 'Old Value', 'New Value', 'Status'], rows));
    });

  clusterOptions(config.command("preview-upgrade"))
    .description("preview configuration changes made by an upgrade")
    .option("--base-os <base-os>", "Base OS to upgrade to.")
    .option("--values-file <values-file>", "Path to values.yml. Uses the cluster copy by default.")
    .option("--skip-global-settings-update", "Skip updating global settings.")
    .argument("[modules...]", "module ids")
    .action(async (modules: string[], options: ConfigUpgradePreviewOptions) => {
      await configUpgradePreview(deps, { ...options, modules });
    });

  clusterOptions(config.command('save-values'))
    .description('save values file in s3 bucket')
    .option('--values-file <values-file>', 'path to values.yml file')
    .action(async (options: { clusterName: string; awsRegion: string; valuesFile?: string }) => {
      const valuesFile = options.valuesFile ?? valuesFilePath(options.clusterName, options.awsRegion);
      const bucket = await clusterBucketName(deps, options.clusterName, options.awsRegion);
      await deps.s3.putObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY, Body: readFileSync(valuesFile) });
      deps.out(`saved ${valuesFile} to s3://${bucket}/${VALUES_FILE_S3_KEY}`);
    });

  clusterOptions(config.command('download-values'))
    .description('download values.yml from s3 bucket to default or provided location')
    .option('--values-dir <values-dir>', 'Path to folder to save values.yml file')
    .action(async (options: { clusterName: string; awsRegion: string; valuesDir?: string }) => {
      const valuesFile =
        options.valuesDir === undefined
          ? valuesFilePath(options.clusterName, options.awsRegion)
          : join(mkdirSync(options.valuesDir, { recursive: true }) ?? options.valuesDir, 'values.yml');
      const bucket = await clusterBucketName(deps, options.clusterName, options.awsRegion);
      let body: string;
      try {
        body = await deps.s3.getObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY });
      } catch (error) {
        deps.err(
          `Values file not found at ${valuesFile} and could not be downloaded from ` +
            `s3://${bucket}/${VALUES_FILE_S3_KEY}: ${(error as Error).message}. Restore values.yml to ` +
            `${valuesFile} from a backup, then upload it with: ideactl config save-values`,
        );
        throw new ExitWithCode(1);
      }
      writeFileSync(valuesFile, toYaml(yaml.load(body)));
      deps.out(`downloaded s3://${bucket}/${VALUES_FILE_S3_KEY} to ${valuesFile}`);
    });

  return config;
}
