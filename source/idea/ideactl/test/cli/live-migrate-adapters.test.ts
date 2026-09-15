/**
 * The live migration adapters, driven from fakes.
 *
 * The record store is exercised through the real journal so the conditional
 * write contract is proved rather than asserted, and every refusal is checked
 * in both directions: a green input must pass the same check that a sabotaged
 * input fails.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";

import type { Deps } from "../../src/cli/cdk-invoker.ts";
import {
  MIGRATION_STEPS,
  MigrationRefusedError,
  migrateCluster,
  type MigrateOptions,
  type MigrationContext,
} from "../../src/cli/commands/migrate.ts";
import {
  LiveMigrationSteps,
  MIGRATION_CAPABILITIES,
  MIGRATION_STEP_CAPABILITIES,
  canonicalJson,
  ACTING_MIGRATION_STEPS,
  capabilityReport,
  captureKey,
  createMigrationStateObjects,
  deployableStacks,
  driftReportFingerprint,
  firstUnsupportedStep,
  isMissingObject,
  isWriteConditionFailure,
  templateComparisonFingerprint,
  templateDigest,
  type MigrationAccountReads,
} from "../../src/cli/live-migrate-adapters.ts";
import {
  SchedulerStateUnreadableError,
  type BatchServerState,
} from "../../src/cli/scheduler-state-read.ts";
import { ClusterConfig, type ModuleInfo, type ScanPage } from "../../src/config/cluster-config.ts";
import type { UpgradeDriftInput } from "../../src/config/upgrade-drift.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const BUCKET = "sample-cluster-cluster-bucket";
const ACCOUNT = "123456789012";
const RELEASE = "26.09.0";
const IMAGE = `registry.example.invalid/control-plane@sha256:${"a".repeat(64)}`;
const DEPLOYMENT = "00000000-0000-0000-0000-000000000001";

const MODULES: ModuleInfo[] = [
  { module_id: "cluster", name: "cluster", type: "stack", stack_name: `${CLUSTER}-cluster` },
  { module_id: "cluster-manager", name: "cluster-manager", type: "app", stack_name: `${CLUSTER}-cluster-manager` },
  { module_id: "scheduler", name: "scheduler", type: "app", stack_name: `${CLUSTER}-scheduler` },
  { module_id: "global-settings", name: "global-settings", type: "config", stack_name: "True" },
];

const SETTINGS = [
  { key: "global-settings.module_sets.default.cluster.module_id", value: "cluster" },
  { key: "global-settings.module_sets.default.cluster-manager.module_id", value: "cluster-manager" },
  { key: "global-settings.module_sets.default.scheduler.module_id", value: "scheduler" },
  { key: "cluster-manager.maintenance.enabled", value: false },
  { key: "cluster.cluster_s3_bucket", value: BUCKET },
  { key: "cluster.route53.private_hosted_zone_id", value: "Z0000000000000000000" },
  {
    key: "cluster.load_balancers.external_alb.certificates.custom_dns_name",
    value: "cluster.example.invalid",
  },
];

const TEMPLATES: Record<string, string> = {
  [`${CLUSTER}-cluster`]: JSON.stringify({ Resources: { Vpc: { Type: "AWS::EC2::VPC" } } }),
  [`${CLUSTER}-cluster-manager`]: JSON.stringify({ Resources: { Asg: { Type: "AWS::AutoScaling::AutoScalingGroup" } } }),
  [`${CLUSTER}-scheduler`]: JSON.stringify({ Resources: { Record: { Type: "AWS::Route53::RecordSet" } } }),
};

// ---------------------------------------------------------------------------------------------
// fakes
// ---------------------------------------------------------------------------------------------

interface StoredObject {
  body: string;
  etag: string;
}

/** In-memory bucket that honours the two conditional headers S3 applies. */
class MemoryBucket {
  readonly objects = new Map<string, StoredObject>();
  readonly conditions: Array<Record<string, unknown>> = [];
  private revision = 0;

  async send(command: unknown): Promise<unknown> {
    if (command instanceof GetObjectCommand) {
      const key = `${command.input.Bucket}/${command.input.Key}`;
      const stored = this.objects.get(key);
      if (stored === undefined) {
        throw Object.assign(new Error("NoSuchKey"), { name: "NoSuchKey", $metadata: { httpStatusCode: 404 } });
      }
      return { Body: { transformToString: async () => stored.body }, ETag: stored.etag };
    }
    if (command instanceof PutObjectCommand) {
      const key = `${command.input.Bucket}/${command.input.Key}`;
      const stored = this.objects.get(key);
      this.conditions.push({
        key,
        IfMatch: command.input.IfMatch,
        IfNoneMatch: command.input.IfNoneMatch,
        ContentType: command.input.ContentType,
      });
      const matches = command.input.IfNoneMatch === "*"
        ? stored === undefined
        : command.input.IfMatch === undefined || stored?.etag === command.input.IfMatch;
      if (!matches) {
        throw Object.assign(new Error("At least one of the pre-conditions you specified did not hold"), {
          name: "PreconditionFailed",
          $metadata: { httpStatusCode: 412 },
        });
      }
      this.revision += 1;
      const etag = `"revision-${this.revision}"`;
      this.objects.set(key, { body: String(command.input.Body), etag });
      return { ETag: etag };
    }
    throw new Error("unexpected command");
  }
}

interface FakeState {
  settings: Array<Record<string, unknown>>;
  modules: ModuleInfo[];
  templates: Record<string, string>;
  stackStatus: Record<string, string>;
  bucket: MemoryBucket;
  puts: Array<{ key: string; body: string }>;
  output: string[];
  httpStatus: number;
  queueProfiles: Array<Record<string, unknown>>;
  /** When false the queue profile table read throws, modelling an unobservable scheduler. */
  queueProfilesReadable: boolean;
  /** The batch server's own answer, or, when false, a server that does not answer at all. */
  schedulerReadable: boolean;
  serverScheduling: boolean;
  serverQueues: Array<{ name: string; enabled: boolean; started: boolean }>;
  serverStateCounts: Record<string, number>;
  writes: Array<{ key: string; value: unknown }>;
}

