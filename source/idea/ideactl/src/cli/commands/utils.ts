/** Utility operator commands for service discovery and managed prefix lists. */

import { cpSync, existsSync, rmSync } from "node:fs";

import { Command } from "commander";

import { ClusterConfig, ClusterConfigError, isEmpty } from "../../config/cluster-config.ts";
import { convertConfigToKeyValuePairs, generateConfigFromTemplates } from "../../config/generator.ts";
import { loadValuesFile } from "../../config/values.ts";
import { clusterConfigDir, valuesFilePath } from "../cdk-invoker.ts";
import { renderTable } from "./config.ts";
import { registerDirectoryServiceCommands, type DirectoryServiceDeps, type DirectoryServiceDepsFactory } from "./directoryservice.ts";
import { registerSharedStorageCommands, type SharedStorageDeps, type SharedStorageDepsFactory } from "./shared-storage.ts";
import { registerSsoCommands, type SsoDeps, type SsoDepsFactory } from "./sso.ts";
import { registerSupportCommands, type SupportDeps, type SupportDepsFactory } from "./support.ts";
import { registerIntegrationTestCommands, type IntegrationTestDeps, type IntegrationTestDepsFactory } from "./tests.ts";

export interface ServiceInfo {
  title: string;
  required: boolean;
}

export const IDEA_SERVICES: Readonly<Record<string, ServiceInfo>> = {
  acm: { title: "AWS Certificate Manager (ACM)", required: true }, "acm-pca": { title: "ACM Private CA", required: false },
  aps: { title: "Amazon Managed Service for Prometheus", required: false }, backup: { title: "AWS Backup", required: true },
  budgets: { title: "AWS Budgets", required: false }, cloudformation: { title: "AWS CloudFormation", required: true },
  cloudwatch: { title: "Amazon CloudWatch", required: true }, "cognito-idp": { title: "Amazon Cognito - User Pools", required: true },
  ds: { title: "AWS Directory Service for Microsoft Active Directory", required: false }, dynamodb: { title: "Amazon DynamoDB", required: true },
  dynamodbstreams: { title: "Amazon DynamoDB Streams", required: true }, ebs: { title: "Amazon Elastic Block Store (EBS)", required: true },
  ec2: { title: "Amazon Elastic Compute Cloud (EC2)", required: true }, efs: { title: "Amazon Elastic File System (EFS)", required: true },
  elb: { title: "Amazon Elastic Load Balancing (ELB)", required: true }, es: { title: "Amazon OpenSearch Service", required: true },
  eventbridge: { title: "Amazon EventBridge", required: true }, events: { title: "Amazon Events", required: true },
  filecache: { title: "Amazon File Cache", required: false }, fsx: { title: "Amazon FSx", required: false },
  "fsx-lustre": { title: "Amazon FSx for Lustre", required: false }, "fsx-ontap": { title: "Amazon FSx for NetApp ONTAP", required: false },
  "fsx-openzfs": { title: "Amazon FSx for OpenZFS", required: false }, "fsx-windows": { title: "Amazon FSx for Windows File Server", required: false },
  grafana: { title: "Amazon Managed Grafana", required: false }, iam: { title: "AWS Identity and Access Management (IAM)", required: true },
  kinesis: { title: "Amazon Kinesis", required: true }, kms: { title: "AWS Key Management Service (KMS)", required: true },
  lambda: { title: "AWS Lambda", required: true }, logs: { title: "Amazon CloudWatch Logs", required: true },
  pricing: { title: "AWS Pricing API", required: false }, route53: { title: "Amazon Route 53", required: true },
  route53resolver: { title: "Amazon Route 53 Resolver", required: false }, s3: { title: "Amazon Simple Storage Service (S3)", required: true },
  secretsmanager: { title: "AWS Secrets Manager", required: true }, "service-quotas": { title: "AWS Service Quotas", required: true },
  ses: { title: "Amazon Simple Email Service (SES)", required: false }, sns: { title: "Amazon Simple Notification Service (SNS)", required: true },
  sqs: { title: "Amazon Simple Queue Service (SQS)", required: true }, ssm: { title: "AWS Systems Manager (SSM)", required: true },
  sts: { title: "AWS Security Token Service (STS)", required: true }, vpc: { title: "Amazon Virtual Private Cloud (VPC)", required: true },
};

