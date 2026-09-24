/**
 * Upgrade an existing cluster by applying the administrator's ordered upgrade
 * sequence. Each external operation is injected so callers can replay it.
 */

import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { isDeepStrictEqual } from "node:util";

import type { Command } from "commander";

import { ideaVersion } from "../../version.ts";
import { ClusterConfigError, GeneralException, type ModuleInfo } from "../../config/cluster-config.ts";
import {
  convertConfigToKeyValuePairs,
  generateConfigFromTemplates,
  readModulesFromFiles,
  type ConfigEntry,
} from "../../config/generator.ts";
import { loadRegionAmiConfig, resolveRegionAmi, type RegionsConfig } from "../../config/region-ami.ts";
import {
  compareUpgradeDrift,
  renderUpgradeDrift,
  type CurrentConfigRow,
  type StackSettingsPlan,
  type UpgradeDriftInput,
  type UpgradeDriftReport,
} from "../../config/upgrade-drift.ts";
import { loadValuesFile } from "../../config/values.ts";
import {
  buildTree,
  toYaml,
  type ConfigDriftPreviewDeps,
  type ConfigUpgradePreviewOptions,
} from "./config.ts";
import { asBoolFlag } from "./deploy.ts";
import { awsClientOptions, type AwsClientOptions } from "../aws-client-options.ts";
import { DeploymentHelper } from "../deployment-helper.ts";
import { compareIdeaRelease, upgradeFloorRefusal } from "../upgrade-floor.ts";
import { ExitWithCode, VALUES_FILE_S3_KEY, valuesFilePath, type Deps } from "../cdk-invoker.ts";

export const EOL_BASE_OS: Readonly<Record<string, string>> = {
  amazonlinux2: "amazonlinux2023",
};

export const UPGRADE_BASE_OS: readonly string[] = [
  "amazonlinux2023",
  "rhel8",
  "rhel9",
  "rhel10",
  "rocky8",
  "rocky9",
  "rocky10",
];

const ECS_MODULE = "ecs";

/** One effective ECS account setting returned by the pre-flight reader. */
export interface EcsAccountSetting {
  name: string;
  value: string;
}

/** Read-only ECS account settings required before an ECS deployment. */
export interface EcsAccountSettingsApi {
  listAccountSettings(input: {
    awsProfile?: string;
    awsRegion: string;
    effectiveSettings: true;
    name: "awsvpcTrunking";
  }): Promise<EcsAccountSetting[]>;
}

/** The injected dependencies used by the ECS trunking pre-flight. */
export interface EcsTrunkingPreflightDeps {
  out?: (line: string) => void;
  accountId(): Promise<string>;
  ecsAccountSettings?: EcsAccountSettingsApi;
  err(line: string): void;
}

/** The operator-selected values needed to print the remediation command. */
export interface EcsTrunkingPreflightOptions {
  awsProfile?: string;
  awsRegion: string;
}

/**
 * Builds the one-time operator command for the account-wide ECS prerequisite.
 */
export function awsvpcTrunkingCommand(options: EcsTrunkingPreflightOptions): string {
  return [
    "aws",
    "ecs",
    "put-account-setting-default",
    "--name",
    "awsvpcTrunking",
    "--value",
    "enabled",
    "--region",
    options.awsRegion,
    ...(options.awsProfile === undefined ? [] : ["--profile", options.awsProfile]),
  ].join(" ");
}

/** Client options for the live upgrade readers, with the operator's profile bound. */
export async function upgradeLiveClientOptions(
  awsRegion: string,
  awsProfile?: string,
): Promise<AwsClientOptions> {
  return awsClientOptions(awsRegion, awsProfile);
}

/**
 * Refuses an ECS deployment until the account's effective task ENI trunking
 * setting is enabled. This check reads account state only.
 */
export async function checkAwsvpcTrunking(
  deps: EcsTrunkingPreflightDeps,
  options: EcsTrunkingPreflightOptions,
): Promise<void> {
  if (deps.ecsAccountSettings === undefined) {
    throw new GeneralException("ECS account-settings reader is required for the awsvpcTrunking pre-flight");
  }
  const [account, settings] = await Promise.all([
    deps.accountId(),
    deps.ecsAccountSettings.listAccountSettings({
      // Read the account the rest of the run will deploy into, which a named profile selects.
      ...(options.awsProfile === undefined ? {} : { awsProfile: options.awsProfile }),
      awsRegion: options.awsRegion,
      effectiveSettings: true,
      name: "awsvpcTrunking",
    }),
  ]);
  const enabled = settings.some((setting) => setting.name === "awsvpcTrunking" && setting.value === "enabled");
  if (enabled) {
    deps.out?.(`ECS awsvpcTrunking is enabled for account ${account} in ${options.awsRegion}.`);
    return;
  }

  deps.err(
    `ECS awsvpcTrunking is not enabled for account ${account} in ${options.awsRegion}. Without it, an m7g.large host of the planned size fits only two tasks, so placement silently starves.`,
  );
  deps.err("Run this once for the account, then repeat the deploy:");
  deps.err(awsvpcTrunkingCommand(options));
  throw new ExitWithCode(1);
}

const MODULE_HOST_INSTANCE_TYPE = "m7i.large";
const MODULE_HOST_INSTANCE_TYPE_OLD = "m6i.large";
const OPENSEARCH_DATA_NODE_INSTANCE_TYPE = "m7g.large.search";
const OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD = "m5.large.search";
const COMPUTE_IMAGE_PREFIX = ["idea", "compute", "node", ""].join("-");

const AMI_UPDATE_KEYS: Readonly<Record<string, ReadonlyArray<readonly [string, string]>>> = {
  "bastion-host": [["base_os", "instance_ami"]],
  "cluster-manager": [["ec2.autoscaling.base_os", "ec2.autoscaling.instance_ami"]],
  directoryservice: [["base_os", "instance_ami"]],
  scheduler: [
    ["base_os", "instance_ami"],
    ["compute_node_os", "compute_node_ami"],
  ],
  "virtual-desktop-controller": [
    ["controller.autoscaling.base_os", "controller.autoscaling.instance_ami"],
    ["dcv_broker.autoscaling.base_os", "dcv_broker.autoscaling.instance_ami"],
    ["dcv_connection_gateway.autoscaling.base_os", "dcv_connection_gateway.autoscaling.instance_ami"],
  ],
};

const HOST_INSTANCE_TYPE_KEYS: Readonly<Record<string, readonly string[]>> = {
  "bastion-host": ["instance_type"],
  "cluster-manager": ["ec2.autoscaling.instance_type"],
  directoryservice: ["instance_type"],
  scheduler: ["instance_type"],
  "virtual-desktop-controller": [
    "controller.autoscaling.instance_type",
    "dcv_broker.autoscaling.instance_type",
    "dcv_connection_gateway.autoscaling.instance_type",
  ],
};

export interface InstanceImage {
  ImageId?: string;
  Name?: string;
  CreationDate?: string;
}

export interface UpgradeEc2Api {
  describeImages(input: { awsRegion: string; imageIds: string[] }): Promise<InstanceImage[]>;
  describeInstanceTypeOfferings(input: { awsRegion: string; instanceType: string }): Promise<string[]>;
  describeInstanceAttribute(input: { awsRegion: string; instanceId: string }): Promise<boolean>;
  modifyInstanceAttribute(input: { awsRegion: string; instanceId: string; protected: boolean }): Promise<void>;
  createTags(input: { awsRegion: string; instanceId: string; value: string }): Promise<void>;
  deleteTags(input: { awsRegion: string; instanceId: string }): Promise<void>;
  describeLiveInstances(input: { awsRegion: string; instanceIds: string[]; tagKey?: string }): Promise<string[]>;
}

export interface UpgradeCloudFormationApi {
  listStackResources(input: {
    awsRegion: string;
    stackName: string;
    nextToken?: string;
  }): Promise<{ instanceIds: string[]; nextToken?: string }>;
}

export interface UpgradeOpenSearchApi {
  describeDomain(input: { awsRegion: string; domainName?: string }): Promise<{ engineVersion?: string }>;
  listInstanceTypeDetails(input: { awsRegion: string; engineVersion?: string }): Promise<string[]>;
}

export interface EolSoftwareStackApi {
  setEnabled(input: { awsRegion: string; tableName: string; baseOs: string; stackId: string; enabled: boolean }): Promise<void>;
  delete(input: { awsRegion: string; tableName: string; baseOs: string; stackId: string }): Promise<void>;
}

export interface UpgradeDeploymentOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  terminationProtection: boolean;
  deploymentId?: string;
  forceBuildBootstrap: boolean;
  rollback: boolean;
  optimizeDeployment: boolean;
  moduleSet: string;
  allModules: boolean;
  moduleIds?: readonly string[];
  allowReplacement?: readonly string[];
}

/** Jobs the batch server on a scheduler host still holds, by state. */
export interface SchedulerJobInventory {
  queued: number;
  running: number;
  other: number;
}

/** Reads the host scheduler's job inventory, over SSM in the live tool. */
export interface SchedulerJobsApi {
  activeJobs(input: { awsRegion: string; instanceId: string }): Promise<SchedulerJobInventory>;
}

export interface ContainerHost {
  arn: string;
  instanceId: string;
  status: string;
  runningTasks: number;
  registeredAt?: string;
}

/** The container host group behind a cluster's capacity provider. */
export interface ContainerHostsApi {
  hostGroup(input: { awsRegion: string; capacityProvider: string }): Promise<{ name: string; minSize: number; desiredCapacity: number; instanceIds: string[] }>;
  containerInstances(input: { awsRegion: string; cluster: string }): Promise<ContainerHost[]>;
  drain(input: { awsRegion: string; cluster: string; arn: string }): Promise<void>;
  /** Put a host back in service after a drain that could not empty it. */
  activate(input: { awsRegion: string; cluster: string; arn: string }): Promise<void>;
  /** Poll until the host runs no tasks; false when the wait runs out. */
  waitUntilEmpty(input: { awsRegion: string; cluster: string; arn: string; timeoutMs: number }): Promise<boolean>;
  /** Clear scale-in protection on the empty host and shrink the group so it is the one removed. */
  release(input: { awsRegion: string; name: string; instanceId: string; desiredCapacity: number }): Promise<void>;
}

export interface UpgradeDeps extends ConfigDriftPreviewDeps {
  ec2: UpgradeEc2Api;
  containerHosts?: ContainerHostsApi;
  ecsAccountSettings?: EcsAccountSettingsApi;
  cloudFormation: UpgradeCloudFormationApi;
  openSearch: UpgradeOpenSearchApi;
  eolSoftwareStacks: EolSoftwareStackApi;
  schedulerJobs?: SchedulerJobsApi;
  historicalIam?: (roleName: string, policyName: string, options: UpgradeCommandOptions, ownsPolicy?: boolean) => Promise<{ attached: string[]; inline: string[]; collision: boolean; available: number }>;
  deploy(options: UpgradeDeploymentOptions): Promise<void>;
  regionAmiConfig?: () => RegionsConfig;
}

export interface UpgradeCommandOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  terminationProtection?: string | boolean;
  keepBorrowedHosts?: boolean;
  deploymentId?: string;
  baseOs?: string;
  forceBuildBootstrap?: boolean;
  rollback?: boolean;
  optimizeDeployment?: boolean;
  moduleSet: string;
  force?: boolean;
  acceptConfigDrift?: boolean;
  skipGlobalSettingsUpdate?: boolean;
  disableEolStacksInUse?: boolean;
  /** Close job submission and wait for the host scheduler to empty before the container cutover. */
  drain?: boolean;
  drainTimeoutMinutes?: number;
  /** Skip the job inventory check before the container cutover; the operator has drained by hand. */
  skipDrainCheck?: boolean;
  modules?: readonly string[];
  allowReplacement?: string[];
}

interface EolStack {
  stackId: string;
  baseOs: string;
  name: string;
  architecture: string;
}

interface EolSession {
  stackId: string;
  baseOs: string;
  sessionId: string;
  owner: string;
  name: string;
}

interface EolPlan {
  tableName: string;
  sessions: EolSession[];
  toDelete: EolStack[];
  toDisable: EolStack[];
}

interface ClearedInstance {
  stackName: string;
  instanceId: string;
}