function fakeState(overrides: Partial<FakeState> = {}): FakeState {
  return {
    settings: SETTINGS.map((entry) => ({ ...entry })),
    modules: MODULES.map((entry) => ({ ...entry })),
    templates: { ...TEMPLATES },
    stackStatus: {
      [`${CLUSTER}-cluster`]: "UPDATE_COMPLETE",
      [`${CLUSTER}-cluster-manager`]: "CREATE_COMPLETE",
      [`${CLUSTER}-scheduler`]: "UPDATE_COMPLETE",
    },
    bucket: new MemoryBucket(),
    puts: [],
    output: [],
    httpStatus: 200,
    queueProfiles: [
      { name: "compute", enabled: true, queues: ["high", "normal", "low"] },
      { name: "test", enabled: true, queues: ["test"] },
    ],
    queueProfilesReadable: true,
    schedulerReadable: true,
    serverScheduling: true,
    serverQueues: [
      { name: "normal", enabled: true, started: true },
      { name: "test", enabled: true, started: true },
    ],
    serverStateCounts: { Transit: 0, Queued: 0, Held: 0, Waiting: 0, Running: 0, Exiting: 0, Begun: 0 },
    writes: [],
    ...overrides,
  };
}

function fakeDeps(state: FakeState): Deps {
  const scan = async (input: { TableName: string }): Promise<ScanPage> => {
    if (input.TableName === `${CLUSTER}.cluster-settings`) return { Items: state.settings };
    if (input.TableName === `${CLUSTER}.modules`) return { Items: state.modules as unknown as Array<Record<string, unknown>> };
    if (input.TableName === `${CLUSTER}.scheduler.queue-profiles`) {
      if (!state.queueProfilesReadable) {
        throw Object.assign(new Error("no table"), { name: "ResourceNotFoundException" });
      }
      return { Items: state.queueProfiles };
    }
    throw Object.assign(new Error("no table"), { name: "ResourceNotFoundException" });
  };
  return {
    spawn: async () => 0,
    scan,
    cfn: {
      describeChangeSet: async () => ({}),
      executeChangeSet: async () => {},
      describeStack: async () => ({}),
    },
    s3: {
      putObject: async (input) => {
        state.puts.push({ key: input.Key, body: String(input.Body) });
        state.bucket.objects.set(`${input.Bucket}/${input.Key}`, { body: String(input.Body), etag: '"capture"' });
      },
      getObject: async (input) => {
        const stored = state.bucket.objects.get(`${input.Bucket}/${input.Key}`);
        if (stored === undefined) throw Object.assign(new Error("NoSuchKey"), { name: "NoSuchKey" });
        return stored.body;
      },
    },
    configWriter: async () => ({
      syncModulesInDb: async () => {},
      syncClusterSettingsInDb: async () => {},
      setConfigEntry: async (key, value) => {
        state.writes.push({ key, value });
        const existing = state.settings.find((row) => row["key"] === key);
        if (existing === undefined) state.settings.push({ key, value });
        else existing["value"] = value;
      },
      deleteConfigEntries: async () => {},
    }),
    accountId: async () => ACCOUNT,
    callerIdentity: async () => ({ account: ACCOUNT, arn: `arn:aws:iam::${ACCOUNT}:role/operator` }),
    httpStatus: async () => state.httpStatus,
    sleep: async () => {},
    now: () => Date.UTC(2026, 8, 11, 0, 0, 0),
    uuid: () => DEPLOYMENT,
    out: (line) => state.output.push(line),
    err: (line) => state.output.push(line),
    prompt: async () => true,
  };
}

function fakeReads(state: FakeState): MigrationAccountReads {
  return {
    async stackTemplate(input) {
      const template = state.templates[input.stackName];
      if (template === undefined) throw new Error(`no template for ${input.stackName}`);
      return template;
    },
    async stackSummary(input) {
      const status = state.stackStatus[input.stackName];
      if (status === undefined) throw new Error(`no stack ${input.stackName}`);
      return { status, lastUpdated: "2026-09-10T00:00:00.000Z", parameters: { BootstrapVersion: "28" } };
    },
    async clusterInstances() {
      return [
        { instanceId: "i-0000000000000001", state: "running", moduleId: "cluster-manager" },
        { instanceId: "i-0000000000000002", state: "stopped", moduleId: "vdc", nodeType: "dcv-host" },
      ];
    },
    async clusterListeners() {
      return [
        {
          loadBalancerArn: "lb-1",
          listenerArn: "listener-1",
          port: 443,
          defaultTargetGroupArns: ["tg-1"],
          rules: [{ ruleArn: "rule-1", priority: "1", targetGroupArns: ["tg-2"] }],
        },
      ];
    },
    async targetGroupHealth(input) {
      return input.targetGroupArns.map((targetGroupArn) => ({
        targetGroupArn,
        targetGroupName: targetGroupArn,
        healthy: 1,
        unhealthy: 0,
      }));
    },
    async recordSets() {
      return [{ name: `scheduler.${CLUSTER}.${REGION}.local.`, type: "A", values: ["192.0.2.10"] }];
    },
    async bucketObjects(input) {
      return [...state.bucket.objects.entries()]
        .filter(([key]) => key.startsWith(`${input.bucket}/${input.prefix}`))
        .map(([key, stored]) => ({
          key: key.slice(input.bucket.length + 1),
          etag: stored.etag,
          size: stored.body.length,
        }));
    },
  };
}

const driftInput: UpgradeDriftInput = {
  current: [{ key: "cluster.locale", value: "en_US" }],
  generated: [{ key: "cluster.locale", value: "en_US" }],
  replaceGlobalSettings: true,
  syncFullConfiguration: true,
};

/** A global row the run rewrites whose stored value differs from the generator's. */
const lossyDrift: UpgradeDriftInput = {
  current: [{ key: "global-settings.locale", value: "en_US", source: "operator" }],
  generated: [{ key: "global-settings.locale", value: "en_US.UTF-8" }],
  replaceGlobalSettings: true,
  syncFullConfiguration: true,
};