const GATEWAY_ENDPOINTS = ["s3", "dynamodb"];
const INTERFACE_ENDPOINTS = ["application-autoscaling", "autoscaling", "cloudformation", "ec2", "ec2messages", "ebs", "elasticfilesystem", "elasticfilesystem-fips", "elasticloadbalancing", "logs", "monitoring", "secretsmanager", "sns", "sqs", "events", "ssm", "ssmmessages", "fsx", "fsx-fips", "backup", "grafana", "acm-pca", "kinesis-streams"];

export interface UtilsApi {
  getParametersByPath(input: { Path: string; NextToken?: string }): Promise<{ Parameters?: Array<{ Value?: string }>; NextToken?: string }>;
  describeVpcEndpointServices(input?: { Filters?: Array<{ Name: string; Values: string[] }> }): Promise<{
    ServiceDetails?: Array<{ ServiceName?: string; ServiceType?: Array<{ ServiceType?: string }>; AvailabilityZones?: string[] }>;
  }>;
  getManagedPrefixListEntries(input: { PrefixListId: string; NextToken?: string }): Promise<{ Entries?: Array<{ Cidr?: string; Description?: string }>; NextToken?: string }>;
  describeManagedPrefixLists(input: { PrefixListIds: string[] }): Promise<{ PrefixLists?: Array<{ Version?: number }> }>;
  modifyManagedPrefixList(input: { PrefixListId: string; CurrentVersion: number; AddEntries?: Array<{ Cidr: string; Description?: string }>; RemoveEntries?: Array<{ Cidr: string }> }): Promise<void>;
}

/** The managed prefix-list subset, which the deploy tool also uses. */
export type PrefixListApi = Pick<
  UtilsApi,
  "getManagedPrefixListEntries" | "describeManagedPrefixLists" | "modifyManagedPrefixList"
>;

/** What the prefix-list helpers below need; `UtilsDeps` satisfies it. */
export interface PrefixListDeps {
  api: PrefixListApi;
  config: ClusterConfig;
  out(line: string): void;
}

export interface UtilsDeps {
  api: UtilsApi;
  config: ClusterConfig;
  dnsSuffix(): Promise<string>;
  syncGlobalSettings?: (input: { deletePrefix: string; entries: Array<{ key: string; value: unknown }> }) => Promise<void>;
  exportConfig?: (input: { clusterName: string; awsRegion: string; moduleSet?: string; configDir: string }) => Promise<void>;
  now?: () => Date;
  prompt?: (message: string) => Promise<boolean>;
  out(line: string): void;
}

export type UtilsDepsFactory = (options: {
  clusterName?: string;
  awsRegion?: string;
  awsProfile?: string;
  moduleSet?: string;
}) => Promise<UtilsDeps>;

type UtilsDepsSource = UtilsDeps | UtilsDepsFactory;

/** Dependencies for every command group owned by the remaining operator commands. */
export interface RemainingOperatorCommandDeps {
  sso: SsoDeps | SsoDepsFactory;
  directoryService: DirectoryServiceDeps | DirectoryServiceDepsFactory;
  sharedStorage: SharedStorageDeps | SharedStorageDepsFactory;
  utils: UtilsDeps | UtilsDepsFactory;
  support: SupportDeps | SupportDepsFactory;
  integrationTests: IntegrationTestDeps | IntegrationTestDepsFactory;
}