function valueAsString(value: unknown, defaultValue = ""): string {
  return typeof value === "string" && value !== "" ? value : defaultValue;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function toModuleInfo(row: Record<string, unknown>): ModuleInfo | undefined {
  const moduleId = valueAsString(row["module_id"]);
  const name = valueAsString(row["name"]);
  const type = valueAsString(row["type"]);
  return moduleId === "" || name === "" || type === "" ? undefined : { ...row, module_id: moduleId, name, type };
}

function settingString(rows: readonly Record<string, unknown>[], key: string): string | undefined {
  const value = rows.find((entry) => entry["key"] === key)?.["value"];
  return typeof value === "string" && value !== "" ? value : undefined;
}

/**
 * Give back the host the upgrade borrowed. Services roll with more tasks than they keep, so the
 * host group grows past its minimum; managed scaling only removes an empty host and nothing moves
 * tasks off one by itself. Drain the newest extra host, wait for it to empty, then shrink the group
 * with that host unprotected so it is the one removed.
 */
export async function returnBorrowedHosts(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "clusterName" | "awsRegion">,
  rows?: readonly Record<string, unknown>[],
): Promise<void> {
  const hostsApi = deps.containerHosts;
  if (hostsApi === undefined) return;
  const settings = rows ?? await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const cluster = settingString(settings, "ecs.cluster_name");
  const capacityProvider = settingString(settings, "ecs.capacity_provider");
  if (cluster === undefined || capacityProvider === undefined) return;
  const group = await hostsApi.hostGroup({ awsRegion: options.awsRegion, capacityProvider });
  const extra = group.instanceIds.length - group.minSize;
  if (extra <= 0) {
    deps.out(`Host group ${group.name} holds ${group.instanceIds.length} hosts at its minimum; nothing to return`);
    return;
  }
  const hosts = (await hostsApi.containerInstances({ awsRegion: options.awsRegion, cluster }))
    .filter((host) => host.status === "ACTIVE" && group.instanceIds.includes(host.instanceId))
    .sort((a, b) => (b.registeredAt ?? "").localeCompare(a.registeredAt ?? "") || a.runningTasks - b.runningTasks);
  let desired = group.instanceIds.length;
  for (const host of hosts.slice(0, extra)) {
    deps.out(`Returning borrowed host ${host.instanceId} (${host.runningTasks} tasks): draining`);
    await hostsApi.drain({ awsRegion: options.awsRegion, cluster, arn: host.arn });
    const empty = await hostsApi.waitUntilEmpty({ awsRegion: options.awsRegion, cluster, arn: host.arn, timeoutMs: 15 * 60_000 });
    if (!empty) {
      await hostsApi.activate({ awsRegion: options.awsRegion, cluster, arn: host.arn });
      deps.out(`warning: ${host.instanceId} still ran tasks after 15 minutes and is back in service; its tasks found no room elsewhere. Run return-hosts later.`);
      return;
    }
    desired -= 1;
    await hostsApi.release({ awsRegion: options.awsRegion, name: group.name, instanceId: host.instanceId, desiredCapacity: desired });
    deps.out(`Returned ${host.instanceId}; host group ${group.name} desired capacity is now ${desired}`);
  }
}

async function scanAll(deps: Deps, tableName: string): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: tableName, ExclusiveStartKey: startKey, ConsistentRead: true });
    rows.push(...(page.Items ?? []));
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  return rows;
}

async function scanModuleTable(deps: Deps, tableName: string): Promise<Array<Record<string, unknown>>> {
  try {
    return await scanAll(deps, tableName);
  } catch (error) {
    if ((error as { name?: string }).name === "ResourceNotFoundException") return [];
    throw error;
  }
}

async function clusterModules(deps: Deps, clusterName: string): Promise<ModuleInfo[]> {
  const result: ModuleInfo[] = [];
  for (const row of await scanAll(deps, `${clusterName}.modules`)) {
    const module = toModuleInfo(row);
    if (module === undefined) throw new ClusterConfigError("Malformed module row; repair the module inventory before upgrading");
    result.push(module);
  }
  return result;
}

function describeStack(stack: EolStack): string {
  return `${stack.stackId} (${stack.name}, ${stack.architecture})`;
}

async function findEolReferences(deps: UpgradeDeps, clusterName: string): Promise<string[]> {
  const findings: string[] = [];
  for (const entry of await scanAll(deps, `${clusterName}.cluster-settings`)) {
    const key = valueAsString(entry["key"]);
    const value = valueAsString(entry["value"]);
    if ((key.endsWith("base_os") || key.endsWith("compute_node_os")) && EOL_BASE_OS[value] !== undefined) {
      findings.push(`cluster setting ${key} = ${value}`);
    }
  }
  for (const module of await clusterModules(deps, clusterName)) {
    if (module.name !== "scheduler") continue;
    for (const row of await scanModuleTable(deps, `${clusterName}.${module.module_id}.queue-profiles`)) {
      const baseOs = valueAsString(row["param_base_os"]);
      if (EOL_BASE_OS[baseOs] !== undefined) {
        findings.push(`HPC queue profile ${valueAsString(row["queue_profile_name"], "<unnamed>")} = ${baseOs}`);
      }
    }
  }
  return findings;
}

async function planEolSoftwareStacks(deps: UpgradeDeps, clusterName: string): Promise<EolPlan[]> {
  const plans: EolPlan[] = [];
  for (const module of await clusterModules(deps, clusterName)) {
    if (module.name !== "virtual-desktop-controller") continue;
    const tableName = `${clusterName}.${module.module_id}.controller.software-stacks`;
    const eolStacks = (await scanModuleTable(deps, tableName))
      .filter((row) => EOL_BASE_OS[valueAsString(row["base_os"])] !== undefined)
      .map((row) => ({
        stackId: valueAsString(row["stack_id"]),
        baseOs: valueAsString(row["base_os"]),
        name: valueAsString(row["name"], "<unnamed>"),
        architecture: valueAsString(row["architecture"], "<unknown>"),
      }));
    if (eolStacks.length === 0) continue;

    const ids = new Set(eolStacks.map((stack) => stack.stackId));
    const sessions: EolSession[] = [];
    for (const row of await scanModuleTable(deps, `${clusterName}.${module.module_id}.controller.user-sessions`)) {
      if (valueAsString(row["state"]).toUpperCase() === "DELETED") continue;
      const softwareStack = asRecord(row["software_stack"]);
      const stackId = valueAsString(softwareStack["stack_id"]);
      const baseOs = valueAsString(softwareStack["base_os"]) || valueAsString(row["base_os"]);
      if (ids.has(stackId) || EOL_BASE_OS[baseOs] !== undefined) {
        sessions.push({
          stackId,
          baseOs,
          sessionId: valueAsString(row["idea_session_id"], "<unknown>"),
          owner: valueAsString(row["owner"], "<unknown>"),
          name: valueAsString(row["name"], "<unnamed>"),
        });
      }
    }

    const idsInUse = new Set(sessions.filter((session) => session.stackId !== "").map((session) => session.stackId));
    const baseOsInUse = new Set(sessions.filter((session) => session.stackId === "").map((session) => session.baseOs));
    plans.push({
      tableName,
      sessions,
      toDelete: eolStacks.filter((stack) => !idsInUse.has(stack.stackId) && !baseOsInUse.has(stack.baseOs)),
      toDisable: eolStacks.filter((stack) => idsInUse.has(stack.stackId) || baseOsInUse.has(stack.baseOs)),
    });
  }
  return plans;
}

async function checkEolBaseOs(
  deps: UpgradeDeps,
  options: UpgradeCommandOptions,
): Promise<EolPlan[]> {
  const findings = await findEolReferences(deps, options.clusterName);
  if (findings.length > 0) {
    deps.err("This cluster still references a Base OS that has reached end-of-life and is no longer supported by IDEA.");
    for (const finding of findings) deps.err(`  - ${finding}`);
    deps.err(`Move each to a supported Base OS first, for example: ideactl config set --cluster-name ${options.clusterName} --aws-region ${options.awsRegion} 'Key=scheduler.compute_node_os,Type=str,Value=amazonlinux2023' (queue profiles change in the portal's HPC queue settings). upgrade-cluster then sets the matching compute image.`);
    throw new ExitWithCode(1);
  }

  const plans = await planEolSoftwareStacks(deps, options.clusterName);
  const sessions = plans.flatMap((plan) => plan.sessions);
  if (sessions.length > 0 && options.disableEolStacksInUse !== true) {
    deps.err(`${sessions.length} virtual desktop session(s) still use a Base OS that has reached end-of-life. Nothing has been changed.`);
    for (const session of sessions) {
      deps.err(`  - session ${session.sessionId} owned by ${session.owner} on software stack ${session.stackId || session.baseOs}`);
    }
    deps.err("Delete these virtual desktops, then re-run upgrade-cluster.");
    throw new ExitWithCode(1);
  }

  for (const plan of plans) {
    for (const stack of plan.toDisable) deps.out(`will disable end-of-life eVDI software stack ${describeStack(stack)}`);
    for (const stack of plan.toDelete) deps.out(`will delete end-of-life eVDI software stack ${describeStack(stack)}`);
  }
  return plans;
}

async function applyEolSoftwareStacks(deps: UpgradeDeps, awsRegion: string, plans: EolPlan[]): Promise<void> {
  let disabled = 0;
  for (const plan of plans) {
    for (const stack of plan.toDisable) {
      await deps.eolSoftwareStacks.setEnabled({
        awsRegion,
        tableName: plan.tableName,
        baseOs: stack.baseOs,
        stackId: stack.stackId,
        enabled: false,
      });
      disabled += 1;
      const inUse = plan.sessions
        .filter((session) => session.stackId === stack.stackId || (session.stackId === "" && session.baseOs === stack.baseOs))
        .map((session) => `${session.owner} (${session.name})`)
        .join(", ");
      deps.out(`disabled end-of-life eVDI software stack ${describeStack(stack)}, still in use by ${inUse}`);
    }
    for (const stack of plan.toDelete) {
      await deps.eolSoftwareStacks.delete({
        awsRegion,
        tableName: plan.tableName,
        baseOs: stack.baseOs,
        stackId: stack.stackId,
      });
      deps.out(`deleted end-of-life eVDI software stack ${describeStack(stack)}`);
    }
  }
  if (disabled > 0) {
    deps.out(`${disabled} software stack(s) are disabled in DynamoDB but still read as enabled in the eVDI search index until it is reindexed.`);
  }
}

async function resolveUpgradeBaseOs(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "clusterName" | "baseOs">,
): Promise<string> {
  let current: string[] = [];
  try {
    current = [...new Set(
      (await scanAll(deps, `${options.clusterName}.cluster-settings`))
        .filter((entry) => valueAsString(entry["key"]).endsWith(".base_os"))
        .map((entry) => valueAsString(entry["value"]))
        .filter((value) => value !== ""),
    )].sort();
  } catch (error) {
    if (options.baseOs === undefined || options.baseOs === "") {
      deps.err(`Could not read the cluster settings to determine the current Base OS: ${(error as Error).message}. Re-run with an explicit --base-os.`);
      throw new ExitWithCode(1);
    }
  }

  if (options.baseOs === undefined || options.baseOs === "") {
    if (current.length !== 1) {
      const found = current.length === 0 ? "no base_os setting found" : current.join(", ");
      deps.err(`Could not determine the Base OS this cluster runs from its settings (${found}). Re-run with an explicit --base-os to say which Base OS every module should use.`);
      throw new ExitWithCode(1);
    }
    const [baseOs] = current;
    deps.out(`No --base-os given: keeping the Base OS this cluster runs: ${baseOs}`);
    return baseOs;
  }

  if (current.length > 0 && !current.includes(options.baseOs)) {
    deps.out(`--base-os ${options.baseOs} changes this cluster from ${current.join(", ")}: every module is redeployed onto ${options.baseOs}.`);
  }
  return options.baseOs;
}

async function validateBaseOs(deps: UpgradeDeps, options: UpgradeCommandOptions, baseOs: string): Promise<void> {
  const replacement = EOL_BASE_OS[baseOs];
  if (replacement !== undefined) {
    deps.err(`Base OS ${baseOs} has reached end-of-life and is no longer supported by IDEA. Upgrade to ${replacement} instead.`);
    throw new ExitWithCode(1);
  }
  if (!UPGRADE_BASE_OS.includes(baseOs)) {
    deps.err(`Invalid base_os: ${baseOs}. Must be one of: ${UPGRADE_BASE_OS.join(", ")}`);
    throw new ExitWithCode(1);
  }
  if (baseOs !== "rhel10" && baseOs !== "rocky10") return;
  let modules: ModuleInfo[];
  try {
    modules = await clusterModules(deps, options.clusterName);
  } catch (error) {
    deps.err(`Could not read the cluster modules table to validate ${baseOs} eVDI compatibility: ${(error as Error).message}`);
    throw new ExitWithCode(1);
  }
  if (modules.some((module) => module.name === "virtual-desktop-controller" && module.status === "deployed")) {
    deps.err(`base_os ${baseOs} is not supported on clusters with the virtual-desktop-controller module deployed: Amazon DCV publishes no EL10 packages.`);
    throw new ExitWithCode(1);
  }
}

function backupDir(configDir: string, now: number): string {
  const date = new Date(now);
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${configDir}.golden.${pad(date.getMonth() + 1)}${pad(date.getDate())}${date.getFullYear()}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

async function updateValuesBaseOs(deps: UpgradeDeps, options: UpgradeCommandOptions, baseOs: string): Promise<string> {
  const path = valuesFilePath(options.clusterName, options.awsRegion);
  if (existsSync(path)) {
    try {
      const bucket = (await scanAll(deps, `${options.clusterName}.cluster-settings`))
        .find((entry) => entry["key"] === "cluster.cluster_s3_bucket")?.["value"];
      if (typeof bucket === "string") {
        const remote = await deps.s3.getObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY });
        if (remote !== readFileSync(path, "utf8")) deps.out("warning: local values.yml differs from the copy in the cluster bucket.");
      }
    } catch (error) {
      deps.out(`warning: could not compare local values.yml with the cluster bucket: ${(error as Error).message}`);
    }
  } else {
    const bucket = (await scanAll(deps, `${options.clusterName}.cluster-settings`))
      .find((entry) => entry["key"] === "cluster.cluster_s3_bucket")?.["value"];
    if (typeof bucket !== "string" || bucket === "") throw new ClusterConfigError("cluster.cluster_s3_bucket is required to restore values.yml");
    deps.out(`values.yml not found at ${path}, restoring it from the cluster bucket ...`);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, await deps.s3.getObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY }));
  }

  const values = readFileSync(path, "utf8");
  const updated = values.replace(/^base_os:.*$/m, `base_os: ${baseOs}`);
  if (updated === values && !/^base_os:.*$/m.test(values)) {
    deps.err(`${path} has no base_os key, so the upgrade cannot set it to ${baseOs}.`);
    throw new ExitWithCode(1);
  }
  writeFileSync(path, updated);
  deps.out(`Successfully updated base_os to ${baseOs} in values.yml`);
  return path;
}