function steps(state: FakeState, options: {
  trunking?: boolean;
  drift?: UpgradeDriftInput;
} = {}): LiveMigrationSteps {
  return new LiveMigrationSteps(stepsInput(state, options));
}

function stepsInput(state: FakeState, options: {
  trunking?: boolean;
  drift?: UpgradeDriftInput;
} = {}) {
  return {
    deps: fakeDeps(state),
    reads: fakeReads(state),
    release: () => RELEASE,
    async clusterConfig() {
      return new ClusterConfig(
        state.settings as Array<{ key: string; value?: unknown }>,
        state.modules,
      );
    },
    async driftInput() {
      return options.drift ?? driftInput;
    },
    async trunkingEnabled() {
      return options.trunking !== false;
    },
    async schedulerState() {
      if (!state.schedulerReadable) {
        throw new SchedulerStateUnreadableError("the batch server did not answer qstat -B");
      }
      return batchServerState(state);
    },
  };
}

/** The batch server's answer, assembled the way the parser assembles a real one. */
function batchServerState(state: FakeState): BatchServerState {
  const counts = state.serverStateCounts;
  const sum = (names: string[]): number => names.reduce((total, name) => total + (counts[name] ?? 0), 0);
  return {
    serverName: "ip-192-0-2-10",
    scheduling: state.serverScheduling,
    queues: state.serverQueues.map((queue) => ({ ...queue })),
    stateCounts: { ...counts },
    queuedJobs: sum(["Queued", "Waiting", "Transit", "Held"]),
    provisioningJobs: sum(["Begun"]),
    runningJobs: sum(["Running", "Exiting"]),
  };
}

/** A cluster whose every admission control is closed and whose queue is empty. */
function closedState(overrides: Partial<FakeState> = {}): FakeState {
  return fakeState({
    serverScheduling: false,
    serverQueues: [
      { name: "normal", enabled: false, started: false },
      { name: "test", enabled: false, started: false },
    ],
    queueProfiles: [
      { name: "compute", enabled: false, queues: ["high", "normal", "low"] },
      { name: "test", enabled: false, queues: ["test"] },
    ],
    settings: [...SETTINGS.map((entry) => ({ ...entry }))],
    ...overrides,
  });
}

function context(overrides: Partial<MigrationContext> = {}): MigrationContext {
  return {
    clusterName: CLUSTER,
    awsRegion: REGION,
    targetVersion: RELEASE,
    targetBaseOs: "amazonlinux2023",
    imageDigest: IMAGE,
    moduleSet: "default",
    selectedModules: ["cluster", "cluster-manager"],
    deploymentId: DEPLOYMENT,
    resuming: false,
    ...overrides,
  };
}

function expectedTemplateAcceptance(state: FakeState): string {
  return templateComparisonFingerprint(
    RELEASE,
    deployableStacks(state.modules, CLUSTER).map((stack) => ({
      stackName: stack.stackName,
      digest: templateDigest(state.templates[stack.stackName] ?? ""),
    })),
  );
}

// ---------------------------------------------------------------------------------------------
// fingerprints
// ---------------------------------------------------------------------------------------------

test("a template fingerprint ignores key order and formatting but not content", () => {
  const compact = '{"Resources":{"A":{"Type":"X"},"B":{"Type":"Y"}}}';
  const reordered = '{\n  "Resources": {\n    "B": { "Type": "Y" },\n    "A": { "Type": "X" }\n  }\n}';
  const changed = '{"Resources":{"A":{"Type":"X"},"B":{"Type":"Z"}}}';

  assert.equal(templateDigest(compact), templateDigest(reordered));
  assert.notEqual(templateDigest(compact), templateDigest(changed));
  assert.equal(canonicalJson({ b: 1, a: [2, { d: 3, c: 4 }] }), '{"a":[2,{"c":4,"d":3}],"b":1}');
});

test("an acceptance is bound to the release and to every deployed template", () => {
  const stacks = [{ stackName: "a", digest: "1" }, { stackName: "b", digest: "2" }];
  const base = templateComparisonFingerprint(RELEASE, stacks);

  assert.equal(base, templateComparisonFingerprint(RELEASE, [...stacks].reverse()));
  assert.notEqual(base, templateComparisonFingerprint("26.10.0", stacks));
  assert.notEqual(base, templateComparisonFingerprint(RELEASE, [{ stackName: "a", digest: "1" }, { stackName: "b", digest: "3" }]));
  assert.notEqual(driftReportFingerprint(RELEASE, ["a"]), driftReportFingerprint(RELEASE, ["a", "b"]));
  assert.equal(driftReportFingerprint(RELEASE, ["b", "a"]), driftReportFingerprint(RELEASE, ["a", "b"]));
});

test("only module rows with a deployed stack of this cluster are compared", () => {
  assert.deepEqual(deployableStacks(MODULES, CLUSTER), [
    { moduleId: "cluster", stackName: `${CLUSTER}-cluster` },
    { moduleId: "cluster-manager", stackName: `${CLUSTER}-cluster-manager` },
    { moduleId: "scheduler", stackName: `${CLUSTER}-scheduler` },
  ]);
  assert.deepEqual(deployableStacks(MODULES, "other-cluster"), []);
});

// ---------------------------------------------------------------------------------------------
// capability table
// ---------------------------------------------------------------------------------------------

