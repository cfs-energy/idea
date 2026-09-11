/**
 * Rehearse the first deployment from a values file without contacting AWS.
 *
 * Each stack receives the settings generated from the values file plus the
 * settings published by earlier stacks. CloudFormation references are replaced
 * with shape-valid local values after synthesis because their concrete values
 * do not exist until deployment completes.
 */

import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { IdeaCodeAsset } from "../../src/cdk/code-asset.ts";
import { buildApp } from "../../src/cdk/app.ts";
import { deploymentOrder } from "../../src/cli/deployment-helper.ts";
import { ConfigKeyNotFound, ClusterConfig, type ModuleInfo } from "../../src/config/cluster-config.ts";
import { convertConfigToKeyValuePairs, generateConfig, type ConfigEntry, type ModuleEntry } from "../../src/config/generator.ts";
import { loadValuesFile, type UserValues } from "../../src/config/values.ts";
import { CALLER_IDENTITY_KEY } from "../../src/cdk/synth-reads.ts";

const DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000000";

type Settings = Map<string, unknown>;
type SettingOrigins = Map<string, string>;

export interface StackRehearsal {
  moduleId: string;
  moduleName: string;
  status: "SYNTHESIZED" | "BLOCKED";
  reads: string[];
  priorSettingSources: string[];
  publishedSettings: string[];
  freshResourceChecks: string[];
  problem?: string;
}

export interface DayZeroRehearsal {
  clusterName: string;
  containerInstallRequested: boolean;
  generatedStateProblems: string[];
  deploymentOrder: string[];
  stacks: StackRehearsal[];
  problems: string[];
  orderingProblems: string[];
  dependencyProblems: string[];
  offlineReadBlocks: string[];
  localShims: string[];
}

