/**
 * The three failure paths a real operator hits on a bootstrap or a migration run,
 * driven against injected fakes so each one can be caused at an exact moment.
 *
 * 1. CloudFormation rejects a stack update on its own merits.
 * 2. A federated session expires partway through a long run.
 * 3. The operator runs the command again after a failure.
 *
 * For each: what the operator is shown, what durable state is left, and whether
 * the next run converges. A test whose name starts with GAP pins behaviour that is
 * observable today and is wrong, so its assertions change when that behaviour does.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { CdkInvoker, type Deps, type StackDescription } from "../../src/cli/cdk-invoker.ts";
import { DeploymentHelper } from "../../src/cli/deployment-helper.ts";
import { run } from "../../src/cli/main.ts";
import {
  registerUpgradeCommands,
  upgradeCluster,
  type UpgradeDeps,
} from "../../src/cli/commands/upgrade.ts";
import type { ConfigWriter } from "../../src/cli/cdk-invoker.ts";
import { Command } from "commander";
import { ideaVersion } from "../../src/version.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const ACCOUNT = "123456789012";
const BUCKET = "sample-cluster-bucket";

const home = mkdtempSync(join(tmpdir(), "ideactl-failure-injection-"));
const bin = mkdtempSync(join(tmpdir(), "ideactl-failure-injection-bin-"));
const previousHome = process.env.IDEA_USER_HOME;
const previousCdkBin = process.env.IDEA_CDK_BIN;
const previousPath = process.env.PATH;

before(() => {
  process.env.IDEA_USER_HOME = home;
  process.env.IDEA_CDK_BIN = "/opt/idea/bin/cdk";
  // Every deploy builds its `--app` re-entry by running `command -v ideactl`, and a PATH miss
  // costs seconds per call on a workstation with a long or networked PATH. Putting a stub first
  // makes the lookup a hit. Nothing runs it: the CDK CLI is a fake here.
  const stub = join(bin, "ideactl");
  writeFileSync(stub, "#!/bin/sh\nexit 0\n");
  chmodSync(stub, 0o755);
  process.env.PATH = `${bin}:${previousPath ?? ""}`;
});

after(() => {
  if (previousHome === undefined) delete process.env.IDEA_USER_HOME;
  else process.env.IDEA_USER_HOME = previousHome;
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  if (previousPath === undefined) delete process.env.PATH;
  else process.env.PATH = previousPath;
  rmSync(home, { recursive: true, force: true });
  rmSync(bin, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------------------------
// replay
// ---------------------------------------------------------------------------------------------

/** An AWS SDK service error, with the name the SDK puts on the wire. */
function awsError(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}

interface ReplayOptions {
  /** Terminal status the stack reaches after the change set is executed. */
  terminalStatus?: string;
  /** CloudFormation's stack-level reason. Absent is the common real shape. */
  terminalReason?: string;
  /** How many `_IN_PROGRESS` polls precede the terminal status. */
  inProgressPolls?: number;
  /** Throw this from `describeStack` on the given poll number, 1-based. */
  describeStackThrows?: { onPoll: number; error: Error };
  /** Throw this from the modules-table read the optimized path makes after a group commits. */
  modulesReadThrowsAfterGroup?: Error;
  /** Exit code the spawned CDK CLI returns. */
  spawnExit?: number;
  modules?: Array<Record<string, unknown>>;
  settings?: Array<Record<string, unknown>>;
}

interface Replay {
  deps: Deps;
  stdout: string[];
  stderr: string[];
  executed: string[];
  polls: number;
  /** Stacks observed reaching a terminal status. */
  polled: readonly string[];
  modulesReadsAfterGroup: number;
  finalStackStatus(): string;
}

function moduleRow(moduleId: string, name: string, status = "deployed"): Record<string, unknown> {
  return { module_id: moduleId, name, type: "app", status, stack_name: `${CLUSTER}-${moduleId}`, version: "26.09.0" };
}

