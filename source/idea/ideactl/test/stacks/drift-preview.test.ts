/**
 * Upgrade drift preview classification, command wiring, and approval boundary.
 *
 * Every external operation is injected. The tests do not contact a service or
 * write cluster configuration.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { Command } from "commander";

import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import {
  configUpgradePreview,
  registerConfigCommands,
  type ConfigDriftPreviewDeps,
} from "../../src/cli/commands/config.ts";
import {
  upgradeCluster,
  type UpgradeDeps,
} from "../../src/cli/commands/upgrade.ts";
import {
  compareUpgradeDrift,
  renderUpgradeDrift,
  UPGRADE_DRIFT_ACTIONS,
  type UpgradeDriftInput,
} from "../../src/config/upgrade-drift.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

/** One synthetic table covering every action from the drift analysis. */
function completeInput(): UpgradeDriftInput {
  return {
    current: [
      { key: "global-settings.portal_text", value: "incident text", version: 4 },
      { key: "global-settings.type_value", value: "1", version: 2 },
      { key: "global-settings.removed", value: true, version: 3 },
      { key: "global-settings.same", value: [], version: 1 },
      { key: "cluster.locale", value: "en_US", version: 5 },
      { key: "vdc.usb", value: null, version: 2 },
      { key: "cluster.orphan", value: "stale", version: 7 },
      { key: "scheduler.instance_ami", value: "ami-manual", version: 9 },
      { key: "cluster-manager.endpoint", value: "operator value", source: "stack", version: 6 },
      { key: "cluster-manager.removed", value: "old output", source: "stack", version: 3 },
    ],
    generated: [
      { key: "global-settings.portal_text", value: "generated text" },
      { key: "global-settings.type_value", value: 1 },
      { key: "global-settings.same", value: [] },
      { key: "cluster.new_value", value: true },
      { key: "cluster.locale", value: "en_GB" },
      { key: "vdc.usb", value: [] },
      { key: "scheduler.instance_ami", value: "ami-generated" },
      { key: "cluster-manager.endpoint", value: "generated endpoint" },
    ],
    phase3: [{ key: "scheduler.instance_ami", value: "ami-release" }],
    stacks: [{
      moduleId: "cluster-manager",
      selected: true,
      previous: {
        endpoint: "deployed value",
        removed: "old output",
      },
      target: {
        endpoint: "stack target",
      },
    }],
  };
}

/** Minimal dependency set for read-only command tests. */
function baseDeps(output: string[]): Deps {
  const writer: ConfigWriter = {
    async syncModulesInDb() {
      throw new Error("unexpected settings write");
    },
    async syncClusterSettingsInDb() {
      throw new Error("unexpected settings write");
    },
    async setConfigEntry() {
      throw new Error("unexpected settings write");
    },
    async deleteConfigEntries() {
      throw new Error("unexpected settings write");
    },
  };
  return {
    async spawn() {
      throw new Error("unexpected process");
    },
    cfn: {
      async describeChangeSet() {
        throw new Error("unexpected stack read");
      },
      async executeChangeSet() {
        throw new Error("unexpected stack write");
      },
      async describeStack() {
        throw new Error("unexpected stack read");
      },
    },
    s3: {
      async putObject() {
        throw new Error("unexpected object write");
      },
      async getObject() {
        throw new Error("unexpected object read");
      },
    },
    async scan() {
      throw new Error("unexpected table read");
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
      return 0;
    },
    uuid() {
      return "00000000-0000-0000-0000-000000000000";
    },
    out(line) {
      output.push(line);
    },
    err(line) {
      output.push(line);
    },
    async prompt() {
      return false;
    },
  };
}

test("classifies every upgrade action and marks changed generator drift", (context) => {
  const report = compareUpgradeDrift(completeInput());
  assert.deepEqual(
    [...new Set(report.findings.map((finding) => finding.action))],
    [...UPGRADE_DRIFT_ACTIONS],
  );

  const handEdit = report.findings.find(
    (finding) => finding.key === "global-settings.portal_text",
  );
  assert.equal(handEdit?.action, "GLOBAL_CHANGE");
  assert.equal(handEdit?.effect, "CHANGE");
  assert.equal(handEdit?.differsFromGenerated, true);
  assert.ok(report.changedRowsDifferingFromGenerated.includes("global-settings.portal_text"));

  const rendered = renderUpgradeDrift(report);
  for (const action of UPGRADE_DRIFT_ACTIONS) assert.match(rendered, new RegExp(action));
  assert.match(rendered, /Rows: 1 added, 5 changed, 2 deleted, 3 preserved/);
  assert.doesNotMatch(rendered, /incident text|operator value|ami-manual|stack target/);
  context.diagnostic(rendered);
});