test("the capability report names where the run stops and what each remaining step waits on", () => {
  const report = capabilityReport();
  assert.ok(report !== undefined);
  assert.equal(firstUnsupportedStep(), "LEGACY_SCHEDULER_CAPTURED");
  assert.match(report, /^STOPS AT LEGACY_SCHEDULER_CAPTURED: 4 of 22 boundaries complete on their own and 9 more do the part they own/);
  for (const capability of MIGRATION_CAPABILITIES) {
    assert.match(report, new RegExp(`${capability.id}: `), `${capability.id} is not named`);
    assert.ok(report.includes(capability.request), `${capability.id} does not name its request`);
  }
  for (const step of MIGRATION_STEPS.slice(4)) {
    assert.ok(report.includes(step.id), `${step.id} is not named`);
  }
  // The two admission boundaries are executed and verified now, so nothing waits on them.
  for (const step of ["ADMISSION_CLOSED", "WORKLOAD_DRAINED"] as const) {
    assert.deepEqual(MIGRATION_STEP_CAPABILITIES[step], [], `${step} still waits on a capability`);
  }
  // No step waits on a block of unrelated capabilities: each names at most what it needs.
  for (const [step, capabilities] of Object.entries(MIGRATION_STEP_CAPABILITIES)) {
    assert.ok(capabilities.length <= 2, `${step} waits on ${capabilities.length} capabilities`);
  }
  // Every step that acts is still blocked, otherwise it belongs in neither list.
  for (const step of ACTING_MIGRATION_STEPS) {
    assert.ok(
      (MIGRATION_STEP_CAPABILITIES[step] ?? []).length > 0,
      `${step} acts and is not blocked, so it should complete rather than refuse`,
    );
  }
});

// ---------------------------------------------------------------------------------------------
// step 0
// ---------------------------------------------------------------------------------------------

test("pre-flight passes on green account checks and says where the run will stop", async () => {
  const state = fakeState();
  const observation = await steps(state).checkPrecondition("PREFLIGHT_PASSED", context({
    acceptTemplateComparison: expectedTemplateAcceptance(state),
  }));

  assert.match(observation.detail, /^PASS: 3 passed, 0 failed$/m);
  assert.match(observation.detail, /PASS \[error\] awsvpc-trunking/);
  assert.match(observation.detail, /PASS \[error\] template-comparison/);
  assert.match(observation.detail, /PASS \[error\] configuration-drift/);
  assert.match(observation.detail, /NOT CHECKED: that the control-plane image digest/);
  assert.match(observation.detail, /^STOPS AT LEGACY_SCHEDULER_CAPTURED: 4 of 22 boundaries/m);
  assert.equal(observation.ok, true);
});

test("pre-flight fails on each account check on its own", async () => {
  const state = fakeState();
  const accepted = expectedTemplateAcceptance(state);

  const noTrunking = await steps(state, { trunking: false }).checkPrecondition(
    "PREFLIGHT_PASSED",
    context({ acceptTemplateComparison: accepted }),
  );
  assert.match(noTrunking.detail, /FAIL \[error\] awsvpc-trunking/);
  assert.match(noTrunking.detail, /put-account-setting-default --name awsvpcTrunking --value enabled/);

  const noAcceptance = await steps(state).checkPrecondition("PREFLIGHT_PASSED", context());
  assert.match(noAcceptance.detail, /FAIL \[error\] template-comparison/);
  assert.match(noAcceptance.detail, new RegExp(`--accept-template-comparison ${accepted}`));

  const changed = fakeState({
    templates: { ...TEMPLATES, [`${CLUSTER}-cluster`]: JSON.stringify({ Resources: {} }) },
  });
  const stale = await steps(changed).checkPrecondition("PREFLIGHT_PASSED", context({
    acceptTemplateComparison: accepted,
  }));
  assert.match(stale.detail, /FAIL \[error\] template-comparison/);

  const lossy = await steps(state, { drift: lossyDrift }).checkPrecondition(
    "PREFLIGHT_PASSED",
    context({ acceptTemplateComparison: accepted }),
  );
  assert.match(lossy.detail, /FAIL \[error\] configuration-drift/);
  assert.match(lossy.detail, /--accept-drift [0-9a-f]{64}/);
});

test("an accepted drift fingerprint clears only the report it names", async () => {
  const state = fakeState();
  const drift = lossyDrift;
  const refused = await steps(state, { drift }).checkPrecondition("PREFLIGHT_PASSED", context({
    acceptTemplateComparison: expectedTemplateAcceptance(state),
  }));
  const offered = refused.detail.match(/--accept-drift ([0-9a-f]{64})/)?.[1];
  assert.ok(offered !== undefined);

  const accepted = await steps(state, { drift }).checkPrecondition("PREFLIGHT_PASSED", context({
    acceptTemplateComparison: expectedTemplateAcceptance(state),
    acceptDrift: offered,
  }));
  assert.match(accepted.detail, /PASS \[error\] configuration-drift/);

  const wrong = await steps(state, { drift }).checkPrecondition("PREFLIGHT_PASSED", context({
    acceptTemplateComparison: expectedTemplateAcceptance(state),
    acceptDrift: "0".repeat(64),
  }));
  assert.match(wrong.detail, /FAIL \[error\] configuration-drift/);
});

// ---------------------------------------------------------------------------------------------
// step 1
// ---------------------------------------------------------------------------------------------

test("the operation precondition refuses a stack that is mid-operation", async () => {
  const state = fakeState({
    stackStatus: {
      [`${CLUSTER}-cluster`]: "UPDATE_COMPLETE",
      [`${CLUSTER}-cluster-manager`]: "UPDATE_IN_PROGRESS",
      [`${CLUSTER}-scheduler`]: "UPDATE_COMPLETE",
    },
  });
  const observation = await steps(state).checkPrecondition("OPERATION_STARTED", context());

  assert.equal(observation.ok, false);
  assert.match(observation.detail, new RegExp(`${CLUSTER}-cluster-manager=UPDATE_IN_PROGRESS`));
});

test("the operation precondition refuses a template that changed after the acceptance", async () => {
  const state = fakeState();
  const stale = expectedTemplateAcceptance(state);
  state.templates[`${CLUSTER}-cluster`] = JSON.stringify({ Resources: { Vpc: { Type: "AWS::EC2::VPC", Metadata: {} } } });

  const observation = await steps(state).checkPrecondition("OPERATION_STARTED", context({
    acceptTemplateComparison: stale,
  }));
  assert.equal(observation.ok, false);
  assert.match(observation.detail, /a deployed template changed after the accepted comparison/);

  const current = await steps(state).checkPrecondition("OPERATION_STARTED", context({
    acceptTemplateComparison: expectedTemplateAcceptance(state),
  }));
  assert.equal(current.ok, true);
});