async function exportConfiguration(deps: UpgradeDeps, options: UpgradeCommandOptions, configDir: string): Promise<void> {
  const entries = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const modules = await clusterModules(deps, options.clusterName);
  const tree = buildTree(entries.map((entry) => ({ key: valueAsString(entry["key"]), value: entry["value"] })));
  mkdirSync(configDir, { recursive: true });
  const idea: { modules: Array<{ name: string; id: string; type: string; config_files: string[] }> } = { modules: [] };
  for (const module of modules) {
    const moduleDir = join(configDir, module.module_id);
    mkdirSync(moduleDir, { recursive: true });
    writeFileSync(join(moduleDir, "settings.yml"), toYaml(tree[module.module_id] ?? {}));
    idea.modules.push({ name: module.name, id: module.module_id, type: module.type, config_files: ["settings.yml"] });
  }
  writeFileSync(join(configDir, "idea.yml"), toYaml(idea));
}

// Older applications have a fixed module table that cannot resolve these new names.
// Advertising them before cluster-manager upgrades breaks every portal page.
const MODULES_UNKNOWN_TO_DEPLOYED_APPLICATIONS: ReadonlySet<string> = new Set([ECS_MODULE]);

// The portal resolves every advertised module using its deployed application table.
// ECS capacity alone cannot make an older cluster-manager recognize the new module.
export function heldModuleSetEntries(
  entries: ConfigEntry[],
  portalReady = false,
): { kept: ConfigEntry[]; held: ConfigEntry[] } {
  const kept: ConfigEntry[] = [];
  const held: ConfigEntry[] = [];
  for (const entry of entries) {
    const name = moduleSetEntryModule(entry.key);
    const wait = name !== undefined && MODULES_UNKNOWN_TO_DEPLOYED_APPLICATIONS.has(name) && !portalReady;
    (wait ? held : kept).push(entry);
  }
  return { kept, held };
}

/** `global-settings.module_sets.<set>.<module>.<field>` names a module; anything else does not. */
function moduleSetEntryModule(key: string): string | undefined {
  const parts = key.split(".");
  return parts[0] === "global-settings" && parts[1] === "module_sets" && parts.length >= 5 ? parts[3] : undefined;
}

/** The first release whose cluster-manager resolves the container module in its module table. */
export const MODULE_TABLE_KNOWS_ECS_FROM = "26.09.1";

// The portal resolves every advertised module through its deployed application's module table.
// A cluster-manager from before the container release cannot resolve the new module, so its row
// waits until the stack has completed on the target release. One that already knows the module
// keeps the row for the whole run: holding it back from a portal that depends on it takes every
// page down for the length of the upgrade.
async function clusterManagerReady(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[]): Promise<boolean> {
  const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const id = maintenanceModuleId(settings, options.moduleSet);
  const portal = modules.find((module) => module.module_id === id);
  if (portal?.status !== "deployed") return false;
  const deployed = typeof portal.version === "string" ? portal.version.trim() : "";
  const knows = compareIdeaRelease(deployed, MODULE_TABLE_KNOWS_ECS_FROM);
  // A row already at the target release may have advanced ahead of a stack that never completed,
  // so only a completed stack carrying the tag proves that one; any earlier container release
  // was reached by a completed run and its application resolves the module.
  if (knows !== undefined && knows >= 0 && deployed !== ideaVersion()) return true;
  if (deployed !== ideaVersion()) return false;
  const stack = await deps.cfn.describeStack(portal.stack_name ?? `${options.clusterName}-${id}`);
  return ["CREATE_COMPLETE", "UPDATE_COMPLETE"].includes(stack.StackStatus ?? "")
    && stack.Tags?.some((tag) => tag.Key === "idea:ModuleVersion" && tag.Value === ideaVersion()) === true;
}

async function announceHeldModuleSets(
  deps: UpgradeDeps,
  options: UpgradeCommandOptions,
  configDir: string,
): Promise<void> {
  if (!existsSync(join(configDir, "idea.yml"))) return;
  const { held } = heldModuleSetEntries(convertConfigToKeyValuePairs(configDir, "global-settings"));
  if (held.length === 0) return;
  const currentModules = await clusterModules(deps, options.clusterName);
  if (heldModuleSetEntries(held, await clusterManagerReady(deps, options, currentModules)).held.length > 0) return;
  const writer = await deps.configWriter({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
  });
  await writer.syncClusterSettingsInDb(held, false);
}

// Global rows are rewritten in place so a running application never sees a missing row; the rows
// this release no longer generates are removed only once every stack has deployed and nothing
// running reads them. The writer deletes by prefix, so a key that prefixes another current key is
// left alone and named.
async function removeObsoleteGlobalSettings(deps: UpgradeDeps, options: UpgradeCommandOptions, configDir: string): Promise<void> {
  if (!existsSync(join(configDir, "idea.yml"))) return;
  const generated = new Set(convertConfigToKeyValuePairs(configDir, "global-settings").map((entry) => entry.key));
  const current = (await scanAll(deps, `${options.clusterName}.cluster-settings`)).map((row) => String(row["key"]));
  const obsolete = current.filter((key) => key.startsWith("global-settings.") && !generated.has(key)).sort();
  if (obsolete.length === 0) return;
  const writer = await deps.configWriter({ clusterName: options.clusterName, awsRegion: options.awsRegion, awsProfile: options.awsProfile });
  for (const key of obsolete) {
    if (current.some((other) => other !== key && other.startsWith(key))) {
      deps.out(`${key} is no longer generated but prefixes another row; left in place`);
      continue;
    }
    await writer.deleteConfigEntries(key);
    deps.out(`removed ${key}: no longer generated by this release`);
  }
}

async function backupAndUpdateGlobalSettings(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[]): Promise<string> {
  const regionDir = join(valuesFilePath(options.clusterName, options.awsRegion), "..");
  const configDir = join(regionDir, "config");
  await exportConfiguration(deps, options, configDir);
  const golden = backupDir(configDir, deps.now());
  if (existsSync(golden)) rmSync(golden, { recursive: true });
  cpSync(configDir, golden, { recursive: true });
  deps.out(`Backup created successfully at ${golden}`);

  generateConfigFromTemplates(loadValuesFile(join(regionDir, "values.yml")), configDir);
  const writer = await deps.configWriter({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
  });
  const portalReady = await clusterManagerReady(deps, options, modules);
  const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const ownerKey = `global-settings.module_sets.${options.moduleSet}.cluster-manager.module_id`;
  const ownerId = maintenanceModuleId(settings, options.moduleSet);
  const generated = convertConfigToKeyValuePairs(configDir, "global-settings");
  // Template defaults must not redirect a running application to a different module id.
  // Keeping its selected mapping also lets a retry find the durable baseline.
  const moduleIds = new Map(settings.filter((row) => moduleSetEntryModule(String(row["key"])) !== undefined && String(row["key"]).endsWith(".module_id"))
    .map((row) => [String(row["key"]), row["value"]]));
  const entries = generated.filter((entry) => entry.key !== ownerKey).map((entry) =>
    moduleIds.has(entry.key) ? { ...entry, value: moduleIds.get(entry.key) } : entry);
  entries.push({ key: ownerKey, value: ownerId });
  // Keep the maintenance owner durable across retries of older interrupted upgrades.
  const savedOwners = asRecord(settings.find((entry) => entry["key"] === "cluster.upgrade_module_set_owners")?.["value"]);
  const owners = { ...savedOwners, ...Object.fromEntries(settings.filter((entry) => valueAsString(entry["key"]).startsWith("global-settings.module_sets.") && valueAsString(entry["key"]).endsWith(".cluster-manager.module_id")).map((entry) => [String(entry["key"]), entry["value"]])) };
  await writer.setConfigEntry("cluster.upgrade_module_set_owners", owners);
  // Update rows in place. Running applications resolve module sets and global settings on
  // requests; deleting the prefix exposes missing configuration even when the rewrite succeeds.
  // Retain obsolete rows for old tasks and scoped upgrades that leave some modules untouched.
  await writer.syncClusterSettingsInDb(heldModuleSetEntries(entries, portalReady).kept, true);
  return configDir;
}

// Defaults would mask the old scheduler interval before its replacement can inherit it.
// Keep both keys so the old process and a retried upgrade retain their original inputs.
async function migrateReconcilerIntervals(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[]): Promise<void> {
  const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const writer = await deps.configWriter(options);
  for (const module of modules.filter((entry) => entry.name === "scheduler")) {
    const prefix = `${module.module_id}.job_provisioning.`;
    const old = settings.find((row) => row["key"] === `${prefix}job_periodic_check_interval_seconds`);
    const current = settings.find((row) => row["key"] === `${prefix}job_reconciler_interval_seconds`);
    if (old === undefined) continue;
    if (current === undefined) {
      await writer.syncClusterSettingsInDb([{ key: `${prefix}job_reconciler_interval_seconds`, value: old["value"] }], false);
    } else if (JSON.stringify(old["value"]) !== JSON.stringify(current["value"])) {
      deps.out(`warning: conflicting reconciler intervals for ${module.module_id}; keeping the new value ${JSON.stringify(current["value"])} and old value ${JSON.stringify(old["value"])}`);
    }
  }
}

async function syncFullConfiguration(
  deps: UpgradeDeps,
  options: UpgradeCommandOptions,
  configDir: string,
  modules: ModuleInfo[],
): Promise<void> {
  const writer = await deps.configWriter({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
  });
  // A regenerated configuration can describe a module the table has no row for. The deployment
  // order reads the table, so an unregistered module is skipped while every other stack deploys.
  // Add-only: an existing row keeps its type, status, stack name and version.
  await writer.syncModulesInDb(
    readModulesFromFiles(configDir).map((module) => ({ id: module.id, name: module.name, type: module.type })),
  );
  await writer.syncClusterSettingsInDb(heldModuleSetEntries(convertConfigToKeyValuePairs(configDir), await clusterManagerReady(deps, options, modules)).kept, false);
}

export function buildAmiUpdateEntries(
  amiId: string,
  baseOs: string,
  modules: ModuleInfo[],
  keepKeys: ReadonlySet<string> = new Set(),
): Array<{ key: string; value: string }> {
  const entries: Array<{ key: string; value: string }> = [];
  for (const module of modules) {
    for (const [baseOsKey, amiKey] of AMI_UPDATE_KEYS[module.name] ?? []) {
      const keys = [`${module.module_id}.${baseOsKey}`, `${module.module_id}.${amiKey}`];
      if (keys.some((key) => keepKeys.has(key))) continue;
      entries.push({ key: keys[0] as string, value: baseOs }, { key: keys[1] as string, value: amiId });
    }
  }
  return entries;
}

export function keepBuiltComputeImage(current: InstanceImage | undefined, stock: InstanceImage | undefined): boolean {
  return (
    current?.Name?.startsWith(COMPUTE_IMAGE_PREFIX) === true &&
    typeof current.CreationDate === "string" &&
    current.CreationDate !== "" &&
    typeof stock?.CreationDate === "string" &&
    stock.CreationDate !== "" &&
    current.CreationDate > stock.CreationDate
  );
}

async function computeAmiKeepKeys(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "awsRegion">,
  modules: ModuleInfo[],
  amiId: string,
  settings: readonly CurrentConfigRow[],
): Promise<Set<string>> {
  const kept = new Set<string>();
  for (const scheduler of modules.filter((module) => module.name === "scheduler")) {
    const current = settings.find((entry) => entry.key === `${scheduler.module_id}.compute_node_ami`)?.value;
    if (typeof current !== "string" || current === "" || current === amiId) continue;
    try {
      const images = await deps.ec2.describeImages({ awsRegion: options.awsRegion, imageIds: [current, amiId] });
      if (!keepBuiltComputeImage(images.find((image) => image.ImageId === current), images.find((image) => image.ImageId === amiId))) continue;
      deps.out(`keeping built compute image ${current} for ${scheduler.module_id}, newer than the release image ${amiId}`);
      kept.add(`${scheduler.module_id}.compute_node_os`);
      kept.add(`${scheduler.module_id}.compute_node_ami`);
    } catch (error) {
      deps.out(`warning: could not describe compute image ${current} or release image ${amiId}: ${(error as Error).message}. Compute moves to ${amiId}.`);
    }
  }
  return kept;
}

async function planModuleHostInstanceTypes(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "awsRegion">,
  modules: ModuleInfo[],
  settings: readonly CurrentConfigRow[],
): Promise<ConfigEntry[]> {
  const values = new Map(settings.map((entry) => [entry.key, valueAsString(entry.value)]));
  const keys = modules.flatMap((module) => (HOST_INSTANCE_TYPE_KEYS[module.name] ?? []).map((key) => `${module.module_id}.${key}`))
    .filter((key) => values.get(key) !== "");
  const oldKeys = keys.filter((key) => values.get(key) === MODULE_HOST_INSTANCE_TYPE_OLD);
  if (oldKeys.length === 0) return [];
  let offered: string[];
  try {
    offered = await deps.ec2.describeInstanceTypeOfferings({ awsRegion: options.awsRegion, instanceType: MODULE_HOST_INSTANCE_TYPE });
  } catch (error) {
    deps.out(`warning: could not read whether this region offers ${MODULE_HOST_INSTANCE_TYPE}: ${(error as Error).message}. Keeping ${MODULE_HOST_INSTANCE_TYPE_OLD}.`);
    return [];
  }
  if (!offered.includes(MODULE_HOST_INSTANCE_TYPE)) {
    deps.out(`${MODULE_HOST_INSTANCE_TYPE} is not offered in this region. Keeping ${MODULE_HOST_INSTANCE_TYPE_OLD}.`);
    return [];
  }
  return oldKeys.sort().map((key) => ({ key, value: MODULE_HOST_INSTANCE_TYPE }));
}