export interface RehearseOptions {
  valuesFile: string;
  workDir?: string;
  keepWorkDir?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function valueAttribute(value: unknown): Record<string, unknown> {
  if (value === null || value === undefined) return { NULL: true };
  if (typeof value === "string") return { S: value };
  if (typeof value === "number") return { N: String(value) };
  if (typeof value === "boolean") return { BOOL: value };
  if (Array.isArray(value)) return { L: value.map(valueAttribute) };
  if (isRecord(value)) {
    return {
      M: Object.fromEntries(Object.entries(value).map(([key, item]) => [key, valueAttribute(item)])),
    };
  }
  throw new Error(`unsupported generated setting type: ${typeof value}`);
}

function scanJson(settings: Settings): string {
  return JSON.stringify({
    Items: [...settings.entries()].map(([key, value]) => ({
      key: { S: key },
      value: valueAttribute(value),
      version: { N: "1" },
      source: { S: "day-zero" },
    })),
  });
}

function settingKey(moduleId: string, key: string): string {
  return `${moduleId}.${key}`;
}

function settingValue(key: string, accountId: string): string {
  const name = `day-zero-${key.replace(/[^A-Za-z0-9-]/g, "-")}`;
  if (key.includes(".iam.roles.")) return `arn:aws:iam::${accountId}:role/${name}`;
  if (key.includes(".iam.policies.")) return `arn:aws:iam::${accountId}:policy/${name}`;
  if (key.endsWith("_lambda_arn")) return `arn:aws:lambda:us-east-2:${accountId}:function:${name}`;
  if (key.endsWith("_sqs_queue_arn") || key.endsWith("_topic_arn")) return `arn:aws:sqs:us-east-2:${accountId}:${name}`;
  // A backup vault ARN is parsed by the consuming construct, which wants a colon-separated resource
  // segment. Both `..._vault_arn` and `..._vault.arn` spellings occur in the settings.
  if (key.includes("backup_vault")) return `arn:aws:backup:us-east-2:${accountId}:backup-vault:${name}`;
  // Plural spellings carry a list of ARNs, and a consumer that parses one element rejects a bare
  // placeholder. `target_group_arns` is the case that matters: the construct reading it pulls the
  // region and account out of each element, so the singular branch below has to cover the plural too.
  if (key.endsWith("_arns") || key.endsWith(".arns")) {
    return `arn:aws:elasticloadbalancing:us-east-2:${accountId}:targetgroup/${name}/0123456789abcdef`;
  }
  if (key.endsWith("_arn") || key.endsWith(".arn")) {
    return `arn:aws:service:us-east-2:${accountId}:resource/${name}`;
  }
  if (key.endsWith("_url")) return "https://example.invalid/day-zero";
  if (key.endsWith("_dns_name") || key.endsWith("_hostname")) return "day-zero.example.invalid";
  if (key.endsWith("_ip")) return "192.0.2.10";
  if (key.endsWith(".vpc_id")) return "vpc-12345678";
  if (key.includes(".network.security_groups.")) return "sg-12345678";
  if (key.includes(".subnets")) return "subnet-12345678";
  if (key.endsWith("_id")) return "day-zero-id";
  return "day-zero-value";
}

function materializeSetting(value: unknown, key: string, accountId: string): unknown {
  if (Array.isArray(value)) return value.map((item) => materializeSetting(item, key, accountId));
  if (isRecord(value)) return settingValue(key, accountId);
  return value;
}

function initialSettings(entries: ConfigEntry[]): Settings {
  return new Map(entries.map((entry) => [entry.key, entry.value]));
}

/**
 * Verifies that the production generator represented the operator's container
 * install request. A detached template or an aggregate test map cannot satisfy
 * these checks.
 */
export function validateGeneratedContainerState(
  values: UserValues,
  modules: ModuleEntry[],
  settings: ReadonlyMap<string, unknown>,
): string[] {
  const enabled = values["enable_ecs"];
  if (enabled !== true) {
    return [
      typeof enabled === "boolean" || enabled === undefined || enabled === null
        ? "values file must set enable_ecs: true"
        : `enable_ecs must be a Boolean, got ${typeof enabled}`,
    ];
  }

  const problems: string[] = [];
  const ecsModules = modules.filter((module) => module.name === "ecs" || module.id === "ecs");
  if (
    ecsModules.length !== 1 ||
    ecsModules[0]?.name !== "ecs" ||
    ecsModules[0]?.id !== "ecs" ||
    ecsModules[0]?.type !== "stack"
  ) {
    problems.push("production generator must emit one ecs stack module");
  }
  if (settings.get("global-settings.module_sets.default.ecs.module_id") !== "ecs") {
    problems.push("generated settings must map the ecs module name to module id ecs");
  }
  if (settings.get("ecs.enabled") !== true) {
    problems.push("generated settings must contain ecs.enabled=true");
  }
  return problems;
}

function toModuleInfo(module: ModuleEntry): ModuleInfo {
  return {
    module_id: module.id,
    name: module.name,
    type: module.type,
    status: "not-deployed",
  };
}

function resourceSettings(template: unknown): Array<{ moduleId: string; settings: Record<string, unknown> }> {
  if (!isRecord(template) || !isRecord(template.Resources)) return [];
  const result: Array<{ moduleId: string; settings: Record<string, unknown> }> = [];
  for (const resource of Object.values(template.Resources)) {
    if (!isRecord(resource) || resource.Type !== "Custom::ClusterSettings" || !isRecord(resource.Properties)) continue;
    const moduleId = resource.Properties.module_id;
    const settings = resource.Properties.settings;
    if (typeof moduleId === "string" && isRecord(settings)) result.push({ moduleId, settings });
  }
  return result;
}

function templateHasType(template: unknown, resourceType: string): boolean {
  if (!isRecord(template) || !isRecord(template.Resources)) return false;
  return Object.values(template.Resources).some((resource) => isRecord(resource) && resource.Type === resourceType);
}

function freshResourceChecks(moduleName: string, template: unknown, settings: Settings): string[] {
  if (moduleName !== "cluster") return [];
  const checks: string[] = [];
  const useExistingVpc = settings.get("cluster.network.use_existing_vpc");
  if (useExistingVpc !== true && templateHasType(template, "AWS::EC2::VPC")) {
    checks.push("new VPC emitted when use_existing_vpc=false");
  } else {
    checks.push("PROBLEM: new VPC was not emitted for use_existing_vpc=false");
  }
  return checks;
}

function appendPublishedSettings(
  template: unknown,
  settings: Settings,
  origins: SettingOrigins,
  accountId: string,
  moduleName: string,
): string[] {
  const published: string[] = [];
  for (const resource of resourceSettings(template)) {
    for (const [key, value] of Object.entries(resource.settings)) {
      const fullKey = settingKey(resource.moduleId, key);
      settings.set(fullKey, materializeSetting(value, fullKey, accountId));
      origins.set(fullKey, moduleName);
      published.push(fullKey);
    }
  }
  return published.sort();
}

function dependencies(reads: Set<string>, origins: SettingOrigins): string[] {
  return [...new Set([...reads].map((key) => origins.get(key)).filter((source): source is string => source !== undefined && source !== "values"))].sort();
}

/**
 * Checks that every setting source observed during synthesis is deployed before
 * its consumer. The setting map is populated only after each successful stack,
 * so this catches a future change that makes the reported order inconsistent
 * with the local deployment boundary.
 */
export function validateDeploymentDependencies(report: Pick<DayZeroRehearsal, "deploymentOrder" | "stacks">): string[] {
  const orderIndex = new Map(report.deploymentOrder.map((moduleId, index) => [moduleId, index]));
  const moduleIdByName = new Map(report.stacks.map((stack) => [stack.moduleName, stack.moduleId]));
  const problems: string[] = [];

  for (const stack of report.stacks) {
    const consumerIndex = orderIndex.get(stack.moduleId);
    if (consumerIndex === undefined) {
      problems.push(`${stack.moduleId}: synthesized stack is absent from the deployment order`);
      continue;
    }
    for (const sourceName of stack.priorSettingSources) {
      const sourceId = moduleIdByName.get(sourceName);
      const sourceIndex = sourceId === undefined ? undefined : orderIndex.get(sourceId);
      if (sourceIndex === undefined) {
        problems.push(`${stack.moduleId}: setting source ${sourceName} is absent from the deployment order`);
      } else if (sourceIndex >= consumerIndex) {
        problems.push(`${stack.moduleId}: setting source ${sourceName} is not deployed before its consumer`);
      }
    }
  }
  return problems;
}

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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function classifyProblem(error: unknown): string {
  if (error instanceof ConfigKeyNotFound) return `missing required setting: ${error.message}`;
  const message = errorMessage(error);
  if (message.includes("SynthReadMiss:")) return `live read required: ${message}`;
  if (message.includes("Missing context key")) return `CDK context lookup required: ${message}`;
  return `synthesis failed: ${message}`;
}

function isOfflineReadBlock(problem: string): boolean {
  return problem.startsWith("live read required:") || problem.startsWith("CDK context lookup required:");
}

function writeReplay(file: string, accountId: string): void {
  writeFileSync(
    file,
    JSON.stringify({
      [CALLER_IDENTITY_KEY]: {
        account: accountId,
        arn: `arn:aws:iam::${accountId}:root`,
      },
    }),
  );
}

function withRehearsalEnvironment<T>(workDir: string, action: () => Promise<T>): Promise<T> {
  const priorCwd = process.cwd();
  const priorOutDir = process.env.CDK_OUTDIR;
  const priorNag = process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
  const originalAssetPath = IdeaCodeAsset.prototype.assetPath;
  const assetDir = join(workDir, "empty-lambda-asset");
  mkdirSync(assetDir, { recursive: true });
  IdeaCodeAsset.prototype.assetPath = function (): string {
    return assetDir;
  };
  process.chdir(workDir);
  process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = "false";
  return action().finally(() => {
    IdeaCodeAsset.prototype.assetPath = originalAssetPath;
    process.chdir(priorCwd);
    if (priorOutDir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = priorOutDir;
    if (priorNag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
    else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = priorNag;
  });
}

/**
 * Generates configuration and synthesizes every generated module in deployment order.
 * It never invokes the live configuration or AWS read implementations.
 */
export async function rehearseDayZero(options: RehearseOptions): Promise<DayZeroRehearsal> {
  const root = options.workDir ?? mkdtempSync(join(tmpdir(), "ideactl-day-zero-"));
  const removeRoot = options.workDir === undefined && options.keepWorkDir !== true;
  const configDir = join(root, "config");
  const configFile = join(root, "cluster-settings.json");
  const synthReadsFile = join(root, "synth-reads.json");
  const values = loadValuesFile(options.valuesFile);
  const clusterName = typeof values.cluster_name === "string" ? values.cluster_name : "";
  const accountId = typeof values.aws_account_id === "string" ? values.aws_account_id : "";
  const awsRegion = typeof values.aws_region === "string" ? values.aws_region : "";
  if (clusterName === "" || accountId === "" || awsRegion === "") {
    throw new Error("values file must provide cluster_name, aws_account_id, and aws_region");
  }

  const modules = generateConfig(options.valuesFile, configDir);
  const settings = initialSettings(convertConfigToKeyValuePairs(configDir));
  const containerInstallRequested = values["enable_ecs"] === true;
  const generatedStateProblems = validateGeneratedContainerState(values, modules, settings);
  const origins: SettingOrigins = new Map([...settings.keys()].map((key) => [key, "values"]));
  const orderedModules = deploymentOrder(modules.map(toModuleInfo), modules.map((module) => module.id), false);
  const modulesById = new Map(modules.map((module) => [module.id, module]));
  const stacks: StackRehearsal[] = [];
  const problems = generatedStateProblems.map((problem) => `generated state: ${problem}`);
  const orderingProblems = [...problems];
  const offlineReadBlocks: string[] = [];

  try {
    if (generatedStateProblems.length > 0) {
      return {
        clusterName,
        containerInstallRequested,
        generatedStateProblems,
        deploymentOrder: orderedModules,
        stacks,
        problems,
        orderingProblems,
        dependencyProblems: [],
        offlineReadBlocks,
        localShims: [],
      };
    }

    await withRehearsalEnvironment(root, async () => {
      writeFileSync(join(root, "cdk.context.json"), "{}\n");
      writeReplay(synthReadsFile, accountId);
      for (const moduleId of orderedModules) {
        const module = modulesById.get(moduleId);
        if (module === undefined) {
          const problem = `deployment order selected missing module id: ${moduleId}`;
          stacks.push({
            moduleId,
            moduleName: "<missing>",
            status: "BLOCKED",
            reads: [],
            priorSettingSources: [],
            publishedSettings: [],
            freshResourceChecks: [],
            problem,
          });
          problems.push(problem);
          orderingProblems.push(problem);
          continue;
        }

        writeFileSync(configFile, scanJson(settings));
        process.env.CDK_OUTDIR = join(root, `cdk.out.${module.id}`);
        const trace = traceReads();
        const originsBefore = new Map(origins);
        try {
          const app = await buildApp({
            clusterName,
            awsRegion,
            moduleId: module.id,
            moduleName: module.name,
            deploymentId: DEPLOYMENT_ID,
            terminationProtection: true,
            configFile,
            synthReadsFile,
          });
          const assembly = app.synth();
          const template = assembly.getStackArtifact(`${clusterName}-${module.id}`).template;
          const checks = freshResourceChecks(module.name, template, settings);
          const publishedSettings = appendPublishedSettings(template, settings, origins, accountId, module.name);
          const stackProblems = checks.filter((check) => check.startsWith("PROBLEM:"));
          stacks.push({
            moduleId,
            moduleName: module.name,
            status: "SYNTHESIZED",
            reads: [...trace.reads].sort(),
            priorSettingSources: dependencies(trace.reads, originsBefore),
            publishedSettings,
            freshResourceChecks: checks,
          });
          const namedProblems = stackProblems.map((problem) => `${module.name}: ${problem}`);
          problems.push(...namedProblems);
          orderingProblems.push(...namedProblems);
        } catch (error) {
          const problem = classifyProblem(error);
          const namedProblem = `${module.name}: ${problem}`;
          stacks.push({
            moduleId,
            moduleName: module.name,
            status: "BLOCKED",
            reads: [...trace.reads].sort(),
            priorSettingSources: dependencies(trace.reads, originsBefore),
            publishedSettings: [],
            freshResourceChecks: [],
            problem,
          });
          problems.push(namedProblem);
          if (isOfflineReadBlock(problem)) offlineReadBlocks.push(namedProblem);
          else orderingProblems.push(namedProblem);
        } finally {
          trace.restore();
        }
      }
    });
    const dependencyProblems = validateDeploymentDependencies({
      deploymentOrder: orderedModules,
      stacks,
    });
    orderingProblems.push(...dependencyProblems);
    return {
      clusterName,
      containerInstallRequested,
      generatedStateProblems,
      deploymentOrder: orderedModules,
      stacks,
      problems,
      orderingProblems,
      dependencyProblems,
      offlineReadBlocks,
      localShims: ["Lambda code assets use a temporary empty directory. Asset packaging is outside this rehearsal."],
    };
  } finally {
    if (removeRoot) rmSync(root, { recursive: true, force: true });
  }
}

export function renderRehearsal(report: DayZeroRehearsal): string {
  const lines = [
    `DAY_ZERO cluster=${report.clusterName}`,
    `INSTALL_INTENT containers=${report.containerInstallRequested}`,
    `GENERATED_STATE ${report.generatedStateProblems.length === 0 ? "valid" : `${report.generatedStateProblems.length} problem(s) found`}`,
    ...report.generatedStateProblems.map((problem) => `GENERATED_STATE_PROBLEM ${problem}`),
    `ORDER ${report.deploymentOrder.join(" -> ")}`,
  ];
  for (const stack of report.stacks) {
    const needs = stack.reads.length === 0 ? "none" : stack.reads.join(",");
    const from = stack.priorSettingSources.length === 0 ? "values" : stack.priorSettingSources.join(",");
    lines.push(`STACK ${stack.moduleId} ${stack.status} needs=${needs} prior=${from}`);
    if (stack.publishedSettings.length > 0) lines.push(`PUBLISHED ${stack.moduleId} ${stack.publishedSettings.join(",")}`);
    for (const check of stack.freshResourceChecks) lines.push(`CREATE_PATH ${stack.moduleId} ${check}`);
    if (stack.problem !== undefined) lines.push(`PROBLEM ${stack.moduleId} ${stack.problem}`);
  }
  if (report.generatedStateProblems.length > 0) {
    lines.push("DEPENDENCY_CHECK not run because generated state is invalid");
  } else if (report.dependencyProblems.length === 0) {
    lines.push("DEPENDENCY_CHECK all observed stack setting sources precede their consumers");
  } else {
    lines.push(`DEPENDENCY_CHECK ${report.dependencyProblems.length} problem(s) found`);
    for (const problem of report.dependencyProblems) lines.push(`DEPENDENCY_PROBLEM ${problem}`);
  }
  if (report.orderingProblems.length === 0) lines.push("RESULT no ordering or missing-value problems found");
  else {
    lines.push(`RESULT ${report.orderingProblems.length} ordering or missing-value problem(s) found`);
    for (const problem of report.orderingProblems) lines.push(`PROBLEM ${problem}`);
  }
  for (const problem of report.offlineReadBlocks) lines.push(`OFFLINE_BLOCK ${problem}`);
  for (const shim of report.localShims) lines.push(`LOCAL_SHIM ${shim}`);
  return lines.join("\n");
}

function parseArgs(argv: string[]): RehearseOptions {
  let valuesFile: string | undefined;
  let keepWorkDir = false;
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--values-file") {
      const next = argv[index + 1];
      if (next === undefined || next.startsWith("--")) throw new Error("--values-file requires a path");
      valuesFile = resolve(next);
      index += 1;
    } else if (value === "--keep") {
      keepWorkDir = true;
    } else {
      throw new Error(`unknown option: ${value}`);
    }
  }
  if (valuesFile === undefined) throw new Error("--values-file requires a path");
  return { valuesFile, keepWorkDir };
}

async function main(argv: string[]): Promise<number> {
  try {
    const report = await rehearseDayZero(parseArgs(argv));
    console.log(renderRehearsal(report));
    return report.orderingProblems.length === 0 ? 0 : 1;
  } catch (error) {
    console.error(`day-zero rehearsal failed before synthesis: ${errorMessage(error)}`);
    return 2;
  }
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