test("the before-state capture writes one object and reads it back", async () => {
  const state = fakeState();
  const observation = await steps(state).execute("OPERATION_STARTED", context());

  assert.equal(observation.ok, true);
  assert.deepEqual(state.puts.map((put) => put.key), [captureKey(context())]);
  assert.match(observation.detail, /settings=7; modules=4; queueProfiles=2\/2 enabled; stacks=3; instances=2; running=1/);
  assert.match(observation.detail, /listeners=1; targetGroups=2; recordSets=1/);
  assert.match(observation.detail, /preRunHealth=200/);

  const capture = JSON.parse(state.puts[0]?.body ?? "{}");
  assert.equal(capture.clusterName, CLUSTER);
  assert.equal(capture.imageDigest, IMAGE);
  assert.equal(capture.release, RELEASE);
  assert.equal(capture.stacks[`${CLUSTER}-cluster`].status, "UPDATE_COMPLETE");
  assert.equal(capture.stacks[`${CLUSTER}-cluster`].digest, templateDigest(TEMPLATES[`${CLUSTER}-cluster`] ?? ""));
  assert.equal(capture.settings.length, 7);
  assert.equal(capture.queueProfiles.length, 2);
  assert.equal(capture.valuesFileSha256, "absent");
  assert.deepEqual(capture.recordSets, [
    { name: `scheduler.${CLUSTER}.${REGION}.local.`, type: "A", values: ["192.0.2.10"] },
  ]);
});

test("a capture that cannot be read back is not reported as committed", async () => {
  const state = fakeState();
  const deps = fakeDeps(state);
  const executor = new LiveMigrationSteps({
    ...stepsInput(state),
    deps: { ...deps, s3: { ...deps.s3, putObject: async () => {} } },
  });

  const observation = await executor.execute("OPERATION_STARTED", context());
  assert.equal(observation.ok, false);
  assert.match(observation.detail, /a read back did not find it/);
});

test("reconciliation distinguishes a stored capture from a missing one", async () => {
  const state = fakeState();
  const executor = steps(state);

  const missing = await executor.reconcile("OPERATION_STARTED", context());
  assert.equal(missing.state, "retryable");

  await executor.execute("OPERATION_STARTED", context());
  const stored = await executor.reconcile("OPERATION_STARTED", context());
  assert.equal(stored.state, "committed");

  const blocked = await executor.reconcile("ADMISSION_CLOSED", context());
  assert.equal(blocked.state, "retryable");
  assert.match(blocked.detail, /never ran/);
});

// ---------------------------------------------------------------------------------------------
// blocked steps
// ---------------------------------------------------------------------------------------------

const ROUTING_STEP_IDS = new Set([
  "CLUSTER_MANAGER_ROUTED",
  "CLUSTER_MANAGER_LEGACY_REMOVED",
  "VDC_ROUTED",
  "VDC_LEGACY_REMOVED",
  "SCHEDULER_ROUTED",
  "SCHEDULER_LEGACY_REMOVED",
]);

const ACTING_STEPS = new Set(["ADMISSION_CLOSED", "CONFIGURATION_STAGED", "SCHEDULER_DNS_RETAINED"]);

test("every step this release cannot run refuses at its precondition and on execution", async () => {
  const state = fakeState();
  const executor = steps(state);

  for (const step of MIGRATION_STEPS) {
    if (step.id === "PREFLIGHT_PASSED" || step.id === "OPERATION_STARTED") continue;
    // Four steps have a precondition of their own: their refusal is specified behaviour, not a
    // gap in the driver, and three of them write the row they own before refusing.
    if (ACTING_STEPS.has(step.id)) {
      assert.equal((await executor.checkPrecondition(step.id, context())).ok, true, `${step.id} precondition`);
      continue;
    }
    if (step.id === "ADMISSION_CLOSED") {
      // Both admission boundaries have real preconditions now: the first passes on a readable
      // cluster and the second refuses while anything still admits work.
      assert.equal((await executor.checkPrecondition(step.id, context())).ok, true);
      continue;
    }
    if (step.id === "WORKLOAD_DRAINED") {
      const open = await executor.checkPrecondition(step.id, context());
      assert.equal(open.ok, false);
      assert.match(open.detail, /these admission controls are still open/);
      continue;
    }
    if (step.id === "ECS_CONFIGURATION_ACTIVE") {
      const gated = await executor.checkPrecondition(step.id, context());
      assert.equal(gated.ok, false);
      assert.match(gated.detail, /the container module has no row in/);
      continue;
    }
    if (ROUTING_STEP_IDS.has(step.id)) {
      const gated = await executor.checkPrecondition(step.id, context());
      assert.equal(gated.ok, false);
      assert.match(gated.detail, /ecs.enabled is off, so there is nothing to route to/);
      continue;
    }
    const observation = await executor.checkPrecondition(step.id, context());
    assert.equal(observation.ok, false, `${step.id} passed its precondition`);
    assert.match(observation.detail, /cannot be executed or verified by this release/);
    await assert.rejects(
      executor.execute(step.id, context()),
      MigrationRefusedError,
      `${step.id} executed something`,
    );
  }
});

// ---------------------------------------------------------------------------------------------
// closing admission: the tool announces and verifies, a person closes the batch server
// ---------------------------------------------------------------------------------------------

test("closing admission announces maintenance and reports the batch server's own state", async () => {
  const state = fakeState();
  const observation = await steps(state).closeScheduler(context());

  assert.deepEqual(state.writes, [
    { key: "cluster-manager.maintenance.message", value: "This cluster is undergoing maintenance. Job submission is closed." },
    { key: "cluster-manager.maintenance.enabled", value: true },
  ]);
  assert.equal(observation.maintenanceEnabled, true);
  assert.equal(observation.schedulingEnabled, true, "the fake server is still scheduling");
  // Both admission paths are named: the server's own queues and the profile rows.
  assert.deepEqual(observation.enabledQueues, ["normal", "test", "profile:compute", "profile:test"]);
  assert.match(observation.detail, /0 of 2 queue profiles are disabled/);
  assert.match(observation.detail, /scheduling=true/);
  assert.ok(
    state.output.some((line) => line.includes('qmgr -c "set server scheduling = False"')),
    "the commands a person runs were not printed",
  );
  assert.ok(state.output.some((line) => line.includes("set queue normal enabled = False")));
  assert.ok(state.output.some((line) => line.includes("queue profile compute still admits work")));
});

