// Synthetic values keep failure recovery reproducible without a private cluster capture.
// External state lives in the replay so separate upgrade invocations can share it.

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { Command } from "commander";
import { CreateTagsCommand, DeleteTagsCommand, DescribeInstancesCommand, EC2Client } from "@aws-sdk/client-ec2";
import { ideaVersion } from "../../src/version.ts";
import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import {
  countPbsStates,
  createLiveUpgradeDeps,
  registerUpgradeCommands,
  type SchedulerJobInventory,
  type UpgradeDeps,
  upgradeCluster,
} from "../../src/cli/commands/upgrade.ts";

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
  return { module_id: moduleId, name, type: "app", status, stack_name: `${clusterName}-${moduleId}`, version: "26.09.0" };
}

function setting(key: string, value: unknown): Record<string, unknown> {
  return { key, value };
}

function replay(): Replay {
  const events: string[] = [];
  const rows: Record<string, Array<Record<string, unknown>>> = {
    [`${clusterName}.cluster-settings`]: [
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
    async syncClusterSettingsInDb(entries, overwrite) {
      events.push(`sync:${overwrite === true}:${entries[0]?.key ?? ""}`);
      const table = rows[`${clusterName}.cluster-settings`]!;
      for (const entry of entries) {
        const index = table.findIndex((row) => row["key"] === entry.key);
        if (index < 0) table.push({ ...entry });
        else if (overwrite === true) table[index] = { ...entry };
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
      async executeChangeSet() {},
      async describeStack() {
        return {};
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
    async sleep() {},
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
    ec2: {
      async describeImages() {
        return [
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
        assert.equal(input.tagKey, "idea:TerminationProtectionCleared");
        return input.instanceIds.filter((id) => protectionTags.has(id));
      },
    },
    cloudFormation: {
      async listStackResources(input) {
        return input.stackName.endsWith("-scheduler") ? { instanceIds: ["i-sample"] } : { instanceIds: [] };
      },
    },
    openSearch: {
      async describeDomain() {
        return { engineVersion: "OpenSearch_2.0" };
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
    schedulerJobs: {
      async activeJobs() {
        return { queued: 0, running: 0, other: 0 };
      },
    },
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
      "prompt:Overwrite the 3 row(s) above and continue with the cluster upgrade?",
      "prompt:Continue with global settings backup and update?",
      "prompt:Sync full configuration to add new values?",
      "prompt:Continue with AMI and settings updates?",
      "prompt:Proceed with deploying all modules?",
    ]);
  });
});

test("Phase 2 deletes and rewrites only the global settings prefix before Phase 2b add-only sync", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deleteIndex = events.indexOf("delete:global-settings.");
    const overwriteSync = events.findIndex((event) => event.startsWith("sync:true:"));
    const addOnlySync = events.findIndex((event) => event.startsWith("sync:false:"));
    assert.ok(events.some((event) => event.startsWith("Backup created successfully at ")));
    assert.ok(deleteIndex >= 0);
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

test("a module first deployed by this upgrade enters the module set only after the last stack deploys", async () => {
  // The deployed applications carry a fixed module table, and the cluster-manager portal resolves
  // every module in the module set on each page load. Announcing the new module before its stacks
  // deploy would make every portal page fail until the cluster-manager cuts over.
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deploy = events.indexOf("deploy");
    assert.ok(deploy >= 0);
    const before = events.slice(0, deploy).filter((event) => event.startsWith("module-sets:"));
    const after = events.slice(deploy).filter((event) => event.startsWith("module-sets:"));
    assert.ok(before.length > 0, "no module-set rows were written before the deployment");
    for (const event of before) assert.ok(!announcedModules(event).includes("ecs"), `announced early: ${event}`);
    assert.ok(before.some((event) => announcedModules(event).includes("scheduler")), "deployed modules stay announced early");
    assert.equal(after.length, 1, "the held rows are written once, after the deployment");
    assert.deepEqual(announcedModules(after[0] as string), ["ecs"]);
    assert.ok((after[0] as string).startsWith("module-sets:false:"), "held rows are add-only");
    assert.ok(events.indexOf("save-values") > events.indexOf(after[0] as string), "announced before the values upload");
  });
});

function schedulerTemplate(deletionPolicy?: string): string {
  return JSON.stringify({
    Parameters: { BootstrapVersion: { Type: "AWS::SSM::Parameter::Value<String>", Default: "/cdk-bootstrap/hnb659fds/version" } },
    Resources: {
      schedulerdnsrecord: { Type: "AWS::Route53::RecordSet", ...(deletionPolicy === undefined ? {} : { DeletionPolicy: deletionPolicy }), Properties: {} },
      schedulerinstance: { Type: "AWS::EC2::Instance", Properties: {} },
    },
  });
}

test("Phase 0 retains the scheduler's DNS record with a policy-only stack update before containers are turned on", async () => {
  // The container scheduler upserts the same name at start. Removing the CloudFormation record with
  // its deployed DeletionPolicy of Delete would delete the name, so the deployed template is updated
  // first with Retain on that record and nothing else: no synth, no instance replacement.
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    const asked: string[] = [];
    const updates: Array<{ StackName: string; TemplateBody: string; ParameterKeys: readonly string[] }> = [];
    deps.cfn.getTemplate = async (stackName) => {
      asked.push(stackName);
      return stackName.endsWith("-scheduler") ? schedulerTemplate() : '{"Resources":{}}';
    };
    deps.cfn.updateStack = async (input) => {
      updates.push(input);
      events.push(`update-stack:${input.StackName}`);
    };
    deps.cfn.describeStack = async () => ({ StackStatus: "UPDATE_COMPLETE" });
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const phase0 = events.findIndex((event) => event.startsWith("Phase 0: retain the scheduler's DNS record (schedulerdnsrecord)"));
    const update = events.indexOf(`update-stack:${clusterName}-scheduler`);
    const phase1 = events.indexOf("Phase 1: Update Base OS in values.yml");
    assert.ok(phase0 >= 0, "no Phase 0");
    assert.ok(phase0 < update && update < phase1, "the policy-only update precedes every configuration phase");
    assert.deepEqual(asked, [`${clusterName}-scheduler`]);
    assert.ok(!events.includes("deploy:scheduler"), "no scheduler-only deploy, so no instance replacement");
    assert.ok(!events.includes("set:scheduler.retain_dns_record=true"), "no configuration lever");
    const sent = JSON.parse(updates[0]?.TemplateBody ?? "{}") as { Resources: Record<string, Record<string, unknown>> };
    assert.equal(sent.Resources["schedulerdnsrecord"]?.["DeletionPolicy"], "Retain");
    assert.equal(sent.Resources["schedulerdnsrecord"]?.["UpdateReplacePolicy"], "Retain");
    assert.equal(sent.Resources["schedulerinstance"]?.["DeletionPolicy"], undefined, "only record sets change");
    assert.deepEqual(updates[0]?.ParameterKeys, ["BootstrapVersion"]);
  });
});