function replay(options: ReplayOptions = {}): Replay {
  const stdout: string[] = [];
  const stderr: string[] = [];
  const executed: string[] = [];
  const polled: string[] = [];
  const state = { polls: 0, modulesReadsAfterGroup: 0, thrown: false };
  const terminal = options.terminalStatus ?? "UPDATE_COMPLETE";
  const inProgressPolls = options.inProgressPolls ?? 0;

  const modules = options.modules ?? [moduleRow("analytics", "analytics")];
  const settings = options.settings ?? [{ key: "cluster.cluster_s3_bucket", value: BUCKET }];

  const deps: Deps = {
    async spawn() {
      return options.spawnExit ?? 0;
    },
    cfn: {
      async describeChangeSet() {
        return {
          Status: "CREATE_COMPLETE",
          Changes: [{
            ResourceChange: {
              Action: "Modify",
              LogicalResourceId: "analyticsdomain",
              ResourceType: "AWS::OpenSearchService::Domain",
            },
          }],
        };
      },
      async executeChangeSet(input) {
        executed.push(input.StackName);
      },
      async describeStack(stackName): Promise<StackDescription> {
        state.polls += 1;
        if (options.describeStackThrows?.onPoll === state.polls) throw options.describeStackThrows.error;
        if (state.polls <= inProgressPolls) return { StackStatus: "UPDATE_IN_PROGRESS" };
        polled.push(stackName);
        return options.terminalReason === undefined
          ? { StackStatus: terminal }
          : { StackStatus: terminal, StackStatusReason: options.terminalReason };
      },
    },
    s3: {
      async putObject() {},
      async getObject() {
        return "";
      },
    },
    async scan(input) {
      // The optimized path re-reads the modules table only once every module in the group has
      // committed its change set, so that read is the deterministic injection point.
      if (input.TableName === `${CLUSTER}.modules` && executed.length === modules.length) {
        state.modulesReadsAfterGroup += 1;
        if (options.modulesReadThrowsAfterGroup !== undefined && !state.thrown) {
          state.thrown = true;
          throw options.modulesReadThrowsAfterGroup;
        }
      }
      if (input.TableName === `${CLUSTER}.modules`) return { Items: modules };
      if (input.TableName === `${CLUSTER}.cluster-settings`) return { Items: settings };
      return { Items: [] };
    },
    async configWriter() {
      return {
        async syncModulesInDb() {},
        async syncClusterSettingsInDb() {},
        async setConfigEntry() {},
        async deleteConfigEntries() {},
      };
    },
    async accountId() {
      return ACCOUNT;
    },
    async callerIdentity() {
      return { account: ACCOUNT, arn: `arn:aws:sts::${ACCOUNT}:assumed-role/operator/session` };
    },
    async httpStatus() {
      return 200;
    },
    async sleep() {},
    now() {
      return Date.UTC(2026, 8, 10, 12, 0, 0);
    },
    uuid() {
      return "00000000-0000-4000-8000-000000000001";
    },
    out(line) {
      stdout.push(line);
    },
    err(line) {
      stderr.push(line);
    },
    async prompt() {
      return true;
    },
  };

  return {
    deps,
    stdout,
    stderr,
    executed,
    get polls() {
      return state.polls;
    },
    polled,
    get modulesReadsAfterGroup() {
      return state.modulesReadsAfterGroup;
    },
    finalStackStatus: () => terminal,
  };
}

function deployArgv(moduleId = "analytics"): string[] {
  return [
    "deploy",
    moduleId,
    "--cluster-name",
    CLUSTER,
    "--aws-region",
    REGION,
    "--upgrade",
  ];
}

// ---------------------------------------------------------------------------------------------
// 1. CloudFormation rejects the stack update on its own merits
// ---------------------------------------------------------------------------------------------

test("a stack that CloudFormation rolls back names the stack, the status, the reason, and the next action", async () => {
  const state = replay({
    terminalStatus: "UPDATE_ROLLBACK_COMPLETE",
    terminalReason: "The following resource(s) failed to update: [analyticsdomain].",
    inProgressPolls: 2,
  });

  const code = await run(deployArgv(), state.deps);

  assert.equal(code, 1);
  const message = state.stderr.join("\n");
  assert.match(message, /Stack sample-cluster-analytics ended UPDATE_ROLLBACK_COMPLETE/);
  assert.match(message, /analyticsdomain/);
  assert.match(message, /Open the stack events in CloudFormation/);
  assert.match(message, /re-run the same deploy/);
  // The change set was executed, so the cluster has whatever the rollback left behind.
  assert.deepEqual(state.executed, [`${CLUSTER}-analytics`]);
});

