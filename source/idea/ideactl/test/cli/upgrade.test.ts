// Synthetic values keep failure recovery reproducible without a private cluster capture.
// External state lives in the replay so separate upgrade invocations can share it.

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { CreateTagsCommand, DeleteTagsCommand, DescribeInstancesCommand, EC2Client } from "@aws-sdk/client-ec2";
import { DescribeDomainCommand, OpenSearchClient } from "@aws-sdk/client-opensearch";
import { Command } from "commander";
import { Agent, EnvHttpProxyAgent, type RequestInit as RegistryRequestInit } from "undici";
import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import { ExitWithCode } from "../../src/cli/cdk-invoker.ts";
import { configUpgradePreview, registerConfigCommands } from "../../src/cli/commands/config.ts";
import {
  createLiveUpgradeDeps,
  planEcsImageFollowsRelease,
  prepareUpgradeDriftInput,
  publicRegistryImageExists,
  registerUpgradeCommands,
  returnBorrowedHosts,
  upgradeCluster,
  type UpgradeDeps,
} from "../../src/cli/commands/upgrade.ts";
import { DATADOG_AGENT_IMAGE } from "../../src/config/datadog-agent.ts";
import { ideaVersion } from "../../src/version.ts";
import { change } from "../support/deploy-harness.ts";

const fixture = join(import.meta.dirname, "../stacks/ecs-values.yml");
const fixtureValues = readFileSync(fixture, "utf8").replace(/^enable_ecs:.*\n/m, "")
  + "enabled_modules: [scheduler, virtual-desktop-controller]\n";
const clusterName = "idea-test1";
const awsRegion = "us-east-2";

interface Replay {
  deps: UpgradeDeps;
  events: string[];
  rows: Record<string, Array<Record<string, unknown>>>;
  protection: Set<string>;
  protectionTags: Map<string, string>;
}

function moduleRow(moduleId: string, name: string, status = "deployed"): Record<string, unknown> {
  return { module_id: moduleId, name, type: "app", status, stack_name: `${clusterName}-${moduleId}`, version: "26.10.0" };
}

function setting(key: string, value: unknown): Record<string, unknown> {
  return { key, value };
}

function replay(): Replay {
  const events: string[] = [];
  const rows: Record<string, Array<Record<string, unknown>>> = {
    [`${clusterName}.cluster-settings`]: [
      setting("ecs.enabled", true),
      setting("global-settings.module_sets.default.cluster-manager.module_id", "cluster-manager"),
      setting("cluster.cluster_s3_bucket", "sample-cluster-bucket"),
      setting("scheduler.base_os", "amazonlinux2023"),
      setting("scheduler.instance_ami", "ami-old"),
      setting("scheduler.compute_node_os", "amazonlinux2023"),
      setting("scheduler.compute_node_ami", "ami-built"),
      setting("scheduler.instance_type", "m6i.large"),
      setting("analytics.opensearch.data_node_instance_type", "m5.large.search"),
      setting("analytics.opensearch.domain_name", "sample-domain"),
    ],
    [`${clusterName}.modules`]: [
      moduleRow("scheduler", "scheduler"),
      moduleRow("analytics", "analytics"),
      moduleRow("vdc", "virtual-desktop-controller"),
    ],
    [`${clusterName}.vdc.controller.software-stacks`]: [],
    [`${clusterName}.vdc.controller.user-sessions`]: [],
    [`${clusterName}.scheduler.queue-profiles`]: [],
  };

  const writer: ConfigWriter = {
    async syncModulesInDb(modules) {
      events.push(`modules:${modules.map((module) => module.id).sort().join(",")}`);
      const table = rows[`${clusterName}.modules`]!;
      for (const module of modules) {
        if (!table.some((row) => row["module_id"] === module.id)) {
          table.push({ ...moduleRow(module.id, module.name, "not-deployed"), type: module.type, version: undefined });
        }
      }
    },
    async syncClusterSettingsInDb(entries, overwrite, source) {
      assert.equal(source, "template");
      events.push(`sync:${overwrite === true}:${entries[0]?.key ?? ""}`);
      const table = rows[`${clusterName}.cluster-settings`]!;
      for (const entry of entries) {
        const index = table.findIndex((row) => row["key"] === entry.key);
        if (index < 0) table.push({ ...entry, source });
        else if (overwrite === true) table[index] = { ...entry, source };
      }
      const announced = entries
        .map((entry) => entry.key.match(/^global-settings\.module_sets\.default\.([^.]+)\.module_id$/)?.[1])
        .filter((name): name is string => name !== undefined);
      if (announced.length > 0) events.push(`module-sets:${overwrite === true}:${announced.sort().join(",")}`);
    },
    async setConfigEntry(key, value) {
      events.push(`set:${key}=${String(value)}`);
      const table = rows[`${clusterName}.cluster-settings`]!;
      const index = table.findIndex((row) => row["key"] === key);
      if (index < 0) table.push(setting(key, value));
      else table[index] = setting(key, value);
    },
    async deleteConfigEntries(prefix) {
      events.push(`delete:${prefix}`);
      rows[`${clusterName}.cluster-settings`] = rows[`${clusterName}.cluster-settings`]!
        .filter((row) => !String(row["key"]).startsWith(prefix));
    },
  };

  const base: Deps = {
    async spawn() {
      return 0;
    },
    cfn: {
      async describeChangeSet() {
        return {};
      },
      async executeChangeSet() { },
      async describeStack() {
        return { StackStatus: "UPDATE_COMPLETE", Tags: [{ Key: "idea:ModuleVersion", Value: ideaVersion() }] };
      },
    },
    s3: {
      async putObject() {
        events.push("save-values");
      },
      async getObject() {
        events.push("read-values");
        return fixtureValues;
      },
    },
    async scan(input) {
      return { Items: rows[input.TableName] ?? [] };
    },
    async configWriter() {
      return writer;
    },
    async accountId() {
      return "123456789012";
    },
    async httpStatus() {
      return 200;
    },
    async sleep() { },
    now() {
      return Date.UTC(2026, 8, 10, 12, 34, 56);
    },
    uuid() {
      return "00000000-0000-0000-0000-000000000000";
    },
    out(line) {
      events.push(line);
    },
    err(line) {
      events.push(`err:${line}`);
    },
    async prompt(choice) {
      events.push(`prompt:${choice.message}`);
      return true;
    },
  };

  const protection = new Set(["i-sample"]);
  const protectionTags = new Map<string, string>();
  const deps: UpgradeDeps = {
    ...base,
    ecsAccountSettings: { listAccountSettings: async () => [{ name: "awsvpcTrunking", value: "enabled" }] },
    ec2: {
      async describeImages() {
        return [
          { ImageId: "ami-old", Name: "old", CreationDate: "2025-01-01T00:00:00.000Z" },
          { ImageId: "ami-built", Name: ["idea", "compute", "node", "sample"].join("-"), CreationDate: "2026-02-02T00:00:00.000Z" },
          { ImageId: "ami-release", Name: "release", CreationDate: "2026-01-01T00:00:00.000Z" },
        ];
      },
      async describeInstanceTypeOfferings() {
        return ["m7i.large"];
      },
      async describeInstanceAttribute(input) {
        return protection.has(input.instanceId);
      },
      async modifyInstanceAttribute(input) {
        events.push(`${input.protected ? "restore" : "clear"}:${input.instanceId}`);
        if (input.protected) protection.add(input.instanceId);
        else protection.delete(input.instanceId);
      },
      async createTags(input) {
        protectionTags.set(input.instanceId, input.value);
        events.push(`tag:${input.instanceId}`);
      },
      async deleteTags(input) {
        assert.ok(protection.has(input.instanceId));
        protectionTags.delete(input.instanceId);
      },
      async describeLiveInstances(input) {
        assert.ok(input.tagKey === undefined || input.tagKey === "idea:TerminationProtectionCleared");
        return input.instanceIds.filter((id) => input.tagKey === undefined || protectionTags.has(id));
      },
    },
    cloudFormation: {
      async listStackResources(input) {
        return input.stackName.endsWith("-scheduler") ? { instanceIds: ["i-sample"] } : { instanceIds: [] };
      },
    },
    openSearch: {
      async describeDomain() {
        return { engineVersion: "OpenSearch_2.0", serviceSoftwareOptions: { updateAvailable: false, updateStatus: "COMPLETED" } };
      },
      async listInstanceTypeDetails() {
        return ["m7g.large.search"];
      },
    },
    eolSoftwareStacks: {
      async setEnabled() {
        events.push("disable-eol");
      },
      async delete() {
        events.push("delete-eol");
      },
    },
    // A host scheduler holding no jobs, so the cutover gate passes unless a test says otherwise.
    async deploy(input) {
      events.push("deploy");
      events.push(`deploy:${(input.moduleIds ?? ["all"]).join(",")}`);
      for (const row of rows[`${clusterName}.modules`]!) {
        if (input.moduleIds === undefined || input.moduleIds.includes(String(row["module_id"]))) {
          row["status"] = "deployed";
          row["version"] = ideaVersion();
        }
      }
    },
    async releaseImageExists() {
      return true;
    },
    regionAmiConfig() {
      return { "us-east-2": { amazonlinux2023: "ami-release" } };
    },
  };
  return { deps, events, rows, protection, protectionTags };
}

async function withFixture(action: (replayValue: Replay) => Promise<void>): Promise<void> {
  const home = mkdtempSync(join(tmpdir(), "ideactl-upgrade-"));
  const original = process.env.IDEA_USER_HOME;
  process.env.IDEA_USER_HOME = home;
  try {
    const valuesPath = join(home, "clusters", clusterName, awsRegion, "values.yml");
    const directory = join(valuesPath, "..");
    mkdirSync(directory, { recursive: true });
    writeFileSync(valuesPath, fixtureValues, { flag: "w" });
    assert.ok(existsSync(directory));
    await action(replay());
  } finally {
    if (original === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = original;
    rmSync(home, { recursive: true, force: true });
  }
}

test("registers the upgrade-cluster command surface", () => {
  const program = new Command("ideactl");
  registerUpgradeCommands(program, replay().deps);
  const command = program.commands.find((candidate) => candidate.name() === "upgrade-cluster");
  assert.ok(command !== undefined);
  const help = command.helpInformation();
  for (const option of [
    "--cluster-name",
    "--aws-region",
    "--base-os",
    "--termination-protection",
    "--deployment-id",
    "--force-build-bootstrap",
    "--rollback",
    "--optimize-deployment",
    "--module-set",
    "--force",
    "--accept-config-drift",
    "--skip-global-settings-update",
    "--skip-release-image-check",
    "--disable-eol-stacks-in-use",
  ]) {
    assert.ok(help.includes(option));
  }
});

test("runs Phase 1, Phase 2, Phase 2b, Phase 3, and Phase 4 in order", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, {
      clusterName,
      awsRegion,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
      acceptConfigDrift: true,
    });

    const phases = events.filter((event) => event.startsWith("Phase "));
    assert.deepEqual(phases, [
      "Phase 1: Update Base OS in values.yml",
      "Phase 2: Global Settings Backup and Update",
      "Phase 2b: Sync full configuration without overwrite",
      "Phase 3: Update AMI IDs and Settings",
      "Phase 4: Module Deployment",
    ]);
  });
});

