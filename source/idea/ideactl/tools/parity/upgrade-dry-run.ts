// Captured inventories make historical refusals and state changes reproducible offline.
// Writes update only replay state; upgrade-dry-run.md describes the required captures.

import { ideaVersion } from "../../src/version.ts";
import { unmarshall } from "@aws-sdk/util-dynamodb";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import { type UpgradeDeps, upgradeCluster } from "../../src/cli/commands/upgrade.ts";

const ASSUMPTIONS = [
  "deployment is simulated; supplied deployment settings and IAM outcomes are applied, not proven",
  "without --inventory, recent-release runs assume empty auxiliary tables and instance inventory, available offerings and a newer built image",
];

interface Inventory {
  tables: Record<string, Array<Record<string, unknown>>>;
  images: Array<{ ImageId: string; Name?: string; CreationDate?: string }>;
  stacks: Record<string, string[]>;
  protection: Record<string, boolean>;
  protectionTags: string[];
  iam: Record<string, { attached: string[]; inline: string[]; collision: boolean; available: number }>;
  deployedIam: Inventory["iam"];
  deploymentSettings: Array<{ key: string; value: unknown }>;
  instanceTypes: string[];
  openSearchTypes: string[];
  jobs: Record<string, { queued: number; running: number; other: number }>;
  trunking: boolean;
}

const { values: args } = parseArgs({
  options: {
    capture: { type: "string" },
    values: { type: "string" },
    templates: { type: "string" },
    inventory: { type: "string" },
    "enable-ecs": { type: "boolean", default: false },
    out: { type: "string" },
  },
});
if (args.capture === undefined || args.values === undefined) {
  console.error("usage: upgrade-dry-run.ts --capture <dir> --values <values.yml> [--templates <dir>] [--enable-ecs] [--out <file>]");
  process.exit(2);
}

const inventory = args.inventory === undefined ? undefined : JSON.parse(readFileSync(args.inventory, "utf8")) as Inventory;
const tags = new Set(inventory?.protectionTags ?? []);
let deployed = false;
type Row = Record<string, unknown>;
function scanRows(file: string): Row[] {
  const parsed = JSON.parse(readFileSync(file, "utf8")) as { Items?: Array<Record<string, never>> } | Array<Record<string, never>>;
  const items = Array.isArray(parsed) ? parsed : parsed.Items ?? [];
  return items.map((item) => unmarshall(item));
}

const settings = scanRows(join(args.capture, "cluster-settings.json"));
const modules = scanRows(join(args.capture, "modules.json"));
const settingValue = (key: string): string | undefined => {
  const value = settings.find((row) => row.key === key)?.value;
  return typeof value === "string" ? value : undefined;
};
const clusterName = settingValue("cluster.cluster_name") ?? "";
const awsRegion = settingValue("cluster.aws.region") ?? "";
if (clusterName === "" || awsRegion === "") throw new Error("cluster.cluster_name and cluster.aws.region must be in the capture");

const rows: Record<string, Row[]> = {
  [`${clusterName}.cluster-settings`]: settings,
  [`${clusterName}.modules`]: modules,
  ...inventory?.tables,
};
const lines: string[] = [];
const record = (line: string): void => { lines.push(line); };

const home = mkdtempSync(join(tmpdir(), "upgrade-dry-run-"));
process.env.IDEA_USER_HOME = home;
const valuesDirectory = join(home, "clusters", clusterName, awsRegion);
mkdirSync(valuesDirectory, { recursive: true });
let valuesText = readFileSync(args.values, "utf8");
if (args["enable-ecs"] && !/^enable_ecs:\s*true/m.test(valuesText)) valuesText = `${valuesText.replace(/^enable_ecs:.*\n?/m, "")}\nenable_ecs: true\n`;
writeFileSync(join(valuesDirectory, "values.yml"), valuesText);