test("GAP: with no stack-level reason the operator is told a status and nothing about the cause", async () => {
  // CloudFormation frequently leaves StackStatusReason empty on UPDATE_ROLLBACK_COMPLETE; the
  // failing resource and its reason are only on the stack events. The tool has no
  // DescribeStackEvents call, so it cannot name either.
  const state = replay({ terminalStatus: "UPDATE_ROLLBACK_COMPLETE", inProgressPolls: 1 });

  const code = await run(deployArgv(), state.deps);

  assert.equal(code, 1);
  const message = state.stderr.join("\n");
  // The whole statement of cause is the bare status: no logical ID, no reason clause.
  assert.match(message, /ended UPDATE_ROLLBACK_COMPLETE\. No further modules/);
  assert.doesNotMatch(message, /analyticsdomain/);
  assert.doesNotMatch(message, /failed to update/);
});

test("GAP: a rejection during change-set creation exits with the CDK code and no line from the tool", async () => {
  // A template CloudFormation will not accept, or a stack still UPDATE_IN_PROGRESS from an
  // abandoned run, fails inside `cdk deploy --method=prepare-change-set`. The child's own output
  // reaches the terminal because spawn inherits stdio, but the tool adds no statement of what it
  // had already changed or what to do next.
  const state = replay({ spawnExit: 2 });

  const code = await run(deployArgv(), state.deps);

  assert.equal(code, 2);
  assert.deepEqual(state.executed, []);
  assert.deepEqual(state.stderr, []);
});

// ---------------------------------------------------------------------------------------------
// 2. A federated session expires partway through a long run
// ---------------------------------------------------------------------------------------------

test("GAP: an expired session while polling a healthy stack abandons an update that then succeeds", async () => {
  // waitForStack polls DescribeStacks every 15 s for the whole stack update, which is where an
  // hour-long run meets a time-limited session. The exception is not retried and not classified,
  // so the operator sees a raw SDK sentence for a deploy that CloudFormation went on to complete.
  // See failure-modes.md findings 3 and 4.
  const state = replay({
    inProgressPolls: 10,
    describeStackThrows: {
      onPoll: 3,
      error: awsError("ExpiredTokenException", "The security token included in the request is expired"),
    },
  });

  const code = await run(deployArgv(), state.deps);

  assert.equal(code, 1);
  const message = state.stderr.join("\n");
  assert.match(message, /security token included in the request is expired/);
  // Nothing tells the operator to refresh the session, and nothing names the in-flight stack.
  assert.doesNotMatch(message, /federated session|--aws-profile|refresh/i);
  assert.doesNotMatch(message, /sample-cluster-analytics/);
  // It gave up on poll 3 of an update that had 10 in-progress polls to go, so it never saw the
  // stack reach a terminal status at all.
  assert.equal(state.polls, 3);
  assert.deepEqual(state.polled, []);
  assert.deepEqual(state.executed, [`${CLUSTER}-analytics`]);
});

test("the one-shot expired-session retry that does exist covers the optimized path's module re-read", async () => {
  // Positive control for the finding above: the same exception, in the one place the tool handles
  // it, is retried and the run completes.
  const state = replay({
    modules: [moduleRow("analytics", "analytics"), moduleRow("metrics", "metrics")],
    modulesReadThrowsAfterGroup: awsError(
      "ExpiredTokenException",
      "The security token included in the request is expired",
    ),
  });
  const helper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    upgrade: true,
    optimizeDeployment: true,
    staggerMs: 0,
    moduleIds: ["analytics", "metrics"],
    deps: state.deps,
  });

  await helper.invoke();

  assert.equal(state.modulesReadsAfterGroup, 2, "the throwing read was retried exactly once");
  assert.deepEqual([...state.executed].sort(), [`${CLUSTER}-analytics`, `${CLUSTER}-metrics`]);
});