async function planOpenSearchDataNodeInstanceType(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "awsRegion">,
  modules: ModuleInfo[],
  settings: readonly CurrentConfigRow[],
): Promise<ConfigEntry[]> {
  const analytics = modules.find((module) => module.name === "analytics");
  if (analytics === undefined) return [];
  const current = settings.find((entry) => entry.key === `${analytics.module_id}.opensearch.data_node_instance_type`)?.value;
  if (typeof current !== "string" || current === "" || current !== OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD) return [];
  const domainName = settings.find((entry) => entry.key === `${analytics.module_id}.opensearch.domain_name`)?.value;
  try {
    const domain = await deps.openSearch.describeDomain({
      awsRegion: options.awsRegion,
      domainName: typeof domainName === "string" ? domainName : undefined,
    });
    const offered = await deps.openSearch.listInstanceTypeDetails({ awsRegion: options.awsRegion, engineVersion: domain.engineVersion });
    if (!offered.includes(OPENSEARCH_DATA_NODE_INSTANCE_TYPE)) {
      deps.out(`${OPENSEARCH_DATA_NODE_INSTANCE_TYPE} is not offered for ${domain.engineVersion ?? ""} in this region. Keeping analytics data node instance type ${current}.`);
      return [];
    }
    return [{
      key: `${analytics.module_id}.opensearch.data_node_instance_type`,
      value: OPENSEARCH_DATA_NODE_INSTANCE_TYPE,
    }];
  } catch (error) {
    deps.out(`warning: could not read the instance types offered for the analytics domain: ${(error as Error).message}. Keeping analytics data node instance type ${current}.`);
    return [];
  }
}

/** Resolve every Phase 3 write before the upgrade asks for approval. */
export async function planUpgradePhase3Entries(
  deps: UpgradeDeps,
  options: Pick<UpgradeCommandOptions, "awsRegion">,
  modules: ModuleInfo[],
  settings: readonly CurrentConfigRow[],
  amiId: string,
  baseOs: string,
  releaseVersion: string = ideaVersion(),
): Promise<ConfigEntry[]> {
  const keepKeys = await computeAmiKeepKeys(deps, options, modules, amiId, settings);
  return [
    ...buildAmiUpdateEntries(amiId, baseOs, modules, keepKeys),
    ...await planModuleHostInstanceTypes(deps, options, modules, settings),
    ...await planOpenSearchDataNodeInstanceType(deps, options, modules, settings),
    ...planEcsImageFollowsRelease(settings, releaseVersion),
  ];
}

/** The rows the add-only sync leaves on the previous provider when values move metrics to the daemon. */
export const METRICS_PROVIDER_CUTOVER_KEYS = ["metrics.provider", "metrics.dogstatsd.url"] as const;

/**
 * Plan the metrics provider cutover: with `metrics_provider: dogstatsd` and `enable_ecs: true`
 * in values the generated configuration turns the agent daemon on, and the modules must send to
 * it. The full sync never overwrites, so the two rows the modules read are written after Phase 3,
 * from the same generated values, when the table still names another provider or another
 * destination. A cluster whose rows already agree plans nothing.
 */
export function planMetricsProviderCutover(
  generated: readonly ConfigEntry[],
  current: readonly CurrentConfigRow[],
): ConfigEntry[] {
  const generatedRows = new Map(generated.map((entry) => [entry.key, entry.value]));
  if (generatedRows.get("ecs.datadog.enabled") !== true) return [];
  const currentRows = new Map(current.map((entry) => [entry.key, entry.value]));
  const entries: ConfigEntry[] = [];
  for (const key of METRICS_PROVIDER_CUTOVER_KEYS) {
    const value = generatedRows.get(key);
    if (typeof value !== "string" || value === "") continue;
    if (currentRows.get(key) === value) continue;
    entries.push({ key, value });
  }
  return entries;
}

/** A release tag as the release pipeline publishes it: two-digit year, month, patch. */
const RELEASE_IMAGE_TAG = /^\d{2}\.\d{2}\.\d+$/;

/**
 * Plan the image row's move to the release being installed. The row is add-only for the sync, so
 * a routine upgrade would otherwise deploy the new templates on the previous image. Only a row
 * that names this partition's release repository at an older release tag moves; a private
 * registry, a digest-qualified reference, a build tag or a newer tag is the operator's and stays.
 */
export function planEcsImageFollowsRelease(
  current: readonly CurrentConfigRow[],
  releaseVersion: string,
): ConfigEntry[] {
  const rows = new Map(current.map((entry) => [entry.key, entry.value]));
  const image = rows.get("ecs.image");
  const repository = rows.get("ecs.image_repositories.aws");
  if (typeof image !== "string" || typeof repository !== "string" || repository === "") return [];
  if (!image.startsWith(`${repository}:`)) return [];
  const tag = image.slice(repository.length + 1);
  if (!RELEASE_IMAGE_TAG.test(tag) || (compareIdeaRelease(tag, releaseVersion) ?? 0) >= 0) return [];
  return [{ key: "ecs.image", value: `${repository}:${releaseVersion}` }];
}

/** Apply the already previewed Phase 3 plan without recalculating it after approval. */
async function applyPhase3Entries(
  writer: Awaited<ReturnType<UpgradeDeps["configWriter"]>>,
  entries: readonly ConfigEntry[],
  current: readonly CurrentConfigRow[],
  out: (line: string) => void,
): Promise<void> {
  const previous = new Map(current.map((entry) => [entry.key, entry.value]));
  for (const entry of entries) {
    await writer.setConfigEntry(entry.key, entry.value);
    if (entry.key === "ecs.image") {
      out(`${entry.key} moves from ${String(previous.get(entry.key) ?? "(unset)")} to ${String(entry.value)}; every task rolls to the release image`);
    } else if (entry.key === "metrics.provider") {
      out(`${entry.key} moves from ${String(previous.get(entry.key) ?? "(unset)")} to ${String(entry.value)}; the modules send to the agent daemon once they run as tasks`);
    } else if (entry.value === MODULE_HOST_INSTANCE_TYPE && previous.get(entry.key) === MODULE_HOST_INSTANCE_TYPE_OLD) {
      out(`${entry.key} moves from ${MODULE_HOST_INSTANCE_TYPE_OLD} to ${MODULE_HOST_INSTANCE_TYPE}; the host runs it when the instance is next replaced`);
    } else if (
      entry.value === OPENSEARCH_DATA_NODE_INSTANCE_TYPE &&
      previous.get(entry.key) === OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD
    ) {
      out(`analytics data nodes move from ${OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD} to ${OPENSEARCH_DATA_NODE_INSTANCE_TYPE}. OpenSearch Service applies this as a blue/green deployment.`);
    }
  }
}

/** Keep only table fields used by the value-free comparison report. */
function currentConfigRows(rows: readonly Record<string, unknown>[]): CurrentConfigRow[] {
  return rows.flatMap((row) => {
    const key = row["key"];
    if (typeof key !== "string" || key === "") return [];
    return [{
      key,
      value: row["value"],
      source: typeof row["source"] === "string" ? row["source"] : undefined,
      version: typeof row["version"] === "number" ? row["version"] : undefined,
    }];
  });
}

/**
 * Generate the upgrade target in a temporary directory. The cluster's local
 * values and generated configuration remain unchanged until approval.
 */
async function generatedPreviewEntries(
  deps: UpgradeDeps,
  options: ConfigUpgradePreviewOptions,
  baseOs: string,
  current: readonly CurrentConfigRow[],
): Promise<ConfigEntry[]> {
  const root = mkdtempSync(join(tmpdir(), "ideactl-drift-preview-"));
  try {
    const configuredValuesPath = options.valuesFile ?? valuesFilePath(options.clusterName, options.awsRegion);
    let sourceValuesPath = configuredValuesPath;
    if (!existsSync(configuredValuesPath)) {
      if (options.valuesFile !== undefined) {
        throw new ClusterConfigError(`file not found: ${configuredValuesPath}`);
      }
      const bucket = current.find((entry) => entry.key === "cluster.cluster_s3_bucket")?.value;
      if (typeof bucket !== "string" || bucket === "") {
        throw new ClusterConfigError("cluster.cluster_s3_bucket is required to preview a missing values.yml");
      }
      sourceValuesPath = join(root, "values.yml");
      writeFileSync(sourceValuesPath, await deps.s3.getObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY }));
    }

    const values = { ...loadValuesFile(sourceValuesPath), base_os: baseOs };
    const configDir = join(root, "config");
    generateConfigFromTemplates(values, configDir);
    return convertConfigToKeyValuePairs(configDir);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

/**
 * Build a conservative stack ownership plan from current source markers.
 *
 * The marker identifies rows a stack owns, not who last edited them. Without a
 * resolved target settings map the preview reports the unconditional rewrite
 * but does not guess future values or deletions.
 */
function inferredStackPlans(
  current: readonly CurrentConfigRow[],
  modules: readonly ModuleInfo[],
  selectedModuleIds: readonly string[] | undefined,
): StackSettingsPlan[] {
  const settings = new Map<string, Array<[string, unknown]>>();
  for (const row of current) {
    if (row.source !== "stack") continue;
    const module = modules.find((candidate) => row.key.startsWith(`${candidate.module_id}.`));
    if (module === undefined) continue;
    const relativeKey = row.key.slice(module.module_id.length + 1);
    const rows = settings.get(module.module_id) ?? [];
    rows.push([relativeKey, row.value]);
    settings.set(module.module_id, rows);
  }

  const selected = new Set(selectedModuleIds ?? []);
  const allModules = selected.size === 0;
  return modules.flatMap((module) => {
    const rows = settings.get(module.module_id);
    if (rows === undefined) return [];
    return [{
      moduleId: module.module_id,
      selected: allModules || selected.has(module.module_id),
      previous: Object.fromEntries(rows),
    }];
  });
}

/**
 * Read and generate every input needed by both preview entry points.
 *
 * Replay callers may inject a complete input. The normal path reads the table,
 * resolves Phase 3 conditions, and generates configuration only in a temporary
 * directory.
 */
export async function prepareUpgradeDriftInput(
  deps: UpgradeDeps,
  options: ConfigUpgradePreviewOptions,
): Promise<UpgradeDriftInput> {
  if (deps.loadUpgradeDriftInput !== undefined) return deps.loadUpgradeDriftInput(options);

  const baseOs = options.baseOs ?? await resolveUpgradeBaseOs(deps, options);
  const current = currentConfigRows(await scanAll(deps, `${options.clusterName}.cluster-settings`));
  const modules = await clusterModules(deps, options.clusterName);
  const amiId = resolveRegionAmi(
    (deps.regionAmiConfig ?? loadRegionAmiConfig)(),
    options.awsRegion,
    baseOs,
  );
  const generated = await generatedPreviewEntries(deps, options, baseOs, current);
  const phase3 = await planUpgradePhase3Entries(deps, options, modules, current, amiId, baseOs);

  return {
    current,
    generated,
    phase3,
    providerCutover: planMetricsProviderCutover(generated, current),
    stacks: inferredStackPlans(current, modules, options.modules),
    replaceGlobalSettings: options.skipGlobalSettingsUpdate !== true,
    syncFullConfiguration: true,
  };
}

async function moduleInstances(deps: UpgradeDeps, options: UpgradeCommandOptions): Promise<ClearedInstance[]> {
  const instances: ClearedInstance[] = [];
  for (const module of await clusterModules(deps, options.clusterName)) {
    if (module.type === "config") continue;
    const stackName = valueAsString(module.stack_name) || `${options.clusterName}-${module.module_id}`;
    let nextToken: string | undefined;
    try {
      do {
        const page = await deps.cloudFormation.listStackResources({ awsRegion: options.awsRegion, stackName, nextToken });
        instances.push(...page.instanceIds.map((instanceId) => ({ stackName, instanceId })));
        nextToken = page.nextToken;
      } while (nextToken !== undefined);
    } catch (error) {
      const errorShape = error as { name?: string; message?: string };
      if (errorShape.name === "ValidationError" && errorShape.message?.includes("does not exist") === true) continue;
      throw error;
    }
  }
  return instances;
}

const TERMINATION_PROTECTION_TAG = "idea:TerminationProtectionCleared";

async function clearTerminationProtection(
  deps: UpgradeDeps,
  awsRegion: string,
  instances: ClearedInstance[],
): Promise<ClearedInstance[]> {
  const cleared: ClearedInstance[] = [];
  for (const instance of instances) {
    try {
      if (!await deps.ec2.describeInstanceAttribute({ awsRegion, instanceId: instance.instanceId })) continue;
      // Persist the baseline first so an interrupted run can recover it on a later upgrade.
      // If tagging fails, leaving protection enabled avoids losing that baseline.
      await deps.ec2.createTags({ awsRegion, instanceId: instance.instanceId, value: new Date(deps.now()).toISOString() });
      await deps.ec2.modifyInstanceAttribute({ awsRegion, instanceId: instance.instanceId, protected: false });
      cleared.push(instance);
      deps.out(`cleared instance termination protection on ${instance.instanceId} (${instance.stackName})`);
    } catch (error) {
      deps.out(`warning: could not clear termination protection on ${instance.instanceId} (${instance.stackName}): ${(error as Error).message}.`);
    }
  }
  return cleared;
}