/** Writes land in the in-memory table so later phases read what earlier phases produced. */
const writer: ConfigWriter = {
  async syncModulesInDb(moduleList) {
    record(`WRITE modules table: ${moduleList.map((module) => module.id).sort().join(", ")}`);
    for (const module of moduleList) if (!modules.some((row) => row.module_id === module.id)) {
      modules.push({ module_id: module.id, name: module.name, type: module.type, status: "not-deployed" });
    }
  },
  async syncClusterSettingsInDb(entries, overwrite) {
    const table = rows[`${clusterName}.cluster-settings`] ?? [];
    let added = 0;
    let changed = 0;
    for (const entry of entries) {
      const existing = table.find((row) => row.key === entry.key);
      if (existing === undefined) { table.push({ key: entry.key, value: entry.value, source: "config" }); added += 1; continue; }
      if (overwrite === true && JSON.stringify(existing.value) !== JSON.stringify(entry.value)) {
        record(`WRITE ${entry.key}: ${JSON.stringify(existing.value)} -> ${JSON.stringify(entry.value)}`);
        existing.value = entry.value; changed += 1;
      }
    }
    record(`WRITE settings sync (${overwrite === true ? "overwrite" : "add-only"}): ${entries.length} entries, ${added} added, ${changed} changed`);
  },
  async setConfigEntry(key, value) {
    const table = rows[`${clusterName}.cluster-settings`] ?? [];
    const existing = table.find((row) => row.key === key);
    record(`WRITE ${key}: ${existing === undefined ? "(absent)" : JSON.stringify(existing.value)} -> ${JSON.stringify(value)}`);
    if (existing === undefined) table.push({ key, value, source: "config" }); else existing.value = value;
  },
  async deleteConfigEntries(prefix) {
    const table = rows[`${clusterName}.cluster-settings`] ?? [];
    const kept = table.filter((row) => !(typeof row.key === "string" && row.key.startsWith(prefix)));
    record(`WRITE delete ${prefix}*: ${table.length - kept.length} rows`);
    rows[`${clusterName}.cluster-settings`] = kept;
  },
};

const base: Deps = {
  async spawn() { return 0; },
  cfn: {
    async describeChangeSet() { return {}; },
    async executeChangeSet() {},
    async describeStack() { return { StackStatus: "UPDATE_COMPLETE", Tags: deployed ? [{ Key: "idea:ModuleVersion", Value: ideaVersion() }] : [] }; },
    ...(args.templates === undefined ? {} : {
      async getTemplate(stackName: string) {
        const file = join(args.templates as string, `${stackName}.json`);
        if (!existsSync(file)) throw new Error(`no captured template for ${stackName} at ${file}`);
        return readFileSync(file, "utf8");
      },
      async updateStack(input: { StackName: string; TemplateBody: string; ParameterKeys: readonly string[] }) {
        record(`STACK policy-only UpdateStack ${input.StackName} (${input.ParameterKeys.length} parameters kept, ${input.TemplateBody.length} bytes)`);
      },
    }),
  },
  s3: {
    async putObject(input) { record(`S3 put ${input.Bucket}/${input.Key}`); },
    async getObject() { return valuesText; },
  },
  async scan(input) {
    if (inventory !== undefined && rows[input.TableName] === undefined) throw new Error(`Missing captured table ${input.TableName}`);
    return { Items: rows[input.TableName] ?? [] };
  },
  async configWriter() { return writer; },
  async accountId() { return ["123456", "789012"].join(""); },
  async httpStatus() { return 200; },
  async sleep() {},
  now: () => Date.now(),
  uuid: () => "00000000-0000-0000-0000-000000000000",
  out: record,
  err: (line) => record(`ERR ${line}`),
  async prompt(choice) { record(`PROMPT ${choice.message} -> yes`); return true; },
};