test("GAP: any other expired-session exception is a hard failure", async () => {
  // The retry keys on the exact name ExpiredTokenException. S3 and STS raise ExpiredToken, and a
  // revoked or rotated session raises InvalidClientTokenId, none of which are retried anywhere.
  // See failure-modes.md finding 3.
  const state = replay({
    modules: [moduleRow("analytics", "analytics"), moduleRow("metrics", "metrics")],
    modulesReadThrowsAfterGroup: awsError("ExpiredToken", "The provided token has expired."),
  });
  const helper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    upgrade: true,
    optimizeDeployment: true,
    staggerMs: 0,
    moduleIds: ["analytics", "metrics"],
    deps: state.deps,
  });

  await assert.rejects(() => helper.invoke(), /The provided token has expired/);
});

// ---------------------------------------------------------------------------------------------
// 3. The operator runs the command again after a failure
// ---------------------------------------------------------------------------------------------

test("a module already at the target template produces an empty change set and executes nothing", async () => {
  const state = replay();
  state.deps.cfn.describeChangeSet = async () => ({
    Status: "FAILED",
    StatusReason: "The submitted information didn't contain changes. Submit different information to create a change set.",
    Changes: [],
  });

  const verdict = await new CdkInvoker({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleId: "analytics",
    moduleName: "analytics",
    moduleSet: "default",
    deps: state.deps,
  }).deployThroughChangeSet();

  assert.equal(verdict.empty, true);
  assert.deepEqual(state.executed, []);
  assert.ok(state.stdout.includes(`${CLUSTER}-analytics: no changes`));
});

test("a second run redeploys the module that succeeded as a no-op and retries the one that failed", async () => {
  const modules = [moduleRow("analytics", "analytics"), moduleRow("metrics", "metrics")];
  const empty = {
    Status: "FAILED",
    StatusReason: "The submitted information didn't contain changes.",
    Changes: [],
  };

  // Run one: analytics commits, metrics is rolled back by CloudFormation.
  const first = replay({ modules });
  let firstStack = "";
  first.deps.cfn.describeStack = async (stackName) => {
    firstStack = stackName;
    return stackName.endsWith("-metrics")
      ? { StackStatus: "UPDATE_ROLLBACK_COMPLETE", StackStatusReason: "resource failed" }
      : { StackStatus: "UPDATE_COMPLETE" };
  };
  const firstHelper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    upgrade: true,
    moduleIds: ["analytics", "metrics"],
    deps: first.deps,
  });
  await assert.rejects(() => firstHelper.invoke(), /UPDATE_ROLLBACK_COMPLETE/);
  assert.deepEqual(first.executed, [`${CLUSTER}-analytics`, `${CLUSTER}-metrics`]);
  assert.equal(firstStack, `${CLUSTER}-metrics`);

  // Run two: the same selection. analytics is already at target, metrics still has work.
  const second = replay({ modules });
  second.deps.cfn.describeChangeSet = async (input) =>
    input.StackName.endsWith("-analytics")
      ? empty
      : {
        Status: "CREATE_COMPLETE",
        Changes: [{ ResourceChange: { Action: "Modify", LogicalResourceId: "metricsrole", ResourceType: "AWS::IAM::Role" } }],
      };
  second.deps.cfn.describeStack = async () => ({ StackStatus: "UPDATE_COMPLETE" });
  const secondHelper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    upgrade: true,
    moduleIds: ["analytics", "metrics"],
    deps: second.deps,
  });

  await secondHelper.invoke();

  // Convergence: the completed module is re-synthesized but not updated, and only the failed
  // module's change set is executed.
  assert.deepEqual(second.executed, [`${CLUSTER}-metrics`]);
  assert.ok(second.stdout.includes(`${CLUSTER}-analytics: no changes`));
});

// ---------------------------------------------------------------------------------------------
// upgrade-cluster: what a re-run carries over
// ---------------------------------------------------------------------------------------------

interface UpgradeReplay {
  deps: UpgradeDeps;
  events: string[];
  protectedInstances: Set<string>;
  modifies: Array<{ instanceId: string; protected: boolean }>;
}