/** Build the stable service matrix printed by `utils aws-services`. */
export function awsServicesTable(): string {
  return renderTable(["AWS Service", "Name", "Required"], Object.keys(IDEA_SERVICES).sort().map((name) => {
    const service = IDEA_SERVICES[name] as ServiceInfo;
    return [service.title, name, service.required ? "Yes" : "No"];
  }));
}

/** Read all SSM pages and print each service availability for every requested region. */
export async function awsServiceAvailability(deps: UtilsDeps, regions: readonly string[]): Promise<string> {
  const servicesByRegion = new Map<string, Set<string>>();
  for (const region of regions) {
    const available = new Set<string>();
    let token: string | undefined;
    do {
      const page = await deps.api.getParametersByPath({ Path: `/aws/service/global-infrastructure/regions/${region}/services`, NextToken: token });
      for (const parameter of page.Parameters ?? []) if (parameter.Value !== undefined) available.add(parameter.Value);
      token = page.NextToken;
    } while (token !== undefined);
    servicesByRegion.set(region, available);
  }
  const rows = Object.keys(IDEA_SERVICES).sort().map((name) => {
    const service = IDEA_SERVICES[name] as ServiceInfo;
    return [`${service.title} [${name}]`, service.required ? "Yes" : "No", ...regions.map((region) => servicesByRegion.get(region)?.has(name) === true ? "Yes" : "No")];
  });
  return renderTable(["Service", "Required", ...regions], rows);
}

/** Return supported endpoint services from the API response. */
export async function vpcEndpointServiceInfo(deps: UtilsDeps, region: string): Promise<string> {
  const suffixTokens = (await deps.dnsSuffix()).split(".").reverse();
  const domain = suffixTokens.join(".");
  const requested = [...GATEWAY_ENDPOINTS, ...INTERFACE_ENDPOINTS].map((shortName) => `${domain}.${region}.${shortName}`);
  const details = (await deps.api.describeVpcEndpointServices()).ServiceDetails ?? [];
  const rows: string[][] = [];
  for (const serviceName of requested) {
    const matching = details.filter((detail) => detail.ServiceName === serviceName);
    if (matching.length === 0) rows.push([serviceName, "No", "-", "-"]);
    for (const detail of matching) for (const type of detail.ServiceType ?? []) {
      rows.push([serviceName, "Yes", type.ServiceType ?? "", (detail.AvailabilityZones ?? []).join(", ")]);
    }
  }
  return renderTable(["Service Name", `Is Available in ${region}`, "Service Type", "Availability Zones"], rows);
}

async function prefixListId(config: ClusterConfig): Promise<string> {
  const value = config.getString("cluster.network.cluster_prefix_list_id", undefined, { required: true });
  if (isEmpty(value)) throw new ClusterConfigError("cluster.network.cluster_prefix_list_id is required");
  return value as string;
}

/** Scan all prefix-list entry pages. */
export async function prefixListEntries(deps: PrefixListDeps): Promise<Array<{ cidr: string; description?: string }>> {
  const id = await prefixListId(deps.config);
  const entries: Array<{ cidr: string; description?: string }> = [];
  let token: string | undefined;
  do {
    const page = await deps.api.getManagedPrefixListEntries({ PrefixListId: id, NextToken: token });
    for (const entry of page.Entries ?? []) if (entry.Cidr !== undefined) entries.push({ cidr: entry.Cidr, description: entry.Description });
    token = page.NextToken;
  } while (token !== undefined);
  return entries;
}

async function currentVersion(deps: PrefixListDeps, id: string): Promise<number> {
  const version = (await deps.api.describeManagedPrefixLists({ PrefixListIds: [id] })).PrefixLists?.[0]?.Version;
  if (version === undefined) throw new ClusterConfigError(`cluster prefix list not found: ${id}`);
  return Math.trunc(version);
}