test("Phase 0 stops the upgrade when the policy-only update does not complete", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    deps.cfn.getTemplate = async () => schedulerTemplate();
    deps.cfn.updateStack = async () => {};
    deps.cfn.describeStack = async () => ({ StackStatus: "UPDATE_ROLLBACK_COMPLETE", StackStatusReason: "sample" });
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true }),
      /Phase 0 did not complete/,
    );
    assert.ok(!events.some((event) => event.startsWith("Phase 1")));
  });
});

test("Phase 0 is skipped when the record is already retained, and never consulted when containers stay off", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    deps.cfn.getTemplate = async () => schedulerTemplate("Retain");
    deps.cfn.updateStack = async () => {
      throw new Error("nothing to retain");
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(!events.some((event) => event.startsWith("Phase 0")));
    assert.ok(!events.includes("deploy:scheduler"));
  });
  await withFixture(async ({ deps, events }) => {
    deps.cfn.getTemplate = async () => {
      throw new Error("the deployed template is not read when containers stay off");
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(!events.some((event) => event.startsWith("Phase 0")));
  });
});

test("an already-deployed cluster announces nothing after deployment", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    const deploy = events.indexOf("deploy");
    assert.equal(events.slice(deploy).filter((event) => event.startsWith("module-sets:")).length, 0);
  });
});