test("prompts at each pre-mutation and optional phase boundary without --force", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default" });
    assert.deepEqual(events.filter((event) => event.startsWith("prompt:")), [
      // The fixture's hand-set AMI and instance-type rows are what Phase 3 overwrites, so the
      // first prompt names how many rows are at risk rather than asking a question with no subject.
      "prompt:Overwrite the 1 row(s) above and continue with the cluster upgrade?",
      "prompt:Continue with global settings backup and update?",
      "prompt:Sync full configuration to add new values?",
      "prompt:Continue with AMI and settings updates?",
      "prompt:Proceed with deploying all modules?",
    ]);
  });
});

test("Phase 2 updates global settings without removing rows read by running applications", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deleteIndex = events.indexOf("delete:global-settings.");
    const overwriteSync = events.findIndex((event) => event.startsWith("sync:true:"));
    const addOnlySync = events.findIndex((event) => event.startsWith("sync:false:"));
    assert.ok(events.some((event) => event.startsWith("Backup created successfully at ")));
    assert.equal(deleteIndex, -1);
    assert.ok(overwriteSync > deleteIndex);
    assert.ok(addOnlySync > overwriteSync);
  });
});

test("Phase 2b registers every generated module before the add-only settings sync", async () => {
  // A regenerated configuration can describe a module the table has no row for. The deployment
  // order reads the table, so without the registration that module is silently skipped while every
  // other stack deploys.
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const register = events.findIndex((event) => event.startsWith("modules:"));
    const addOnlySync = events.findIndex((event) => event.startsWith("sync:false:"));
    assert.ok(register >= 0, "no module registration happened");
    assert.ok(register < addOnlySync, "modules were registered after the settings sync");
    const registered = (events[register] as string).slice("modules:".length).split(",");
    for (const moduleId of ["cluster", "cluster-manager", "scheduler", "vdc"]) {
      assert.ok(registered.includes(moduleId), moduleId);
    }
  });
});

function enableContainers(): void {
  const valuesPath = join(process.env["IDEA_USER_HOME"] as string, "clusters", clusterName, awsRegion, "values.yml");
  writeFileSync(valuesPath, `${readFileSync(valuesPath, "utf8")}\nenable_ecs: true\n`);
}

function sendMetricsToDatadog(): void {
  const valuesPath = join(process.env["IDEA_USER_HOME"] as string, "clusters", clusterName, awsRegion, "values.yml");
  // The metrics module owns the provider rows; the base fixture leaves it out.
  const values = readFileSync(valuesPath, "utf8")
    .replace(/^metrics_provider:.*\n/m, "")
    .replace(/^enabled_modules:.*\n/m, "enabled_modules: [metrics, scheduler, virtual-desktop-controller]\n");
  writeFileSync(valuesPath, `${values}\nmetrics_provider: dogstatsd\ndatadog_api_key_secret_arn: arn:aws:secretsmanager:us-east-2:123456789012:secret:idea-test1-datadog-api-key-AbCdEf\n`);
}

function trunkingEnabled(deps: UpgradeDeps): void {
  deps.ecsAccountSettings = {
    async listAccountSettings() {
      return [{ name: "awsvpcTrunking", value: "enabled" }];
    },
  };
}

function announcedModules(event: string): string[] {
  return (event.split(":")[2] ?? "").split(",");
}

function schedulerTemplate(deletionPolicy?: string): string {
  return JSON.stringify({
    Parameters: { BootstrapVersion: { Type: "AWS::SSM::Parameter::Value<String>", Default: "/cdk-bootstrap/hnb659fds/version" } },
    Resources: {
      schedulerdnsrecord: { Type: "AWS::Route53::RecordSet", ...(deletionPolicy === undefined ? {} : { DeletionPolicy: deletionPolicy }), Properties: {} },
      schedulerinstance: { Type: "AWS::EC2::Instance", Properties: {} },
    },
  });
}

test("an already-deployed cluster announces nothing after deployment", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deploy = events.indexOf("deploy");
    assert.equal(events.slice(deploy).filter((event) => event.startsWith("module-sets:")).length, 0);
  });
});

test("Phase 3 refreshes paired AMI keys, preserves a newer built compute image, and moves offered defaults", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(!events.includes("set:scheduler.base_os=amazonlinux2023"));
    assert.ok(!events.some((event) => event.startsWith("set:scheduler.instance_ami=")));
    assert.ok(!events.some((event) => event.startsWith("set:scheduler.compute_node_")));
    assert.ok(!events.includes("set:scheduler.instance_type=m7i.large"));
    assert.ok(events.includes("set:analytics.opensearch.data_node_instance_type=m7g.large.search"));
  });
});

for (const serviceSoftwareOptions of [
  { newVersion: "R20260901-P1", updateAvailable: true, updateStatus: "ELIGIBLE" },
  { newVersion: "R20260901-P1", updateAvailable: false, updateStatus: "PENDING_UPDATE" },
  { newVersion: "R20260901-P1", updateAvailable: false, updateStatus: "IN_PROGRESS" },
  undefined,
]) {
  test(`OpenSearch software status ${serviceSoftwareOptions?.updateStatus ?? "unavailable"} keeps the data node instance type unchanged`, async () => {
    await withFixture(async ({ deps, events }) => {
      deps.openSearch.describeDomain = async () => ({ engineVersion: "OpenSearch_2.0", serviceSoftwareOptions });
      let instanceTypesRead = false;
      deps.openSearch.listInstanceTypeDetails = async () => {
        instanceTypesRead = true;
        return ["m7g.large.search"];
      };
      await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
      assert.equal(instanceTypesRead, false);
      assert.ok(!events.includes("set:analytics.opensearch.data_node_instance_type=m7g.large.search"));
      assert.ok(events.includes(`Apply and complete OpenSearch service software update ${serviceSoftwareOptions?.newVersion ?? "(version unavailable)"} before changing the analytics data node instance type (status ${serviceSoftwareOptions?.updateStatus ?? "unavailable"}). Keeping analytics data node instance type m5.large.search.`));
    });
  });
}

test("Phase 4 clears protection, deploys, restores live instances, then saves values", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.deepEqual(
      events.filter((event) => ["clear:i-sample", "deploy", "restore:i-sample", "save-values"].includes(event)),
      ["clear:i-sample", "deploy", "restore:i-sample", "save-values"],
    );
  });
});

test("pre-flight accepts EL10 when eVDI is deployed", async () => {
  // Amazon DCV 2026.0 publishes EL10 packages, so the desktop module no longer rules EL10 out
  await withFixture(async ({ deps, events }) => {
    deps.regionAmiConfig = () => ({ "us-east-2": { rhel10: "ami-release" } });
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "rhel10", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(events.includes("deploy"));
  });
});

test("pre-flight rejects an end-of-life setting before any mutation", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.cluster-settings`]?.push(setting("scheduler.base_os", "amazonlinux2"));
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(!events.some((event) => event.startsWith("Phase ")));
    assert.ok(!events.includes("deploy"));
  });
});

test("disables an in-use end-of-life eVDI stack before Phase 1 using the session base OS fallback", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.vdc.controller.software-stacks`]?.push({
      stack_id: "stack-old",
      base_os: "amazonlinux2",
      name: "old-stack",
      architecture: "x86_64",
    });
    rows[`${clusterName}.vdc.controller.user-sessions`]?.push({
      idea_session_id: "session-sample",
      owner: "operator",
      name: "sample-session",
      base_os: "amazonlinux2",
      software_stack: {},
    });
    await upgradeCluster(deps, {
      clusterName,
      awsRegion,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
      acceptConfigDrift: true,
      disableEolStacksInUse: true,
    });
    const disableAt = events.indexOf("disable-eol");
    const phase1At = events.indexOf("Phase 1: Update Base OS in values.yml");
    assert.ok(disableAt >= 0, "disable-eol never happened");
    assert.ok(phase1At >= 0, "Phase 1 never happened");
    assert.ok(disableAt < phase1At);
  });
});

test("a deployment failure deliberately does not restore cleared termination protection", async () => {
  await withFixture(async ({ deps, events }) => {
    deps.deploy = async () => {
      events.push("deploy");
      throw new Error("simulated deployment failure");
    };
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true }),
      /simulated deployment failure/,
    );
    assert.ok(events.includes("clear:i-sample"));
    assert.ok(!events.includes("restore:i-sample"));
    assert.ok(events.some((event) => event.startsWith("warning: termination protection is still cleared")));
  });
});

test("a successful rerun restores the durable baseline from a failed deployment", async () => {
  await withFixture(async ({ deps, events, rows, protection, protectionTags }) => {
    const options = { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true };
    deps.deploy = async () => { throw new Error("deployment failed"); };
    await assert.rejects(upgradeCluster(deps, options), /deployment failed/);
    assert.equal(protection.has("i-sample"), false);
    assert.equal(protectionTags.get("i-sample"), new Date(deps.now()).toISOString());
    assert.ok(events.indexOf("tag:i-sample") < events.indexOf("clear:i-sample"));
    protectionTags.set("i-other-cluster", "earlier");
    deps.deploy = async () => { for (const row of rows[`${clusterName}.modules`]!) { row["version"] = ideaVersion(); row["status"] = "deployed"; } };
    await upgradeCluster(deps, options);
    assert.ok(protection.has("i-sample"));
    assert.equal(protectionTags.has("i-sample"), false);
    assert.equal(protectionTags.has("i-other-cluster"), true);
    assert.equal(events.filter((event) => event === "clear:i-sample").length, 1);
  });
});

test("a failed marker write leaves protection enabled", async () => {
  await withFixture(async ({ deps, events, protection }) => {
    deps.ec2.createTags = async () => { throw new Error("tagging failed"); };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(protection.has("i-sample"));
    assert.ok(!events.includes("clear:i-sample"));
  });
});