export async function addPrefixListEntry(deps: PrefixListDeps, cidr: string, description: string): Promise<void> {
  if (isEmpty(cidr)) throw new ClusterConfigError("cidr is required");
  const id = await prefixListId(deps.config);
  if ((await prefixListEntries(deps)).some((entry) => entry.cidr === cidr)) throw new ClusterConfigError(`CIDR: ${cidr} already exists in cluster prefix list: ${id}`);
  await deps.api.modifyManagedPrefixList({ PrefixListId: id, CurrentVersion: await currentVersion(deps, id), AddEntries: [{ Cidr: cidr, ...(isEmpty(description) ? {} : { Description: description }) }] });
  deps.out(`CIDR: ${cidr} added to cluster prefix list: ${id}.`);
}

/** The description the cluster stack used to put on the configured client addresses. */
export const CLIENT_IP_ENTRY_DESCRIPTION = "Allow access to cluster from Client IP";

/**
 * Add the configured client addresses to the cluster prefix list, skipping the ones already
 * there. Add-only: an entry an operator added by hand, or one whose description has since been
 * edited, is left exactly as it is.
 */
export async function mergeClientIpEntries(deps: PrefixListDeps): Promise<void> {
  const clientIps = deps.config.getList<string>("cluster.network.client_ip", []);
  if (clientIps.length === 0) return;
  const wanted = clientIps.map((clientIp) => (clientIp.includes("/") ? clientIp : `${clientIp}/32`));
  const present = new Set((await prefixListEntries(deps)).map((entry) => entry.cidr));
  const missing = [...new Set(wanted)].filter((cidr) => !present.has(cidr));
  const id = await prefixListId(deps.config);
  if (missing.length === 0) {
    deps.out(`cluster prefix list ${id} already holds every configured client IP.`);
    return;
  }
  await deps.api.modifyManagedPrefixList({
    PrefixListId: id,
    CurrentVersion: await currentVersion(deps, id),
    AddEntries: missing.map((cidr) => ({ Cidr: cidr, Description: CLIENT_IP_ENTRY_DESCRIPTION })),
  });
  deps.out(`added ${missing.join(", ")} to cluster prefix list: ${id}.`);
}

export async function removePrefixListEntry(deps: PrefixListDeps, cidr: string): Promise<void> {
  if (isEmpty(cidr)) throw new ClusterConfigError("cidr is required");
  const id = await prefixListId(deps.config);
  if (!(await prefixListEntries(deps)).some((entry) => entry.cidr === cidr)) throw new ClusterConfigError(`CIDR: ${cidr} not found in cluster prefix list: ${id}`);
  await deps.api.modifyManagedPrefixList({ PrefixListId: id, CurrentVersion: await currentVersion(deps, id), RemoveEntries: [{ Cidr: cidr }] });
  deps.out(`CIDR: ${cidr} was removed from cluster prefix list: ${id}.`);
}

function backupSuffix(now: Date): string {
  const part = (value: number): string => String(value).padStart(2, "0");
  return `${part(now.getUTCMonth() + 1)}${part(now.getUTCDate())}${now.getUTCFullYear()}_${part(now.getUTCHours())}${part(now.getUTCMinutes())}${part(now.getUTCSeconds())}`;
}

/**
 * Export the local configuration as a timestamped golden directory, regenerate
 * it from values, then replace only global settings in the backing store.
 */
export async function backupUpdateGlobalSettings(
  deps: UtilsDeps,
  options: { clusterName: string; awsRegion: string; force?: boolean; moduleSet?: string },
): Promise<string> {
  if (deps.syncGlobalSettings === undefined) throw new ClusterConfigError("global settings writer is not configured");
  if (deps.exportConfig === undefined) throw new ClusterConfigError("configuration export is not configured");
  if (options.force !== true && deps.prompt !== undefined && !(await deps.prompt("Continue with global settings backup and update?"))) {
    deps.out("Operation aborted by user");
    return "";
  }
  const configDir = clusterConfigDir(options.clusterName, options.awsRegion);
  const backupDir = `${configDir}.golden.${backupSuffix((deps.now ?? (() => new Date()))())}`;
  await deps.exportConfig({ clusterName: options.clusterName, awsRegion: options.awsRegion, configDir });
  if (existsSync(backupDir)) rmSync(backupDir, { recursive: true, force: true });
  if (!existsSync(configDir)) throw new ClusterConfigError(`config directory not found: ${configDir}`);
  cpSync(configDir, backupDir, { recursive: true });
  const valuesPath = valuesFilePath(options.clusterName, options.awsRegion);
  const values = loadValuesFile(valuesPath);
  generateConfigFromTemplates(values, configDir);
  const entries = convertConfigToKeyValuePairs(configDir, "global-settings");
  await deps.syncGlobalSettings({ deletePrefix: "global-settings.", entries });
  deps.out("Global settings backup and update completed successfully");
  return backupDir;
}