test("the trunking pre-flight runs for the upgrade that introduces the container module", async () => {
  // The table has no row for it yet; the values file is what registers it in Phase 2b.
  await withFixture(async ({ deps, events }) => {
    enableContainers();
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
    assert.ok(!events.some((event) => event.startsWith("delete:")), "refused before any mutation");
  });
});

test("Phase 3 refreshes paired AMI keys, preserves a newer built compute image, and moves offered defaults", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(events.includes("set:scheduler.base_os=amazonlinux2023"));
    assert.ok(events.includes("set:scheduler.instance_ami=ami-release"));
    assert.ok(!events.some((event) => event.startsWith("set:scheduler.compute_node_")));
    assert.ok(events.includes("set:scheduler.instance_type=m7i.large"));
    assert.ok(events.includes("set:analytics.opensearch.data_node_instance_type=m7g.large.search"));
  });
});

test("Phase 4 clears protection, deploys, restores live instances, then saves values", async () => {
  await withFixture(async ({ deps, events }) => {
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.deepEqual(
      events.filter((event) => ["clear:i-sample", "deploy", "restore:i-sample", "save-values"].includes(event)),
      ["clear:i-sample", "deploy", "restore:i-sample", "save-values"],
    );
  });
});

test("pre-flight rejects EL10 when eVDI is deployed before any mutation", async () => {
  await withFixture(async ({ deps, events }) => {
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "rhel10", moduleSet: "default", force: true, acceptConfigDrift: true }),
      (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
    );
    assert.ok(!events.some((event) => event.startsWith("Phase ")));
    assert.ok(!events.includes("deploy"));
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
  await withFixture(async ({ deps, events, protection, protectionTags }) => {
    const options = { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true };
    deps.deploy = async () => { throw new Error("deployment failed"); };
    await assert.rejects(upgradeCluster(deps, options), /deployment failed/);
    assert.equal(protection.has("i-sample"), false);
    assert.equal(protectionTags.get("i-sample"), new Date(deps.now()).toISOString());
    assert.ok(events.indexOf("tag:i-sample") < events.indexOf("clear:i-sample"));
    protectionTags.set("i-other-cluster", "earlier");
    deps.deploy = async () => {};
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
  // The fixture table holds a hand-set scheduler image, a hand-set instance type and a retained
  // OpenSearch type, all of which Phase 3 rewrites. An operator reaching for --force to skip
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
      "scheduler.instance_ami",
      "scheduler.instance_type",
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
    assert.ok(!events.some((event) => event.startsWith("sync:true:")));
    assert.ok(events.some((event) => event.startsWith("sync:false:")));
  });
});

