// Runs `upgrade-cluster` against a captured cluster with every write recorded instead of applied.
//
//   node tools/parity/upgrade-dry-run.ts --capture <dir> --values <values.yml> [--templates <dir>] [--enable-ecs] [--out <file>]
//
// <dir> holds cluster-settings.json and modules.json as `aws dynamodb scan` wrote them; the
// cluster name and region come from the settings rows. --templates names a directory of
// `get-template` bodies, one <stack-name>.json each, for the Phase 0 record-set retention.
// --enable-ecs sets `enable_ecs: true` on a copy of the values, the migration's one switch.
//
// The report is every line the command prints plus every table write, module sync, stack update
// and deploy it would perform, in order. Cloud reads the capture cannot answer are stated at the
// top of the report as assumptions.

import { unmarshall } from "@aws-sdk/util-dynamodb";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import { type UpgradeDeps, upgradeCluster } from "../../src/cli/commands/upgrade.ts";

const ASSUMPTIONS = [
  "awsvpcTrunking is enabled in the account (the pre-flight otherwise refuses)",
  "the built compute image is newer than the release image, so it is kept",
  "every instance type in the values is offered in the region",
  "OpenSearch offers m7g.large.search for the domain's engine version",
  "the software-stack, session and queue-profile tables were not captured, so the EOL phase sees none",
  "no stack resources are enumerated, so no termination protection is cleared or restored, and the scheduler cutover gate sees no host to drain",
];

const { values: args } = parseArgs({
  options: {
    capture: { type: "string" },
    values: { type: "string" },
    templates: { type: "string" },
    "enable-ecs": { type: "boolean", default: false },
    out: { type: "string" },
  },
});
if (args.capture === undefined || args.values === undefined) {
  console.error("usage: upgrade-dry-run.ts --capture <dir> --values <values.yml> [--templates <dir>] [--enable-ecs] [--out <file>]");
  process.exit(2);
}

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
    async describeStack() { return { StackStatus: "UPDATE_COMPLETE" }; },
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
  async scan(input) { return { Items: rows[input.TableName] ?? [] }; },
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
  ecsAccountSettings: { async listAccountSettings() { return [{ name: "awsvpcTrunking", value: "enabled" }]; } },
  ec2: {
    async describeImages(input) {
      return input.imageIds.map((imageId, index) => ({
        ImageId: imageId,
        Name: index === 0 ? `idea-compute-node-${clusterName}` : "release",
        CreationDate: index === 0 ? "2026-09-01T00:00:00.000Z" : "2026-01-01T00:00:00.000Z",
      }));
    },
    async describeInstanceTypeOfferings(input) { return [input.instanceType]; },
    async describeInstanceAttribute() { return false; },
    async modifyInstanceAttribute(input) { record(`EC2 termination protection ${input.protected ? "restored" : "cleared"} on ${input.instanceId}`); },
    async createTags(input) { record(`EC2 protection marker created on ${input.instanceId}`); },
    async deleteTags(input) { record(`EC2 protection marker removed on ${input.instanceId}`); },
    async describeLiveInstances(input) { return input.instanceIds; },
  },
  cloudFormation: { async listStackResources() { return { instanceIds: [] }; } },
  openSearch: {
    async describeDomain() { return { engineVersion: "OpenSearch_2.19" }; },
    async listInstanceTypeDetails() { return ["m5.large.search", "m6g.large.search", "m7g.large.search"]; },
  },
  eolSoftwareStacks: {
    async setEnabled(input) { record(`EOL software stack ${JSON.stringify(input)} disabled`); },
    async delete(input) { record(`EOL software stack ${JSON.stringify(input)} deleted`); },
  },
  async deploy(input) { record(`DEPLOY ${(input.moduleIds ?? ["all"]).join(", ")}`); },
};

let failure: string | undefined;
try {
  await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, terminationProtection: true });
} catch (error) {
  failure = (error as Error).message;
} finally {
  rmSync(home, { recursive: true, force: true });
}

const report = [
  `# upgrade-cluster dry run: ${clusterName} (${awsRegion})`,
  `capture: ${args.capture}`,
  `values: ${args.values}${args["enable-ecs"] ? " + enable_ecs: true" : ""}`,
  `templates: ${args.templates ?? "(none; Phase 0 skipped)"}`,
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