function upgradeReplay(input: {
  protectedInstances: Set<string>;
  protectionTags?: Set<string>;
  settings: Array<Record<string, unknown>>;
  deployFails?: boolean;
}): UpgradeReplay {
  const events: string[] = [];
  const modifies: Array<{ instanceId: string; protected: boolean }> = [];
  const protectionTags = input.protectionTags ?? new Set<string>();
  const writer: ConfigWriter = {
    async syncModulesInDb() {},
    async syncClusterSettingsInDb() {},
    async setConfigEntry(key, value) {
      events.push(`set:${key}=${String(value)}`);
      const table = rows[`${CLUSTER}.cluster-settings`]!;
      const row = table.find((entry) => entry["key"] === key);
      if (row === undefined) table.push({ key, value });
      else row["value"] = value;
    },
    async deleteConfigEntries() {},
  };
  const rows: Record<string, Array<Record<string, unknown>>> = {
    // Recovery needs the module-set owner even when only the scheduler is upgraded.
    // Include the deployed mapping so both runs reach the durable protection baseline.
    [`${CLUSTER}.cluster-settings`]: [
      { key: "global-settings.module_sets.default.cluster-manager.module_id", value: "cluster-manager" },
      ...input.settings,
    ],
    [`${CLUSTER}.modules`]: [moduleRow("scheduler", "scheduler")],
  };

  const deps: UpgradeDeps = {
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
        return "base_os: amazonlinux2023\n";
      },
    },
    async scan(scanInput) {
      return { Items: rows[scanInput.TableName] ?? [] };
    },
    async configWriter() {
      return writer;
    },
    async accountId() {
      return ACCOUNT;
    },
    async httpStatus() {
      return 200;
    },
    async sleep() {},
    now() {
      return Date.UTC(2026, 8, 10, 12, 0, 0);
    },
    uuid() {
      return "00000000-0000-4000-8000-000000000001";
    },
    out(line) {
      events.push(line);
    },
    err(line) {
      events.push(`err:${line}`);
    },
    async prompt(choice) {
      events.push(`prompt:${choice.message}`);
      // Declining the add-only sync keeps these tests off the template generator, which needs a
      // complete values.yml. Every other phase is accepted.
      return !choice.message.startsWith("Sync full configuration");
    },
    ec2: {
      async describeImages() {
        return [];
      },
      async describeInstanceTypeOfferings() {
        return [];
      },
      async describeInstanceAttribute(attributeInput) {
        return input.protectedInstances.has(attributeInput.instanceId);
      },
      async modifyInstanceAttribute(modifyInput) {
        modifies.push({ instanceId: modifyInput.instanceId, protected: modifyInput.protected });
        if (modifyInput.protected) input.protectedInstances.add(modifyInput.instanceId);
        else input.protectedInstances.delete(modifyInput.instanceId);
      },
      async createTags(tagInput) { protectionTags.add(tagInput.instanceId); },
      async deleteTags(tagInput) { protectionTags.delete(tagInput.instanceId); },
      async describeLiveInstances(liveInput) {
        return liveInput.instanceIds.filter((id) => protectionTags.has(id));
      },
    },
    cloudFormation: {
      async listStackResources(stackInput) {
        return stackInput.stackName.endsWith("-scheduler") ? { instanceIds: ["i-0sample"] } : { instanceIds: [] };
      },
    },
    openSearch: {
      async describeDomain() {
        return {};
      },
      async listInstanceTypeDetails() {
        return [];
      },
    },
    eolSoftwareStacks: {
      async setEnabled() {},
      async delete() {},
    },
    async deploy() {
      events.push("deploy");
      if (input.deployFails === true) throw new Error("Stack sample-cluster-scheduler ended UPDATE_ROLLBACK_COMPLETE.");
      for (const row of rows[`${CLUSTER}.modules`]!) row["version"] = ideaVersion();
    },
    regionAmiConfig() {
      return { [REGION]: { amazonlinux2023: "ami-release" } };
    },
    // The drift preview is injected so these tests do not need a complete values.yml on disk.
    // It runs after the base-OS resolution the refusal test exercises.
    async loadUpgradeDriftInput() {
      return {
        current: input.settings.map((row) => ({
          key: String(row["key"]),
          value: row["value"],
        })),
        generated: [],
        phase3: [{ key: "scheduler.instance_ami", value: "ami-release" }],
        stacks: [],
        replaceGlobalSettings: false,
        syncFullConfiguration: false,
      };
    },
  };
  return { deps, events, protectedInstances: input.protectedInstances, modifies };
}