test("a failed values.yml upload leaves the upgrade unfinished", async () => {
  await withFixture(async ({ deps, events }) => {
    deps.s3.putObject = async () => {
      throw new Error("simulated object write failure");
    };
    await assert.rejects(
      upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true }),
      /simulated object write failure/,
    );
    assert.ok(!events.includes("All upgrade phases completed successfully"));
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

// -------------------------------------------------------------------------------------------
// the scheduler cutover gate
// -------------------------------------------------------------------------------------------

/** Successive inventories the host scheduler reports; the last one repeats. */
function jobsOnHost(deps: UpgradeDeps, events: string[], sequence: SchedulerJobInventory[]): void {
  let reads = 0;
  deps.schedulerJobs = {
    async activeJobs(input) {
      const inventory = sequence[Math.min(reads, sequence.length - 1)] ?? { queued: 0, running: 0, other: 0 };
      reads += 1;
      events.push(`jobs:${input.instanceId}:${inventory.queued + inventory.running + inventory.other}`);
      return inventory;
    },
  };
}

const containerOptions = { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true } as const;

test("countPbsStates maps PBS states to the inventory and ignores finished jobs", () => {
  assert.deepEqual(countPbsStates({ Q: 3, R: 2, E: 1, H: 1, F: 9 }), { queued: 3, running: 3, other: 1 });
  assert.deepEqual(countPbsStates({}), { queued: 0, running: 0, other: 0 });
});

test("the cutover gate closes submission, refuses a nonempty scheduler, and restores maintenance", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    jobsOnHost(deps, events, [{ queued: 2, running: 1, other: 0 }]);
    await assert.rejects(upgradeCluster(deps, containerOptions), /scheduler cutover gate: the host scheduler i-sample holds 2 queued, 1 running, 0 other job\(s\)[\s\S]*--drain/);
    assert.ok(!events.includes("deploy"));
    const closed = events.indexOf("set:cluster-manager.maintenance.enabled=true");
    const read = events.indexOf("jobs:i-sample:3");
    const restored = events.indexOf("set:cluster-manager.maintenance.enabled=false");
    assert.ok(closed >= 0 && read > closed && restored > read);
    assert.ok(events.includes("set:cluster-manager.maintenance.message="));
    assert.ok(events.includes("delete:cluster-manager.maintenance.upgrade_baseline"));
    assert.ok(!events.some((event) => event.startsWith("sync:")), "no configuration phase ran");
  });
});

test("--drain closes submission, waits for the host scheduler to empty, upgrades, then reopens", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    jobsOnHost(deps, events, [{ queued: 1, running: 1, other: 0 }, { queued: 0, running: 1, other: 0 }, { queued: 0, running: 0, other: 0 }]);
    await upgradeCluster(deps, { ...containerOptions, drain: true });
    const closed = events.indexOf("set:cluster-manager.maintenance.enabled=true");
    const phase1 = events.indexOf("Phase 1: Update Base OS in values.yml");
    const deploy = events.indexOf("deploy");
    const reopened = events.indexOf("set:cluster-manager.maintenance.enabled=false");
    assert.ok(closed >= 0 && closed < phase1, "submission closed before the first phase");
    assert.ok(closed < events.indexOf("jobs:i-sample:2"), "submission closed before inventory read");
    assert.equal(events.filter((event) => event.startsWith("jobs:i-sample:")).length, 3);
    assert.ok(deploy > closed);
    assert.ok(reopened > deploy, "submission reopened after the deployment");
    assert.ok(events.includes("All upgrade phases completed successfully"));
  });
});

test("the cutover gate does not consult a scheduler that already runs as a task", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    deps.cloudFormation = { async listStackResources() { return { instanceIds: [] }; } };
    jobsOnHost(deps, events, [{ queued: 5, running: 5, other: 5 }]);
    await upgradeCluster(deps, containerOptions);
    assert.ok(!events.some((event) => event.startsWith("jobs:")), "no inventory read");
    assert.ok(events.includes("deploy"));
  });
});

test("an unreadable inventory refuses the cutover instead of assuming it is empty", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    deps.schedulerJobs = { async activeJobs() { throw new Error("ssm: InvalidInstanceId"); } };
    await assert.rejects(upgradeCluster(deps, { ...containerOptions, drain: true }), /could not read the job inventory on i-sample: ssm: InvalidInstanceId/);
    assert.ok(!events.includes("deploy"));
    assert.ok(events.includes("set:cluster-manager.maintenance.enabled=true"));
    assert.ok(!events.includes("delete:cluster-manager.maintenance.upgrade_baseline"));
    assert.ok(events.some((event) => event.includes("a completed re-run reopens it")));
  });
});

test("a drain that outlives its timeout stops with submission still closed", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    jobsOnHost(deps, events, [{ queued: 0, running: 1, other: 0 }]);
    let clock = Date.UTC(2026, 8, 15, 12, 0, 0);
    deps.now = () => (clock += 2 * 60_000);
    await assert.rejects(upgradeCluster(deps, { ...containerOptions, drain: true, drainTimeoutMinutes: 1 }), /still on i-sample after 1 minutes[\s\S]*Submission stays closed/);
    assert.ok(events.includes("set:cluster-manager.maintenance.enabled=true"));
    assert.ok(!events.includes("set:cluster-manager.maintenance.enabled=false"));
    assert.ok(!events.includes("deploy"));
  });
});

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