async function restoreTerminationProtection(deps: UpgradeDeps, awsRegion: string, instances: ClearedInstance[], originals: ClearedInstance[] = []): Promise<void> {
  if (instances.length === 0) return;
  const alive = new Set<string>();
  for (const input of [
    ...(originals.length === 0 ? [] : [{ awsRegion, instanceIds: originals.map((instance) => instance.instanceId) }]),
    { awsRegion, instanceIds: instances.map((instance) => instance.instanceId), tagKey: TERMINATION_PROTECTION_TAG },
  ]) {
    try {
      for (const id of await deps.ec2.describeLiveInstances(input)) alive.add(id);
    } catch (error) {
      deps.out(`warning: could not read surviving instances for protection restoration: ${(error as Error).message}`);
    }
  }
  for (const instance of instances) {
    if (!alive.has(instance.instanceId)) {
      continue;
    }
    try {
      await deps.ec2.modifyInstanceAttribute({ awsRegion, instanceId: instance.instanceId, protected: true });
      await deps.ec2.deleteTags({ awsRegion, instanceId: instance.instanceId });
      deps.out(`restored instance termination protection on ${instance.instanceId} (${instance.stackName})`);
    } catch (error) {
      deps.out(`warning: could not restore termination protection on ${instance.instanceId} (${instance.stackName}): ${(error as Error).message}. Re-enable it by hand.`);
    }
  }
}

async function warnClearedProtection(deps: UpgradeDeps, awsRegion: string, cleared: ClearedInstance[]): Promise<void> {
  if (cleared.length === 0) return;
  // An instance the deployment replaced is gone; naming it would send the operator after a ghost.
  let alive: string[] | undefined;
  try {
    alive = await deps.ec2.describeLiveInstances({ awsRegion, instanceIds: cleared.map((instance) => instance.instanceId) });
  } catch {
    alive = undefined;
  }
  const remaining = alive === undefined ? cleared : cleared.filter((instance) => alive.includes(instance.instanceId));
  if (remaining.length > 0) {
    deps.out(`warning: termination protection is still cleared on ${remaining.map((instance) => instance.instanceId).join(", ")}. Re-enable it by hand once the cluster is stable.`);
  }
}