test("all-modules upgrade refuses when ecs is in the table and trunking is disabled", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.modules`]?.push({
      module_id: "ecs",
      name: "ecs",
      type: "stack",
      status: "not-deployed",
      stack_name: `${clusterName}-ecs`,
    });
    deps.ecsAccountSettings = {
      async listAccountSettings() {
        return [{ name: "awsvpcTrunking", value: "disabled" }];
      },
    };
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(!events.includes("deploy"));
    assert.ok(events.some((event) => event.includes("awsvpcTrunking")));
  });
});

test("force alone does not accept overwriting rows that differ from generated configuration", async () => {
  // The fixture table holds a retained OpenSearch type which Phase 3 rewrites. An operator reaching for --force to skip
  // confirmations must not silently lose them, so the run stops and names them.
  await withFixture(async ({ deps, events }) => {
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(!events.includes("deploy"), "no deployment");
    assert.ok(!events.some((event) => event.startsWith("set:")), "no settings write");
    const named = events.find(
      (event) =>
        event.startsWith("err:") &&
        event.includes("configuration row(s) hold a value this upgrade overwrites") &&
        event.includes("differs from generated configuration:"),
    );
    assert.ok(named !== undefined, "the refuse line names the rows at risk");
    for (const key of [
      "analytics.opensearch.data_node_instance_type",
    ]) {
      assert.ok(named.includes(key), key);
    }
    assert.ok(
      events.some((event) => event.includes("--accept-config-drift")),
      "the refusal names the flag that accepts them",
    );
  });
});

test("accept-config-drift proceeds through the same rows without a prompt", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, {
      clusterName,
      awsRegion,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
      acceptConfigDrift: true,
    });
    assert.ok(events.includes("deploy"));
    assert.ok(!events.some((event) => event.startsWith("prompt:")), "force still skips prompts");
    assert.ok(events.some((event) => event.includes("--accept-config-drift: overwriting")));
  });
});

test("createLiveUpgradeDeps supplies the trunking reader used by upgradeCluster", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    const live = createLiveUpgradeDeps(deps);
    const reader = live.ecsAccountSettings?.listAccountSettings;
    assert.equal(typeof reader, "function");
    const source = Function.prototype.toString.call(reader);
    assert.match(source, /ListAccountSettingsCommand/);
    assert.match(source, /ECSClient/);

    const calls: Array<{ name: string; awsRegion: string; effectiveSettings: true }> = [];
    const attached = live.ecsAccountSettings;
    assert.ok(attached !== undefined);
    attached.listAccountSettings = async (input) => {
      calls.push(input);
      return [{ name: "awsvpcTrunking", value: "disabled" }];
    };

    rows[`${clusterName}.modules`]?.push({
      module_id: "ecs",
      name: "ecs",
      type: "stack",
      status: "not-deployed",
      stack_name: `${clusterName}-ecs`,
    });
    await assert.rejects(
      upgradeCluster(live, {
        clusterName,
        awsRegion,
        baseOs: "amazonlinux2023",
        moduleSet: "default",
        force: true,
        acceptConfigDrift: true,
      }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.name, "awsvpcTrunking");
    assert.equal(calls[0]?.awsRegion, awsRegion);
    assert.ok(!events.includes("deploy"));
    assert.ok(events.some((event) => event.includes("awsvpcTrunking")));
  });
});

test("skip-global-settings-update omits the global prefix delete and overwrite sync", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, {
      clusterName,
      awsRegion,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
      acceptConfigDrift: true,
      skipGlobalSettingsUpdate: true,
    });
    assert.ok(!events.includes("delete:global-settings."));
    assert.ok(!events.some((event) => event.startsWith("sync:true:global-settings.")));
    assert.ok(events.some((event) => event.startsWith("sync:false:")));
  });
});

test("a failed values.yml upload warns after a successful deployment", async () => {
  await withFixture(async ({ deps, events }) => {
    deps.s3.putObject = async () => {
      throw new Error("simulated object write failure");
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(events.some((event) => event.includes("warning: Upgrade") && event.includes("simulated object write failure")));
    assert.ok(events.includes("All upgrade phases completed successfully"));
  });
});

test("default upgrade deployment refuses a Replacement change set", async () => {
  await withFixture(async ({ deps, rows }) => {
    const previousCdkBin = process.env.IDEA_CDK_BIN;
    process.env.IDEA_CDK_BIN = "/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk";
    rows[`${clusterName}.modules`] = [moduleRow("analytics", "analytics")];
    try {
      const { withDefaultUpgradeDeployment } = await import("../../src/cli/commands/upgrade.ts");
      const guarded = withDefaultUpgradeDeployment(deps);
      deps.cfn.describeChangeSet = async () => ({
        Status: "CREATE_COMPLETE",
        Changes: [{
          Type: "Resource",
          ResourceChange: {
            Action: "Modify",
            LogicalResourceId: "identityprovideruserpool",
            ResourceType: "AWS::Cognito::UserPool",
            Replacement: "True",
          },
        }],
      });
      await assert.rejects(
        upgradeCluster(guarded, {
          clusterName,
          awsRegion,
          baseOs: "amazonlinux2023",
          moduleSet: "default",
          force: true,
          acceptConfigDrift: true,
          modules: ["analytics"],
        }),
        (error: unknown) => error instanceof Error && error.name === "ChangeSetRefused",
      );
    } finally {
      if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
      else process.env.IDEA_CDK_BIN = previousCdkBin;
    }
  });
});

test("default upgrade deployment executes a settings-only stack change, which stamps the module version", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    const previousCdkBin = process.env.IDEA_CDK_BIN;
    process.env.IDEA_CDK_BIN = "/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk";
    // The row keeps the previous version until the cluster-settings custom resource runs.
    rows[`${clusterName}.modules`] = [{ ...moduleRow("analytics", "analytics"), version: "26.10.0" }];
    let executions = 0;
    deps.cfn.describeChangeSet = async () => ({
      Status: "CREATE_COMPLETE",
      Changes: [change("Modify", "analyticsclustersettings", "Custom::ClusterSettings", "False")],
    });
    deps.cfn.executeChangeSet = async () => {
      executions += 1;
      (rows[`${clusterName}.modules`] as Array<Record<string, unknown>>)[0].version = ideaVersion();
    };
    try {
      const { withDefaultUpgradeDeployment } = await import("../../src/cli/commands/upgrade.ts");
      await upgradeCluster(withDefaultUpgradeDeployment(deps), {
        clusterName,
        awsRegion,
        baseOs: "amazonlinux2023",
        moduleSet: "default",
        force: true,
        acceptConfigDrift: true,
        modules: ["analytics"],
      });
      assert.equal(executions, 1);
      assert.ok(!events.some((event) => event.includes("skipping")));
    } finally {
      if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
      else process.env.IDEA_CDK_BIN = previousCdkBin;
    }
  });
});

test("declining the global-settings prompt leaves Phase 1 applied and stops before Phase 2", async () => {
  await withFixture(async ({ deps, events }) => {
    const answers = ["Yes", "no"];
    deps.prompt = async (choice) => {
      events.push(`prompt:${choice.message}`);
      return answers.shift() ?? false;
    };
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default" }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(events.includes("Phase 1: Update Base OS in values.yml"));
    assert.ok(!events.includes("Phase 2: Global Settings Backup and Update"));
    assert.ok(!events.includes("delete:global-settings."));
  });
});

test("restores values.yml from the cluster bucket when the local file is absent", async () => {
  await withFixture(async ({ deps, events }) => {
    const { valuesFilePath } = await import("../../src/cli/cdk-invoker.ts");
    rmSync(valuesFilePath(clusterName, awsRegion));
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(events.includes("read-values"));
    assert.ok(events.some((event) => event.includes("restoring it from the cluster bucket")));
  });
});

test("deletes an unused end-of-life software stack", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.vdc.controller.software-stacks`]?.push({
      stack_id: "stack-unused",
      base_os: "amazonlinux2",
      name: "unused",
      architecture: "x86_64",
    });
    await upgradeCluster(deps, {
      clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true,
    });
    assert.ok(events.includes("delete-eol"));
    assert.ok(!events.includes("disable-eol"));
  });
});