test("an empty scheduler stays closed from before inventory through the last phase", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    trunkingEnabled(deps);
    jobsOnHost(deps, events, [{ queued: 0, running: 0, other: 0 }]);
    await upgradeCluster(deps, containerOptions);
    const baseline = events.findIndex((event) => event.startsWith("set:cluster-manager.maintenance.upgrade_baseline="));
    const closed = events.indexOf("set:cluster-manager.maintenance.enabled=true");
    const read = events.indexOf("jobs:i-sample:0");
    const restored = events.indexOf("set:cluster-manager.maintenance.enabled=false");
    assert.ok(baseline >= 0 && closed > baseline && read > closed);
    assert.ok(restored > events.indexOf("save-values"));
    assert.ok(restored < events.indexOf("All upgrade phases completed successfully"));
    assert.ok(!rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "cluster-manager.maintenance.upgrade_baseline"));
  });
});

test("--skip-drain-check closes submission for the run without reading inventory", async () => {
  await withFixture(async ({ deps, events }) => {
    enableContainers();
    trunkingEnabled(deps);
    jobsOnHost(deps, events, [{ queued: 3, running: 2, other: 0 }]);
    await upgradeCluster(deps, { ...containerOptions, skipDrainCheck: true });
    assert.ok(!events.some((event) => event.startsWith("jobs:")));
    const closed = events.indexOf("set:cluster-manager.maintenance.enabled=true");
    assert.ok(closed >= 0 && closed < events.indexOf("Phase 1: Update Base OS in values.yml"));
    assert.ok(events.indexOf("set:cluster-manager.maintenance.enabled=false") > events.indexOf("save-values"));
    const program = new Command("ideactl");
    registerUpgradeCommands(program, deps);
    assert.match(program.commands[0]!.helpInformation(), /close submission for the run/);
  });
});

for (const hasHost of [true, false]) {
  test(`scheduler-only upgrade with ECS enabled ${hasHost ? "gates and retains host DNS" : "skips the gate and DNS after cutover"}`, async () => {
    await withFixture(async ({ deps, events, rows }) => {
      rows[`${clusterName}.modules`]!.push(moduleRow("ecs", "ecs"));
      rows[`${clusterName}.cluster-settings`]!.push(setting("ecs.enabled", true));
      deps.cloudFormation.listStackResources = async () => ({ instanceIds: hasHost ? ["i-sample"] : [] });
      deps.cfn.getTemplate = async () => {
        events.push("read-template");
        return schedulerTemplate();
      };
      deps.cfn.updateStack = async () => { events.push("retain-dns"); };
      deps.cfn.describeStack = async () => ({ StackStatus: "UPDATE_COMPLETE" });
      jobsOnHost(deps, events, [{ queued: 0, running: 0, other: 0 }]);
      await upgradeCluster(deps, { ...containerOptions, modules: ["scheduler"] });
      assert.equal(events.includes("jobs:i-sample:0"), hasHost);
      assert.equal(events.includes("set:cluster-manager.maintenance.enabled=true"), hasHost);
      assert.equal(events.includes("read-template"), hasHost);
      assert.equal(events.includes("retain-dns"), hasHost);
      if (hasHost) assert.ok(events.indexOf("retain-dns") < events.indexOf("deploy:scheduler"));
      assert.ok(events.includes("deploy:scheduler"));
      assert.ok(!events.some((event) => event.startsWith("module-sets:") && announcedModules(event).includes("ecs")));
    });
  });
}