test("a rerun is not asked to repeat a command already run", async () => {
  // Scheduling is already off and one queue is not: only the queue line belongs on screen.
  const state = fakeState({
    serverScheduling: false,
    serverQueues: [
      { name: "normal", enabled: true, started: true },
      { name: "test", enabled: false, started: false },
    ],
  });
  await steps(state).closeScheduler(context());

  assert.ok(
    !state.output.some((line) => line.includes("set server scheduling = False")),
    "scheduling is already off and the command was printed again",
  );
  assert.ok(state.output.some((line) => line.includes("set queue normal enabled = False")));
  assert.ok(
    !state.output.some((line) => line.includes("set queue test enabled = False")),
    "a queue that is already disabled was named",
  );
});

test("a closed batch server and closed profiles report a closed admission boundary", async () => {
  const state = closedState();
  const observation = await steps(state).closeScheduler(context());

  assert.equal(observation.schedulingEnabled, false);
  assert.deepEqual(observation.enabledQueues, []);
  assert.equal(observation.queuedJobs, 0);
  assert.equal(observation.runningJobs, 0);
  assert.match(observation.detail, /2 of 2 queue profiles are disabled/);
  assert.ok(
    !state.output.some((line) => line.includes("qmgr")),
    "nothing is left for a person to do, so no command should be printed",
  );
});

test("a job still in flight keeps the closure open even with every queue disabled", async () => {
  const state = closedState({
    serverStateCounts: { Transit: 0, Queued: 0, Held: 0, Waiting: 0, Running: 1, Exiting: 0, Begun: 0 },
  });
  const observation = await steps(state).closeScheduler(context());

  assert.equal(observation.runningJobs, 1);
  assert.match(observation.detail, /Running:1/);
});

