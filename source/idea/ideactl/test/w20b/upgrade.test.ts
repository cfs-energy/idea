/**
 * Phase-boundary tests for `upgrade-cluster`.
 *
 * The real values fixture is intentionally read at runtime. It is not part of
 * the public tree, and every external operation below is an in-memory replay.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { Command } from "commander";
import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import {
  createLiveUpgradeDeps,
  registerUpgradeCommands,
  type UpgradeDeps,
  upgradeCluster,
} from "../../src/cli/commands/upgrade.ts";
import { requireFixtures } from "../support/fixtures.ts";

const fixture = join(process.cwd(), "tools", "parity", "fixtures", "idea-dev27", "values.yml");
requireFixtures(
  [fixture],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);
const clusterName = "idea-dev27";
const awsRegion = "us-east-2";

interface Replay {
  deps: UpgradeDeps;
  events: string[];
  rows: Record<string, Array<Record<string, unknown>>>;
}

function moduleRow(moduleId: string, name: string, status = "deployed"): Record<string, unknown> {
  return { module_id: moduleId, name, type: "app", status, stack_name: `${clusterName}-${moduleId}` };
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
    },
    async syncClusterSettingsInDb(entries, overwrite) {
      events.push(`sync:${overwrite === true}:${entries[0]?.key ?? ""}`);
    },
    async setConfigEntry(key, value) {
      events.push(`set:${key}=${String(value)}`);
    },
    async deleteConfigEntries(prefix) {
      events.push(`delete:${prefix}`);
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
        return readFileSync(fixture, "utf8");
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
      async describeInstanceAttribute() {
        return true;
      },
      async modifyInstanceAttribute(input) {
        events.push(`${input.protected ? "restore" : "clear"}:${input.instanceId}`);
      },
      async describeLiveInstances() {
        return ["i-sample"];
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
    async deploy() {
      events.push("deploy");
    },
    regionAmiConfig() {
      return { "us-east-2": { amazonlinux2023: "ami-release" } };
    },
  };
  return { deps, events, rows };
}

async function withFixture(action: (replayValue: Replay) => Promise<void>): Promise<void> {
  const home = mkdtempSync(join(tmpdir(), "ideactl-upgrade-"));
  const original = process.env.IDEA_USER_HOME;
  process.env.IDEA_USER_HOME = home;
  try {
    const valuesPath = join(home, "clusters", clusterName, awsRegion, "values.yml");
    const directory = join(valuesPath, "..");
    mkdirSync(directory, { recursive: true });
    writeFileSync(valuesPath, readFileSync(fixture, "utf8"), { flag: "w" });
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

test("a failed protection restore warns and still names the instance", async () => {
  await withFixture(async ({ deps, events }) => {
    deps.ec2.modifyInstanceAttribute = async (input) => {
      if (input.protected === true) throw new Error("simulated restore failure");
      events.push(`clear:${input.instanceId}`);
    };
    await upgradeCluster(deps, { clusterName, awsRegion, baseOs: "amazonlinux2023", moduleSet: "default", force: true, acceptConfigDrift: true });
    assert.ok(events.some((event) => event.includes("could not restore termination protection") && event.includes("i-sample")));
  });
});