for (const { retryHasHost, originallyEnabled } of [
  { retryHasHost: true, originallyEnabled: false },
  { retryHasHost: false, originallyEnabled: false },
  { retryHasHost: true, originallyEnabled: true },
  { retryHasHost: false, originallyEnabled: true },
]) {
  test(`a successful retry restores maintenance enabled=${originallyEnabled} ${retryHasHost ? "with the host still present" : "after the host is gone"}`, async () => {
    await withFixture(async ({ deps, events, rows }) => {
      enableContainers();
      trunkingEnabled(deps);
      const key = "cluster-manager.maintenance.upgrade_baseline";
      const original = { enabled: originallyEnabled, message: "Scheduled maintenance" };
      rows[`${clusterName}.cluster-settings`]!.push(
        setting("cluster-manager.maintenance.enabled", original.enabled),
        setting("cluster-manager.maintenance.message", original.message),
      );
      jobsOnHost(deps, events, [{ queued: 0, running: 0, other: 0 }]);
      const deploy = deps.deploy;
      deps.deploy = async () => { throw new Error("cluster-manager deployment failed"); };
      await assert.rejects(upgradeCluster(deps, containerOptions), /cluster-manager deployment failed/);
      const value = (name: string) => rows[`${clusterName}.cluster-settings`]!.find((row) => row["key"] === name)?.["value"];
      assert.deepEqual(JSON.parse(String(value(key))), original);
      assert.equal(value("cluster-manager.maintenance.enabled"), true);
      assert.notEqual(value("cluster-manager.maintenance.message"), original.message);
      assert.ok(events.some((event) => event.includes("a completed re-run reopens it")));
      const retryStart = events.length;
      deps.deploy = deploy;
      if (!retryHasHost) deps.cloudFormation.listStackResources = async () => ({ instanceIds: [] });
      await upgradeCluster(deps, containerOptions);
      assert.equal(value("cluster-manager.maintenance.enabled"), original.enabled);
      assert.equal(value("cluster-manager.maintenance.message"), original.message);
      assert.equal(value(key), undefined);
      assert.equal(events.filter((event) => event.startsWith(`set:${key}=`)).length, 1);
      assert.ok(events.lastIndexOf(`delete:${key}`) > events.lastIndexOf("save-values"));
      assert.equal(events.slice(retryStart).some((event) => event.startsWith("jobs:")), retryHasHost);
    });
  });
}

for (const portalCurrent of [false, true]) {
  test(`deployed ECS module-set rows ${portalCurrent ? "publish for the target cluster-manager release" : "stay held for the previous cluster-manager release"}`, async () => {
    await withFixture(async ({ deps, events, rows }) => {
      enableContainers();
      trunkingEnabled(deps);
      rows[`${clusterName}.modules`]!.push(
        { ...moduleRow("ecs", "ecs"), version: ideaVersion() },
        { ...moduleRow("cluster-manager", "cluster-manager"), version: portalCurrent ? ideaVersion() : "26.09.0" },
      );
      deps.deploy = async () => { events.push("deploy"); };
      await upgradeCluster(deps, containerOptions);
      const publications = events.filter((event) => event.startsWith("module-sets:") && announcedModules(event).includes("ecs"));
      assert.equal(publications.length > 0, portalCurrent);
      if (portalCurrent) assert.ok(events.indexOf(publications[0]!) < events.indexOf("deploy"));
      assert.equal(rows[`${clusterName}.cluster-settings`]!.some((row) => row["key"] === "global-settings.module_sets.default.ecs.module_id"), portalCurrent);
    });
  });
}

test("a scoped upgrade cannot announce ECS even when cluster-manager is at the target release", async () => {
  await withFixture(async ({ deps, events, rows }) => {
    enableContainers();
    rows[`${clusterName}.modules`]!.push(
      { ...moduleRow("ecs", "ecs"), version: ideaVersion() },
      { ...moduleRow("cluster-manager", "cluster-manager"), version: ideaVersion() },
    );
    await upgradeCluster(deps, { ...containerOptions, modules: ["analytics"] });
    assert.ok(!events.some((event) => event.startsWith("module-sets:") && announcedModules(event).includes("ecs")));
    assert.ok(!events.includes("set:cluster-manager.maintenance.enabled=true"));
  });
});