async function saveValuesFile(deps: UpgradeDeps, options: UpgradeCommandOptions): Promise<void> {
  let bucket: string | undefined;
  try {
    const found = (await scanAll(deps, `${options.clusterName}.cluster-settings`))
      .find((entry) => entry["key"] === "cluster.cluster_s3_bucket")?.["value"];
    bucket = typeof found === "string" ? found : undefined;
    if (typeof bucket !== "string" || bucket === "") throw new ClusterConfigError("cluster.cluster_s3_bucket is required");
    await deps.s3.putObject({
      Bucket: bucket,
      Key: VALUES_FILE_S3_KEY,
      Body: readFileSync(valuesFilePath(options.clusterName, options.awsRegion)),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const location =
      typeof bucket === "string" && bucket !== ""
        ? `s3://${bucket}/${VALUES_FILE_S3_KEY}`
        : "the cluster bucket";
    deps.out(
      `warning: Upgrade of ${options.clusterName} finished its stack steps, but values.yml was not saved to ${location} (${detail}). The cluster bucket still has the previous file. Retry ideactl config save-values --cluster-name ${options.clusterName} --aws-region ${options.awsRegion}. Until that works, a run on another machine can restore the old values.yml.`,
    );
  }
}

async function defaultDeployment(deps: Deps, options: UpgradeDeploymentOptions): Promise<void> {
  const helper = await DeploymentHelper.open({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
    terminationProtection: options.terminationProtection,
    deploymentId: options.deploymentId,
    upgrade: true,
    moduleSet: options.moduleSet,
    allModules: options.allModules,
    forceBuildBootstrap: options.forceBuildBootstrap,
    optimizeDeployment: options.optimizeDeployment,
    moduleIds: options.moduleIds,
    rollback: options.rollback,
    allowReplacement: options.allowReplacement,
    deps,
  });
  await helper.invoke();
}

/**
 * Stop where a value the upgrade overwrites differs from what the generator would produce, and
 * nowhere else. An upgrade whose rows all match proceeds without asking: a question asked on every
 * run is a question nobody reads. `--force` skips confirmations; it does not accept losing an edit,
 * so accepting these rows in an unattended run needs the flag that says only that.
 */
async function confirmConfigDrift(
  deps: UpgradeDeps,
  options: UpgradeCommandOptions,
  report: UpgradeDriftReport,
): Promise<void> {
  const atRisk = report.changedRowsDifferingFromGenerated;
  if (atRisk.length === 0) return;

  deps.err(
    `${atRisk.length} configuration row(s) hold a value this upgrade overwrites, and the value differs from generated configuration: ${atRisk.join(", ")}`,
  );
  if (options.acceptConfigDrift === true) {
    deps.out("--accept-config-drift: overwriting the rows above");
    return;
  }
  if (options.force === true) {
    deps.err(
      "Reconcile those rows, or re-run with --accept-config-drift. --force skips confirmations and does not cover them.",
    );
    throw new ExitWithCode(1);
  }
  const confirm = await deps.prompt({
    message: `Overwrite the ${atRisk.length} row(s) above and continue with the cluster upgrade?`,
    default: false,
  });
  if (confirm !== true && confirm !== "Yes") throw new ExitWithCode(0);
}

function maintenanceModuleId(entries: Array<Record<string, unknown>>, moduleSet: string): string {
  const key = `global-settings.module_sets.${moduleSet}.cluster-manager.module_id`;
  const id = entries.find((entry) => entry["key"] === key)?.["value"]
    ?? asRecord(entries.find((entry) => entry["key"] === "cluster.upgrade_module_set_owners")?.["value"])[key];
  if (typeof id !== "string" || id.trim() === "") throw new ClusterConfigError(`${key} is required`);
  return id;
}

function maintenanceKeys(entries: Array<Record<string, unknown>>, moduleSet: string) {
  const id = maintenanceModuleId(entries, moduleSet);
  return { enabled: `${id}.maintenance.enabled`, message: `${id}.maintenance.message`, baseline: `${id}.maintenance.upgrade_baseline` };
}

const DRAIN_MESSAGE = "Scheduler upgrade in progress; job submission reopens when it completes.";
const DRAIN_POLL_MS = 60_000;
const DEFAULT_DRAIN_TIMEOUT_MINUTES = 240;

/**
 * The shell command the live inventory reader runs on the scheduler host: PBS's own view of its
 * jobs, reduced to a count per state so the answer fits in an SSM invocation's output.
 */
export const PBS_STATE_COUNT_COMMAND =
  "/opt/pbs/bin/qstat -f -F json | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); "
  + "print(json.dumps(collections.Counter(j.get(\"job_state\",\"?\") for j in d.get(\"Jobs\",{}).values())))'";

/** PBS job states to the inventory: Q queued, R and E running, F finished and ignored, anything else other. */
export function countPbsStates(states: Record<string, unknown>): SchedulerJobInventory {
  const inventory: SchedulerJobInventory = { queued: 0, running: 0, other: 0 };
  for (const [state, count] of Object.entries(states)) {
    const value = typeof count === "number" ? count : Number(count);
    if (!Number.isFinite(value)) continue;
    if (state === "Q") inventory.queued += value;
    else if (state === "R" || state === "E") inventory.running += value;
    else if (state !== "F") inventory.other += value;
  }
  return inventory;
}

function inventoryTotal(inventory: SchedulerJobInventory): number {
  return inventory.queued + inventory.running + inventory.other;
}

function describeInventory(inventory: SchedulerJobInventory): string {
  return `${inventory.queued} queued, ${inventory.running} running, ${inventory.other} other`;
}

async function restoreSubmission(deps: UpgradeDeps, options: UpgradeCommandOptions): Promise<void> {
  const current = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const keys = maintenanceKeys(current, options.moduleSet);
  const saved = current.find((entry) => entry["key"] === keys.baseline)?.["value"];
  if (saved === undefined) return;
  const baseline = JSON.parse(String(saved)) as { enabled: boolean; message: string };
  const writer = await deps.configWriter({ clusterName: options.clusterName, awsRegion: options.awsRegion, awsProfile: options.awsProfile });
  await writer.setConfigEntry(keys.message, baseline.message);
  await writer.setConfigEntry(keys.enabled, baseline.enabled);
  await writer.deleteConfigEntries(keys.baseline);
  deps.out(`scheduler cutover gate: previous submission maintenance state restored on ${options.clusterName}`);
}

// A queue observed empty can accept new jobs before the scheduler stack replaces its host.
// Closing submission first protects that interval, and a durable baseline survives failed runs.
async function schedulerCutoverGate(
  deps: UpgradeDeps,
  options: UpgradeCommandOptions,
  host: string,
): Promise<void> {
  const current = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const keys = maintenanceKeys(current, options.moduleSet);
  const prior = (key: string): unknown => current.find((entry) => entry["key"] === key)?.["value"];
  const writer = await deps.configWriter({ clusterName: options.clusterName, awsRegion: options.awsRegion, awsProfile: options.awsProfile });
  const existingBaseline = prior(keys.baseline);
  if (existingBaseline === undefined) {
    await writer.setConfigEntry(keys.baseline, JSON.stringify({
      enabled: prior(keys.enabled) === true,
      message: typeof prior(keys.message) === "string" ? prior(keys.message) : "",
    }));
  }
  await writer.setConfigEntry(keys.enabled, true);
  await writer.setConfigEntry(keys.message, DRAIN_MESSAGE);
  deps.out(`scheduler cutover gate: submission maintenance written on ${options.clusterName}`);
  if (options.skipDrainCheck === true) {
    deps.out(`scheduler cutover gate: inventory check skipped on request; submission stays closed for the run; jobs the host scheduler ${host} still holds will be lost`);
    return;
  }
  if (deps.schedulerJobs === undefined) {
    throw new GeneralException("scheduler cutover gate: this build cannot read the host scheduler's job inventory; drain by hand and re-run with --skip-drain-check");
  }
  const read = async (): Promise<SchedulerJobInventory> => {
    try {
      return await deps.schedulerJobs!.activeJobs({ awsRegion: options.awsRegion, instanceId: host });
    } catch (error) {
      throw new GeneralException(`scheduler cutover gate: could not read the job inventory on ${host}: ${(error as Error).message}. An unknown inventory is not an empty one; drain by hand and re-run with --skip-drain-check.`);
    }
  };
  const timeoutMs = (options.drainTimeoutMinutes ?? DEFAULT_DRAIN_TIMEOUT_MINUTES) * 60_000;
  const startedAt = deps.now();
  // Submission settings propagate asynchronously to the scheduler.
  // Let it observe maintenance before counting any inventory toward a safe cutover.
  await deps.sleep(30_000);
  let emptyReads = 0;
  for (;;) {
    const inventory = await read();
    emptyReads = inventoryTotal(inventory) === 0 ? emptyReads + 1 : 0;
    if (emptyReads === 2) {
      deps.out(`scheduler cutover gate: two empty inventories at least thirty seconds apart after maintenance write; continuing`);
      return;
    }
    if (inventoryTotal(inventory) > 0 && options.drain !== true) {
      if (existingBaseline === undefined) await restoreSubmission(deps, options);
      throw new ClusterConfigError(
        `scheduler cutover gate: the host scheduler ${host} holds ${describeInventory(inventory)} job(s). Re-run with --drain to close submission and wait, or drain by hand and re-run with --skip-drain-check.`,
      );
    }
    if (deps.now() - startedAt > timeoutMs) {
      throw new GeneralException(
        `scheduler cutover gate: ${describeInventory(inventory)} job(s) still on ${host} after ${Math.round(timeoutMs / 60_000)} minutes. Submission stays closed; re-run with --drain or --drain-timeout-minutes.`,
      );
    }
    deps.out(`scheduler cutover gate: ${describeInventory(inventory)}; waiting for settings propagation and stable empty inventory`);
    await deps.sleep(emptyReads > 0 ? 30_000 : DRAIN_POLL_MS);
  }
}

function includesModule(options: UpgradeCommandOptions, modules: ModuleInfo[], name: string): boolean {
  const requested = options.modules ?? [];
  return requested.length === 0 || requested.includes(modules.find((module) => module.name === name)?.module_id ?? name);
}

// Scheduler synthesis can switch to ECS even when capacity was deployed in an earlier run.
// Checking the host also avoids touching submission or DNS after that transition has completed.
async function pendingSchedulerCutover(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[], restoredEcs = false): Promise<string | undefined> {
  if (!includesModule(options, modules, "scheduler")) return;
  const valuesPath = valuesFilePath(options.clusterName, options.awsRegion);
  const enabledInValues = restoredEcs || existsSync(valuesPath) && (loadValuesFile(valuesPath) as Record<string, unknown>)["enable_ecs"] === true;
  const ecs = modules.find((module) => module.name === ECS_MODULE);
  if (!enabledInValues) {
    if (ecs === undefined) return;
    const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
    if (!settings.some((entry) => entry["key"] === `${ecs.module_id}.enabled` && entry["value"] === true)) return;
  }
  const scheduler = modules.find((module) => module.name === "scheduler");
  if (scheduler === undefined) return;
  const stackName = scheduler.stack_name ?? `${options.clusterName}-${scheduler.module_id}`;
  let nextToken: string | undefined;
  do {
    const page = await deps.cloudFormation.listStackResources({ awsRegion: options.awsRegion, stackName, nextToken });
    if (page.instanceIds.length > 0) return page.instanceIds[0];
    nextToken = page.nextToken;
  } while (nextToken !== undefined);
}

async function upgradeDeploysEcs(deps: UpgradeDeps, options: UpgradeCommandOptions, restoredEcs = false): Promise<boolean> {
  const requested = options.modules ?? [];
  if (requested.length > 0) return requested.includes(ECS_MODULE);
  if (restoredEcs) return true;
  // The migration that introduces the module has no table row for it yet: the values file is
  // what Phase 2b will register from, so it is read here too.
  const valuesPath = valuesFilePath(options.clusterName, options.awsRegion);
  if (existsSync(valuesPath) && (loadValuesFile(valuesPath) as Record<string, unknown>)["enable_ecs"] === true) return true;
  const modules = await clusterModules(deps, options.clusterName);
  return modules.some((module) => module.module_id === ECS_MODULE || module.name === ECS_MODULE);
}

/**
 * The deployed template with `DeletionPolicy: Retain` (and `UpdateReplacePolicy`) on every Route 53
 * record set that lacks it. Nothing else changes, so CloudFormation applies it as a policy-only
 * update: no resource is touched, no instance replaced. CDK templates are JSON; anything else is
 * left alone and reported as nothing to retain.
 */
export function retainRecordSets(templateBody: string | undefined): { body: string; parameterKeys: string[]; retained: string[] } {
  const none = { body: "", parameterKeys: [], retained: [] };
  if (templateBody === undefined) return none;
  let template: { Parameters?: Record<string, unknown>; Resources?: Record<string, { Type?: unknown; DeletionPolicy?: unknown; UpdateReplacePolicy?: unknown }> };
  try {
    template = JSON.parse(templateBody);
  } catch {
    return none;
  }
  const retained: string[] = [];
  for (const [logicalId, resource] of Object.entries(template.Resources ?? {})) {
    if (resource?.Type !== "AWS::Route53::RecordSet" || resource.DeletionPolicy === "Retain") continue;
    resource.DeletionPolicy = "Retain";
    resource.UpdateReplacePolicy = "Retain";
    retained.push(logicalId);
  }
  return { body: JSON.stringify(template), parameterKeys: Object.keys(template.Parameters ?? {}), retained };
}

/**
 * The scheduler's host DNS record is the name execution hosts and clients resolve, and the container
 * scheduler upserts that same name itself when it starts. The container cutover removes the
 * CloudFormation-managed record; with the deployed DeletionPolicy of Delete, CloudFormation would
 * delete the name, and fail doing so once the task has re-pointed it. So when this upgrade turns
 * containers on for a cluster whose scheduler still runs on a host, the deployed template is updated
 * first with Retain on that record and nothing else. A host-shape deploy from this release would do
 * the same and also replace the scheduler instance one last time; a policy-only update replaces nothing.
 */
async function retainSchedulerDnsRecord(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[]): Promise<void> {
  if (deps.cfn.getTemplate === undefined || deps.cfn.updateStack === undefined) return;
  const scheduler = modules.find((module) => module.name === "scheduler");
  if (scheduler === undefined) return;
  const stackName = scheduler.stack_name ?? `${options.clusterName}-${scheduler.module_id}`;
  const { body, parameterKeys, retained } = retainRecordSets(await deps.cfn.getTemplate(stackName));
  if (retained.length === 0) return;
  deps.out(`Phase 0: retain the scheduler's DNS record (${retained.join(", ")}) with a policy-only update of ${stackName} before the container cutover`);
  await deps.cfn.updateStack({ StackName: stackName, TemplateBody: body, ParameterKeys: parameterKeys });
  for (;;) {
    const stack = await deps.cfn.describeStack(stackName);
    const status = stack.StackStatus ?? "";
    if (status.endsWith("_IN_PROGRESS")) {
      await deps.sleep(10_000);
      continue;
    }
    if (status !== "UPDATE_COMPLETE") {
      throw new GeneralException(`Phase 0 did not complete: ${stackName} is ${status}. ${stack.StackStatusReason ?? ""}`.trim());
    }
    return;
  }
}

/**
 * The module-relative keys a deployed stack's settings resources publish, read from the deployed
 * template. A stack that stops publishing a key (the metrics stack drops its CloudWatch dashboard
 * once the provider is DogStatsD) deletes the row through its settings handler, and the read-back
 * must not demand it. Undefined when the template cannot be read or carries no settings resource,
 * in which case the caller falls back to requiring every previously published row.
 */
async function deployedPublishedKeys(deps: UpgradeDeps, stackName: string): Promise<Set<string> | undefined> {
  if (deps.cfn.getTemplate === undefined) return undefined;
  let template: string | undefined;
  try {
    template = await deps.cfn.getTemplate(stackName);
  } catch {
    return undefined;
  }
  if (template === undefined) return undefined;
  const resources = asRecord(asRecord(JSON.parse(template))["Resources"]);
  const keys = new Set<string>();
  let found = false;
  for (const resource of Object.values(resources)) {
    if (asRecord(resource)["Type"] !== "Custom::ClusterSettings") continue;
    found = true;
    for (const key of Object.keys(asRecord(asRecord(asRecord(resource)["Properties"])["settings"]))) keys.add(key);
  }
  return found ? keys : undefined;
}

// A successful stack operation does not prove that its settings publisher finished.
// Read back the intended writes and deployed versions before reopening submission.
async function verifyUpgradeCompletion(
  deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[], expected: Map<string, unknown>,
  drift: UpgradeDriftInput, historical: boolean, schedulerCutover: boolean,
): Promise<void> {
  const current = new Map((await scanAll(deps, `${options.clusterName}.cluster-settings`)).map((row) => [String(row["key"]), row["value"]]));
  const published = new Set(drift.current.filter((row) => row.source === "stack").map((row) => row.key));
  const checkpoints = new Set(modules.filter((module) => module.name === "cluster-manager").flatMap((module) =>
    ["metrics.cost.last_collected", "metrics.storage.last_collected", "metrics.storage.usage_snapshot", "accounts.reconcile.last_completed"].map((key) => `${module.module_id}.${key}`)));
  for (const [key, value] of expected) {
    // Application checkpoints can advance while deployment runs; configuration cannot.
    if (!published.has(key) && checkpoints.has(key)) continue;
    if (!published.has(key) && !isDeepStrictEqual(current.get(key), value)) throw new ClusterConfigError(`Completion verification failed for setting ${key}`);
  }
  for (const stack of drift.stacks ?? []) {
    if (!stack.selected) continue;
    const stackName = valueAsString(modules.find((module) => module.module_id === stack.moduleId)?.stack_name) || `${options.clusterName}-${stack.moduleId}`;
    const publishedNow = stack.target === undefined ? await deployedPublishedKeys(deps, stackName) : undefined;
    for (const [key, value] of Object.entries(stack.target ?? stack.previous)) {
      // The container scheduler no longer publishes its retired host identity.
      if (schedulerCutover && stack.target === undefined && modules.some((module) => module.module_id === stack.moduleId && module.name === "scheduler") && ["instance_id", "private_ip"].includes(key)) continue;
      const fullKey = `${stack.moduleId}.${key}`;
      // The bastion keeps its DNS record and module stack during cutover. Its instance rows
      // disappear through the settings delta, so the deployed template is the completion contract.
      if (publishedNow !== undefined && !publishedNow.has(key)) {
        deps.out(`${fullKey} is no longer published by the deployed ${stackName} stack`);
        continue;
      }
      if (!current.has(fullKey) || (stack.target !== undefined && !isDeepStrictEqual(current.get(fullKey), value))) {
        throw new ClusterConfigError(`Completion verification failed for published setting ${fullKey}`);
      }
    }
  }
  if (drift.replaceGlobalSettings !== false) {
    for (const key of current.keys()) if (key.startsWith("global-settings.") && !expected.has(key)) throw new ClusterConfigError(`Completion verification failed for removed setting ${key}`);
  }
  const after = await clusterModules(deps, options.clusterName);
  for (const module of modules.filter((entry) => entry.type !== "config")) {
    if (options.modules?.length && !options.modules.includes(module.module_id)) continue;
    const updated = after.find((row) => row.module_id === module.module_id);
    if (updated?.status !== "deployed" || updated.version !== ideaVersion()) throw new ClusterConfigError(`Completion verification failed for module ${module.module_id}`);
    if (historical && module.status === "deployed" && module.name === "virtual-desktop-controller") {
      const roleKey = `${module.module_id}.dcv_host_role_name`;
      const role = current.get(roleKey);
      const arn = current.get(`${module.module_id}.dcv_host_policy_arn`);
      if (role !== expected.get(roleKey) || typeof role !== "string" || typeof arn !== "string" || !arn.includes(":policy/")) throw new ClusterConfigError(`Completion verification failed for DCV policy publication: ${module.module_id}`);
      for (const suffix of ["dcv_host_role_arn", "dcv_host_role_id"]) {
        const key = `${module.module_id}.${suffix}`;
        const previous = drift.current.find((row) => row.key === key);
        if (previous !== undefined && current.get(key) !== previous.value) throw new ClusterConfigError(`Completion verification failed for DCV identity: ${key}`);
      }
      const inventory = await deps.historicalIam!(role, `${options.clusterName}-${options.awsRegion}-${module.module_id}-host`, options);
      if (!inventory.attached.includes(arn.split("/").at(-1)!) || inventory.inline.length > 0) throw new ClusterConfigError(`Completion verification failed for DCV policy attachment: ${module.module_id}`);
    }
  }
  deps.out("Completion verified: settings and deployed module versions read back");
}

// Missing local values must not hide a requested container transition from its gates.
// Parse the remote copy in scratch space while the cluster and local values remain unchanged.
async function historicalValuesEnableEcs(deps: UpgradeDeps, options: UpgradeCommandOptions): Promise<boolean> {
  const path = valuesFilePath(options.clusterName, options.awsRegion);
  if (existsSync(path)) return loadValuesFile(path)["enable_ecs"] === true;
  const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const bucket = settings.find((row) => row["key"] === "cluster.cluster_s3_bucket")?.["value"];
  if (typeof bucket !== "string" || bucket === "") throw new ClusterConfigError("cluster.cluster_s3_bucket is required to plan missing values.yml");
  const root = mkdtempSync(join(tmpdir(), "upgrade-values-plan-"));
  try {
    const scratch = join(root, "values.yml");
    writeFileSync(scratch, await deps.s3.getObject({ Bucket: bucket, Key: VALUES_FILE_S3_KEY }));
    return loadValuesFile(scratch)["enable_ecs"] === true;
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

// Historical changes touch every deployed service and need a complete baseline.
// Refuse missing inventory and unreadable dependencies before maintenance or settings writes.
export async function planHistoricalUpgrade(deps: UpgradeDeps, options: UpgradeCommandOptions, modules: ModuleInfo[], registersEcs = false): Promise<void> {
  const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
  const ids = new Set(modules.map((module) => module.module_id));
  // A private registry (GovCloud has no public ECR) is set as ecs.image before the run that registers the module.
  if (registersEcs) ids.add(ECS_MODULE);
  for (const row of settings) {
    const key = valueAsString(row["key"]);
    const owner = key.split(".")[0]!;
    if (!ids.has(owner)) throw new ClusterConfigError(`Missing module row for ${owner}`);
    if (key.startsWith("global-settings.module_sets.") && key.endsWith(".module_id") && !ids.has(valueAsString(row["value"]))) {
      throw new ClusterConfigError(`Missing module row for ${String(row["value"])}`);
    }
  }
  if (options.modules?.length && modules.some((module) => module.type !== "config" && module.status === "deployed" && !options.modules?.includes(module.module_id))) {
    throw new ClusterConfigError("Historical migration requires every deployed module; omit the module selection");
  }
  if (deps.cfn.getTemplate === undefined) throw new ClusterConfigError("Historical migration requires deployed templates");
  for (const module of modules.filter((entry) => entry.type !== "config" && entry.status === "deployed")) {
    const template = await deps.cfn.getTemplate(module.stack_name ?? `${options.clusterName}-${module.module_id}`);
    if (template === undefined || asRecord(JSON.parse(template))["Resources"] === undefined) throw new ClusterConfigError(`Missing deployed template for ${module.module_id}`);
    if (module.name === "scheduler") await scanAll(deps, `${options.clusterName}.${module.module_id}.queue-profiles`);
    if (module.name === "virtual-desktop-controller") {
      await scanAll(deps, `${options.clusterName}.${module.module_id}.controller.software-stacks`);
      await scanAll(deps, `${options.clusterName}.${module.module_id}.controller.user-sessions`);
      const role = settings.find((row) => row["key"] === `${module.module_id}.dcv_host_role_name`)?.["value"];
      if (typeof role !== "string" || deps.historicalIam === undefined) throw new ClusterConfigError(`Missing DCV IAM inventory for ${module.module_id}`);
      const policy = `${options.clusterName}-${options.awsRegion}-${module.module_id}-host`;
      const resources = asRecord(asRecord(JSON.parse(template))["Resources"]);
      const ownsPolicy = Object.values(resources).some((resource) => asRecord(resource)["Type"] === "AWS::IAM::ManagedPolicy" && asRecord(asRecord(resource)["Properties"])["ManagedPolicyName"] === policy);
      const inventory = await deps.historicalIam(role, policy, options, ownsPolicy);
      if (inventory.collision || inventory.available < 1) throw new ClusterConfigError(`DCV managed-policy collision or quota exhausted for ${module.module_id}`);
    }
  }
  // Only existing compute images affect preservation; host images are superseded.
  const computeKeys = new Set(modules.filter((module) => module.name === "scheduler").map((module) => `${module.module_id}.compute_node_ami`));
  const imageIds = [...new Set(settings.flatMap((row) => computeKeys.has(String(row["key"])) && typeof row["value"] === "string" && row["value"] !== "" ? [row["value"]] : []))];
  if (imageIds.length > 0) {
    const images = await deps.ec2.describeImages({ awsRegion: options.awsRegion, imageIds });
    for (const id of imageIds) if (!images.some((image) => image.ImageId === id)) throw new ClusterConfigError(`Missing AMI metadata for ${id}`);
  }
  for (const instance of await moduleInstances(deps, options)) {
    await deps.ec2.describeInstanceAttribute({ awsRegion: options.awsRegion, instanceId: instance.instanceId });
  }
}

/** Execute Phases 1 through 4 after every pre-flight refusal has passed. */
export async function upgradeCluster(deps: UpgradeDeps, options: UpgradeCommandOptions): Promise<void> {
  // Version validation must precede even the maintenance gate.
  // An incomplete inventory cannot establish a safe starting release.
  const modulesBefore = await clusterModules(deps, options.clusterName);
  if (modulesBefore.length === 0) throw new ClusterConfigError("Missing module rows");
  const floorRefusal = upgradeFloorRefusal(options.clusterName, modulesBefore);
  if (floorRefusal !== undefined) throw new ClusterConfigError(floorRefusal);
  const pendingHistorical = (await scanAll(deps, `${options.clusterName}.cluster-settings`)).some((row) => row["key"] === "cluster.upgrade_historical_pending" && row["value"] === true);
  const historical = pendingHistorical || modulesBefore.some((module) => module.type !== "config" && module.status === "deployed" && (compareIdeaRelease(String(module.version), "26.09.0") ?? 0) < 0);
  const restoredEcs = historical && await historicalValuesEnableEcs(deps, options);
  if (historical) {
    await planHistoricalUpgrade(deps, options, modulesBefore, restoredEcs);
    options = { ...options, skipGlobalSettingsUpdate: false };
    deps.out("Historical migration: global replacement, full configuration sync, AMI/settings updates and deployment of every deployed module are required; phase skip flags and prompts cannot omit them.");
  }
  let cleared: ClearedInstance[] = [];
  try {
    if (await upgradeDeploysEcs(deps, options, restoredEcs)) await checkAwsvpcTrunking(deps, options);
    const cutoverHost = await pendingSchedulerCutover(deps, options, modulesBefore, restoredEcs);
    const baseOs = await resolveUpgradeBaseOs(deps, options);
    await validateBaseOs(deps, options, baseOs);
    const eolPlans = await checkEolBaseOs(deps, options);
    const allModules = options.modules === undefined || options.modules.length === 0;
    deps.out(allModules ? "No modules specified, upgrading all modules" : `Upgrade scope: Specific modules - ${options.modules?.join(", ")}`);
    const driftInput = await prepareUpgradeDriftInput(deps, { ...options, baseOs });
    if (historical) {
      const stock = resolveRegionAmi((deps.regionAmiConfig ?? loadRegionAmiConfig)(), options.awsRegion, baseOs);
      const images = await deps.ec2.describeImages({ awsRegion: options.awsRegion, imageIds: [stock] });
      if (!images.some((image) => image.ImageId === stock)) throw new ClusterConfigError(`Missing release AMI metadata for ${stock}`);
    }
    const driftReport = compareUpgradeDrift(driftInput);
    deps.out(renderUpgradeDrift(driftReport));
    await confirmConfigDrift(deps, options, driftReport);
    const expected = new Map(driftInput.current.map((row) => [row.key, row.value]));
    const expectedModules = new Map(modulesBefore.map((module) => [module.module_id, module]));
    const originalDeps = deps;
    deps = Object.create(deps) as UpgradeDeps;
    deps.configWriter = async (input) => {
      const writer = await originalDeps.configWriter(input);
      return {
        async syncModulesInDb(modules) {
          for (const module of modules) if (!expectedModules.has(module.id)) expectedModules.set(module.id, { module_id: module.id, name: module.name, type: module.type });
          await writer.syncModulesInDb(modules);
        },
        async syncClusterSettingsInDb(entries, overwrite) {
          for (const entry of entries) if (overwrite || !expected.has(entry.key)) expected.set(entry.key, entry.value);
          await writer.syncClusterSettingsInDb(entries, overwrite);
        },
        async setConfigEntry(key, value) { expected.set(key, value); await writer.setConfigEntry(key, value); },
        async deleteConfigEntries(prefix) {
          for (const key of expected.keys()) if (key.startsWith(prefix)) expected.delete(key);
          await writer.deleteConfigEntries(prefix);
        },
      };
    };
    if (historical && !options.force && allModules) {
      const confirm = await deps.prompt({ message: "Proceed with deploying all modules?", default: true });
      if (confirm !== true && confirm !== "Yes") throw new ExitWithCode(0);
    }
    // Module versions advance before completion, so retries need a durable migration marker.
    if (historical) await (await deps.configWriter(options)).setConfigEntry("cluster.upgrade_historical_pending", true);
    if (cutoverHost !== undefined) await schedulerCutoverGate(deps, options, cutoverHost);
    await applyEolSoftwareStacks(deps, options.awsRegion, eolPlans);
    if (cutoverHost !== undefined) await retainSchedulerDnsRecord(deps, options, modulesBefore);

    deps.out("Phase 1: Update Base OS in values.yml");
    resolveRegionAmi((deps.regionAmiConfig ?? loadRegionAmiConfig)(), options.awsRegion, baseOs);
    await updateValuesBaseOs(deps, options, baseOs);

    let configDir = join(valuesFilePath(options.clusterName, options.awsRegion), "..", "config");
    if (options.skipGlobalSettingsUpdate !== true) {
      if (!historical && options.force !== true) {
        const confirm = await deps.prompt({ message: "Continue with global settings backup and update?", default: true });
        if (confirm !== true && confirm !== "Yes") throw new ExitWithCode(0);
      }
      deps.out("Phase 2: Global Settings Backup and Update");
      configDir = await backupAndUpdateGlobalSettings(deps, options, modulesBefore);
    }

    await migrateReconcilerIntervals(deps, options, modulesBefore);
    let syncFullConfig = historical || options.force === true;
    if (!historical && options.force !== true) {
      const confirm = await deps.prompt({ message: "Sync full configuration to add new values?", default: true });
      syncFullConfig = confirm === true || confirm === "Yes";
    }
    if (syncFullConfig) {
      if (options.skipGlobalSettingsUpdate === true) {
        generateConfigFromTemplates(loadValuesFile(valuesFilePath(options.clusterName, options.awsRegion)), configDir);
      }
      deps.out("Phase 2b: Sync full configuration without overwrite");
      await syncFullConfiguration(deps, options, configDir, modulesBefore);
    }

    deps.out("Phase 3: Update AMI IDs and Settings");
    let updateAmis = historical || options.force === true;
    if (!historical && options.force !== true) {
      const confirm = await deps.prompt({ message: "Continue with AMI and settings updates?", default: true });
      updateAmis = confirm === true || confirm === "Yes";
    }
    if (updateAmis) {
      const writer = await deps.configWriter({ clusterName: options.clusterName, awsRegion: options.awsRegion, awsProfile: options.awsProfile });
      await applyPhase3Entries(writer, [...(driftInput.phase3 ?? []), ...(driftInput.providerCutover ?? [])], driftInput.current, deps.out);
    }

    deps.out("Phase 4: Module Deployment");
    if (!historical && !options.force && allModules) {
      const confirm = await deps.prompt({ message: "Proceed with deploying all modules?", default: true });
      if (confirm !== true && confirm !== "Yes") throw new ExitWithCode(0);
    }
    try {
      cleared = await clearTerminationProtection(deps, options.awsRegion, await moduleInstances(deps, options));
    } catch (error) {
      deps.out(`warning: pre-upgrade termination-protection sweep failed: ${(error as Error).message}. Verify replaced instances are terminated after the upgrade.`);
    }
    const deployment: UpgradeDeploymentOptions = {
      clusterName: options.clusterName,
      awsRegion: options.awsRegion,
      awsProfile: options.awsProfile,
      terminationProtection: asBoolFlag(options.terminationProtection, true),
      deploymentId: options.deploymentId,
      forceBuildBootstrap: options.forceBuildBootstrap === true,
      rollback: options.rollback !== false,
      optimizeDeployment: options.optimizeDeployment === true,
      moduleSet: options.moduleSet,
      allModules,
      moduleIds: allModules ? undefined : options.modules,
      allowReplacement: options.allowReplacement,
    };
    await deps.deploy(deployment);
    await announceHeldModuleSets(deps, options, configDir);
    if (options.skipGlobalSettingsUpdate !== true) await removeObsoleteGlobalSettings(deps, options, configDir);
    let restore = cleared;
    try {
      restore = [...new Map([...cleared, ...await moduleInstances(deps, options)].map((instance) => [instance.instanceId, instance])).values()];
    } catch (error) {
      deps.out(`warning: could not enumerate current instances for protection restoration: ${(error as Error).message}`);
    }
    try {
      await restoreTerminationProtection(deps, options.awsRegion, restore, cleared);
    } catch (error) {
      deps.out(`warning: could not restore termination protection: ${(error as Error).message}`);
      await warnClearedProtection(deps, options.awsRegion, cleared);
    }
    await verifyUpgradeCompletion(deps, options, [...expectedModules.values()], expected, driftInput, historical, cutoverHost !== undefined);
    await saveValuesFile(deps, options);
    await restoreSubmission(deps, options);
    if (!historical && options.keepBorrowedHosts !== true) {
      try {
        await returnBorrowedHosts(deps, options);
      } catch (error) {
        deps.out(`warning: could not return a borrowed host: ${(error as Error).message}. Run return-hosts later.`);
      }
    }
    await (await deps.configWriter(options)).deleteConfigEntries("cluster.upgrade_module_set_owners");
    if (historical) await (await deps.configWriter(options)).deleteConfigEntries("cluster.upgrade_historical_pending");
    deps.out("All upgrade phases completed successfully");
  } catch (error) {
    await warnClearedProtection(deps, options.awsRegion, cleared);
    const settings = await scanAll(deps, `${options.clusterName}.cluster-settings`);
    if (settings.some((entry) => entry["key"] === maintenanceKeys(settings, options.moduleSet).baseline)) {
      deps.out(`warning: job submission is still closed on ${options.clusterName} (${maintenanceKeys(settings, options.moduleSet).enabled}); a completed re-run reopens it.`);
    }
    throw error;
  }
}

/** Register the command group. The caller supplies all replayable external effects. */
export function registerUpgradeCommands(program: Command, deps: UpgradeDeps): void {
  program
    .command("upgrade-cluster")
    .description("upgrade an existing cluster")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .option("--termination-protection <termination-protection>", "Set termination protection to true or false. Default: true", "true")
    .option("--keep-borrowed-hosts", "Leave a container host the upgrade added beyond the host group minimum in service.")
    .option("--deployment-id <deployment-id>", "A UUID to identify the deployment.")
    .option("--base-os <base-os>", "Base OS to upgrade to.")
    .option("--force-build-bootstrap", "Render bootstrap packages again.")
    .option("--rollback", "Rollback stack to stable state on failure. Default.", true)
    .option("--no-rollback", "Do not roll back on failure.")
    .option("--optimize-deployment", "Deploy applicable stacks in parallel.")
    .option("--module-set <module-set>", "Name of the ModuleSet. Default: default", "default")
    .option("--force", "Skip all confirmation prompts.")
    .option(
      "--accept-config-drift",
      "Overwrite configuration rows whose value differs from generated configuration. Not covered by --force.",
    )
    .option(
      "--allow-replacement <logical-id>",
      "Accept a change-set entry the deploy guard would refuse, by logical ID. Repeatable.",
      (value: string, previous: string[] = []) => [...previous, value],
    )
    .option("--skip-global-settings-update", "Skip updating global settings.")
    .option("--disable-eol-stacks-in-use", "Disable end-of-life eVDI software stacks that are in use.")
    .option("--drain", "Before the host-to-container scheduler cutover, close job submission and wait for the host scheduler to empty.")
    .option("--drain-timeout-minutes <minutes>", `Give up waiting for the drain after this long. Default: ${DEFAULT_DRAIN_TIMEOUT_MINUTES}`, (value: string) => Number(value))
    .option("--skip-drain-check", "Skip the host scheduler's job inventory check but close submission for the run; jobs it still holds are lost.")
    .argument("[modules...]", "module ids")
    .action(async (modules: string[], commandOptions: UpgradeCommandOptions) => {
      await upgradeCluster(deps, { ...commandOptions, modules });
    });
  program
    .command("return-hosts")
    .description("drain and remove a container host the host group holds beyond its minimum")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .action(async (options: Pick<UpgradeCommandOptions, "clusterName" | "awsRegion" | "awsProfile">) => {
      await returnBorrowedHosts(deps, options);
    });
}

/**
 * Attach live SDK implementations to the command-core dependencies. Imports
 * occur only when an upgrade operation reaches the corresponding phase.
 */
/**
 * The live account-settings reader behind the container pre-flight. Deploy, quick-setup and
 * upgrade all run that pre-flight, so they all need it: without it the check cannot read the
 * account and refuses every container deployment.
 */
export function liveEcsAccountSettings(): EcsAccountSettingsApi {
  return {
    async listAccountSettings(input) {
      const { ECSClient, ListAccountSettingsCommand } = await import("@aws-sdk/client-ecs");
      const client = new ECSClient(await upgradeLiveClientOptions(input.awsRegion, input.awsProfile));
      const result = await client.send(
        new ListAccountSettingsCommand({ effectiveSettings: input.effectiveSettings, name: input.name }),
      );
      return (result.settings ?? []).flatMap((setting) =>
        setting.name === undefined || setting.value === undefined
          ? []
          : [{ name: setting.name.toString(), value: setting.value }],
      );
    },
  };
}

/** The live inventory reader: one SSM shell command on the scheduler host, polled to completion. */
export function liveSchedulerJobs(sleep: (ms: number) => Promise<void>): SchedulerJobsApi {
  return {
    async activeJobs(input) {
      const { GetCommandInvocationCommand, SSMClient, SendCommandCommand } = await import("@aws-sdk/client-ssm");
      const client = new SSMClient(await awsClientOptions(input.awsRegion));
      const sent = await client.send(
        new SendCommandCommand({
          InstanceIds: [input.instanceId],
          DocumentName: "AWS-RunShellScript",
          Parameters: { commands: [PBS_STATE_COUNT_COMMAND] },
        }),
      );
      const commandId = sent.Command?.CommandId;
      if (commandId === undefined) throw new GeneralException("ssm:SendCommand returned no command id");
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await sleep(2_000);
        let result;
        try {
          result = await client.send(new GetCommandInvocationCommand({ CommandId: commandId, InstanceId: input.instanceId }));
        } catch (error) {
          // The invocation record trails the send by a moment.
          if ((error as { name?: string }).name === "InvocationDoesNotExist") continue;
          throw error;
        }
        const status = result.Status ?? "";
        if (status === "Pending" || status === "InProgress" || status === "Delayed") continue;
        if (status !== "Success") {
          throw new GeneralException(`qstat on ${input.instanceId} ended ${status}: ${(result.StandardErrorContent ?? "").trim().slice(0, 300)}`);
        }
        return countPbsStates(JSON.parse((result.StandardOutputContent ?? "{}").trim() || "{}") as Record<string, unknown>);
      }
      throw new GeneralException(`qstat on ${input.instanceId} did not finish within 60 s`);
    },
  };
}

export function createLiveUpgradeDeps(deps: Deps): UpgradeDeps {
  const ecsAccountSettings = liveEcsAccountSettings();
  const containerHosts: ContainerHostsApi = {
    async hostGroup(input) {
      const { DescribeCapacityProvidersCommand, ECSClient } = await import("@aws-sdk/client-ecs");
      const { AutoScalingClient, DescribeAutoScalingGroupsCommand } = await import("@aws-sdk/client-auto-scaling");
      const providers = await new ECSClient(await awsClientOptions(input.awsRegion)).send(
        new DescribeCapacityProvidersCommand({ capacityProviders: [input.capacityProvider] }),
      );
      const arn = providers.capacityProviders?.[0]?.autoScalingGroupProvider?.autoScalingGroupArn;
      if (arn === undefined) throw new GeneralException(`capacity provider ${input.capacityProvider} has no host group`);
      const name = arn.slice(arn.lastIndexOf("/") + 1);
      const groups = await new AutoScalingClient(await awsClientOptions(input.awsRegion)).send(
        new DescribeAutoScalingGroupsCommand({ AutoScalingGroupNames: [name] }),
      );
      const group = groups.AutoScalingGroups?.[0];
      if (group === undefined) throw new GeneralException(`host group ${name} was not found`);
      return {
        name,
        minSize: group.MinSize ?? 0,
        desiredCapacity: group.DesiredCapacity ?? 0,
        instanceIds: (group.Instances ?? []).filter((i) => i.LifecycleState === "InService").map((i) => i.InstanceId ?? "").filter(Boolean),
      };
    },
    async containerInstances(input) {
      const { DescribeContainerInstancesCommand, ECSClient, ListContainerInstancesCommand } = await import("@aws-sdk/client-ecs");
      const client = new ECSClient(await awsClientOptions(input.awsRegion));
      const arns = (await client.send(new ListContainerInstancesCommand({ cluster: input.cluster }))).containerInstanceArns ?? [];
      if (arns.length === 0) return [];
      const described = await client.send(new DescribeContainerInstancesCommand({ cluster: input.cluster, containerInstances: arns }));
      return (described.containerInstances ?? []).map((host) => ({
        arn: host.containerInstanceArn ?? "",
        instanceId: host.ec2InstanceId ?? "",
        status: host.status ?? "",
        runningTasks: host.runningTasksCount ?? 0,
        registeredAt: host.registeredAt?.toISOString(),
      }));
    },
    async drain(input) {
      const { ECSClient, UpdateContainerInstancesStateCommand } = await import("@aws-sdk/client-ecs");
      await new ECSClient(await awsClientOptions(input.awsRegion)).send(
        new UpdateContainerInstancesStateCommand({ cluster: input.cluster, containerInstances: [input.arn], status: "DRAINING" }),
      );
    },
    async activate(input) {
      const { ECSClient, UpdateContainerInstancesStateCommand } = await import("@aws-sdk/client-ecs");
      await new ECSClient(await awsClientOptions(input.awsRegion)).send(
        new UpdateContainerInstancesStateCommand({ cluster: input.cluster, containerInstances: [input.arn], status: "ACTIVE" }),
      );
    },
    async waitUntilEmpty(input) {
      const { DescribeContainerInstancesCommand, ECSClient } = await import("@aws-sdk/client-ecs");
      const client = new ECSClient(await awsClientOptions(input.awsRegion));
      const deadline = Date.now() + input.timeoutMs;
      do {
        const described = await client.send(new DescribeContainerInstancesCommand({ cluster: input.cluster, containerInstances: [input.arn] }));
        const host = described.containerInstances?.[0];
        if ((host?.runningTasksCount ?? 0) === 0 && (host?.pendingTasksCount ?? 0) === 0) return true;
        await new Promise((resolve) => setTimeout(resolve, 15_000));
      } while (Date.now() < deadline);
      return false;
    },
    async release(input) {
      const { AutoScalingClient, SetDesiredCapacityCommand, SetInstanceProtectionCommand } = await import("@aws-sdk/client-auto-scaling");
      const client = new AutoScalingClient(await awsClientOptions(input.awsRegion));
      await client.send(new SetInstanceProtectionCommand({ AutoScalingGroupName: input.name, InstanceIds: [input.instanceId], ProtectedFromScaleIn: false }));
      await client.send(new SetDesiredCapacityCommand({ AutoScalingGroupName: input.name, DesiredCapacity: input.desiredCapacity, HonorCooldown: false }));
    },
  };
  const ec2: UpgradeEc2Api = {
    async describeImages(input) {
      const { DescribeImagesCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      const result = await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new DescribeImagesCommand({ ImageIds: input.imageIds }),
      );
      return (result.Images ?? []).map((image) => ({
        ImageId: image.ImageId,
        Name: image.Name,
        CreationDate: image.CreationDate,
      }));
    },
    async describeInstanceTypeOfferings(input) {
      const { DescribeInstanceTypeOfferingsCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      const result = await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new DescribeInstanceTypeOfferingsCommand({
          LocationType: "region",
          Filters: [{ Name: "instance-type", Values: [input.instanceType] }],
        }),
      );
      return (result.InstanceTypeOfferings ?? []).flatMap((offering) =>
        offering.InstanceType === undefined ? [] : [offering.InstanceType.toString()],
      );
    },
    async describeInstanceAttribute(input) {
      const { DescribeInstanceAttributeCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      const result = await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new DescribeInstanceAttributeCommand({ InstanceId: input.instanceId, Attribute: "disableApiTermination" }),
      );
      return result.DisableApiTermination?.Value === true;
    },
    async modifyInstanceAttribute(input) {
      const { EC2Client, ModifyInstanceAttributeCommand } = await import("@aws-sdk/client-ec2");
      await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new ModifyInstanceAttributeCommand({
          InstanceId: input.instanceId,
          DisableApiTermination: { Value: input.protected },
        }),
      );
    },
    async createTags(input) {
      const { CreateTagsCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new CreateTagsCommand({ Resources: [input.instanceId], Tags: [{ Key: TERMINATION_PROTECTION_TAG, Value: input.value }] }),
      );
    },
    async deleteTags(input) {
      const { DeleteTagsCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      await new EC2Client(await awsClientOptions(input.awsRegion)).send(
        new DeleteTagsCommand({ Resources: [input.instanceId], Tags: [{ Key: TERMINATION_PROTECTION_TAG }] }),
      );
    },
    async describeLiveInstances(input) {
      const { DescribeInstancesCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      const client = new EC2Client(await awsClientOptions(input.awsRegion));
      const ids: string[] = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(new DescribeInstancesCommand({
          Filters: [
            { Name: "instance-id", Values: input.instanceIds },
            { Name: "instance-state-name", Values: ["pending", "running", "stopping", "stopped"] },
            ...(input.tagKey === undefined ? [] : [{ Name: "tag-key", Values: [input.tagKey] }]),
          ],
          NextToken: nextToken,
        }));
        ids.push(...(result.Reservations ?? []).flatMap((reservation) => reservation.Instances ?? [])
          .flatMap((instance) => instance.InstanceId === undefined ? [] : [instance.InstanceId]));
        nextToken = result.NextToken;
      } while (nextToken);
      return ids;
    },
  };

  return {
    ...deps,
    ec2,
    ecsAccountSettings,
    containerHosts,
    schedulerJobs: liveSchedulerJobs(deps.sleep),
    async historicalIam(roleName, policyName, options, ownsPolicy) {
      const { IAMClient, GetAccountSummaryCommand, ListAttachedRolePoliciesCommand, ListRolePoliciesCommand, ListPoliciesCommand } = await import("@aws-sdk/client-iam");
      const client = new IAMClient(await awsClientOptions(options.awsRegion, options.awsProfile));
      const summary = await client.send(new GetAccountSummaryCommand({}));
      const attached: string[] = [];
      const inline: string[] = [];
      let marker: string | undefined;
      do {
        const page: import("@aws-sdk/client-iam").ListAttachedRolePoliciesCommandOutput = await client.send(new ListAttachedRolePoliciesCommand({ RoleName: roleName, Marker: marker }));
        attached.push(...(page.AttachedPolicies ?? []).flatMap((policy) => policy.PolicyName === undefined ? [] : [policy.PolicyName]));
        marker = page.IsTruncated ? page.Marker : undefined;
      } while (marker !== undefined);
      do {
        const page: import("@aws-sdk/client-iam").ListRolePoliciesCommandOutput = await client.send(new ListRolePoliciesCommand({ RoleName: roleName, Marker: marker }));
        inline.push(...(page.PolicyNames ?? []));
        marker = page.IsTruncated ? page.Marker : undefined;
      } while (marker !== undefined);
      let collision = false;
      do {
        const page: import("@aws-sdk/client-iam").ListPoliciesCommandOutput = await client.send(new ListPoliciesCommand({ Scope: "Local", Marker: marker }));
        collision ||= (page.Policies ?? []).some((policy) => policy.PolicyName === policyName) && ownsPolicy !== true;
        marker = page.IsTruncated ? page.Marker : undefined;
      } while (marker !== undefined);
      const available = attached.includes(policyName) ? 1 : Math.min(
        (summary.SummaryMap?.PoliciesQuota ?? 0) - (summary.SummaryMap?.Policies ?? 0),
        (summary.SummaryMap?.AttachedPoliciesPerRoleQuota ?? 10) - attached.length,
      );
      return { attached, inline, collision, available };
    },
    cloudFormation: {
      async listStackResources(input) {
        const { CloudFormationClient, ListStackResourcesCommand } = await import("@aws-sdk/client-cloudformation");
        const result = await new CloudFormationClient(await awsClientOptions(input.awsRegion)).send(
          new ListStackResourcesCommand({ StackName: input.stackName, NextToken: input.nextToken }),
        );
        return {
          instanceIds: (result.StackResourceSummaries ?? [])
            .filter((resource) => resource.ResourceType === "AWS::EC2::Instance")
            .map((resource) => resource.PhysicalResourceId)
            .filter((instanceId): instanceId is string => instanceId !== undefined),
          nextToken: result.NextToken,
        };
      },
    },
    openSearch: {
      async describeDomain(input) {
        const { DescribeDomainCommand, OpenSearchClient } = await import("@aws-sdk/client-opensearch");
        const result = await new OpenSearchClient(await awsClientOptions(input.awsRegion)).send(
          new DescribeDomainCommand({ DomainName: input.domainName }),
        );
        return { engineVersion: result.DomainStatus?.EngineVersion };
      },
      async listInstanceTypeDetails(input) {
        const { ListInstanceTypeDetailsCommand, OpenSearchClient } = await import("@aws-sdk/client-opensearch");
        const result = await new OpenSearchClient(await awsClientOptions(input.awsRegion)).send(
          new ListInstanceTypeDetailsCommand({ EngineVersion: input.engineVersion }),
        );
        return (result.InstanceTypeDetails ?? []).flatMap((detail) =>
          detail.InstanceType === undefined ? [] : [detail.InstanceType.toString()],
        );
      },
    },
    eolSoftwareStacks: {
      async setEnabled(input) {
        const { DynamoDBClient } = await import("@aws-sdk/client-dynamodb");
        const { DynamoDBDocumentClient, UpdateCommand } = await import("@aws-sdk/lib-dynamodb");
        const client = DynamoDBDocumentClient.from(new DynamoDBClient(await awsClientOptions(input.awsRegion)));
        await client.send(
          new UpdateCommand({
            TableName: input.tableName,
            Key: { base_os: input.baseOs, stack_id: input.stackId },
            UpdateExpression: "SET #enabled = :enabled",
            ExpressionAttributeNames: { "#enabled": "enabled" },
            ExpressionAttributeValues: { ":enabled": input.enabled },
          }),
        );
      },
      async delete(input) {
        const { DynamoDBClient } = await import("@aws-sdk/client-dynamodb");
        const { DeleteCommand, DynamoDBDocumentClient } = await import("@aws-sdk/lib-dynamodb");
        const client = DynamoDBDocumentClient.from(new DynamoDBClient(await awsClientOptions(input.awsRegion)));
        await client.send(
          new DeleteCommand({
            TableName: input.tableName,
            Key: { base_os: input.baseOs, stack_id: input.stackId },
          }),
        );
      },
    },
    deploy: (options) => defaultDeployment(deps, options),
  };
}

/** Build the standard deployment adapter for callers that have no special deployment hook. */
export function withDefaultUpgradeDeployment(deps: Omit<UpgradeDeps, "deploy">): UpgradeDeps {
  // Layered rather than copied: a copy drops the prototype methods of a class based deps object
  // and hides anything the caller replaces after this returns.
  return new Proxy(deps, {
    get: (target, property, receiver) =>
      property === "deploy"
        ? (options: UpgradeDeploymentOptions) => defaultDeployment(deps, options)
        : Reflect.get(target, property, receiver),
  }) as UpgradeDeps;
}