/** Register `utils`, `utils vpc-endpoints`, and `utils cluster-prefix-list`. */
export function registerUtilsCommands(program: Command, deps: UtilsDepsSource): Command {
  const resolveDeps = async (options: {
    clusterName?: string;
    awsRegion?: string;
    awsProfile?: string;
    moduleSet?: string;
  }): Promise<UtilsDeps> => typeof deps === "function" ? deps(options) : deps;
  const utils = program.command("utils").description("utility commands");
  utils.command("aws-services").action(async () => (await resolveDeps({})).out(awsServicesTable()));
  utils.command("check-aws-services").option("--aws-profile <aws-profile>").argument("<aws-regions...>").action(async (regions: string[], options: { awsProfile?: string }) => {
    const actionDeps = await resolveDeps({ awsRegion: regions[0], awsProfile: options.awsProfile });
    actionDeps.out(await awsServiceAvailability(actionDeps, regions));
  });
  const endpoints = utils.command("vpc-endpoints").description("vpc endpoint commands");
  endpoints.command("service-info").requiredOption("--aws-region <aws-region>").option("--aws-profile <aws-profile>").action(async (options: { awsRegion: string; awsProfile?: string }) => {
    const actionDeps = await resolveDeps(options);
    actionDeps.out(await vpcEndpointServiceInfo(actionDeps, options.awsRegion));
  });
  const prefixes = utils.command("cluster-prefix-list").description("cluster prefix list commands");
  const shared = (command: Command): Command => command.requiredOption("--cluster-name <cluster-name>").requiredOption("--aws-region <aws-region>").option("--aws-profile <aws-profile>");
  shared(prefixes.command("show")).action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string }) => {
    const actionDeps = await resolveDeps(options);
    actionDeps.out(renderTable(["CIDR", "Description"], (await prefixListEntries(actionDeps)).map((entry) => [entry.cidr, entry.description ?? "-"])));
  });
  shared(prefixes.command("add-entry")).requiredOption("--cidr <cidr>").requiredOption("--description <description>").action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string; cidr: string; description: string }) => {
    await addPrefixListEntry(await resolveDeps(options), options.cidr, options.description);
  });
  shared(prefixes.command("remove-entry")).requiredOption("--cidr <cidr>").action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string; cidr: string }) => {
    await removePrefixListEntry(await resolveDeps(options), options.cidr);
  });
  program.command("backup-update-global-settings")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .option("--force")
    .option("--module-set <module-set>", "Name of the ModuleSet. Default: default")
    .action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string; force?: boolean; moduleSet?: string }) => {
      await backupUpdateGlobalSettings(await resolveDeps(options), options);
    });
  return utils;
}

/** Register all remaining operator command groups onto the command program. */
export function registerRemainingOperatorCommands(program: Command, deps: RemainingOperatorCommandDeps): void {
  registerSsoCommands(program, deps.sso);
  registerDirectoryServiceCommands(program, deps.directoryService);
  registerSharedStorageCommands(program, deps.sharedStorage);
  registerUtilsCommands(program, deps.utils);
  registerSupportCommands(program, deps.support);
  registerIntegrationTestCommands(program, deps.integrationTests);
}
