/**
 * Rehearse an existing-cluster container migration from captured local inputs.
 *
 * The rehearsal models table synchronization, synthesizes the final templates in
 * deployment order, and checks upgrade-only boundaries. It never selects a live
 * configuration reader.
 */

import { copyFileSync, existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";
import { fileURLToPath } from "node:url";

import { buildApp } from "../../src/cdk/app.ts";
import { IdeaCodeAsset } from "../../src/cdk/code-asset.ts";
import { deploymentOrder } from "../../src/cli/deployment-helper.ts";
import {
  ClusterConfig,
  unmarshallAttribute,
  type ModuleInfo,
} from "../../src/config/cluster-config.ts";
import {
  convertConfigToKeyValuePairs,
  generateConfigFromTemplates,
  type ConfigEntry,
  type ModuleEntry,
} from "../../src/config/generator.ts";
import {
  compareUpgradeDrift,
  type CurrentConfigRow,
  type StackSettingsPlan,
} from "../../src/config/upgrade-drift.ts";
import { loadValuesFile, type UserValues } from "../../src/config/values.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = resolve(HERE, "../..");
const DEFAULT_FIXTURE_DIR = join(PKG, "tools/parity/fixtures/idea-dev27");
const DEFAULT_LIVE_DIR = join(PKG, "tools/parity/live");
const DEPLOYMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const TARGET_IMAGE = "example.invalid/idea-control-plane:rehearsal";
const CONTAINER_MODULE_SET_KEY = "global-settings.module_sets.default.ecs.module_id";
const CONTAINER_FLAG_KEY = "ecs.enabled";
const SCHEDULER_DESIRED_KEY = "ecs.tasks.scheduler.desired";
const STABLE_NAME_KEY = "scheduler.use_stable_server_name";
const RETAIN_DNS_KEY = "scheduler.retain_dns_record";
const RETAIN_HOSTS_KEY = "ecs.retain_existing_hosts";
/** The modules whose migration splits into a routed step and a legacy-removed step. */
const ROUTED_MODULES = ["cluster-manager", "vdc", "scheduler"] as const;

/**
 * Settings keys the live migration executor actually writes, read from its source.
 *
 * The two "undriven" findings below used to fire unconditionally: the harness proved the stack could
 * express an intermediate shape, then asserted nothing drove it, without ever checking. Once the
 * executor started writing those rows the findings stayed red and said something untrue, which is
 * the same class of defect as a check that cannot fail, just pointing the other way.
 *
 * This scans for a write of each key rather than importing the executor, because the executor pulls
 * in cloud clients the rehearsal deliberately never loads. It is brittle toward red: renaming a key
 * or dropping a write brings the finding back, which is the safe direction for a gate.
 */
function executorWrittenKeys(): ReadonlySet<string> {
  const source = join(PKG, "src/cli/live-migrate-adapters.ts");
  if (!existsSync(source)) return new Set();
  const text = readFileSync(source, "utf8");
  const constants = new Map<string, string>();
  for (const match of text.matchAll(/const\s+([A-Z0-9_]+)\s*=\s*"([^"]+)"/g)) {
    constants.set(match[1] as string, match[2] as string);
  }
  const written = new Set<string>();
  for (const match of text.matchAll(/\{\s*key:\s*([A-Za-z0-9_]+|"[^"]+")\s*,\s*value:/g)) {
    const token = match[1] as string;
    if (token.startsWith('"')) written.add(token.slice(1, -1));
    else {
      const resolved = constants.get(token);
      if (resolved !== undefined) written.add(resolved);
    }
  }
  return written;
}

const STAGE_NAMES = [
  "PREFLIGHT_PASSED",
  "OPERATION_STARTED",
  "ADMISSION_CLOSED",
  "WORKLOAD_DRAINED",
  "LEGACY_SCHEDULER_CAPTURED",
  "SOFTWARE_STACKS_RECONCILED",
  "CONFIGURATION_STAGED",
  "PROVIDER_STACKS_COMMITTED",
  "SCHEDULER_DNS_RETAINED",
  "ECS_CONFIGURATION_ACTIVE",
  "ECS_STAGED",
  "PBS_STATE_SEEDED",
  "ECS_SCHEDULER_READY",
  "CLUSTER_MANAGER_ROUTED",
  "CLUSTER_MANAGER_LEGACY_REMOVED",
  "VDC_ROUTED",
  "VDC_LEGACY_REMOVED",
  "SCHEDULER_ROUTED",
  "SCHEDULER_LEGACY_REMOVED",
  "TARGET_PROVED",
  "ADMISSION_REOPENED",
  "OPERATION_COMPLETED",
] as const;

type StageName = (typeof STAGE_NAMES)[number];
type FindingSeverity = "BLOCKING" | "OBSERVED" | "RISK";
type StageStatus = "MODELED" | "SYNTHESIZED" | "TEMPLATE_ONLY" | "BLOCKED" | "REAL_CLUSTER_ONLY";
type Template = Record<string, unknown>;
type Settings = Map<string, unknown>;

export interface ConfigurationSyncPlan {
  globalDeletes: string[];
  globalWrites: string[];
  addOnlyWrites: string[];
  changedGlobalRows: string[];
  removedGlobalRows: string[];
  preservedDrift: string[];
}

export interface LaterWriterRead {
  consumer: string;
  key: string;
  writer: string;
}

export interface ReplacementFinding {
  logicalId: string;
  resourceType: string;
  property: "Name";
}

export interface RehearsalFinding {
  stage: number;
  code: string;
  severity: FindingSeverity;
  summary: string;
  evidence: string[];
}

export interface RehearsalStage {
  number: number;
  name: StageName;
  status: StageStatus;
  evidence: string[];
}

export interface StackSynthesis {
  moduleId: string;
  moduleName: string;
  status: "SYNTHESIZED" | "BLOCKED";
  reads: string[];
  publishedSettings: string[];
  resourceCount: number;
  problem?: string;
}

interface InternalStackSynthesis extends StackSynthesis {
  template?: Template;
  settings?: Record<string, unknown>;
}

export interface RealClusterProof {
  stages: number[];
  proof: string;
}

export interface UpgradeRehearsal {
  clusterName: string;
  currentRows: number;
  generatedRows: number;
  currentModules: string[];
  generatedModules: string[];
  actualUpgradeOrder: string[];
  rehearsalOrder: string[];
  sync: ConfigurationSyncPlan;
  stages: RehearsalStage[];
  stacks: StackSynthesis[];
  findings: RehearsalFinding[];
  realClusterProofs: RealClusterProof[];
  localShims: string[];
}

export interface RehearseOptions {
  fixtureDir?: string;
  liveDir?: string;
  workDir?: string;
  keepWorkDir?: boolean;
}

interface MaterializationContext {
  accountId: string;
  partition: string;
  region: string;
}

interface FixturePaths {
  clusterSettings: string;
  modules: string;
  values: string;
  synthReads: string;
  cdkContext: string;
}

/** Return true only for non-array objects. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parse one JSON object and fail with the input path in the diagnostic. */
function readJsonObject(file: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(parsed)) throw new TypeError(`${file}: expected a JSON object`);
  return parsed;
}

/** Decode one typed scan fixture without loading a network client. */
function readTypedScan(file: string): Array<Record<string, unknown>> {
  const scan = readJsonObject(file);
  if (!Array.isArray(scan.Items)) throw new TypeError(`${file}: expected an Items array`);
  return scan.Items.map((item, index) => {
    if (!isRecord(item)) throw new TypeError(`${file}: Items[${index}] must be an object`);
    const decoded: Record<string, unknown> = {};
    for (const [key, attribute] of Object.entries(item)) {
      if (!isRecord(attribute)) {
        throw new TypeError(`${file}: Items[${index}].${key} must be a typed attribute`);
      }
      decoded[key] = unmarshallAttribute(attribute);
    }
    return decoded;
  });
}

/** Validate and convert decoded settings rows. */
function currentConfigRows(rows: readonly Record<string, unknown>[]): CurrentConfigRow[] {
  return rows.map((row, index) => {
    const key = row.key;
    if (typeof key !== "string" || key === "") {
      throw new TypeError(`cluster settings row ${index} has no string key`);
    }
    return {
      key,
      value: row.value,
      source: typeof row.source === "string" ? row.source : undefined,
      version: typeof row.version === "number" ? row.version : undefined,
    };
  });
}

/** Validate and convert decoded module rows. */
function moduleRows(rows: readonly Record<string, unknown>[]): ModuleInfo[] {
  return rows.map((row, index) => {
    if (
      typeof row.module_id !== "string" ||
      typeof row.name !== "string" ||
      typeof row.type !== "string"
    ) {
      throw new TypeError(`module row ${index} has no module_id, name, or type`);
    }
    return {
      ...row,
      module_id: row.module_id,
      name: row.name,
      type: row.type,
      status: typeof row.status === "string" ? row.status : undefined,
      stack_name: typeof row.stack_name === "string" ? row.stack_name : null,
      version: typeof row.version === "string" ? row.version : null,
    };
  });
}

/** Resolve the exact captured files used by the rehearsal. */
function fixturePaths(fixtureDir: string): FixturePaths {
  const firstExisting = (candidates: string[], label: string): string => {
    const found = candidates.find(existsSync);
    if (found === undefined) {
      throw new Error(`missing ${label}: looked for ${candidates.join(", ")}`);
    }
    return found;
  };

  return {
    clusterSettings: firstExisting(
      [
        join(fixtureDir, "raw/cluster-settings.scan.json"),
        join(fixtureDir, "cluster-settings.json"),
      ],
      "cluster settings fixture",
    ),
    modules: firstExisting(
      [join(fixtureDir, "raw/modules.scan.json"), join(fixtureDir, "modules.json")],
      "modules fixture",
    ),
    values: firstExisting(
      [join(fixtureDir, "python/values.yml"), join(fixtureDir, "values.yml")],
      "values fixture",
    ),
    synthReads: firstExisting([join(fixtureDir, "synth-reads.json")], "synth reads fixture"),
    cdkContext: firstExisting(
      [join(fixtureDir, "cdk.context.json"), join(fixtureDir, "python/_cdk/cdk.context.json")],
      "CDK context fixture",
    ),
  };
}

/** Reject duplicate configuration keys before comparing synchronization effects. */
function entryMap<T extends ConfigEntry>(entries: readonly T[], label: string): Map<string, T> {
  const result = new Map<string, T>();
  for (const entry of entries) {
    if (entry.key === "") throw new TypeError(`${label} contains an empty key`);
    if (result.has(entry.key)) throw new TypeError(`${label} contains duplicate key ${entry.key}`);
    result.set(entry.key, entry);
  }
  return result;
}

/**
 * Compute the global update in place plus the non-global add-only
 * synchronization. Lists contain keys only, so no captured value is exposed.
 */
export function planConfigurationSync(
  current: readonly CurrentConfigRow[],
  generated: readonly ConfigEntry[],
): ConfigurationSyncPlan {
  const currentByKey = entryMap(current, "current settings");
  const generatedByKey = entryMap(generated, "generated settings");
  const globalDeletes: string[] = [];
  const globalWrites = [...generatedByKey.keys()]
    .filter((key) => key.startsWith("global-settings."))
    .sort();
  const addOnlyWrites: string[] = [];
  const changedGlobalRows: string[] = [];
  const removedGlobalRows = globalDeletes
    .filter((key) => !generatedByKey.has(key))
    .sort();
  const preservedDrift: string[] = [];

  for (const [key, target] of generatedByKey) {
    const before = currentByKey.get(key);
    if (key.startsWith("global-settings.")) {
      if (before !== undefined && !isDeepStrictEqual(before.value, target.value)) {
        if (/^global-settings\.module_sets\.[^.]+\.[^.]+\.module_id$/.test(key)) preservedDrift.push(key);
        else changedGlobalRows.push(key);
      }
      continue;
    }
    if (before === undefined) {
      addOnlyWrites.push(key);
    } else if (!isDeepStrictEqual(before.value, target.value)) {
      preservedDrift.push(key);
    }
  }

  return {
    globalDeletes,
    globalWrites,
    addOnlyWrites: addOnlyWrites.sort(),
    changedGlobalRows: changedGlobalRows.sort(),
    removedGlobalRows,
    preservedDrift: preservedDrift.sort(),
  };
}

/** Apply the synchronization policy to an in-memory settings map. */
function applyConfigurationSync(
  current: readonly CurrentConfigRow[],
  generated: readonly ConfigEntry[],
): Settings {
  const settings: Settings = new Map(current.map((row) => [row.key, row.value]));
  for (const entry of generated) {
    if (/^global-settings\.module_sets\.[^.]+\.[^.]+\.module_id$/.test(entry.key) && settings.has(entry.key)) continue;
    if (entry.key.startsWith("global-settings.") || !settings.has(entry.key)) {
      settings.set(entry.key, entry.value);
    }
  }
  return settings;
}


/** Encode one local value into the typed scan shape consumed by ClusterConfig. */
function valueAttribute(value: unknown): Record<string, unknown> {
  if (value === null || value === undefined) return { NULL: true };
  if (typeof value === "string") return { S: value };
  if (typeof value === "number") return { N: String(value) };
  if (typeof value === "boolean") return { BOOL: value };
  if (Array.isArray(value)) return { L: value.map(valueAttribute) };
  if (isRecord(value)) {
    return {
      M: Object.fromEntries(
        Object.entries(value).map(([key, item]) => [key, valueAttribute(item)]),
      ),
    };
  }
  throw new TypeError(`unsupported setting type: ${typeof value}`);
}

/** Serialize the current in-memory settings for one isolated synthesis. */
function scanJson(settings: Settings): string {
  return JSON.stringify({
    Items: [...settings.entries()].map(([key, value]) => ({
      key: { S: key },
      value: valueAttribute(value),
      version: { N: "1" },
      source: { S: "rehearsal" },
    })),
  });
}

/** Produce one stable, shape-valid local identifier for an unresolved stack output. */
function materializedString(
  key: string,
  context: MaterializationContext,
  index: number,
): string {
  const slug = key.replace(/[^A-Za-z0-9-]/g, "-").slice(0, 48);
  const suffix = String(index + 1).padStart(4, "0");
  if (key.includes("target_group_arns")) {
    return `arn:${context.partition}:elasticloadbalancing:${context.region}:${context.accountId}:targetgroup/${slug}/${suffix}`;
  }
  if (key.endsWith("service_arn")) {
    return `arn:${context.partition}:ecs:${context.region}:${context.accountId}:service/rehearsal/${slug}-${suffix}`;
  }
  if (
    key.includes("iam_role_arn") ||
    key.endsWith(".role_arn") ||
    key.includes(".iam.roles.")
  ) {
    return `arn:${context.partition}:iam::${context.accountId}:role/${slug}-${suffix}`;
  }
  if (key.includes("policy_arn") || key.includes(".iam.policies.")) {
    return `arn:${context.partition}:iam::${context.accountId}:policy/${slug}-${suffix}`;
  }
  if (key.includes("secret_arn")) {
    return `arn:${context.partition}:secretsmanager:${context.region}:${context.accountId}:secret:${slug}-ABCDEF`;
  }
  // A backup vault ARN is parsed by the consuming construct, which wants a colon-separated
  // resource segment rather than the slash form the generic branch below emits.
  if (key.includes("backup_vault")) {
    return `arn:${context.partition}:backup:${context.region}:${context.accountId}:backup-vault:${slug}`;
  }
  // Both spellings occur: `cluster.backups.backup_vault.arn` alongside `...vault_arn`. Returning a
  // non-ARN for either one fails validation inside a consuming stack instead of here.
  if (key.endsWith("_arn") || key.endsWith(".arn")) {
    return `arn:${context.partition}:service:${context.region}:${context.accountId}:resource/${slug}-${suffix}`;
  }
  if (key.endsWith("_url")) return "https://example.invalid/rehearsal";
  if (key.endsWith("_dns_name") || key.endsWith("_hostname")) return "rehearsal.example.invalid";
  if (key.endsWith("_ip")) return "192.0.2.10";
  if (key.endsWith(".vpc_id")) return "vpc-12345678";
  if (key.includes(".security_groups.") || key.endsWith("_security_group_id")) return "sg-12345678";
  if (key.includes(".subnets")) return "subnet-12345678";
  if (key.endsWith("_id")) return `${slug}-${suffix}`;
  return `${slug}-${suffix}`;
}

/** Replace unresolved template objects while retaining scalar output values. */
function materializeSetting(
  value: unknown,
  key: string,
  context: MaterializationContext,
  index = 0,
): unknown {
  if (Array.isArray(value)) {
    return value.map((item, itemIndex) => materializeSetting(item, key, context, itemIndex));
  }
  if (isRecord(value)) return materializedString(key, context, index);
  return value;
}

/** Return every resource in a synthesized or captured template. */
function templateResources(template: Template): Record<string, Record<string, unknown>> {
  if (!isRecord(template.Resources)) return {};
  const resources: Record<string, Record<string, unknown>> = {};
  for (const [logicalId, resource] of Object.entries(template.Resources)) {
    if (isRecord(resource)) resources[logicalId] = resource;
  }
  return resources;
}

/** Extract the one or more settings maps written by a stack. */
function stackSettings(template: Template): Array<{
  moduleId: string;
  settings: Record<string, unknown>;
}> {
  const result: Array<{ moduleId: string; settings: Record<string, unknown> }> = [];
  for (const resource of Object.values(templateResources(template))) {
    if (resource.Type !== "Custom::ClusterSettings" || !isRecord(resource.Properties)) continue;
    const moduleId = resource.Properties.module_id;
    const settings = resource.Properties.settings;
    if (typeof moduleId === "string" && isRecord(settings)) {
      result.push({ moduleId, settings });
    }
  }
  return result;
}

/** Publish synthesized settings into the local state boundary for later stacks. */
function publishStackSettings(
  template: Template,
  settings: Settings,
  context: MaterializationContext,
): { keys: string[]; relative: Record<string, unknown> } {
  const keys: string[] = [];
  const relative: Record<string, unknown> = {};
  for (const output of stackSettings(template)) {
    for (const [key, value] of Object.entries(output.settings)) {
      const fullKey = `${output.moduleId}.${key}`;
      relative[key] = value;
      settings.set(fullKey, materializeSetting(value, fullKey, context));
      keys.push(fullKey);
    }
  }
  return { keys: keys.sort(), relative };
}

/** Trace every resolved setting key read during one synthesis. */
function traceReads(): { reads: Set<string>; restore(): void } {
  const reads = new Set<string>();
  const original = ClusterConfig.prototype.getRealKey;
  ClusterConfig.prototype.getRealKey = function (key: string, moduleId?: string): string {
    const resolved = original.call(this, key, moduleId);
    reads.add(resolved);
    return resolved;
  };
  return {
    reads,
    restore(): void {
      ClusterConfig.prototype.getRealKey = original;
    },
  };
}

/** Run synthesis with local context and an empty code-asset directory. */
async function withRehearsalEnvironment<T>(
  root: string,
  contextFile: string,
  action: () => Promise<T>,
): Promise<T> {
  const priorCwd = process.cwd();
  const priorOutDir = process.env.CDK_OUTDIR;
  const priorNag = process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
  const originalAssetPath = IdeaCodeAsset.prototype.assetPath;
  const assetDir = join(root, "empty-code-asset");
  mkdirSync(assetDir, { recursive: true });
  copyFileSync(join(PKG, "cdk.json"), join(root, "cdk.json"));
  copyFileSync(contextFile, join(root, "cdk.context.json"));
  IdeaCodeAsset.prototype.assetPath = function (): string {
    return assetDir;
  };
  process.chdir(root);
  process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = "false";

  try {
    return await action();
  } finally {
    IdeaCodeAsset.prototype.assetPath = originalAssetPath;
    process.chdir(priorCwd);
    if (priorOutDir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = priorOutDir;
    if (priorNag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
    else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = priorNag;
  }
}

/** Convert a generated module to the table row shape used by deploymentOrder. */
function generatedModuleInfo(module: ModuleEntry, current?: ModuleInfo): ModuleInfo {
  return {
    ...current,
    module_id: module.id,
    name: module.name,
    type: module.type,
    status: current?.status ?? "not-deployed",
    stack_name: current?.stack_name ?? null,
    version: current?.version ?? null,
  };
}

/** Find stack setting reads whose target writer occurs later in the same order. */
export function findLaterWriterReads(
  order: readonly string[],
  stacks: readonly Pick<StackSynthesis, "moduleId" | "reads" | "publishedSettings">[],
): LaterWriterRead[] {
  const position = new Map(order.map((moduleId, index) => [moduleId, index]));
  const writer = new Map<string, string>();
  for (const stack of stacks) {
    for (const key of stack.publishedSettings) writer.set(key, stack.moduleId);
  }

  const findings: LaterWriterRead[] = [];
  for (const stack of stacks) {
    const consumerPosition = position.get(stack.moduleId);
    if (consumerPosition === undefined) continue;
    for (const key of stack.reads) {
      const writerId = writer.get(key);
      const writerPosition = writerId === undefined ? undefined : position.get(writerId);
      if (
        writerId !== undefined &&
        writerPosition !== undefined &&
        writerPosition > consumerPosition
      ) {
        findings.push({ consumer: stack.moduleId, key, writer: writerId });
      }
    }
  }
  return findings.sort(
    (left, right) =>
      left.consumer.localeCompare(right.consumer) ||
      left.writer.localeCompare(right.writer) ||
      left.key.localeCompare(right.key),
  );
}

/**
 * Find common target groups whose explicit Name changes. CloudFormation treats
 * this property as replacement-only.
 */
export function findImmutableNameReplacements(
  before: Template,
  after: Template,
): ReplacementFinding[] {
  const oldResources = templateResources(before);
  const newResources = templateResources(after);
  const findings: ReplacementFinding[] = [];
  for (const [logicalId, oldResource] of Object.entries(oldResources)) {
    const newResource = newResources[logicalId];
    if (
      newResource === undefined ||
      oldResource.Type !== "AWS::ElasticLoadBalancingV2::TargetGroup" ||
      newResource.Type !== oldResource.Type ||
      !isRecord(oldResource.Properties) ||
      !isRecord(newResource.Properties) ||
      oldResource.Properties.Name === undefined ||
      newResource.Properties.Name === undefined ||
      isDeepStrictEqual(oldResource.Properties.Name, newResource.Properties.Name)
    ) {
      continue;
    }
    findings.push({
      logicalId,
      resourceType: "AWS::ElasticLoadBalancingV2::TargetGroup",
      property: "Name",
    });
  }
  return findings.sort((left, right) => left.logicalId.localeCompare(right.logicalId));
}

/** Count the DNS records one template keeps, and how many of those it retains on removal. */
function retainedDnsRecords(template: Template | undefined): { kept: number; retain: number } {
  if (template === undefined) return { kept: 0, retain: 0 };
  let kept = 0;
  let retain = 0;
  for (const resource of Object.values(templateResources(template))) {
    if (resource.Type !== "AWS::Route53::RecordSet") continue;
    kept += 1;
    if (resource.DeletionPolicy === "Retain") retain += 1;
  }
  return { kept, retain };
}

/** Find logical IDs of one resource type. */
function resourceIds(template: Template, resourceType: string): string[] {
  return Object.entries(templateResources(template))
    .filter(([, resource]) => resource.Type === resourceType)
    .map(([logicalId]) => logicalId)
    .sort();
}

/** Count retained endpoint resources whose properties differ in the target. */
function changedEndpointCount(before: Template, after: Template): number {
  const oldResources = templateResources(before);
  const newResources = templateResources(after);
  let count = 0;
  for (const [logicalId, oldResource] of Object.entries(oldResources)) {
    const newResource = newResources[logicalId];
    if (
      newResource !== undefined &&
      typeof oldResource.Type === "string" &&
      oldResource.Type.startsWith("Custom::") &&
      oldResource.Type.includes("Endpoint") &&
      newResource.Type === oldResource.Type &&
      !isDeepStrictEqual(oldResource.Properties, newResource.Properties)
    ) {
      count += 1;
    }
  }
  return count;
}

/** Return true when a nested template value contains one exact string fragment. */
function containsString(value: unknown, fragment: string): boolean {
  if (typeof value === "string") return value.includes(fragment);
  if (Array.isArray(value)) return value.some((item) => containsString(item, fragment));
  if (isRecord(value)) {
    return Object.values(value).some((item) => containsString(item, fragment));
  }
  return false;
}

/** Count retained roles that gain the container-task service principal. */
function rolesGainingContainerTrust(before: Template, after: Template): number {
  const oldResources = templateResources(before);
  const newResources = templateResources(after);
  let count = 0;
  for (const [logicalId, oldResource] of Object.entries(oldResources)) {
    const newResource = newResources[logicalId];
    if (
      oldResource.Type === "AWS::IAM::Role" &&
      newResource?.Type === "AWS::IAM::Role" &&
      !containsString(oldResource.Properties, "ecs-tasks.") &&
      containsString(newResource.Properties, "ecs-tasks.")
    ) {
      count += 1;
    }
  }
  return count;
}

/** Return a compact error message without assuming the thrown value is an Error. */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Build the explicit list of facts that fixture replay cannot establish. */
function realClusterProofs(): RealClusterProof[] {
  return [
    {
      stages: [0],
      proof: "account trunking, quotas, permissions, image availability, and current stack status",
    },
    {
      stages: [1, 21],
      proof: "durable operation lock, journal writes, snapshot retention, and resume reconciliation",
    },
    {
      stages: [2, 3, 20],
      proof: "maintenance, queue, scheduling, and job inventory read-back",
    },
    {
      stages: [4, 11, 12, 18],
      proof: "scheduler stop, archive read-back, shared-state restore, ownership, and PBS health",
    },
    {
      stages: [5],
      proof: "desktop software-stack rows, active sessions, and search-index reconciliation",
    },
    {
      stages: [7, 8, 10, 13, 14, 15, 16, 17, 18, 19],
      proof: "change-set classification, stack completion, target health, routing, DNS, host deletion, and application paths",
    },
    {
      stages: [19],
      proof: "termination-protection restoration and live service replacement with an isolated running job",
    },
  ];
}

/** Create every ordered stage with evidence from the local model or its explicit boundary. */
function buildStages(input: {
  fixtureCount: number;
  currentRows: number;
  currentModules: number;
  sync: ConfigurationSyncPlan;
  providerSynths: number;
  stackSynths: number;
  stackTotal: number;
  stackDeletes: number;
  stackWrites: number;
  findings: readonly RehearsalFinding[];
}): RehearsalStage[] {
  const blockedStages = new Set(
    input.findings
      .filter((finding) => finding.severity === "BLOCKING")
      .map((finding) => finding.stage),
  );
  const stage = (
    number: number,
    status: StageStatus,
    ...evidence: string[]
  ): RehearsalStage => ({
    number,
    name: STAGE_NAMES[number] as StageName,
    status: blockedStages.has(number) ? "BLOCKED" : status,
    evidence,
  });

  return [
    stage(0, "MODELED", `${input.fixtureCount} required fixture inputs loaded without a live reader`),
    stage(
      1,
      "MODELED",
      `${input.currentRows} typed settings rows, ${input.currentModules} module rows, and deployed templates captured`,
    ),
    stage(2, "REAL_CLUSTER_ONLY", "no admission-control snapshot is present in the fixture"),
    stage(3, "REAL_CLUSTER_ONLY", "no administrator, PBS, or compute-node inventory is present"),
    stage(4, "REAL_CLUSTER_ONLY", "no stopped scheduler archive or read-back checksum is present"),
    stage(5, "REAL_CLUSTER_ONLY", "no desktop software-stack or active-session table is present"),
    stage(
      6,
      "MODELED",
      `${input.sync.globalDeletes.length} global deletes, ${input.sync.globalWrites.length} global writes, ${input.sync.addOnlyWrites.length} add-only writes`,
    ),
    stage(7, "SYNTHESIZED", `${input.providerSynths} provider target templates synthesized`),
    stage(8, "TEMPLATE_ONLY", "standard scheduler target checked against the required retain-only boundary"),
    stage(9, "MODELED", "container flag, stable scheduler name, target image, and scheduler desired count boundaries modeled"),
    stage(10, "TEMPLATE_ONLY", "container stack template synthesized, runtime health requires a real cluster"),
    stage(11, "REAL_CLUSTER_ONLY", "fixture has no scheduler archive or target shared-directory state"),
    stage(12, "TEMPLATE_ONLY", "scheduler stack target template carries the scheduler service, task and PBS health require a real cluster"),
    stage(13, "TEMPLATE_ONLY", "cluster-manager routed template compared against the deployed and the hostless targets"),
    stage(14, "TEMPLATE_ONLY", "cluster-manager final hostless target template synthesized"),
    stage(15, "TEMPLATE_ONLY", "desktop routed template compared against the deployed and the hostless targets"),
    stage(16, "TEMPLATE_ONLY", "desktop final hostless target template synthesized"),
    stage(17, "TEMPLATE_ONLY", "scheduler routed template compared against the deployed and the hostless targets"),
    stage(18, "TEMPLATE_ONLY", "scheduler final hostless target template synthesized"),
    stage(
      19,
      input.stackSynths === input.stackTotal ? "SYNTHESIZED" : "BLOCKED",
      `${input.stackSynths}/${input.stackTotal} target stacks synthesized, ${input.stackWrites} stack settings writes and ${input.stackDeletes} deletes modeled`,
    ),
    stage(20, "REAL_CLUSTER_ONLY", "queue, scheduling, maintenance, and API read-back are not captured"),
    stage(21, "REAL_CLUSTER_ONLY", "final fingerprints, durable completion, and lock release require service observations"),
  ];
}

/**
 * Generate the target, replay the single-run data boundaries, and synthesize
 * every target stack. Local shims are reported and never written to fixtures.
 */
export async function rehearseUpgrade(options: RehearseOptions = {}): Promise<UpgradeRehearsal> {
  const fixtureDir = resolve(options.fixtureDir ?? DEFAULT_FIXTURE_DIR);
  const liveDir = resolve(options.liveDir ?? DEFAULT_LIVE_DIR);
  const paths = fixturePaths(fixtureDir);
  const root = options.workDir ?? mkdtempSync(join(tmpdir(), "ideactl-upgrade-rehearsal-"));
  const removeRoot = options.workDir === undefined && options.keepWorkDir !== true;
  const configDir = join(root, "target-config");
  const configFile = join(root, "cluster-settings.json");

  try {
    const values = loadValuesFile(paths.values);
    const clusterName = typeof values.cluster_name === "string" ? values.cluster_name : "";
    const accountId = typeof values.aws_account_id === "string" ? values.aws_account_id : "";
    const region = typeof values.aws_region === "string" ? values.aws_region : "";
    const partition =
      typeof values.aws_partition === "string" && values.aws_partition !== ""
        ? values.aws_partition
        : "aws";
    if (clusterName === "" || accountId === "" || region === "") {
      throw new Error("values fixture must provide cluster_name, aws_account_id, and aws_region");
    }

    const current = currentConfigRows(readTypedScan(paths.clusterSettings));
    const currentModuleRows = moduleRows(readTypedScan(paths.modules));
    // CONFIGURATION_STAGED regenerates from the cluster's own values file with container routing
    // still disabled, which is what that step says it stages. ECS_CONFIGURATION_ACTIVE is the
    // boundary that turns it on, so the container module's own settings arrive there, not here.
    const stagedValues: UserValues = { ...values, enable_ecs: false };
    const targetValues: UserValues = { ...values, enable_ecs: true };
    const stagedDir = join(root, "staged-config");
    generateConfigFromTemplates(stagedValues, stagedDir);
    const staged = convertConfigToKeyValuePairs(stagedDir);
    const generatedModuleRows = generateConfigFromTemplates(targetValues, configDir);
    const generated = convertConfigToKeyValuePairs(configDir);
    const productionHasEcs = generatedModuleRows.some(
      (module) => module.id === "ecs" && module.name === "ecs",
    );
    const sync = planConfigurationSync(current, staged);
    const settings = applyConfigurationSync(current, staged);
    const materialization: MaterializationContext = {
      accountId,
      partition,
      region,
    };

    const currentById = new Map(currentModuleRows.map((module) => [module.module_id, module]));
    const targetStackModules = generatedModuleRows.filter((module) => module.type !== "config");
    const targetModuleInfos = targetStackModules.map((module) =>
      generatedModuleInfo(module, currentById.get(module.id)),
    );
    const generatedIds = targetStackModules.map((module) => module.id);
    const actualUpgradeOrder = deploymentOrder(
      currentModuleRows,
      currentModuleRows.map((module) => module.module_id),
      true,
    );
    const rehearsalOrder = deploymentOrder(targetModuleInfos, generatedIds, true);
    const targetById = new Map(targetStackModules.map((module) => [module.id, module]));
    const missingModuleRows = targetStackModules
      .filter((module) => !currentById.has(module.id))
      .map((module) => module.id)
      .sort();

    const findings: RehearsalFinding[] = [];
    if (!productionHasEcs) {
      findings.push({
        stage: 6,
        code: "GENERATOR_MODULE_GAP",
        severity: "BLOCKING",
        summary: "enable_ecs=true does not add the ECS module through the production generator",
        evidence: [
          "the production template path generated no ecs module",
          `generated modules were ${generatedModuleRows.map((module) => module.id).join(", ")}`,
          "without the module the deployment order has no container stack to place",
        ],
      });
    }
    const generatedByKey = new Map(generated.map((entry) => [entry.key, entry.value]));
    if (productionHasEcs && generatedByKey.get(CONTAINER_MODULE_SET_KEY) !== "ecs") {
      findings.push({
        stage: 6,
        code: "GENERATOR_MODULE_GAP",
        severity: "BLOCKING",
        summary: "the generated settings carry no module-set entry for the container module",
        evidence: [
          `${CONTAINER_MODULE_SET_KEY} is absent from the generated settings`,
          "every key read as ecs.* resolves its module id through that entry",
          "the production global settings template does not render the entry",
        ],
      });
    }
    if (missingModuleRows.length > 0) {
      findings.push({
        stage: 6,
        code: "MODULE_REGISTRATION",
        severity: "OBSERVED",
        summary: `${missingModuleRows.join(", ")} is generated, absent from the captured table, and registered by the sync`,
        evidence: [
          `generated module list contains ${missingModuleRows.join(", ")}`,
          "the captured module table does not",
          "the upgrade path registers every generated module add-only before syncing settings",
          "the deployment order below places the registered module rather than skipping it",
        ],
      });
    }

    // One shared-capacity row per consumer, each published by the container stack and each
    // required by that stack when it builds its own service.
    const earlyReads = [
      ["cluster-manager", "ecs.cluster_name"],
      ["scheduler", "ecs.host_security_group_id"],
      ["vdc", "ecs.namespace_id"],
    ] as const;
    for (const [consumer, key] of earlyReads) {
      if (settings.get("ecs.enabled") === true && !settings.has(key)) {
        findings.push({
          stage: 6,
          code: "EARLY_CONTAINER_ACTIVATION",
          severity: "BLOCKING",
          summary: `${consumer} selects its hostless branch before ${key} exists`,
          evidence: [
            "generated synchronization sets ecs.enabled=true",
            `${key} is absent after configuration synchronization`,
            `${consumer} requires the key when its target template is synthesized`,
          ],
        });
      }
    }

    // No template renders the stable-name row, and both real clusters are missing it. It is left
    // exactly as captured: the container branch derives the record-changing permission from the
    // flag and writes the row itself, so an absent row is a supported starting state and the
    // rehearsal has to exercise it rather than paper over it.
    const stableNameBefore = settings.get(STABLE_NAME_KEY);

    // ECS_CONFIGURATION_ACTIVE: the activation boundary applies the container module's generated
    // settings add-only, then turns the flag on and names the release image.
    for (const entry of generated) {
      if (!settings.has(entry.key)) settings.set(entry.key, entry.value);
    }
    settings.set(CONTAINER_FLAG_KEY, true);
    settings.set("ecs.image", TARGET_IMAGE);
    findings.push({
      stage: 9,
      code: "STABLE_NAME_STARTING_STATE",
      severity: "OBSERVED",
      summary: `the container flag is turned on with ${STABLE_NAME_KEY}=${String(stableNameBefore)}`,
      evidence: [
        `the captured settings carry ${STABLE_NAME_KEY}=${String(stableNameBefore)}`,
        "no configuration template renders the row, so regeneration never adds it",
        "the scheduler target below is synthesized from this state, not from a supplied row",
      ],
    });
    const generatedSchedulerDesired = generatedByKey.get(SCHEDULER_DESIRED_KEY);
    // The captured settings will not carry the seed, because the seed is written during the run
    // rather than generated. So an absent row is only a gap when nothing in the run writes it.
    if (settings.get(SCHEDULER_DESIRED_KEY) !== 0 && !executorWrittenKeys().has(SCHEDULER_DESIRED_KEY)) {
      findings.push({
        stage: 10,
        code: "SCHEDULER_DESIRED_STAGE_GAP",
        severity: "BLOCKING",
        summary: "the container scheduler starts during ECS staging, before its state is restored",
        evidence: [
          `the generated ${SCHEDULER_DESIRED_KEY} is ${String(generatedSchedulerDesired)}`,
          "ECS_STAGED runs before PBS_STATE_SEEDED, so a task started here has no restored state",
          "the activation sync is add-only, so a row seeded to 0 at CONFIGURATION_STAGED would survive it",
          "no step in the migration path seeds that row",
        ],
      });
      settings.set(SCHEDULER_DESIRED_KEY, 0);
    }
    const syntheses: InternalStackSynthesis[] = [];
    let ecsStagedTemplate: Template | undefined;
    let ecsReadyTemplate: Template | undefined;
    let ecsReadyProblem: string | undefined;
    let retainStageTemplate: Template | undefined;
    let retainStageProblem: string | undefined;
    const routedTemplates = new Map<string, Template>();
    const routedProblems = new Map<string, string>();

    /**
     * The routed step of one application module: the same activation state plus
     * `ecs.retain_existing_hosts`, which routes the endpoints to the container services and keeps
     * the hosts. Synthesized from the snapshot the standard target was synthesized from, and its
     * settings are never published, so the shared state boundary is untouched.
     */
    const synthesizeRoutedStage = async (
      module: ModuleEntry,
      snapshot: Settings,
    ): Promise<void> => {
      const routedSettings: Settings = new Map(snapshot);
      routedSettings.set(RETAIN_HOSTS_KEY, true);
      const routedConfigFile = join(root, `cluster-settings.routed.${module.id}.json`);
      writeFileSync(routedConfigFile, scanJson(routedSettings));
      process.env.CDK_OUTDIR = join(root, `cdk.out.routed.${module.id}`);
      try {
        const app = await buildApp({
          clusterName,
          awsRegion: region,
          moduleId: module.id,
          moduleName: module.name,
          deploymentId: DEPLOYMENT_ID,
          terminationProtection: true,
          configFile: routedConfigFile,
          synthReadsFile: paths.synthReads,
        });
        routedTemplates.set(
          module.id,
          app.synth().getStackArtifact(`${clusterName}-${module.id}`).template as Template,
        );
      } catch (error) {
        routedProblems.set(module.id, errorMessage(error));
      }
    };

    /**
     * SCHEDULER_DNS_RETAINED: the scheduler deploys on its own with container routing still off, so
     * the record gains a retain policy before any later template stops managing it. It runs at the
     * boundary it occupies, after the provider stacks have published, not from the staged snapshot.
     */
    const synthesizeRetainStage = async (): Promise<void> => {
      const schedulerModule = targetById.get("scheduler");
      if (schedulerModule === undefined) return;
      const retainStageSettings: Settings = new Map(settings);
      retainStageSettings.set(CONTAINER_FLAG_KEY, false);
      retainStageSettings.set(RETAIN_DNS_KEY, true);
      const retainConfigFile = join(root, "cluster-settings.retain-stage.json");
      writeFileSync(retainConfigFile, scanJson(retainStageSettings));
      process.env.CDK_OUTDIR = join(root, "cdk.out.scheduler-dns-retained");
      try {
        const app = await buildApp({
          clusterName,
          awsRegion: region,
          moduleId: schedulerModule.id,
          moduleName: schedulerModule.name,
          deploymentId: DEPLOYMENT_ID,
          terminationProtection: true,
          configFile: retainConfigFile,
          synthReadsFile: paths.synthReads,
        });
        retainStageTemplate = app
          .synth()
          .getStackArtifact(`${clusterName}-${schedulerModule.id}`).template as Template;
      } catch (error) {
        retainStageProblem = errorMessage(error);
      }
    };

    await withRehearsalEnvironment(root, paths.cdkContext, async () => {
      for (const moduleId of rehearsalOrder) {
        if (moduleId === "ecs") await synthesizeRetainStage();
        const module = targetById.get(moduleId);
        if (module === undefined) {
          syntheses.push({
            moduleId,
            moduleName: "<missing>",
            status: "BLOCKED",
            reads: [],
            publishedSettings: [],
            resourceCount: 0,
            problem: "deployment order references an unknown generated module",
          });
          continue;
        }

        const snapshot: Settings = new Map(settings);
        writeFileSync(configFile, scanJson(settings));
        process.env.CDK_OUTDIR = join(root, `cdk.out.${module.id}`);
        const trace = traceReads();
        try {
          const app = await buildApp({
            clusterName,
            awsRegion: region,
            moduleId: module.id,
            moduleName: module.name,
            deploymentId: DEPLOYMENT_ID,
            terminationProtection: true,
            configFile,
            synthReadsFile: paths.synthReads,
          });
          const assembly = app.synth();
          const template = assembly.getStackArtifact(`${clusterName}-${module.id}`).template as Template;
          const published = publishStackSettings(template, settings, materialization);
          syntheses.push({
            moduleId: module.id,
            moduleName: module.name,
            status: "SYNTHESIZED",
            reads: [...trace.reads].sort(),
            publishedSettings: published.keys,
            resourceCount: Object.keys(templateResources(template)).length,
            template,
            settings: published.relative,
          });
          if (module.id === "ecs") {
            ecsStagedTemplate = template;
            settings.set("ecs.tasks.scheduler.desired", 1);
          }
        } catch (error) {
          syntheses.push({
            moduleId: module.id,
            moduleName: module.name,
            status: "BLOCKED",
            reads: [...trace.reads].sort(),
            publishedSettings: [],
            resourceCount: 0,
            problem: errorMessage(error),
          });
        } finally {
          trace.restore();
        }
        if ((ROUTED_MODULES as readonly string[]).includes(module.id)) {
          await synthesizeRoutedStage(module, snapshot);
        }
      }

      // The scheduler task belongs to the scheduler stack, so the scheduler-ready shape is that
      // stack re-synthesized once the desired count has been raised off zero.
      const schedulerModule = targetById.get("scheduler");
      if (ecsStagedTemplate !== undefined && schedulerModule !== undefined) {
        writeFileSync(configFile, scanJson(settings));
        process.env.CDK_OUTDIR = join(root, "cdk.out.ecs-scheduler-ready");
        try {
          const app = await buildApp({
            clusterName,
            awsRegion: region,
            moduleId: schedulerModule.id,
            moduleName: "scheduler",
            deploymentId: DEPLOYMENT_ID,
            terminationProtection: true,
            configFile,
            synthReadsFile: paths.synthReads,
          });
          ecsReadyTemplate = app
            .synth()
            .getStackArtifact(`${clusterName}-${schedulerModule.id}`).template as Template;
        } catch (error) {
          ecsReadyProblem = errorMessage(error);
        }
      }
    });

    for (const stack of syntheses.filter((entry) => entry.status === "BLOCKED")) {
      findings.push({
        stage: 19,
        code: "SYNTHESIS_BLOCKED",
        severity: "BLOCKING",
        summary: `${stack.moduleId} target synthesis was blocked`,
        evidence: [stack.problem ?? "no diagnostic was returned"],
      });
    }

    for (const dependency of findLaterWriterReads(rehearsalOrder, syntheses)) {
      // A container-stack read of a later writer was the deploy-order cycle: the stack it read
      // from read the target groups the container stack published, so neither could go first. The
      // module stacks own their own services and target groups now, so the container stack reads
      // nothing an application stack writes and this should no longer fire for it.
      const cycle = dependency.consumer === "ecs";
      findings.push({
        stage: 10,
        code: "LATER_WRITER_READ",
        severity: "OBSERVED",
        summary: `${dependency.consumer} reads ${dependency.key} before ${dependency.writer} rewrites it`,
        evidence: [
          `${dependency.consumer} precedes ${dependency.writer} in the serialized target order`,
          `${dependency.consumer} target synthesis read ${dependency.key}`,
          `${dependency.writer} target Custom::ClusterSettings writes ${dependency.key}`,
          ...(cycle
            ? [`${dependency.writer} reads the target groups ${dependency.consumer} publishes, so the order cannot be swapped; the container stack owning the resource is the resolution`]
            : []),
        ],
      });
    }
    if (ecsReadyProblem !== undefined) {
      findings.push({
        stage: 12,
        code: "SCHEDULER_READY_SYNTHESIS_BLOCKED",
        severity: "BLOCKING",
        summary: "the scheduler-ready scheduler template did not synthesize",
        evidence: [ecsReadyProblem],
      });
    } else if (ecsReadyTemplate !== undefined && resourceIds(ecsReadyTemplate, "AWS::ECS::Service").length !== 1) {
      findings.push({
        stage: 12,
        code: "SCHEDULER_READY_SERVICE_MISSING",
        severity: "BLOCKING",
        summary: "the scheduler-ready template carries no container scheduler service",
        evidence: [
          `${SCHEDULER_DESIRED_KEY} is ${String(settings.get(SCHEDULER_DESIRED_KEY))} at this stage`,
          "the scheduler stack owns the scheduler task, so its target template must contain one service",
        ],
      });
    }

    const templatesByModule = new Map(
      syntheses.flatMap((stack) =>
        stack.template === undefined ? [] : [[stack.moduleId, stack.template] as const],
      ),
    );
    const oldTemplates = new Map<string, Template>();
    for (const module of currentModuleRows.filter((entry) => entry.type !== "config")) {
      const file = join(liveDir, `${clusterName}-${module.module_id}.json`);
      if (!existsSync(file)) {
        throw new Error(`missing deployed template: ${file}`);
      }
      oldTemplates.set(module.module_id, readJsonObject(file));
    }

    for (const [moduleId, oldTemplate] of oldTemplates) {
      const targetTemplate = templatesByModule.get(moduleId);
      if (targetTemplate === undefined) continue;
      for (const replacement of findImmutableNameReplacements(oldTemplate, targetTemplate)) {
        findings.push({
          stage: 7,
          code: "REPLACEMENT",
          severity: "OBSERVED",
          summary: `${moduleId}.${replacement.logicalId} is replaced instead of updated`,
          evidence: [
            `${replacement.resourceType}.${replacement.property} differs between deployed and target templates`,
            "the Name property is replacement-only",
            "the logical resource id is unchanged",
          ],
        });
      }
    }

    // Each of these rows changes from an autoscaling-group identity to a container-service
    // identity. The service is created by the stack that publishes the row, so the row is the
    // service name spelled out rather than a reference, and the container cluster it names is the
    // one row the target synthesis reads to build it.
    const semanticContracts = [
      { moduleId: "cluster-manager", setting: "asg_arn", targetRead: "ecs.cluster_name", stage: 14 },
      { moduleId: "vdc", setting: "controller.asg_arn", targetRead: "ecs.cluster_name", stage: 16 },
      { moduleId: "vdc", setting: "dcv_broker.asg_arn", targetRead: "ecs.cluster_name", stage: 16 },
      {
        moduleId: "vdc",
        setting: "dcv_connection_gateway.asg_arn",
        targetRead: "ecs.cluster_name",
        stage: 16,
      },
    ] as const;
    for (const contract of semanticContracts) {
      const oldSettings = oldTemplates.get(contract.moduleId);
      const targetStack = syntheses.find((entry) => entry.moduleId === contract.moduleId);
      const prior = oldSettings === undefined ? undefined : stackSettings(oldSettings)[0]?.settings;
      if (
        prior?.[contract.setting] !== undefined &&
        targetStack?.settings?.[contract.setting] !== undefined &&
        targetStack.reads.includes(contract.targetRead)
      ) {
        findings.push({
          stage: contract.stage,
          code: "SEMANTIC_CHANGE",
          severity: "OBSERVED",
          summary: `${contract.moduleId}.${contract.setting} changes from an autoscaling-group identity to a container-service identity`,
          evidence: [
            `deployed Custom::ClusterSettings contains ${contract.setting}`,
            `target Custom::ClusterSettings retains ${contract.setting}`,
            `target synthesis obtains its value from ${contract.targetRead}`,
          ],
        });
      }
    }

    const schedulerOld = oldTemplates.get("scheduler");
    const schedulerTarget = templatesByModule.get("scheduler");
    if (
      schedulerOld !== undefined &&
      schedulerTarget !== undefined &&
      resourceIds(schedulerOld, "AWS::Route53::RecordSet").length > 0 &&
      resourceIds(schedulerTarget, "AWS::Route53::RecordSet").length === 0
    ) {
      // The target stops managing the record, so CloudFormation deletes the name unless an earlier
      // deploy already gave it a retain policy. The stack can express that shape; the question the
      // rehearsal answers is whether the intermediate template actually carries the policy.
      const retained = retainedDnsRecords(retainStageTemplate);
      const evidence = [
        "the deployed scheduler template contains an AWS::Route53::RecordSet",
        "the target scheduler template contains no AWS::Route53::RecordSet",
        retainStageProblem === undefined
          ? `the retain-only synthesis with ${RETAIN_DNS_KEY}=true kept ${retained.kept} record(s), ${retained.retain} with DeletionPolicy Retain`
          : `the retain-only synthesis failed: ${retainStageProblem}`,
        `no step in the migration path sets ${RETAIN_DNS_KEY}`,
      ];
      if (retained.retain === 0 || retained.retain !== retained.kept) {
        findings.push({
          stage: 8,
          code: "DNS_RETAIN_STAGE_MISSING",
          severity: "BLOCKING",
          summary: "the retain-only scheduler template does not retain the DNS record",
          evidence,
        });
      } else if (!executorWrittenKeys().has(RETAIN_DNS_KEY)) {
        findings.push({
          stage: 8,
          code: "DNS_RETAIN_STAGE_UNDRIVEN",
          severity: "BLOCKING",
          summary: `the retain-only scheduler shape works, but nothing sets ${RETAIN_DNS_KEY}`,
          evidence,
        });
      }
    }

    const routeChecks = [
      {
        moduleId: "cluster-manager",
        stage: 13,
        hostType: "AWS::AutoScaling::AutoScalingGroup",
      },
      {
        moduleId: "vdc",
        stage: 15,
        hostType: "AWS::AutoScaling::AutoScalingGroup",
      },
      {
        moduleId: "scheduler",
        stage: 17,
        hostType: "AWS::EC2::Instance",
      },
    ] as const;
    for (const check of routeChecks) {
      const before = oldTemplates.get(check.moduleId);
      const after = templatesByModule.get(check.moduleId);
      if (before === undefined || after === undefined) continue;
      const oldHosts = resourceIds(before, check.hostType);
      const targetHosts = new Set(resourceIds(after, check.hostType));
      const removedHosts = oldHosts.filter((logicalId) => !targetHosts.has(logicalId));
      const endpointChanges = changedEndpointCount(before, after);
      if (removedHosts.length === 0 || endpointChanges === 0) continue;

      // The routed step needs a template that moves the endpoints and keeps the hosts, so the new
      // target is proved before the old one is gone. `ecs.retain_existing_hosts` expresses it; this
      // measures whether the template it produces actually holds both halves.
      const routed = routedTemplates.get(check.moduleId);
      const routedProblem = routedProblems.get(check.moduleId);
      const routedHosts =
        routed === undefined ? new Set<string>() : new Set(resourceIds(routed, check.hostType));
      const lostInRouted = oldHosts.filter((logicalId) => !routedHosts.has(logicalId));
      const routedEndpointChanges =
        routed === undefined ? 0 : changedEndpointCount(before, routed);
      const evidence = [
        `${removedHosts.length} ${check.hostType} resource(s) are removed by the combined target: ${removedHosts.join(", ")}`,
        `${endpointChanges} retained endpoint custom resource(s) change properties`,
        routedProblem === undefined
          ? `the routed synthesis with ${RETAIN_HOSTS_KEY}=true kept ${routedHosts.size} of ${oldHosts.length} ${check.hostType} resource(s) and changed ${routedEndpointChanges} endpoint custom resource(s)`
          : `the routed synthesis with ${RETAIN_HOSTS_KEY}=true failed: ${routedProblem}`,
        `stage ${check.stage + 1} removes the hosts the routed template keeps`,
        `no step in the migration path sets ${RETAIN_HOSTS_KEY}`,
      ];
      if (routedProblem !== undefined || lostInRouted.length > 0 || routedEndpointChanges === 0) {
        findings.push({
          stage: check.stage,
          code: "ROUTE_STAGE_MISSING",
          severity: "BLOCKING",
          summary: `${check.moduleId} has no template that routes the endpoints and keeps the hosts`,
          evidence,
        });
      } else if (!executorWrittenKeys().has(RETAIN_HOSTS_KEY)) {
        findings.push({
          stage: check.stage,
          code: "ROUTE_STAGE_UNDRIVEN",
          severity: "BLOCKING",
          summary: `the routed ${check.moduleId} shape works, but nothing sets ${RETAIN_HOSTS_KEY}`,
          evidence,
        });
      }
    }

    const ecsPosition = rehearsalOrder.indexOf("ecs");
    for (const moduleId of ["cluster-manager", "scheduler", "vdc"]) {
      const before = oldTemplates.get(moduleId);
      const after = templatesByModule.get(moduleId);
      const modulePosition = rehearsalOrder.indexOf(moduleId);
      if (
        before === undefined ||
        after === undefined ||
        ecsPosition < 0 ||
        modulePosition <= ecsPosition
      ) {
        continue;
      }
      const changedRoles = rolesGainingContainerTrust(before, after);
      if (changedRoles === 0) continue;
      findings.push({
        stage: 10,
        code: "ROLE_TRUST_ORDER",
        severity: "BLOCKING",
        summary: `${moduleId} adds container-task trust only after the container services are staged`,
        evidence: [
          `${changedRoles} retained IAM role(s) gain the ecs-tasks service principal`,
          `ecs precedes ${moduleId} in the serialized target order`,
          "the deployed role template does not contain that service principal",
        ],
      });
    }

    const stackPlans: StackSettingsPlan[] = syntheses.flatMap((stack) => {
      if (stack.settings === undefined) return [];
      const oldTemplate = oldTemplates.get(stack.moduleId);
      const previous = oldTemplate === undefined ? {} : stackSettings(oldTemplate)[0]?.settings ?? {};
      return [{
        moduleId: stack.moduleId,
        selected: true,
        previous,
        target: stack.settings,
      }];
    });
    const drift = compareUpgradeDrift({
      current,
      generated,
      stacks: stackPlans,
      replaceGlobalSettings: true,
      syncFullConfiguration: true,
    });
    const stackWrites = drift.findings.filter((finding) => finding.action === "STACK_OVERWRITE").length;
    const stackDeletes = drift.findings.filter((finding) => finding.action === "STACK_DELETE").length;
    const providerIds = new Set([
      "cluster",
      "analytics",
      "identity-provider",
      "directoryservice",
      "metrics",
      "shared-storage",
    ]);
    const providerSynths = syntheses.filter(
      (stack) => providerIds.has(stack.moduleId) && stack.status === "SYNTHESIZED",
    ).length;
    const successfulSynths = syntheses.filter((stack) => stack.status === "SYNTHESIZED").length;
    const stages = buildStages({
      fixtureCount: 5 + oldTemplates.size,
      currentRows: current.length,
      currentModules: currentModuleRows.length,
      sync,
      providerSynths,
      stackSynths: successfulSynths,
      stackTotal: rehearsalOrder.length,
      stackDeletes,
      stackWrites,
      findings,
    });

    return {
      clusterName,
      currentRows: current.length,
      generatedRows: generated.length,
      currentModules: currentModuleRows.map((module) => module.module_id),
      generatedModules: generatedModuleRows.map((module) => module.id),
      actualUpgradeOrder,
      rehearsalOrder,
      sync,
      stages,
      stacks: syntheses.map(({ template: _template, settings: _settings, ...stack }) => stack),
      findings: findings.sort(
        (left, right) =>
          left.stage - right.stage ||
          left.code.localeCompare(right.code) ||
          left.summary.localeCompare(right.summary),
      ),
      realClusterProofs: realClusterProofs(),
      localShims: [
        `The activation boundary is modeled here: ${CONTAINER_FLAG_KEY}, ${STABLE_NAME_KEY} and the image are set by this harness, not by a migration step.`,
        // These two lines used to end "no migration step sets it", which stopped being true once the
        // executor began writing both rows. A shim declaration that describes the harness is useful;
        // one that asserts something about the code under test goes stale silently, so each now
        // reports what it found rather than a fixed claim.
        `The retain-only scheduler stage is modeled here by setting ${RETAIN_DNS_KEY}; ${
          executorWrittenKeys().has(RETAIN_DNS_KEY)
            ? "a migration step sets the same row on a real run"
            : "no migration step sets it"
        }.`,
        `The routed stage of each application module is modeled here by setting ${RETAIN_HOSTS_KEY}; ${
          executorWrittenKeys().has(RETAIN_HOSTS_KEY)
            ? "a migration step sets the same row on a real run"
            : "no migration step sets it"
        }.`,
        "The staged ECS synthesis uses scheduler desired count 0, then a second synthesis raises it to 1.",
        "The release image is a local example.invalid reference because no release digest is captured.",
        "Stack output tokens become shape-valid local identifiers after each synthesis.",
        "Code assets use an empty local directory because package contents are outside this rehearsal.",
      ],
    };
  } finally {
    if (removeRoot) rmSync(root, { recursive: true, force: true });
  }
}

/** Render every stage, every finding with evidence, and every live-only proof. */
export function renderUpgradeRehearsal(report: UpgradeRehearsal): string {
  const findingsByStage = new Map<number, RehearsalFinding[]>();
  for (const finding of report.findings) {
    const values = findingsByStage.get(finding.stage) ?? [];
    values.push(finding);
    findingsByStage.set(finding.stage, values);
  }

  const lines = [
    `UPGRADE_REHEARSAL cluster=${report.clusterName}`,
    `CURRENT settings=${report.currentRows} modules=${report.currentModules.length}`,
    `TARGET settings=${report.generatedRows} modules=${report.generatedModules.length}`,
    `ACTUAL_UPGRADE_ORDER ${report.actualUpgradeOrder.join(" -> ")}`,
    `REHEARSAL_ORDER ${report.rehearsalOrder.join(" -> ")}`,
    `SYNC global_delete=${report.sync.globalDeletes.length} global_write=${report.sync.globalWrites.length} add_only_write=${report.sync.addOnlyWrites.length} changed=${report.sync.changedGlobalRows.length} removed=${report.sync.removedGlobalRows.length} preserved_drift=${report.sync.preservedDrift.length}`,
  ];

  for (const stage of report.stages) {
    lines.push(`STAGE ${String(stage.number).padStart(2, "0")} ${stage.name} ${stage.status}`);
    for (const evidence of stage.evidence) lines.push(`  EVIDENCE ${evidence}`);
    for (const finding of findingsByStage.get(stage.number) ?? []) {
      lines.push(`  FINDING ${finding.severity} ${finding.code} ${finding.summary}`);
      for (const evidence of finding.evidence) lines.push(`    EVIDENCE ${evidence}`);
    }
  }

  for (const stack of report.stacks) {
    const suffix = stack.problem === undefined ? "" : ` problem=${stack.problem}`;
    lines.push(
      `STACK ${stack.moduleId} ${stack.status} resources=${stack.resourceCount} reads=${stack.reads.length} writes=${stack.publishedSettings.length}${suffix}`,
    );
  }
  for (const proof of report.realClusterProofs) {
    lines.push(`REAL_CLUSTER_ONLY stages=${proof.stages.join(",")} ${proof.proof}`);
  }
  for (const shim of report.localShims) lines.push(`LOCAL_SHIM ${shim}`);

  const blockers = report.findings.filter((finding) => finding.severity === "BLOCKING").length;
  lines.push(
    `RESULT blocking_findings=${blockers} observed_findings=${report.findings.length - blockers}`,
  );
  return lines.join("\n");
}

/** Parse the small command-line surface for one-command local execution. */
function parseArgs(argv: string[]): RehearseOptions {
  let fixtureDir: string | undefined;
  let liveDir: string | undefined;
  let keepWorkDir = false;
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--fixtures" || value === "--live-dir") {
      const next = argv[index + 1];
      if (next === undefined || next.startsWith("--")) {
        throw new Error(`${value} requires a path`);
      }
      if (value === "--fixtures") fixtureDir = resolve(next);
      else liveDir = resolve(next);
      index += 1;
    } else if (value === "--keep") {
      keepWorkDir = true;
    } else {
      throw new Error(`unknown option: ${value}`);
    }
  }
  return { fixtureDir, liveDir, keepWorkDir };
}

/** Run the rehearsal and use a nonzero status when the modeled run is blocked. */
async function main(argv: string[]): Promise<number> {
  try {
    const report = await rehearseUpgrade(parseArgs(argv));
    console.log(renderUpgradeRehearsal(report));
    return report.findings.some((finding) => finding.severity === "BLOCKING") ? 1 : 0;
  } catch (error) {
    console.error(`upgrade rehearsal failed: ${errorMessage(error)}`);
    return 2;
  }
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