const AL2023_SETTINGS = [
  { key: "cluster.cluster_s3_bucket", value: BUCKET },
  { key: "scheduler.base_os", value: "amazonlinux2023" },
];

test("a second run restores termination protection from the first run's marker", async () => {
  // Separate dependency objects model a restart, so recovery must rely on shared EC2 state.
  // The marker outlives the first run even though its in-memory restore list does not.
  const protectedInstances = new Set(["i-0sample"]);
  const protectionTags = new Set<string>();

  const first = upgradeReplay({ protectedInstances, protectionTags, settings: AL2023_SETTINGS, deployFails: true });
  await assert.rejects(
    () => upgradeCluster(first.deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: "default",
      baseOs: "amazonlinux2023",
      skipGlobalSettingsUpdate: true,
      modules: ["scheduler"],
    }),
    /UPDATE_ROLLBACK_COMPLETE/,
  );
  assert.deepEqual(first.modifies, [{ instanceId: "i-0sample", protected: false }]);
  assert.ok(
    first.events.some((line) => line.includes("termination protection is still cleared on i-0sample")),
    "the failing run names the instance it left unprotected",
  );
  assert.equal(protectedInstances.has("i-0sample"), false);
  assert.deepEqual([...protectionTags], ["i-0sample"]);

  const second = upgradeReplay({ protectedInstances, protectionTags, settings: AL2023_SETTINGS });
  await upgradeCluster(second.deps, {
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    baseOs: "amazonlinux2023",
    skipGlobalSettingsUpdate: true,
    modules: ["scheduler"],
  });

  assert.deepEqual(second.modifies, [{ instanceId: "i-0sample", protected: true }]);
  assert.equal(protectedInstances.has("i-0sample"), true);
  assert.equal(protectionTags.size, 0);
  assert.ok(second.events.includes("All upgrade phases completed successfully"));
});

test("a re-run after a partial base-OS rewrite refuses with an actionable message", async () => {
  // Phase 3 writes each row independently, so a failure inside it leaves both the old and the
  // target base OS in the table. Running again with no --base-os fails closed and names the fix.
  const state = upgradeReplay({
    protectedInstances: new Set(),
    settings: [
      { key: "cluster.cluster_s3_bucket", value: BUCKET },
      { key: "scheduler.base_os", value: "rhel9" },
      { key: "bastion-host.base_os", value: "amazonlinux2023" },
    ],
  });

  await assert.rejects(
    () => upgradeCluster(state.deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: "default",
      force: true,
      skipGlobalSettingsUpdate: true,
    }),
  );

  const message = state.events.join("\n");
  assert.match(message, /Could not determine the Base OS this cluster runs from its settings/);
  assert.match(message, /amazonlinux2023, rhel9/);
  assert.match(message, /Re-run with an explicit --base-os/);
  assert.deepEqual(state.modifies, [], "nothing was changed before the refusal");
});

test("GAP: upgrade-cluster offers no way to resume a failed run", async () => {
  // src/config/upgrade-state.ts holds a durable operation record with boundary and module
  // markers, a conditional-write lock and snapshot retention. Only the migrate driver uses it.
  // upgrade-cluster has no record, so a re-run starts at Phase 1 with a fresh deployment ID and
  // no statement of what the previous run completed.
  const program = new Command("ideactl");
  registerUpgradeCommands(program, upgradeReplay({ protectedInstances: new Set(), settings: AL2023_SETTINGS }).deps);
  const command = program.commands.find((candidate) => candidate.name() === "upgrade-cluster");
  assert.ok(command !== undefined, "upgrade-cluster is registered");
  // Exact Commander long names. `--deployment-identity` must not satisfy `--deployment-id`.
  const longs = command.options
    .map((option) => option.long)
    .filter((flag): flag is string => flag !== undefined && flag !== null);

  assert.ok(longs.includes("--deployment-id"));
  assert.equal(longs.some((flag) => flag === "--resume" || flag.startsWith("--resume-")), false);
  assert.equal(longs.includes("--state-bucket"), false);
  assert.equal(longs.includes("--continue"), false);
});