const deps: UpgradeDeps = {
  ...base,
  ecsAccountSettings: { async listAccountSettings() { return [{ name: "awsvpcTrunking", value: inventory?.trunking === false ? "disabled" : "enabled" }]; } },
  ...(inventory === undefined ? {} : {
    async historicalIam(role: string) {
      const state = (deployed ? inventory.deployedIam : inventory.iam)[role];
      if (state === undefined) throw new Error(`Missing captured IAM role ${role}`);
      return state;
    },
    schedulerJobs: { async activeJobs(input: { instanceId: string }) {
      const jobs = inventory.jobs[input.instanceId];
      if (jobs === undefined) throw new Error(`Missing captured job inventory for ${input.instanceId}`);
      return jobs;
    } },
  }),
  ec2: {
    async describeImages(input) {
      if (inventory !== undefined) {
        return input.imageIds.map((id) => {
          const image = inventory.images.find((entry) => entry.ImageId === id);
          if (image === undefined) throw new Error(`Missing captured image ${id}`);
          return image;
        });
      }
      return input.imageIds.map((imageId, index) => ({
        ImageId: imageId,
        Name: index === 0 ? `idea-compute-node-${clusterName}` : "release",
        CreationDate: index === 0 ? "2026-09-01T00:00:00.000Z" : "2026-01-01T00:00:00.000Z",
      }));
    },
    async describeInstanceTypeOfferings(input) { return inventory?.instanceTypes ?? [input.instanceType]; },
    async describeInstanceAttribute(input) {
      if (inventory === undefined) return false;
      if (!(input.instanceId in inventory.protection)) throw new Error(`Missing protection for ${input.instanceId}`);
      return inventory.protection[input.instanceId]!;
    },
    async modifyInstanceAttribute(input) { if (inventory !== undefined) inventory.protection[input.instanceId] = input.protected; record(`EC2 termination protection ${input.protected ? "restored" : "cleared"} on ${input.instanceId}`); },
    async createTags(input) { tags.add(input.instanceId); record(`EC2 protection marker created on ${input.instanceId}`); },
    async deleteTags(input) { tags.delete(input.instanceId); record(`EC2 protection marker removed on ${input.instanceId}`); },
    async describeLiveInstances(input) { return input.instanceIds.filter((id) => input.tagKey === undefined || tags.has(id)); },
  },
  cloudFormation: { async listStackResources(input) {
    if (inventory === undefined) return { instanceIds: [] };
    const instanceIds = inventory.stacks[input.stackName];
    if (instanceIds === undefined) {
      if (modules.some((row) => `${clusterName}-${String(row.module_id)}` === input.stackName && row.status === "not-deployed")) return { instanceIds: [] };
      throw new Error(`Missing captured resources for ${input.stackName}`);
    }
    return { instanceIds };
  } },
  openSearch: {
    async describeDomain() { return { engineVersion: "OpenSearch_2.19" }; },
    async listInstanceTypeDetails() { return inventory?.openSearchTypes ?? ["m5.large.search", "m6g.large.search", "m7g.large.search"]; },
  },
  eolSoftwareStacks: {
    async setEnabled(input) {
      const row = rows[input.tableName]?.find((entry) => entry.stack_id === input.stackId && entry.base_os === input.baseOs);
      if (row !== undefined) row.enabled = input.enabled;
      record(`EOL software stack ${JSON.stringify(input)} disabled`); },
    async delete(input) {
      rows[input.tableName] = (rows[input.tableName] ?? []).filter((entry) => entry.stack_id !== input.stackId || entry.base_os !== input.baseOs);
      record(`EOL software stack ${JSON.stringify(input)} deleted`); },
  },
  async deploy(input) {
    record(`DEPLOY ${(input.moduleIds ?? ["all"]).join(", ")}`);
    for (const module of modules) if (input.moduleIds === undefined || input.moduleIds.includes(String(module.module_id))) {
      module.status = "deployed";
      module.version = ideaVersion();
    }
    for (const entry of inventory?.deploymentSettings ?? []) await writer.setConfigEntry(entry.key, entry.value);
    deployed = true;
  },
};

let failure: string | undefined;
try {
  await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true, terminationProtection: true });
} catch (error) {
  failure = (error as Error).message;
} finally {
  rmSync(home, { recursive: true, force: true });
}

const report = [
  `# upgrade-cluster dry run: ${clusterName} (${awsRegion})`,
  `capture: ${args.capture}`,
  `values: ${args.values}${args["enable-ecs"] ? " + enable_ecs: true" : ""}`,
  `templates: ${args.templates ?? "(none; historical migration refuses)"}`,
  `inventory: ${args.inventory ?? "(none)"}`,
  "assumptions:",
  ...ASSUMPTIONS.map((assumption) => `  - ${assumption}`),
  "",
  ...lines,
  "",
  failure === undefined ? "RESULT completed" : `RESULT stopped: ${failure}`,
  `writes=${lines.filter((line) => line.startsWith("WRITE ")).length} deploys=${lines.filter((line) => line.startsWith("DEPLOY ")).length} stack-updates=${lines.filter((line) => line.startsWith("STACK ")).length}`,
].join("\n");
if (args.out === undefined) console.log(report); else { writeFileSync(args.out, `${report}\n`); console.log(report.split("\n").slice(-2).join("\n")); }
process.exit(failure === undefined ? 0 : 1);