test("registers and runs the standalone preview from injected reads", async () => {
  const output: string[] = [];
  const deps: ConfigDriftPreviewDeps = {
    ...baseDeps(output),
    async loadUpgradeDriftInput() {
      return completeInput();
    },
  };
  const report = await configUpgradePreview(deps, {
    clusterName: CLUSTER,
    awsRegion: REGION,
  });
  assert.equal(report.findings.length, 11);
  assert.equal(output.length, 1);
  assert.match(output[0] ?? "", /^Configuration preview/);

  const program = new Command("ideactl");
  registerConfigCommands(program, deps);
  const config = program.commands.find((command) => command.name() === "config");
  const preview = config?.commands.find((command) => command.name() === "preview-upgrade");
  assert.ok(preview !== undefined);
  assert.match(preview.helpInformation(), /--base-os/);
  assert.match(preview.helpInformation(), /--skip-global-settings-update/);

  output.length = 0;
  await program.parseAsync([
    "node",
    "ideactl",
    "config",
    "preview-upgrade",
    "--cluster-name",
    CLUSTER,
    "--aws-region",
    REGION,
  ]);
  assert.equal(output.length, 1);
  assert.match(output[0] ?? "", /^Configuration preview/);
});

interface UpgradeHarness {
  deps: UpgradeDeps;
  events: string[];
  tables: Record<string, Array<Record<string, unknown>>>;
}

/** Upgrade dependencies that record every possible mutation. */
function upgradeHarness(answer: boolean): UpgradeHarness {
  const events: string[] = [];
  const tables: Record<string, Array<Record<string, unknown>>> = {
    [`${CLUSTER}.cluster-settings`]: [
      // Maintenance recovery resolves its owner before the preview can complete.
      // Keep that required mapping even when no stack is selected in this fixture.
      { key: "global-settings.module_sets.default.cluster-manager.module_id", value: "cluster-manager" },
      { key: "cluster.base_os", value: "amazonlinux2023" },
    ],
    [`${CLUSTER}.modules`]: [{ module_id: "cluster", name: "cluster", type: "stack", status: "deployed", version: "26.09.0" }],
  };
  const base = baseDeps(events);
  const deps: UpgradeDeps = {
    ...base,
    async scan(input) {
      events.push(`read:${input.TableName}`);
      return { Items: tables[input.TableName] ?? [] };
    },
    async loadUpgradeDriftInput() {
      events.push("preview-read");
      return completeInput();
    },
    async prompt(choice) {
      events.push(`prompt:${choice.message}`);
      return answer;
    },
    ec2: {
      async describeImages() {
        throw new Error("unexpected image read");
      },
      async describeInstanceTypeOfferings() {
        throw new Error("unexpected instance-type read");
      },
      async describeInstanceAttribute() {
        throw new Error("unexpected instance read");
      },
      async modifyInstanceAttribute() {
        events.push("instance-write");
      },
      async createTags() { throw new Error("unexpected tag write"); },
      async deleteTags() { throw new Error("unexpected tag write"); },
      async describeLiveInstances() {
        throw new Error("unexpected instance read");
      },
    },
    cloudFormation: {
      async listStackResources() {
        throw new Error("unexpected stack read");
      },
    },
    openSearch: {
      async describeDomain() {
        throw new Error("unexpected search read");
      },
      async listInstanceTypeDetails() {
        throw new Error("unexpected search read");
      },
    },
    eolSoftwareStacks: {
      async setEnabled() {
        events.push("software-stack-write");
      },
      async delete() {
        events.push("software-stack-delete");
      },
    },
    async deploy() {
      events.push("deploy");
    },
    regionAmiConfig() {
      return { [REGION]: { amazonlinux2023: "ami-release" } };
    },
  };
  return { deps, events, tables };
}

test("upgrade prints the preview and refuses every write until confirmed", async () => {
  const { deps, events } = upgradeHarness(false);
  await assert.rejects(
    upgradeCluster(deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
    }),
    (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
  );

  const preview = events.findIndex((event) => event.startsWith("Configuration preview"));
  const prompt = events.findIndex((event) => event.startsWith("prompt:Overwrite the "));
  assert.ok(preview >= 0);
  assert.ok(prompt > preview, "the rows are named before the question is asked");
  assert.ok(!events.some((event) => event.endsWith("-write") || event === "deploy"));
});

test("--force with --accept-config-drift bypasses the preview confirmation", async () => {
  const { deps, events, tables } = upgradeHarness(false);
  tables[`${CLUSTER}.modules`]?.push({
    module_id: "vdc",
    name: "virtual-desktop-controller",
    type: "stack",
    status: "deployed",
    version: "26.09.0",
  });
  tables[`${CLUSTER}.vdc.controller.software-stacks`] = [{
    stack_id: "stack-old",
    base_os: "amazonlinux2",
    name: "old-stack",
    architecture: "x86_64",
  }];
  tables[`${CLUSTER}.vdc.controller.user-sessions`] = [];
  deps.eolSoftwareStacks.delete = async () => {
    events.push("software-stack-delete");
    throw new Error("stop after confirmation bypass");
  };

  await assert.rejects(
    upgradeCluster(deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
      acceptConfigDrift: true,
    }),
    /stop after confirmation bypass/,
  );
  assert.ok(events.some((event) => event.startsWith("Configuration preview")));
  assert.ok(events.includes("software-stack-delete"));
  assert.ok(!events.some((event) => event.startsWith("prompt:")));
});