test("refuses an in-use end-of-life software stack without --disable-eol-stacks-in-use", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.vdc.controller.software-stacks`]?.push({
      stack_id: "stack-old",
      base_os: "amazonlinux2",
      name: "old-stack",
      architecture: "x86_64",
    });
    rows[`${clusterName}.vdc.controller.user-sessions`]?.push({
      idea_session_id: "session-sample",
      owner: "operator",
      name: "sample-session",
      base_os: "amazonlinux2",
      software_stack: {},
    });
    await assert.rejects(
      upgradeCluster(deps, {
        clusterName,
        awsRegion,
        baseOs: "amazonlinux2023",
        moduleSet: "default",
        force: true,
        acceptConfigDrift: true,
      }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(!events.includes("delete-eol"));
    assert.ok(!events.includes("disable-eol"));
    assert.ok(!events.some((event) => event.startsWith("Phase ")));
    assert.ok(!events.includes("deploy"));
    assert.ok(events.some((event) => event.includes("virtual desktop session") && event.includes("end-of-life")));
  });
});

test("a failed protection restore preserves its marker and names the instance", async () => {
  await withFixture(async ({ deps, events, protectionTags }) => {
    deps.ec2.modifyInstanceAttribute = async (input) => {
      if (input.protected === true) throw new Error("simulated restore failure");
      events.push(`clear:${input.instanceId}`);
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(protectionTags.has("i-sample"));
    assert.ok(events.some((event) => event.includes("could not restore termination protection") && event.includes("i-sample")));
  });
});

const containerOptions = { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true } as const;

test("live protection markers use EC2 tags and paginate the scoped survivor query", async (t) => {
  const calls: unknown[] = [];
  t.mock.method(EC2Client.prototype, "send", async (command: CreateTagsCommand | DeleteTagsCommand | DescribeInstancesCommand) => {
    calls.push(command.input);
    if (command instanceof DescribeInstancesCommand) {
      return command.input.NextToken === undefined
        ? { Reservations: [{ Instances: [{ InstanceId: "i-first" }] }], NextToken: "next" }
        : { Reservations: [{ Instances: [{ InstanceId: "i-second" }] }] };
    }
    return {};
  });
  const ec2 = createLiveUpgradeDeps(replay().deps).ec2;
  const key = "idea:TerminationProtectionCleared";
  await ec2.createTags({ awsRegion, instanceId: "i-first", value: "2026-09-15T00:00:00.000Z" });
  assert.deepEqual(await ec2.describeLiveInstances({ awsRegion, instanceIds: ["i-first", "i-second"], tagKey: key }), ["i-first", "i-second"]);
  await ec2.deleteTags({ awsRegion, instanceId: "i-first" });
  const Filters = [
    { Name: "instance-id", Values: ["i-first", "i-second"] },
    { Name: "instance-state-name", Values: ["pending", "running", "stopping", "stopped"] },
    { Name: "tag-key", Values: [key] },
  ];
  assert.deepEqual(calls, [
    { Resources: ["i-first"], Tags: [{ Key: key, Value: "2026-09-15T00:00:00.000Z" }] },
    { Filters, NextToken: undefined },
    { Filters, NextToken: "next" },
    { Resources: ["i-first"], Tags: [{ Key: key }] },
  ]);
});

test("the live OpenSearch adapter reads service software update availability and status", async (t) => {
  t.mock.method(OpenSearchClient.prototype, "send", async (command: DescribeDomainCommand) => {
    assert.equal(command.input.DomainName, undefined);
    return {
      DomainStatus: {
        EngineVersion: "OpenSearch_2.0",
        ServiceSoftwareOptions: { NewVersion: "R20260901-P1", UpdateAvailable: true, UpdateStatus: "ELIGIBLE" },
      },
    };
  });
  assert.deepEqual(await createLiveUpgradeDeps(replay().deps).openSearch.describeDomain({ awsRegion }), {
    engineVersion: "OpenSearch_2.0",
    serviceSoftwareOptions: { newVersion: "R20260901-P1", updateAvailable: true, updateStatus: "ELIGIBLE" },
  });
});

test("a failed marker deletion can be retried after protection is restored", async () => {
  await withFixture(async ({ deps, protection, protectionTags }) => {
    const deleteTags = deps.ec2.deleteTags;
    deps.ec2.deleteTags = async () => { throw new Error("tag deletion failed"); };
    const options = { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true };
    await upgradeCluster(deps, options);
    assert.ok(protection.has("i-sample"));
    assert.ok(protectionTags.has("i-sample"));
    deps.ec2.deleteTags = deleteTags;
    await upgradeCluster(deps, options);
    assert.ok(protection.has("i-sample"));
    assert.equal(protectionTags.has("i-sample"), false);
  });
});

test("--allow-replacement reaches the deploy guard from upgrade-cluster", async () => {
  await withFixture(async ({ deps }) => {
    const seen: Array<readonly string[] | undefined> = [];
    const originalDeploy = deps.deploy;
    deps.deploy = async (input) => { seen.push(input.allowReplacement); await originalDeploy(input); };
    await upgradeCluster(deps, { ...containerOptions, allowReplacement: ["bastionhostinstance", "other"] });
    assert.deepEqual(seen, [["bastionhostinstance", "other"]]);
    const program = new Command("ideactl");
    registerUpgradeCommands(program, deps);
    assert.match(program.commands[0]!.helpInformation(), /--allow-replacement <logical-id>/);
  });
});

test("a cluster-manager that already knows the container module keeps its module-set row through the run", async () => {
  // On the first production upgrade of a container cluster the row was held for the whole run and
  // every portal page answered 500 until the last stack finished: the hold asked whether the
  // portal was already on the target release, not whether it could resolve the module.
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    trunkingEnabled(deps);
    rows[`${clusterName}.modules`]!.push(
      { ...moduleRow("ecs", "ecs"), version: "26.10.0" },
      { ...moduleRow("cluster-manager", "cluster-manager"), version: "26.10.0" },
    );
    rows[`${clusterName}.cluster-settings`]!.push(setting("global-settings.module_sets.default.ecs.module_id", "ecs"));
    rows[`${clusterName}.cluster-settings`]!.push(
      setting("global-settings.module_sets.default.analytics.module_id", "search"),
      setting("global-settings.retired_option", "still needed"),
    );
    const originalWriter = deps.configWriter;
    deps.configWriter = async (input) => {
      const writer = await originalWriter(input);
      return new Proxy(writer, {
        get(target, property) {
          const method = Reflect.get(target, property) as (...args: unknown[]) => Promise<void>;
          if (typeof method !== "function") return method;
          return async (...args: unknown[]) => {
            await method.apply(target, args);
            // Every write before deployment leaves the rows a running application reads in place;
            // a row this release no longer generates goes only once the stacks have deployed.
            assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "global-settings.module_sets.default.analytics.module_id")?.["value"], "search");
            if (!events.includes("deploy")) assert.ok(rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "global-settings.retired_option"));
            assert.ok(rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "global-settings.module_sets.default.ecs.module_id"));
          };
        },
      });
    };
    deps.deploy = async () => {
      events.push("deploy");
      for (const row of rows[`${clusterName}.modules`]!) { row["version"] = ideaVersion(); row["status"] = "deployed"; }
    };
    await upgradeCluster(deps, containerOptions);
    const globalRewrite = events.findIndex((event) => event.startsWith("sync:true:"));
    const deploy = events.indexOf("deploy");
    const republished = events.findIndex((event, index) => index > globalRewrite && event.startsWith("module-sets:") && announcedModules(event).includes("ecs"));
    assert.ok(globalRewrite >= 0 && republished > globalRewrite && republished < deploy, `the row must come back before any stack deploys: ${events.join(" | ")}`);
  });
});

test("a scoped upgrade preserves ECS when cluster-manager completed the target release", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    rows[`${clusterName}.modules`]!.push(
      { ...moduleRow("ecs", "ecs"), version: ideaVersion() },
      { ...moduleRow("cluster-manager", "cluster-manager"), version: ideaVersion() },
    );
    rows[`${clusterName}.cluster-settings`]!.push(setting("global-settings.module_sets.default.ecs.module_id", "ecs"));
    await upgradeCluster(deps, { ...containerOptions, modules: ["analytics"] });
    assert.ok(rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "global-settings.module_sets.default.ecs.module_id"));
    assert.ok(!events.includes("set:cluster-manager.maintenance.enabled=true"));
  });
});

function supportedReplay(replayValue: Replay): void {
  const { deps, rows } = replayValue;
  rows[`${clusterName}.modules`]!.push(moduleRow("cluster-manager", "cluster-manager"), { ...moduleRow("cluster", "cluster"), type: "stack" }, { ...moduleRow("global-settings", "global-settings"), type: "config" });
  for (const row of rows[`${clusterName}.modules`]!) row["version"] = "26.10.0";
  rows[`${clusterName}.cluster-settings`]!.push(setting("vdc.dcv_host_role_name", "sample-host"));
  deps.cfn.getTemplate = async () => JSON.stringify({ Resources: {} });
}

for (const failure of ["version", "setting", "published"]) {
  test(`completion refuses missing ${failure} readback`, async () => {
    await withFixture(async ({ deps, rows, events }) => {
      rows[`${clusterName}.cluster-settings`]!.push({ ...setting("scheduler.published", "old"), source: "stack" });
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        if (failure === "version") rows[`${clusterName}.modules`]![0]!["version"] = "26.09.0";
        else rows[`${clusterName}.cluster-settings`] = rows[`${clusterName}.cluster-settings`]!.filter((row) => row["key"] !== (failure === "setting" ? "scheduler.instance_ami" : "scheduler.published"));
      };
      await assert.rejects(upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true }), /Completion verification failed/);
      assert.ok(!events.includes("All upgrade phases completed successfully"));
    });
  });
}

for (const scenario of ["promoted", "requeued", "arm64", "missing", "failed", "wrong OS", "wrong AMI", "custom row", "desktop row", "operator write", "other setting"]) {
  test(`completion scopes the compute pipeline promotion exception: ${scenario}`, async () => {
    await withFixture(async ({ deps, rows, events }) => {
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        const key = scenario === "other setting" ? "scheduler.instance_ami" : "scheduler.compute_node_ami";
        const settingRow = rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!;
        settingRow["value"] = "ami-promoted";
        settingRow["source"] = scenario === "operator write" ? "user" : "sdk";
        rows[`${clusterName}.scheduler.image-builds`] = scenario === "missing" ? [] : [{
          base_os: scenario === "wrong OS" ? "rocky9" : "amazonlinux2023",
          architecture: scenario === "custom row" ? "x86_64#custom" : scenario === "arm64" ? "arm64" : "x86_64",
          kind: scenario === "desktop row" ? "desktop" : "compute",
          // A refresh after promotion requeues the row but keeps the active image.
          status: scenario === "failed" ? "failed" : scenario === "requeued" ? "queued" : "current",
          current_image_id: scenario === "wrong AMI" ? "ami-other" : "ami-promoted",
        }];
        // The pipeline's atomic promotion also moves queue profiles referencing the old AMI.
        rows[`${clusterName}.scheduler.queue-profiles`] = [{ queue_profile_id: "compute", param_instance_ami: "ami-promoted" }];
      };
      const run = upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true });
      if (["promoted", "requeued", "arm64"].includes(scenario)) {
        await run;
        assert.ok(events.some((event) => event.startsWith("Completion verified pipeline promotion for scheduler.compute_node_ami")));
      } else {
        await assert.rejects(run, /Completion verification failed for setting scheduler\./);
        assert.ok(!events.includes("All upgrade phases completed successfully"));
      }
    });
  });
}

test("each scheduler keeps its newer built compute image", async () => {
  await withFixture(async ({ deps, rows }) => {
    rows[`${clusterName}.modules`]!.push(moduleRow("batch", "scheduler"));
    rows[`${clusterName}.cluster-settings`]!.push(setting("batch.compute_node_ami", "ami-built"), setting("batch.compute_node_os", "rocky9"));
    await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "batch.compute_node_ami")?.["value"], "ami-built");
    assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "batch.compute_node_os")?.["value"], "rocky9");
  });
});

for (const enumerationFails of [false, true]) {
  test(`restore a surviving original instance after deployment, enumeration fails=${enumerationFails}`, async () => {
    await withFixture(async ({ deps, protection }) => {
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        deps.cloudFormation.listStackResources = async () => {
          if (enumerationFails) throw new Error("enumeration unavailable");
          return { instanceIds: [] };
        };
      };
      await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true });
      assert.ok(protection.has("i-sample"));
    });
  });
}

for (const point of ["global-write", "full-sync", "phase3", "deployment"]) {
  test(`retry converges after failure at ${point}`, async () => {
    await withFixture(async ({ deps, rows, events }) => {
      const originalWriter = deps.configWriter;
      const originalDeploy = deps.deploy;
      let failed = false;
      deps.configWriter = async (input) => {
        const writer = await originalWriter(input);
        return {
          ...writer,
          async syncClusterSettingsInDb(entries, overwrite, source) {
            if (!failed && ((point === "global-write" && overwrite) || (point === "full-sync" && !overwrite))) {
              failed = true;
              throw new Error("injected interruption");
            }
            await writer.syncClusterSettingsInDb(entries, overwrite, source);
          },
          async setConfigEntry(key, value) {
            if (!failed && point === "phase3" && key === "analytics.opensearch.data_node_instance_type") {
              failed = true;
              throw new Error("injected interruption");
            }
            await writer.setConfigEntry(key, value);
          },
        };
      };
      deps.deploy = async (input) => {
        if (!failed && point === "deployment") { failed = true; throw new Error("injected interruption"); }
        await originalDeploy(input);
      };
      const options = { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true };
      await assert.rejects(upgradeCluster(deps, options), /injected interruption/);
      assert.ok(!events.includes("All upgrade phases completed successfully"));
      await upgradeCluster(deps, options);
      assert.ok(events.includes("All upgrade phases completed successfully"));
      assert.ok(!rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "cluster.upgrade_module_set_owners"));
    });
  });
}

test("original cleared IDs restore protection even when their tags disappear", async () => {
  await withFixture(async ({ deps, protection, protectionTags }) => {
    const deploy = deps.deploy;
    deps.deploy = async (input) => { await deploy(input); protectionTags.clear(); };
    await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(protection.has("i-sample"));
  });
});

for (const suffix of ["metrics.cost.last_collected", "metrics.storage.last_collected", "metrics.storage.usage_snapshot", "accounts.reconcile.last_completed", "metrics.cost.enabled", "unrelated.last_published"]) {
  test(`completion scopes runtime checkpoint exceptions: ${suffix}`, async () => {
    await withFixture(async ({ deps, rows }) => {
      rows[`${clusterName}.modules`]!.push(moduleRow("cluster-manager", "cluster-manager"));
      const key = `cluster-manager.${suffix}`;
      rows[`${clusterName}.cluster-settings`]!.push(setting(key, 1));
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!["value"] = 2;
      };
      if (suffix.endsWith("last_collected") || suffix.endsWith("last_completed") || suffix.endsWith("usage_snapshot")) await upgradeCluster(deps, containerOptions);
      else await assert.rejects(upgradeCluster(deps, containerOptions), /Completion verification failed for setting/);
    });
  });
}

test("completion lets the desktop controller record its seeded release during the upgrade", async () => {
  await withFixture(async ({ deps, rows }) => {
    const key = "vdc.software_stacks.base_stacks_seeded_release";
    rows[`${clusterName}.cluster-settings`]!.push(setting(key, "26.09.5"));
    const deploy = deps.deploy;
    deps.deploy = async (input) => {
      await deploy(input);
      rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!["value"] = "26.10.0";
    };
    await upgradeCluster(deps, containerOptions);
  });
});

for (const reorderArray of [false, true]) {
  test(`completion compares nested maps structurally and arrays in order: ${reorderArray}`, async () => {
    await withFixture(async ({ deps, rows }) => {
      const key = "scheduler.custom_map";
      rows[`${clusterName}.cluster-settings`]!.push(setting(key, { first: { a: 1, b: 2 }, second: [1, 2] }));
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!["value"] = { second: reorderArray ? [2, 1] : [1, 2], first: { b: 2, a: 1 } };
      };
      if (reorderArray) await assert.rejects(upgradeCluster(deps, containerOptions), /setting scheduler.custom_map/);
      else await upgradeCluster(deps, containerOptions);
    });
  });
}

for (const changed of [false, true]) {
  test(`completion structurally checks target publications: changed=${changed}`, async () => {
    await withFixture(async ({ deps, rows }) => {
      const key = "scheduler.published_map";
      rows[`${clusterName}.cluster-settings`]!.push({ ...setting(key, { a: 0 }), source: "stack" });
      const drift = await prepareUpgradeDriftInput(deps, containerOptions);
      drift.stacks!.find((stack) => stack.moduleId === "scheduler")!.target = { published_map: { a: 1, b: { c: 2, d: 3 } } };
      deps.loadUpgradeDriftInput = async () => drift;
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!["value"] = { b: { d: 3, c: changed ? 4 : 2 }, a: 1 };
      };
      if (changed) await assert.rejects(upgradeCluster(deps, containerOptions), /published setting scheduler.published_map/);
      else await upgradeCluster(deps, containerOptions);
    });
  });
}

test("completion accepts reordered saved module-set owners", async () => {
  await withFixture(async ({ deps, rows }) => {
    rows[`${clusterName}.cluster-settings`]!.push(setting("global-settings.module_sets.secondary.cluster-manager.module_id", "cluster-manager"));
    const scan = deps.scan;
    deps.scan = async (input) => {
      const result = await scan(input);
      return {
        ...result, Items: result.Items?.map((row) => row["key"] === "cluster.upgrade_module_set_owners"
          ? { ...row, value: Object.fromEntries(Object.entries(row["value"] as Record<string, unknown>).reverse()) } : row)
      };
    };
    await upgradeCluster(deps, containerOptions);
  });
});

test("moving metrics to the agent daemon writes the provider rows the add-only sync would skip", async () => {
  // The host cluster runs on CloudWatch. With `metrics_provider: dogstatsd` in values the generated
  // configuration turns the daemon on, but `metrics.provider` already exists, so the full sync
  // keeps CloudWatch and the tasks would start sending nowhere. The one run must land both rows.
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    sendMetricsToDatadog();
    trunkingEnabled(deps);
    rows[`${clusterName}.cluster-settings`]!.push(setting("metrics.provider", "cloudwatch"));
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });

    const table = rows[`${clusterName}.cluster-settings`]!;
    const value = (key: string): unknown => table.find((row) => row["key"] === key)?.["value"];
    assert.equal(value("metrics.provider"), "dogstatsd");
    assert.equal(value("metrics.dogstatsd.url"), "unix:///var/run/datadog/dsd.socket");
    assert.equal(value("ecs.datadog.enabled"), true);
    assert.equal(value("ecs.datadog.image"), DATADOG_AGENT_IMAGE, "the release's official agent image is the default");
    const cutover = events.indexOf("set:metrics.provider=dogstatsd");
    const phase3 = events.indexOf("Phase 3: Update AMI IDs and Settings");
    const phase4 = events.indexOf("Phase 4: Module Deployment");
    assert.ok(cutover > phase3 && cutover < phase4, `the provider moves in Phase 3: ${events.join(" | ")}`);
  });
});

test("a cluster that keeps CloudWatch sees no provider write", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    trunkingEnabled(deps);
    rows[`${clusterName}.cluster-settings`]!.push(setting("metrics.provider", "cloudwatch"));
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });

    assert.ok(!events.some((event) => event.startsWith("set:metrics.provider=")), events.filter((event) => event.startsWith("set:")).join(" | "));
    const table = rows[`${clusterName}.cluster-settings`]!;
    assert.equal(table.find((row) => row["key"] === "metrics.provider")?.["value"], "cloudwatch");
  });
});

/** A deployed template whose settings resource publishes exactly `keys`. */
function publishingTemplate(keys: string[]): string {
  return JSON.stringify({
    Resources: {
      settings: { Type: "Custom::ClusterSettings", Properties: { module_id: "scheduler", settings: Object.fromEntries(keys.map((key) => [key, { Ref: "AWS::StackId" }])) } },
      schedulerdnsrecord: { Type: "AWS::Route53::RecordSet", Properties: {} },
    },
  });
}

for (const stillPublished of [false, true]) {
  test(`completion reads the deployed template: a row the stack stopped publishing may be gone (${stillPublished ? "still published, must exist" : "dropped, may be absent"})`, async () => {
    // The metrics stack publishes its CloudWatch dashboard ARN only on a CloudWatch cluster; once the
    // provider is DogStatsD the deployed template no longer names the row and its settings handler
    // deletes it. Requiring every previously published row to survive stopped a live upgrade at the
    // read-back after every stack had deployed. The deployed template is the truth about what a
    // stack publishes now.
    await withFixture(async ({ deps, rows, events }) => {
      rows[`${clusterName}.cluster-settings`]!.push({ ...setting("scheduler.published", "old"), source: "stack" });
      deps.cfn.getTemplate = async () => publishingTemplate(stillPublished ? ["deployment_id", "published"] : ["deployment_id"]);
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        await deploy(input);
        rows[`${clusterName}.cluster-settings`] = rows[`${clusterName}.cluster-settings`]!.filter((row) => row["key"] !== "scheduler.published");
      };
      const options = { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true };
      if (stillPublished) {
        await assert.rejects(upgradeCluster(deps, options), /Completion verification failed for published setting scheduler.published/);
      } else {
        await upgradeCluster(deps, options);
        assert.ok(events.includes("scheduler.published is no longer published by the deployed idea-test1-scheduler stack"), events.filter((event) => event.includes("published")).join(" | "));
        assert.ok(events.includes("All upgrade phases completed successfully"));
      }
    });
  });
}

test("the cleared-protection warning names only instances that still exist", async () => {
  // The deployment replaces the instances it cleared, and a warning naming a terminated instance
  // sends the operator after a ghost.
  await withFixture(async ({ deps, rows, events, protection }) => {
    rows[`${clusterName}.cluster-settings`]!.push({ ...setting("scheduler.published", "old"), source: "stack" });
    protection.add("i-survivor");
    protection.add("i-replaced");
    deps.cloudFormation.listStackResources = async (input) => ({ instanceIds: input.stackName.endsWith("-scheduler") ? ["i-survivor", "i-replaced"] : [] });
    deps.ec2.describeLiveInstances = async (input) => input.instanceIds.filter((id) => id === "i-survivor");
    const deploy = deps.deploy;
    deps.deploy = async (input) => {
      await deploy(input);
      rows[`${clusterName}.cluster-settings`] = rows[`${clusterName}.cluster-settings`]!.filter((row) => row["key"] !== "scheduler.published");
    };
    await assert.rejects(upgradeCluster(deps, { clusterName, awsRegion, moduleSet: "default", force: true, acceptConfigDrift: true }), /Completion verification failed/);
    const warning = events.find((event) => event.startsWith("warning: termination protection is still cleared"));
    assert.ok(warning, events.filter((event) => event.startsWith("warning")).join(" | "));
    assert.ok(warning.includes("i-survivor") && !warning.includes("i-replaced"), warning);
  });
});

for (const hasHost of [true, false]) {
  test(`bastion cutover reads deployed publications and tolerates no instance: host=${hasHost}`, async () => {
    await withFixture(async ({ deps, rows, events, protection }) => {
      enableContainers();
      trunkingEnabled(deps);
      rows[`${clusterName}.modules`]!.push({ ...moduleRow('bastion-host', 'bastion-host'), type: 'stack' });
      rows[`${clusterName}.cluster-settings`]!.push(
        setting('bastion-host.base_os', 'amazonlinux2023'),
        setting('bastion-host.instance_ami', 'ami-old'),
        setting('bastion-host.instance_type', 'm7i.large'),
        ...['instance_id', 'private_ip', 'instance_profile_arn', 'public_ip', 'private_dns_name'].map((key) => ({ ...setting(`bastion-host.${key}`, 'old'), source: 'stack' })),
      );
      let deployed = false;
      const list = deps.cloudFormation.listStackResources;
      deps.cloudFormation.listStackResources = async (input) => input.stackName.endsWith('-bastion-host')
        ? { instanceIds: hasHost && !deployed ? ['i-bastion'] : [] } : list(input);
      if (hasHost) protection.add('i-bastion');
      deps.ec2.describeLiveInstances = async (input) => input.instanceIds.filter((id) => !(deployed && id === 'i-bastion'));
      deps.cfn.getTemplate = async (stackName) => stackName.endsWith('-bastion-host') && deployed
        ? JSON.stringify({ Resources: { settings: { Type: 'Custom::ClusterSettings', Properties: { settings: { public_ip: 'stable', private_dns_name: 'nlb.example.invalid' } } } } })
        : '{}';
      const deploy = deps.deploy;
      deps.deploy = async (input) => {
        assert.deepEqual(input.allowReplacement ?? [], []);
        await deploy(input);
        deployed = true;
        const retired = ['instance_id', 'private_ip', 'instance_profile_arn'].map((key) => `bastion-host.${key}`);
        rows[`${clusterName}.cluster-settings`] = rows[`${clusterName}.cluster-settings`]!.filter((row) => !retired.includes(String(row['key'])));
      };
      await upgradeCluster(deps, containerOptions);
      assert.ok(events.includes('All upgrade phases completed successfully'));
      assert.ok(!events.includes('restore:i-bastion'));
      assert.equal(events.includes('clear:i-bastion'), hasHost);
      assert.ok(events.some((event) => event.includes('bastion-host.instance_id is no longer published')));
      assert.ok(rows[`${clusterName}.modules`]!.some((row) => row['module_id'] === 'bastion-host'));
    });
  });
}

test("a global row this release no longer generates is removed after every stack has deployed, never before", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    rows[`${clusterName}.cluster-settings`]!.push(setting("global-settings.retired_option", "still needed"), setting("global-settings.retired_option_family", "kept"));
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deploy = events.indexOf("deploy");
    const removed = events.indexOf("delete:global-settings.retired_option_family");
    assert.ok(removed > deploy, `removed after deployment: ${events.filter((event) => event.startsWith("delete:")).join(" | ")}`);
    assert.ok(!events.slice(0, deploy).some((event) => event.startsWith("delete:global-settings.")), "nothing global is deleted while applications may read it");
    // The writer deletes by prefix, so the key that prefixes its sibling is left alone and named.
    assert.ok(!events.includes("delete:global-settings.retired_option"));
    assert.ok(events.some((event) => event.includes("global-settings.retired_option is no longer generated but prefixes another row")));
  });
});

test("the image row follows the release only when it names the release repository at an older release tag", () => {
  const repository = "public.ecr.aws/s5o2b4m0/idea-control-plane";
  const rows = (image: string) => [
    { key: "ecs.image_repositories.aws", value: repository },
    { key: "ecs.image", value: image },
  ];
  assert.deepEqual(
    planEcsImageFollowsRelease(rows(`${repository}:26.09.3`), "26.09.4"),
    [{ key: "ecs.image", value: `${repository}:26.09.4` }],
  );
  for (const image of [
    `${repository}:26.09.4`,
    `${repository}:26.10.0`,
    `${repository}:build-6e82332-demo1`,
    `${repository}@sha256:${"0".repeat(64)}`,
    "private.example/idea-control-plane:26.09.3",
  ]) {
    assert.deepEqual(planEcsImageFollowsRelease(rows(image), "26.09.4"), [], image);
  }
  assert.deepEqual(planEcsImageFollowsRelease([{ key: "ecs.image", value: `${repository}:26.09.3` }], "26.09.4"), [], "no repository row");
});

test("an earlier release's candidate image follows the release; this release's candidate stays", () => {
  const repository = "public.ecr.aws/s5o2b4m0/idea-control-plane";
  const rows = (image: string) => [
    { key: "ecs.image_repositories.aws", value: repository },
    { key: "ecs.image", value: image },
  ];
  for (const tag of ["26.10.3-rc.2", "26.09.1-rc.9"]) {
    assert.deepEqual(
      planEcsImageFollowsRelease(rows(`${repository}:${tag}`), "26.10.4"),
      [{ key: "ecs.image", value: `${repository}:26.10.4` }],
      tag,
    );
  }
  // this release's own candidate is the build being proven, so it stays
  for (const tag of ["26.10.4-rc.1", "26.10.4-rc.12", "26.10.5-rc.1", "26.11.0-rc.1", "26.10.4-rc.0", "26.10.4-rc", "26.10.4-rc.1x", "26.10.4-beta.1"]) {
    assert.deepEqual(planEcsImageFollowsRelease(rows(`${repository}:${tag}`), "26.10.4"), [], tag);
  }
  assert.deepEqual(
    planEcsImageFollowsRelease(
      [{ key: "ecs.image_repositories.aws", value: repository }, { key: "ecs.image", value: "private.example/idea-control-plane:26.10.4-rc.1" }],
      "26.10.4",
    ),
    [],
    "a private registry stays",
  );
});

function hostReturnDeps(hosts: { minSize: number; instances: string[] }, empties: boolean) {
  const out: string[] = [];
  const calls: string[] = [];
  const deps = {
    out: (line: string) => out.push(line),
    containerHosts: {
      async hostGroup() {
        return { name: "hosts", minSize: hosts.minSize, desiredCapacity: hosts.instances.length, instanceIds: hosts.instances };
      },
      async containerInstances() {
        return [
          { arn: "arn:a", instanceId: "i-a", status: "ACTIVE", runningTasks: 5, registeredAt: "2026-09-15T00:00:00.000Z" },
          { arn: "arn:d", instanceId: "i-d", status: "ACTIVE", runningTasks: 3, registeredAt: "2026-09-21T14:23:00.000Z" },
          { arn: "arn:b", instanceId: "i-b", status: "ACTIVE", runningTasks: 2, registeredAt: "2026-09-15T00:00:00.000Z" },
          { arn: "arn:c", instanceId: "i-c", status: "ACTIVE", runningTasks: 5, registeredAt: "2026-09-15T00:00:00.000Z" },
        ];
      },
      async drain(input: { arn: string }) { calls.push(`drain ${input.arn}`); },
      async activate(input: { arn: string }) { calls.push(`activate ${input.arn}`); },
      async waitUntilEmpty(input: { arn: string }) { calls.push(`wait ${input.arn}`); return empties; },
      async release(input: { instanceId: string; desiredCapacity: number }) { calls.push(`release ${input.instanceId} desired=${input.desiredCapacity}`); },
    },
  } as unknown as UpgradeDeps;
  return { deps, out, calls };
}

const HOST_ROWS = [{ key: "ecs.cluster_name", value: "cluster-ecs" }, { key: "ecs.capacity_provider", value: "cluster-capacity" }];

test("return-hosts drains the newest borrowed host and shrinks the group once it is empty", async () => {
  const { deps, out, calls } = hostReturnDeps({ minSize: 3, instances: ["i-a", "i-b", "i-c", "i-d"] }, true);
  await returnBorrowedHosts(deps, { clusterName: "cluster", awsRegion: "us-east-1" }, HOST_ROWS);
  assert.deepEqual(calls, ["drain arn:d", "wait arn:d", "release i-d desired=3"]);
  assert.ok(out.some((line) => line.includes("Returned i-d")), out.join("\n"));
});

test("return-hosts leaves a host in service when it does not empty in time", async () => {
  const { deps, out, calls } = hostReturnDeps({ minSize: 3, instances: ["i-a", "i-b", "i-c", "i-d"] }, false);
  await returnBorrowedHosts(deps, { clusterName: "cluster", awsRegion: "us-east-1" }, HOST_ROWS);
  assert.deepEqual(calls, ["drain arn:d", "wait arn:d", "activate arn:d"]);
  assert.ok(out.some((line) => line.startsWith("warning: i-d still ran tasks")), out.join("\n"));
});

test("return-hosts does nothing at the group minimum or without container settings", async () => {
  const atMinimum = hostReturnDeps({ minSize: 3, instances: ["i-a", "i-b", "i-c"] }, true);
  await returnBorrowedHosts(atMinimum.deps, { clusterName: "cluster", awsRegion: "us-east-1" }, HOST_ROWS);
  assert.deepEqual(atMinimum.calls, []);
  assert.ok(atMinimum.out.some((line) => line.includes("nothing to return")));
  const noContainers = hostReturnDeps({ minSize: 3, instances: ["i-a", "i-b", "i-c", "i-d"] }, true);
  await returnBorrowedHosts(noContainers.deps, { clusterName: "cluster", awsRegion: "us-east-1" }, []);
  assert.deepEqual(noContainers.calls, []);
});

async function routineReleaseFixture(state: Replay): Promise<Map<string, unknown>> {
  enableContainers();
  const { deps, rows } = state;
  rows[`${clusterName}.modules`]!.push({ ...moduleRow("ecs", "ecs"), type: "stack" });
  deps.ecsAccountSettings = { async listAccountSettings() { return [{ name: "awsvpcTrunking", value: "enabled" }]; } };
  const preview = await prepareUpgradeDriftInput(deps, containerOptions);
  const generated = new Map(preview.generated.map((entry) => [entry.key, entry.value]));
  for (const row of rows[`${clusterName}.cluster-settings`]!) {
    if (generated.has(String(row["key"]))) row["value"] = generated.get(String(row["key"]));
  }
  return generated;
}

test("routine release moves the release image without accepting drift and preserves a private image", async () => {
  for (const privateImage of [false, true]) {
    await withFixture(async (state) => {
      const generated = await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      const repository = generated.get("ecs.image_repositories.aws");
      const image = privateImage ? "private.example/control-plane:custom" : `${repository}:26.09.0`;
      rows[`${clusterName}.cluster-settings`]!.push(
        setting("ecs.image_repositories.aws", repository),
        { ...setting("ecs.image", image), source: privateImage ? "cli" : "stack" },
      );
      await upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: false });
      assert.ok(events.includes("deploy"));
      const stored = rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "ecs.image");
      assert.equal(stored?.["value"], privateImage ? image : `${repository}:${ideaVersion()}`);
      assert.equal(events.some((event) => event.startsWith("ecs.image moves from")), !privateImage);
      assert.ok(events.some((event) => event.includes(privateImage ? "PRESERVE_DRIFT" : "release image updated")));
      assert.ok(!events.some((event) => event.startsWith("err:") && event.includes("configuration row(s)")));
    });
  }
});

test("template defaults update silently, operator rows keep their values, legacy and stack markers still require review", async () => {
  const defaults = [
    { key: "global-settings.gpu_settings.instance_families", value: ["g4dn", "g5"] },
    { key: "global-settings.package_config.efa.version", value: "1.0.0" },
    { key: "global-settings.package_config.efs_utils.version", value: "v1.0.0" },
  ];
  for (const source of ["template", "cli", "api", "stack", undefined]) {
    await withFixture(async (state) => {
      const generated = await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      rows[`${clusterName}.cluster-settings`]!.push(...defaults.map((entry) => ({ ...entry, source })));
      const run = () => upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: false });
      const operator = source === "cli" || source === "api";
      if (source !== "template" && !operator) {
        await assert.rejects(run(), (error: unknown) => error instanceof Error && error.name === "ExitWithCode");
        assert.ok(!events.includes("deploy"));
        assert.ok(!events.some((event) => event.startsWith("set:") || event.startsWith("sync:")));
        await upgradeCluster(deps, containerOptions);
      } else {
        await run();
        assert.ok(!events.some((event) => event.includes("--accept-config-drift:")));
        if (operator) assert.ok(events.some((event) => event.includes("Operator values kept:")));
        else assert.ok(events.some((event) => event.includes("Template defaults updated:")));
      }
      for (const { key, value } of defaults) {
        const row = rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key);
        // The operator's row keeps its value and its marker; every other row follows the template.
        assert.deepEqual(row?.["value"], operator ? value : generated.get(key));
        assert.equal(row?.["source"], operator ? source : "template");
      }
      assert.ok(events.includes("deploy"));
    });
  }
});

test("an unpublished release image stops the upgrade before anything is written", async () => {
  for (const answer of ["missing", "unknown"] as const) {
    await withFixture(async (state) => {
      const generated = await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      const repository = generated.get("ecs.image_repositories.aws");
      const image = `${repository}:26.09.0`;
      rows[`${clusterName}.cluster-settings`]!.push(
        setting("ecs.image_repositories.aws", repository),
        { ...setting("ecs.image", image), source: "stack" },
      );
      const asked: string[] = [];
      deps.releaseImageExists = async (target: string) => {
        asked.push(target);
        if (answer === "unknown") throw new Error("registry unreachable");
        return false;
      };
      await assert.rejects(
        upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: false }),
        (error: unknown) =>
          error instanceof Error &&
          error.message.includes(`${repository}:${ideaVersion()}`) &&
          error.message.includes(`ecs.image stays ${image}`) &&
          (answer === "missing" ? error.message.includes(`${repository}:${ideaVersion()}-rc.<N>`) : error.message.includes("registry unreachable")),
      );
      assert.deepEqual(asked, [`${repository}:${ideaVersion()}`]);
      assert.ok(!events.includes("deploy"));
      assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "ecs.image")?.["value"], image);
    });
  }
});

test("preview reports release image blockers, completes the report, and exits non-zero at the command boundary", async () => {
  for (const answer of ["missing", "unknown"] as const) {
    await withFixture(async (state) => {
      const generated = await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      const repository = generated.get("ecs.image_repositories.aws");
      rows[`${clusterName}.cluster-settings`]!.push(
        setting("ecs.image_repositories.aws", repository),
        { ...setting("ecs.image", `${repository}:26.09.0`), source: "stack" },
      );
      deps.releaseImageExists = async () => {
        if (answer === "unknown") throw new Error("registry unreachable");
        return false;
      };
      let refusal = "";
      await assert.rejects(upgradeCluster(deps, containerOptions), (error: Error) => {
        refusal = error.message;
        return true;
      });
      // Keep the real input builder while injecting the replay's read adapters.
      const previewDeps = Object.create(deps) as UpgradeDeps;
      previewDeps.loadUpgradeDriftInput = (options) => prepareUpgradeDriftInput(deps, options, true);
      const report = await configUpgradePreview(previewDeps, containerOptions);
      assert.deepEqual(report.blockingFindings, [refusal]);
      assert.ok(report.findings.length > 0, "remaining preview is complete");
      assert.ok(events.some((event) => event.includes(`BLOCKING: ${refusal}`)));
      const program = new Command("ideactl");
      registerConfigCommands(program, previewDeps);
      await assert.rejects(program.parseAsync([
        "config", "preview-upgrade", "--cluster-name", clusterName, "--aws-region", awsRegion,
      ], { from: "user" }), (error: unknown) => error instanceof ExitWithCode && error.code === 1);
      assert.ok(!events.some((event) => event === "deploy" || event.startsWith("set:") || event.startsWith("sync:")));
    });
  }
});

test("both command flags skip the registry lookup and keep the release image move", async () => {
  for (const preview of [false, true]) {
    await withFixture(async (state) => {
      const generated = await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      const repository = generated.get("ecs.image_repositories.aws");
      rows[`${clusterName}.cluster-settings`]!.push(
        setting("ecs.image_repositories.aws", repository),
        { ...setting("ecs.image", `${repository}:26.09.0`), source: "stack" },
      );
      deps.releaseImageExists = async () => assert.fail("skipped lookup must not run");
      const program = new Command("ideactl");
      if (preview) {
        const previewDeps = Object.create(deps) as UpgradeDeps;
        previewDeps.loadUpgradeDriftInput = async (options) => {
          const input = await prepareUpgradeDriftInput(deps, options, true);
          assert.deepEqual(input.blockingFindings, []);
          assert.ok(input.phase3?.some((entry) => entry.key === "ecs.image" && entry.value === `${repository}:${ideaVersion()}`));
          return input;
        };
        registerConfigCommands(program, previewDeps);
      } else registerUpgradeCommands(program, deps);
      await program.parseAsync([
        ...(preview ? ["config", "preview-upgrade"] : ["upgrade-cluster", "--force", "--accept-config-drift"]),
        "--cluster-name", clusterName, "--aws-region", awsRegion, "--skip-release-image-check",
      ], { from: "user" });
      assert.equal(events.filter((event) => event.includes("was not verified (--skip-release-image-check)")).length, 1);
      if (!preview) {
        assert.ok(events.includes("deploy"));
        assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "ecs.image")?.["value"], `${repository}:${ideaVersion()}`);
      }
    });
  }
});

test("upgrade-cluster deploys each priority group's stacks in parallel unless told not to", async () => {
  // The serial path waited on each module stack in turn, so the scheduler stack queued behind the
  // desktop stack although neither reads what the other writes.
  for (const [flags, expected] of [[[], true], [["--no-optimize-deployment"], false], [["--optimize-deployment"], true]] as const) {
    await withFixture(async (state) => {
      await routineReleaseFixture(state);
      const { deps } = state;
      const seen: boolean[] = [];
      const deploy = deps.deploy.bind(deps);
      deps.deploy = async (input) => { seen.push(input.optimizeDeployment); await deploy(input); };
      const program = new Command("ideactl");
      registerUpgradeCommands(program, deps);
      await program.parseAsync([
        "upgrade-cluster", "--force", "--accept-config-drift", "--skip-release-image-check",
        "--cluster-name", clusterName, "--aws-region", awsRegion, ...flags,
      ], { from: "user" });
      assert.deepEqual(seen, [expected], flags.join(" ") || "default");
    });
  }
});

test("ECR token and manifest requests honor proxy environment and NO_PROXY without network", async () => {
  const savedEnv = process.env;
  try {
    for (const env of [
      { HTTPS_PROXY: "http://upper.invalid:8080" },
      { HTTPS_PROXY: "http://upper.invalid:8080", https_proxy: "http://lower.invalid:8080" },
      { HTTPS_PROXY: "http://upper.invalid:8080", NO_PROXY: "public.ecr.aws" },
      { https_proxy: "http://lower.invalid:8080", no_proxy: ".ecr.aws" },
    ]) {
      process.env = { ...savedEnv };
      for (const key of ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"]) delete process.env[key];
      Object.assign(process.env, env);
      const routes: string[] = [];
      const dispatcher = new EnvHttpProxyAgent({
        connect: (_options, callback) => { routes.push("direct"); callback(new Error("route probe"), null); },
        clientFactory: (origin) => new Agent({
          connect: (_options, callback) => { routes.push(origin.origin); callback(new Error("route probe"), null); },
        }),
      });
      assert.equal(await publicRegistryImageExists("public.ecr.aws/example/control:26.10.4", async (url, init) => {
        assert.equal(init.dispatcher, dispatcher);
        const target = new URL(url);
        await assert.rejects(init.dispatcher.request({ origin: target.origin, path: target.pathname, method: "GET" }), /route probe/);
        return target.pathname === "/token/" ? new Response(JSON.stringify({ token: "test" })) : new Response(null);
      }, () => dispatcher), true);
      const expected = env.NO_PROXY || env.no_proxy ? "direct" : env.https_proxy || env.HTTPS_PROXY;
      assert.deepEqual(routes, [expected, expected]);
      await assert.rejects(dispatcher.request({ origin: "https://public.ecr.aws", path: "/", method: "GET" }), /destroyed|closed/i);
    }
  } finally {
    process.env = savedEnv;
  }
});

test("a cluster proving this release's candidate keeps its image and never asks the registry", async () => {
  await withFixture(async (state) => {
    const generated = await routineReleaseFixture(state);
    const { deps, rows, events } = state;
    const repository = generated.get("ecs.image_repositories.aws");
    const image = `${repository}:${ideaVersion()}-rc.3`;
    rows[`${clusterName}.cluster-settings`]!.push(
      setting("ecs.image_repositories.aws", repository),
      { ...setting("ecs.image", image), source: "cli" },
    );
    deps.releaseImageExists = async () => assert.fail("a candidate of this release must not be looked up");
    await upgradeCluster(deps, containerOptions);
    assert.ok(events.includes("deploy"));
    assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "ecs.image")?.["value"], image);
  });
});

test("the ECR Public lookup reads a manifest anonymously and refuses to guess", async () => {
  const calls: { url: string; init?: RegistryRequestInit }[] = [];
  const fetcher = (status: number, tokenStatus = 200, token: unknown = "t0k") =>
  (async (url: string, init?: RegistryRequestInit) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("/token/")) return new Response(JSON.stringify({ token }), { status: tokenStatus });
    return new Response(null, { status });
  });
  const image = "public.ecr.aws/s5o2b4m0/idea-control-plane:26.10.4";
  assert.equal(await publicRegistryImageExists(image, fetcher(200)), true);
  assert.equal(calls[0]!.url, "https://public.ecr.aws/token/?scope=repository:s5o2b4m0/idea-control-plane:pull");
  assert.equal(calls[1]!.url, "https://public.ecr.aws/v2/s5o2b4m0/idea-control-plane/manifests/26.10.4");
  assert.equal(calls[1]!.init?.method, "HEAD");
  assert.equal((calls[1]!.init?.headers as Record<string, string>)["Authorization"], "Bearer t0k");
  assert.match((calls[1]!.init?.headers as Record<string, string>)["Accept"]!, /oci\.image\.index/);
  assert.equal(await publicRegistryImageExists(image, fetcher(404)), false);
  await assert.rejects(publicRegistryImageExists(image, fetcher(500)), /HTTP 500/);
  await assert.rejects(publicRegistryImageExists(image, fetcher(200, 403)), /token request returned HTTP 403/);
  await assert.rejects(publicRegistryImageExists(image, fetcher(200, 200, "")), /no pull token/);
  for (const other of ["private.example/x:1", "public.ecr.aws/a/b@sha256:abc", "public.ecr.aws/a/b"]) {
    await assert.rejects(publicRegistryImageExists(other, fetcher(200)), /not a tagged ECR Public image/);
  }
});

test("release image planning uses the cluster partition's repository", () => {
  const repository = "registry.example/control-plane";
  const rows = [
    { key: "cluster.aws.partition", value: "aws-us-gov" },
    { key: "ecs.image_repositories.aws", value: "public.example/control-plane" },
    { key: "ecs.image_repositories.aws-us-gov", value: repository },
    { key: "ecs.image", value: `${repository}:26.09.3` },
  ];
  assert.deepEqual(planEcsImageFollowsRelease(rows, "26.09.4"), [
    { key: "ecs.image", value: `${repository}:26.09.4` },
  ]);
  assert.deepEqual(planEcsImageFollowsRelease(rows.filter((row) => row.key !== "ecs.image_repositories.aws-us-gov"), "26.09.4"), []);
});

import { hostPoolDeploymentTests } from "../support/host-pool-harness.ts";
hostPoolDeploymentTests("upgrade-cluster");

test("upgrade forwards explicit host refresh consent and preserves its rerun options", async () => {
  await withFixture(async ({ deps }) => {
    const deploy = deps.deploy;
    let reached = false;
    deps.deploy = async (input) => {
      reached = true;
      assert.equal(input.refreshHosts, true);
      assert.match(input.hostPoolRerunCommand ?? "", /^ideactl upgrade-cluster /);
      assert.match(input.hostPoolRerunCommand ?? "", /--refresh-hosts$/);
      await deploy(input);
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true, refreshHosts: true });
    assert.equal(reached, true);
  });
});

for (const metadata of [
  { Name: 'idea-compute-node-rocky9-v01012020-000000', CreationDate: '2020-01-01T00:00:00Z' },
  { Name: 'idea-compute-node-rocky9-v01012020-000000' },
]) {
  test(`Phase 3 keeps managed compute until validated promotion, date=${metadata.CreationDate ?? 'absent'}`, async () => {
    await withFixture(async ({ deps, events }) => {
      deps.ec2.describeImages = async ({ imageIds }) => imageIds.map((ImageId) => ({ ImageId, ...metadata }));
      await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: 'default', force: true, acceptConfigDrift: true });
      assert.ok(!events.some((event) => event.startsWith('set:scheduler.compute_node_')));
      assert.ok(events.includes(`set:scheduler.images.refresh_requested_release=${ideaVersion()}`));
    });
  });
}

for (const unreadable of [false, true]) {
  test(`Phase 3 preserves a pinned custom compute default, metadata unreadable=${unreadable}`, async () => {
    await withFixture(async ({ deps, events, rows }) => {
      rows[`${clusterName}.cluster-settings`]!.push(setting('scheduler.images.default_image_pinned', true));
      if (unreadable) rows[`${clusterName}.cluster-settings`]!.find((row) => row['key'] === 'scheduler.compute_node_ami')!['value'] = 'ami-release';
      deps.ec2.describeImages = async ({ imageIds }) => {
        if (unreadable) throw new Error('metadata unavailable');
        return imageIds.map((ImageId) => ({ ImageId, Name: 'custom-image' }));
      };
      await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: 'default', force: true, acceptConfigDrift: true });
      assert.ok(!events.some((event) => event.startsWith('set:scheduler.compute_node_')));
    });
  });
}

test('compute image metadata failure preserves the image instead of bypassing validation', async () => {
  await withFixture(async ({ deps, events }) => {
    deps.ec2.describeImages = async () => { throw new Error('metadata unavailable'); };
    await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: 'default', force: true, acceptConfigDrift: true });
    assert.ok(!events.some((event) => event.startsWith('set:scheduler.compute_node_')));
  });
});

test('image pipeline checkpoints may advance while the upgrade deploys', async () => {
  await withFixture(async ({ deps, rows }) => {
    const keys = ['scheduler.images.refreshed_release', 'scheduler.images.image_refresh_last_run_on', 'vdc.software_stacks.image_refresh_last_run_on', 'vdc.software_stacks.images_baked_release'];
    rows[`${clusterName}.cluster-settings`]!.push(...keys.map((key) => setting(key, 'before')));
    const deploy = deps.deploy;
    deps.deploy = async (input) => {
      await deploy(input);
      for (const key of keys) rows[`${clusterName}.cluster-settings`]!.find((row) => row['key'] === key)!['value'] = 'after';
    };
    await upgradeCluster(deps, { clusterName, awsRegion, moduleSet: 'default', force: true, acceptConfigDrift: true });
  });
});

for (const [name, before, after] of [
  ["add", undefined, ["192.0.2.0/24"]],
  ["change", ["192.0.2.0/24"], ["198.51.100.0/24"]],
  ["shrink", ["192.0.2.0/24", "198.51.100.0/24"], ["192.0.2.0/24"]],
  ["remove", ["192.0.2.0/24"], undefined],
  ["empty", ["192.0.2.0/24"], []],
] as const) {
  test(`SSH sources follow values.yml: ${name}`, async () => {
    await withFixture(async (state) => {
      await routineReleaseFixture(state);
      const { deps, rows, events } = state;
      const key = "cluster.network.ssh_client_ip";
      if (before !== undefined) rows[`${clusterName}.cluster-settings`]!.push({ ...setting(key, before), source: "cli" });
      const valuesPath = join(process.env.IDEA_USER_HOME!, "clusters", clusterName, awsRegion, "values.yml");
      const values = readFileSync(valuesPath, "utf8").replace(/^ssh_client_ip:.*\n/m, "");
      writeFileSync(valuesPath, values + (after === undefined ? "" : `\nssh_client_ip: ${JSON.stringify(after)}\n`));
      if (before !== undefined) {
        await assert.rejects(upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: false }), { name: 'ExitWithCode' });
        assert.ok(!events.includes('deploy'));
      }
      await upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: before !== undefined });
      assert.deepEqual(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)?.["value"], after ?? []);
      assert.ok(events.some((event) => event.includes("VALUES_UPDATE") && event.includes(key) && event.includes("restored from values.yml")));
      assert.ok(events.includes("deploy"));
    });
  });
}

test("SSH sources completion rejects a stale stored list", async () => {
  await withFixture(async (state) => {
    await routineReleaseFixture(state);
    const { deps, rows } = state;
    const key = "cluster.network.ssh_client_ip";
    rows[`${clusterName}.cluster-settings`]!.push({ ...setting(key, ["192.0.2.0/24"]), source: "cli" });
    const deploy = deps.deploy;
    deps.deploy = async (input) => {
      const result = await deploy(input);
      rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === key)!["value"] = ["192.0.2.0/24"];
      return result;
    };
    await assert.rejects(upgradeCluster(deps, containerOptions), /Completion verification failed for setting cluster.network.ssh_client_ip/);
  });
});

for (const [partition, configured] of [
  ["aws-us-gov", undefined], ["aws-us-gov", null], ["aws-us-gov", ""],
  ["aws-us-gov", "registry.example/control-plane"], ["aws", undefined],
] as const) {
  test(`release repository upgrade: ${partition}, ${String(configured)}`, async () => {
    for (const exists of [true, false]) {
      await withFixture(async (state) => {
        const generated = await routineReleaseFixture(state);
        const { deps, rows, events } = state;
        const publicRepository = generated.get("ecs.image_repositories.aws");
        const repository = configured || publicRepository;
        const oldImage = `${repository}:26.09.0`;
        rows[`${clusterName}.cluster-settings`]!.push(
          setting("cluster.aws.partition", partition),
          setting("ecs.image_repositories.aws", publicRepository),
          setting("ecs.image", oldImage),
        );
        if (configured !== undefined) rows[`${clusterName}.cluster-settings`]!.push(setting("ecs.image_repositories.aws-us-gov", configured));
        const asked: string[] = [];
        deps.releaseImageExists = async (target) => { asked.push(target); return exists; };
        const digest = `sha256:${'a'.repeat(64)}`;
        deps.releaseImageDigest = async (target) => { asked.push(target); return exists ? digest : undefined; };
        const expectedImage = `${repository}:${ideaVersion()}${partition === 'aws-us-gov' && !configured ? `@${digest}` : ''}`;
        if (exists) await upgradeCluster(deps, { ...containerOptions, acceptConfigDrift: false });
        else await assert.rejects(upgradeCluster(deps, containerOptions), /release image .* does not exist yet/);
        assert.deepEqual(asked, [`${repository}:${ideaVersion()}`]);
        assert.equal(rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === "ecs.image")?.["value"], exists ? expectedImage : oldImage);
        assert.equal(events.includes("deploy"), exists);
      });
    }
  });
}

test("an explicit GovCloud repository never advances an image from the public repository", () => {
  assert.deepEqual(planEcsImageFollowsRelease([
    { key: "cluster.aws.partition", value: "aws-us-gov" },
    { key: "ecs.image_repositories.aws", value: "public.example/control-plane" },
    { key: "ecs.image_repositories.aws-us-gov", value: "registry.example/control-plane" },
    { key: "ecs.image", value: "public.example/control-plane:26.09.0" },
  ], "26.10.9"), []);
});