test("an unreadable batch server refuses before anything is announced", async () => {
  const state = fakeState({ schedulerReadable: false });
  const observation = await steps(state).checkPrecondition("ADMISSION_CLOSED", context());

  assert.equal(observation.ok, false);
  assert.match(observation.detail, /the batch server's own state could not be read/);
  assert.match(observation.detail, /an unobservable scheduler is not a closed scheduler/);
  assert.deepEqual(state.writes, [], "nothing was announced against an unreadable batch server");
});

test("an unreadable queue profile table refuses before anything is announced", async () => {
  const state = fakeState({ queueProfilesReadable: false });
  const observation = await steps(state).checkPrecondition("ADMISSION_CLOSED", context());

  assert.equal(observation.ok, false);
  assert.match(observation.detail, /an unobservable scheduler is not a closed scheduler/);
  assert.deepEqual(state.writes, [], "nothing was announced on an unreadable cluster");
});

// ---------------------------------------------------------------------------------------------
// the drain boundary
// ---------------------------------------------------------------------------------------------

test("the drain boundary refuses while any admission control is still open", async () => {
  for (const [label, state] of [
    ["maintenance off", closedState()],
    ["scheduling on", closedState({ serverScheduling: true })],
    ["a batch queue enabled", closedState({
      serverQueues: [{ name: "normal", enabled: true, started: false }],
    })],
    ["a queue profile enabled", closedState({
      queueProfiles: [{ name: "compute", enabled: true, queues: ["normal"] }],
    })],
  ] as Array<[string, FakeState]>) {
    if (label !== "maintenance off") {
      state.settings.push({ key: "cluster-manager.maintenance.enabled", value: true });
    }
    const observation = await steps(state).checkPrecondition("WORKLOAD_DRAINED", context());
    assert.equal(observation.ok, false, `${label} passed the drain precondition`);
    assert.match(observation.detail, /these admission controls are still open/);
  }
});

test("the drain boundary passes once every control is closed", async () => {
  const state = closedState();
  state.settings.push({ key: "cluster-manager.maintenance.enabled", value: true });

  const observation = await steps(state).checkPrecondition("WORKLOAD_DRAINED", context());
  assert.equal(observation.ok, true, observation.detail);
  assert.match(observation.detail, /maintenance is enabled, no queue profile admits work/);
});

test("an unreadable batch server refuses the drain precondition rather than assuming it drained", async () => {
  const state = closedState({ schedulerReadable: false });
  state.settings.push({ key: "cluster-manager.maintenance.enabled", value: true });

  const observation = await steps(state).checkPrecondition("WORKLOAD_DRAINED", context());
  assert.equal(observation.ok, false);
  assert.match(observation.detail, /an unobservable scheduler is not a closed scheduler/);
});

test("confirming the drain refuses on work in flight and on a surviving compute node", async () => {
  const held = closedState({
    serverStateCounts: { Transit: 0, Queued: 0, Held: 2, Waiting: 0, Running: 0, Exiting: 0, Begun: 0 },
  });
  const onJobs = await steps(held).execute("WORKLOAD_DRAINED", context());
  assert.equal(onJobs.ok, false);
  assert.match(onJobs.detail, /still holds 2 jobs/);
  assert.match(onJobs.detail, /do not cancel them here/);

  const empty = closedState();
  const clean = await steps(empty).execute("WORKLOAD_DRAINED", context());
  assert.equal(clean.ok, true, clean.detail);
  assert.match(clean.detail, /no compute node carries the legacy scheduler address/);
});

test("a compute node still registered refuses the drain rather than terminating it", async () => {
  const state = closedState();
  const executor = new LiveMigrationSteps({
    ...stepsInput(state),
    reads: {
      ...fakeReads(state),
      async clusterInstances() {
        return [
          { instanceId: "i-0000000000000009", state: "running", moduleId: "scheduler", nodeType: "compute-node" },
        ];
      },
    },
  });

  const observation = await executor.execute("WORKLOAD_DRAINED", context());
  assert.equal(observation.ok, false);
  assert.match(observation.detail, /1 compute nodes still carry the legacy scheduler address/);
  assert.match(observation.detail, /i-0000000000000009=running/);
});

// ---------------------------------------------------------------------------------------------
// the configuration boundaries
// ---------------------------------------------------------------------------------------------

test("staging seeds the container scheduler at zero and refuses on the generated rows", async () => {
  const state = fakeState();

  await assert.rejects(steps(state).execute("CONFIGURATION_STAGED", context()), (error: unknown) => {
    assert.ok(error instanceof MigrationRefusedError);
    assert.match(error.message, /seeded ecs.tasks.scheduler.desired=0/);
    assert.match(error.message, /waiting on container-configuration: /);
    return true;
  });

  assert.deepEqual(state.writes, [{ key: "ecs.tasks.scheduler.desired", value: 0 }]);
});

test("staging refuses when container routing is already on", async () => {
  const state = fakeState();
  state.settings.push({ key: "ecs.enabled", value: true });

  const observation = await steps(state).checkPrecondition("CONFIGURATION_STAGED", context());
  assert.equal(observation.ok, false);
  assert.match(observation.detail, /already true, which is the ECS_CONFIGURATION_ACTIVE boundary/);
  assert.deepEqual(state.writes, []);
});

test("the retain step sets the scheduler DNS flag and refuses on the deploy it cannot run", async () => {
  const state = fakeState();

  assert.equal((await steps(state).checkPrecondition("SCHEDULER_DNS_RETAINED", context())).ok, true);
  await assert.rejects(steps(state).execute("SCHEDULER_DNS_RETAINED", context()), (error: unknown) => {
    assert.ok(error instanceof MigrationRefusedError);
    assert.match(error.message, /set scheduler.retain_dns_record=true/);
    assert.match(error.message, /waiting on module-deployment: /);
    return true;
  });

  assert.deepEqual(state.writes, [{ key: "scheduler.retain_dns_record", value: true }]);
});

test("activation writes the container rows only once the module is registered", async () => {
  const blocked = fakeState();
  const gated = await steps(blocked).checkPrecondition("ECS_CONFIGURATION_ACTIVE", context());
  assert.equal(gated.ok, false);
  assert.match(gated.detail, /the container module has no row in sample-cluster.modules/);
  assert.deepEqual(blocked.writes, []);

  const ready = fakeState();
  ready.modules.push({ module_id: "ecs", name: "ecs", type: "stack", stack_name: `${CLUSTER}-ecs` });
  ready.templates[`${CLUSTER}-ecs`] = JSON.stringify({ Resources: {} });
  ready.stackStatus[`${CLUSTER}-ecs`] = "CREATE_COMPLETE";

  assert.equal((await steps(ready).checkPrecondition("ECS_CONFIGURATION_ACTIVE", context())).ok, true);
  const observation = await steps(ready).execute("ECS_CONFIGURATION_ACTIVE", context());
  assert.equal(observation.ok, true);
  assert.deepEqual(ready.writes, [
    { key: "ecs.image", value: IMAGE },
    { key: "scheduler.use_stable_server_name", value: true },
    { key: "ecs.tasks.scheduler.desired", value: 0 },
    { key: "ecs.enabled", value: true },
  ], "the flag is written last, after the rows its consumers read");
});

test("a row that does not read back as written is a refusal, not a pass", async () => {
  const state = fakeState();
  const deps = fakeDeps(state);
  const executor = new LiveMigrationSteps({
    ...stepsInput(state),
    deps: {
      ...deps,
      // A writer that accepts and drops, which is what a wrong-permission path looks like.
      configWriter: async () => ({
        syncModulesInDb: async () => {},
        syncClusterSettingsInDb: async () => {},
        setConfigEntry: async () => {},
        deleteConfigEntries: async () => {},
      }),
    },
    reads: fakeReads(state),
    release: () => RELEASE,
    async clusterConfig() {
      return new ClusterConfig(state.settings as Array<{ key: string; value?: unknown }>, state.modules);
    },
    async driftInput() {
      return driftInput;
    },
    async trunkingEnabled() {
      return true;
    },
  });

  await assert.rejects(executor.execute("SCHEDULER_DNS_RETAINED", context()), (error: unknown) => {
    assert.ok(error instanceof MigrationRefusedError);
    assert.match(error.message, /did not read back as written: scheduler.retain_dns_record=undefined/);
    return true;
  });
});

test("the routed and removed boundaries drive the retain-hosts row in both directions", async () => {
  const routed = fakeState();
  routed.settings.push({ key: "ecs.enabled", value: true });
  assert.equal((await steps(routed).checkPrecondition("VDC_ROUTED", context())).ok, true);
  await assert.rejects(steps(routed).execute("VDC_ROUTED", context()), (error: unknown) => {
    assert.ok(error instanceof MigrationRefusedError);
    assert.match(error.message, /set ecs.retain_existing_hosts=true/);
    assert.match(error.message, /keeps every legacy host/);
    assert.match(error.message, /waiting on module-deployment: /);
    return true;
  });
  assert.deepEqual(routed.writes, [{ key: "ecs.retain_existing_hosts", value: true }]);

  const removed = fakeState();
  removed.settings.push({ key: "ecs.enabled", value: true });
  await assert.rejects(steps(removed).execute("VDC_LEGACY_REMOVED", context()), (error: unknown) => {
    assert.ok(error instanceof MigrationRefusedError);
    assert.match(error.message, /set ecs.retain_existing_hosts=false/);
    assert.match(error.message, /removes only its legacy host resources/);
    return true;
  });
  assert.deepEqual(removed.writes, [{ key: "ecs.retain_existing_hosts", value: false }]);
});

// ---------------------------------------------------------------------------------------------
// durable record store
// ---------------------------------------------------------------------------------------------

test("the record store claims an absent record and then advances it by revision", async () => {
  const bucket = new MemoryBucket();
  const store = createMigrationStateObjects({ client: async () => bucket });

  assert.equal(await store.getObject({ bucket: BUCKET, key: "values/upgrade-state.json" }), undefined);
  const first = await store.putObject({
    bucket: BUCKET,
    key: "values/upgrade-state.json",
    body: "{}",
    condition: { kind: "absent" },
  });
  assert.ok(first !== undefined);
  assert.deepEqual(bucket.conditions[0]?.IfNoneMatch, "*");
  assert.equal(bucket.conditions[0]?.ContentType, "application/json");

  assert.equal(
    await store.putObject({
      bucket: BUCKET,
      key: "values/upgrade-state.json",
      body: "{}",
      condition: { kind: "absent" },
    }),
    undefined,
    "a second claim of the same record must not succeed",
  );
  const second = await store.putObject({
    bucket: BUCKET,
    key: "values/upgrade-state.json",
    body: '{"n":2}',
    condition: { kind: "revision", revision: first.revision },
  });
  assert.ok(second !== undefined);
  assert.notEqual(second.revision, first.revision);
  assert.equal(
    await store.putObject({
      bucket: BUCKET,
      key: "values/upgrade-state.json",
      body: '{"n":3}',
      condition: { kind: "revision", revision: first.revision },
    }),
    undefined,
    "a stale revision must not advance the record",
  );
  const stored = await store.getObject({ bucket: BUCKET, key: "values/upgrade-state.json" });
  assert.equal(stored?.body, '{"n":2}');
});

test("write-condition and missing-object errors are classified, other errors are not", () => {
  assert.equal(isWriteConditionFailure({ name: "PreconditionFailed" }), true);
  assert.equal(isWriteConditionFailure({ $metadata: { httpStatusCode: 409 } }), true);
  assert.equal(isWriteConditionFailure({ name: "AccessDenied", $metadata: { httpStatusCode: 403 } }), false);
  assert.equal(isMissingObject({ name: "NoSuchKey" }), true);
  assert.equal(isMissingObject({ $metadata: { httpStatusCode: 404 } }), true);
  assert.equal(isMissingObject({ name: "AccessDenied", $metadata: { httpStatusCode: 403 } }), false);
});

/** Drive the real driver over the real record store, with only the account faked. */
async function driveMigration(state: FakeState, overrides: Partial<MigrateOptions> = {}): Promise<MemoryBucket> {
  const bucket = new MemoryBucket();
  const options: MigrateOptions = {
    clusterName: CLUSTER,
    awsRegion: REGION,
    stateBucket: BUCKET,
    targetBaseOs: "amazonlinux2023",
    imageDigest: IMAGE,
    moduleSet: "default",
    selectedModules: ["cluster", "cluster-manager"],
    deploymentId: DEPLOYMENT,
    acceptTemplateComparison: expectedTemplateAcceptance(state),
    ...overrides,
  };

  await assert.rejects(
    migrateCluster(
      {
        stateObjects: createMigrationStateObjects({ client: async () => bucket }),
        steps: steps(state),
        uuid: () => DEPLOYMENT,
        targetVersion: () => RELEASE,
        out: (line) => state.output.push(line),
        now: () => Date.UTC(2026, 8, 11, 0, 0, 0),
      },
      options,
    ),
    MigrationRefusedError,
  );
  return bucket;
}

test("the run reaches the admission boundary, announces maintenance and stops there", async () => {
  const state = fakeState();
  const bucket = await driveMigration(state);

  const record = JSON.parse([...bucket.objects.values()][0]?.body ?? "{}");
  const snapshots = (record.snapshots as Array<{ name: string }>).map((snapshot) => snapshot.name);

  assert.ok(snapshots.includes("migration:PREFLIGHT_PASSED:committed"));
  assert.ok(snapshots.includes("migration:OPERATION_STARTED:committed"));
  assert.ok(snapshots.includes("migration:ADMISSION_CLOSED:started"));
  assert.ok(!snapshots.includes("migration:ADMISSION_CLOSED:committed"));
  assert.equal(record.status, "failed");
  assert.deepEqual(state.puts.map((put) => put.key), [captureKey(context())]);
  assert.deepEqual(state.writes.map((write) => write.key), [
    "cluster-manager.maintenance.message",
    "cluster-manager.maintenance.enabled",
  ]);
  assert.ok(state.output.some((line) => line.startsWith("ANNOUNCE [ADMISSION_CLOSED]")));
  assert.ok(state.output.some((line) => line.startsWith("RUN [OPERATION_STARTED]")));
  assert.ok(!state.output.some((line) => line.startsWith("RUN [WORKLOAD_DRAINED]")));
});

test("a failed account check stops the run before a record exists", async () => {
  const state = fakeState();
  const bucket = await driveMigration(state, { acceptTemplateComparison: undefined });

  assert.equal(bucket.objects.size, 0, "a refused pre-flight must not create a record");
  assert.deepEqual(state.writes, []);
  assert.ok(!state.output.some((line) => line.startsWith("RUN [")));
});

test("with admission closed by hand the run clears both admission boundaries and stops at the capture", async () => {
  const state = closedState();
  const bucket = await driveMigration(state);

  const record = JSON.parse([...bucket.objects.values()][0]?.body ?? "{}");
  const snapshots = (record.snapshots as Array<{ name: string }>).map((snapshot) => snapshot.name);

  assert.ok(snapshots.includes("migration:ADMISSION_CLOSED:committed"), "admission did not commit");
  assert.ok(snapshots.includes("migration:WORKLOAD_DRAINED:committed"), "the drain did not commit");
  assert.ok(
    !snapshots.includes("migration:LEGACY_SCHEDULER_CAPTURED:started"),
    "a blocked step must refuse before its started marker",
  );
  assert.ok(state.output.some((line) => line.startsWith("RUN [WORKLOAD_DRAINED]")));
  assert.ok(
    state.output.some((line) => line.includes("LEGACY_SCHEDULER_CAPTURED cannot be executed")),
    "the run did not name where it stopped",
  );
});