test("--force alone refuses the rows at risk and names the flag that accepts them", async () => {
  // --force means skip confirmations. Reading it as consent to lose an operator edit is what makes
  // the gate worthless, because that is the flag an unattended run already passes.
  const { deps, events } = upgradeHarness(true);
  await assert.rejects(
    upgradeCluster(deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
      force: true,
    }),
    (error: unknown) => error instanceof Error && error.name === "ExitWithCode",
  );
  const refuse = events.find((event) => event.includes("hold a value this upgrade overwrites"));
  assert.equal(
    refuse,
    "4 configuration row(s) hold a value this upgrade overwrites, and the value differs from generated configuration: global-settings.portal_text, global-settings.type_value, scheduler.instance_ami, cluster-manager.endpoint",
  );
  assert.ok(events.some((event) => event.includes("--accept-config-drift")), "names the flag");
  assert.ok(!events.some((event) => event.endsWith("-write") || event === "deploy"), "no mutation");
});

test("nothing at risk asks nothing about configuration", async () => {
  // A question asked on every run is a question nobody reads, so a preview with no rows at risk
  // goes straight to the next boundary.
  const { deps, events } = upgradeHarness(false);
  deps.loadUpgradeDriftInput = async () => ({
    current: [{ key: "cluster.locale", value: "en_US", version: 1 }],
    generated: [{ key: "cluster.locale", value: "en_US" }],
  });

  // This harness has no values.yml and no bucket to restore one from, so Phase 1 is where it stops.
  // Reaching Phase 1 at all is the point: nothing was asked about configuration on the way.
  await assert.rejects(
    upgradeCluster(deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      baseOs: "amazonlinux2023",
      moduleSet: "default",
    }),
    /values\.yml/,
  );
  assert.ok(events.some((event) => event.startsWith("Configuration preview")), "the preview still prints");
  assert.ok(!events.some((event) => event.startsWith("prompt:")), "no question before the first phase");
  assert.ok(events.includes("Phase 1: Update Base OS in values.yml"), "the run went straight to Phase 1");
});

test("a stack row whose future value is unknown is not an operator edit at risk", () => {
  // The shape every real cluster has: the operator leaves a certificate or secret out of values.yml,
  // so generated configuration holds nothing for it, the stack creates it and writes the resolved
  // value back, and the next upgrade cannot resolve the stack's target at comparison time. Compared
  // against generated, such a row differs by definition, which is not evidence of anything.
  const report = compareUpgradeDrift({
    current: [
      { key: "vdc.gateway_certificate_secret_arn", value: "arn:aws:secretsmanager:resolved", source: "stack", version: 44 },
      { key: "cluster.locale", value: "en_GB", version: 2 },
    ],
    generated: [
      { key: "vdc.gateway_certificate_secret_arn", value: null },
      { key: "cluster.locale", value: "en_US" },
    ],
    stacks: [{ moduleId: "vdc", selected: true, previous: { gateway_certificate_secret_arn: "arn:aws:secretsmanager:resolved" } }],
  });

  const stackRow = report.findings.find((row) => row.key === "vdc.gateway_certificate_secret_arn");
  assert.equal(stackRow?.action, "STACK_OVERWRITE");
  assert.equal(stackRow?.targetType, "?", "the target is unknown, and the report says so");
  assert.equal(stackRow?.differsFromGenerated, false);
  assert.deepEqual(report.changedRowsDifferingFromGenerated, []);

  // The add-only sync preserves the edited row rather than overwriting it, so it is not at risk
  // either, and the gate stays silent on a table that holds one real edit and one stack value.
  const edited = report.findings.find((row) => row.key === "cluster.locale");
  assert.equal(edited?.action, "PRESERVE_DRIFT");
  assert.equal(edited?.effect, "PRESERVE");
});

test("a stack row whose future value is known is still compared", () => {
  const report = compareUpgradeDrift({
    current: [{ key: "vdc.endpoint", value: "operator value", source: "stack", version: 6 }],
    generated: [{ key: "vdc.endpoint", value: "generated value" }],
    stacks: [{
      moduleId: "vdc",
      selected: true,
      previous: { endpoint: "deployed value" },
      target: { endpoint: "stack target" },
    }],
  });

  const row = report.findings.find((finding) => finding.key === "vdc.endpoint");
  assert.equal(row?.targetType, "S");
  assert.equal(row?.differsFromGenerated, true);
  assert.deepEqual(report.changedRowsDifferingFromGenerated, ["vdc.endpoint"]);
});
