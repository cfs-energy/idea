/**
 * Migration driver tests use an in-memory conditional object and injected
 * cluster operations. No network client is constructed.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { Command } from "commander";

import {
  MIGRATION_STEPS,
  MigrationRefusedError,
  migrateCluster,
  registerMigrateCommands,
  type ExecutableMigrationStepId,
  type MigrateDeps,
  type MigrateOptions,
  type MigrationContext,
  type MigrationObservation,
  type MigrationReconciliation,
  type MigrationStepExecutor,
  type MigrationStepId,
  type SchedulerClosureObservation,
} from "../../src/cli/commands/migrate.ts";
import {
  parseUpgradeOperationRecord,
  type UpgradeStateObjectApi,
  type UpgradeStateWriteCondition,
  type VersionedUpgradeStateObject,
} from "../../src/config/upgrade-state.ts";

const imageDigest = `registry.example.invalid/control-plane@sha256:${"a".repeat(64)}`;
const deploymentId = "00000000-0000-0000-0000-000000000001";

const newRunOptions: MigrateOptions = {
  clusterName: "sample-cluster",
  awsRegion: "us-east-2",
  awsProfile: "sample-profile",
  stateBucket: "sample-cluster-state",
  targetBaseOs: "amazonlinux2023",
  imageDigest,
  moduleSet: "default",
  selectedModules: ["cluster", "cluster-manager", "scheduler", "vdc", "ecs"],
  deploymentId,
};

/** Atomic object storage used to exercise the real shared journal. */
class MemoryStateObjects implements UpgradeStateObjectApi {
  private object: VersionedUpgradeStateObject | undefined;
  private nextRevision = 1;

  async getObject(): Promise<VersionedUpgradeStateObject | undefined> {
    return this.object === undefined ? undefined : { ...this.object };
  }

  async putObject(input: {
    bucket: string;
    key: string;
    body: string;
    condition: UpgradeStateWriteCondition;
  }): Promise<{ revision: string } | undefined> {
    const matches = input.condition.kind === "absent"
      ? this.object === undefined
      : this.object?.revision === input.condition.revision;
    if (!matches) return undefined;
    const revision = `"revision-${this.nextRevision}"`;
    this.nextRevision += 1;
    this.object = { body: input.body, revision };
    return { revision };
  }

  record() {
    if (this.object === undefined) throw new Error("No durable record exists");
    return parseUpgradeOperationRecord(this.object.body);
  }

  exists(): boolean {
    return this.object !== undefined;
  }
}

interface FakeStepOptions {
  failPrecondition?: MigrationStepId;
  throwAt?: ExecutableMigrationStepId;
  scheduler?: SchedulerClosureObservation;
  reconciliation?: Readonly<Partial<Record<MigrationStepId, MigrationReconciliation>>>;
}

/** Records every injected operation and can fail one named boundary. */
class FakeSteps implements MigrationStepExecutor {
  readonly checks: MigrationStepId[] = [];
  readonly actions: MigrationStepId[] = [];
  readonly reconciliations: MigrationStepId[] = [];
  private readonly options: FakeStepOptions;

  constructor(options: FakeStepOptions = {}) {
    this.options = options;
  }

  async checkPrecondition(
    step: MigrationStepId,
    _context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    this.checks.push(step);
    if (this.options.failPrecondition === step) {
      return { ok: false, detail: `${step} precondition failed` };
    }
    return { ok: true, detail: `${step} precondition passed` };
  }

  async execute(
    step: ExecutableMigrationStepId,
    _context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    this.actions.push(step);
    if (this.options.throwAt === step) throw new Error(`simulated interruption at ${step}`);
    return { ok: true, detail: `${step} completed and reconciled` };
  }

  async closeScheduler(_context: Readonly<MigrationContext>): Promise<SchedulerClosureObservation> {
    this.actions.push("ADMISSION_CLOSED");
    return this.options.scheduler ?? {
      detail: "maintenance and PBS controls read back closed with empty inventories",
      maintenanceEnabled: true,
      schedulingEnabled: false,
      enabledQueues: [],
      queuedJobs: 0,
      provisioningJobs: 0,
      runningJobs: 0,
    };
  }

  async reconcile(
    step: MigrationStepId,
    _context: Readonly<MigrationContext>,
  ): Promise<MigrationReconciliation> {
    this.reconciliations.push(step);
    return this.options.reconciliation?.[step] ?? {
      state: "retryable",
      detail: `${step} did not commit and is safe to retry`,
    };
  }
}

function makeDeps(
  stateObjects: MemoryStateObjects,
  steps: FakeSteps,
  output: string[] = [],
): MigrateDeps {
  let uuidSequence = 0;
  return {
    stateObjects,
    steps,
    uuid() {
      uuidSequence += 1;
      return `00000000-0000-0000-0000-${String(uuidSequence).padStart(12, "0")}`;
    },
    targetVersion: () => "26.09.0",
    out: (line) => output.push(line),
    now: () => Date.UTC(2026, 8, 10, 12, 0, 0),
  };
}

test("the injected driver executes and records the whole sequence in order", async () => {
  const stateObjects = new MemoryStateObjects();
  const steps = new FakeSteps();
  const output: string[] = [];

  await migrateCluster(makeDeps(stateObjects, steps, output), newRunOptions);

  assert.deepEqual(steps.checks, MIGRATION_STEPS.map((step) => step.id));
  assert.deepEqual(
    steps.actions,
    MIGRATION_STEPS.map((step) => step.id).filter((step) => step !== "PREFLIGHT_PASSED"),
  );
  assert.equal(stateObjects.record().status, "completed");
  assert.deepEqual(stateObjects.record().completedModules, newRunOptions.selectedModules);
  for (const step of MIGRATION_STEPS) {
    assert.ok(stateObjects.record().snapshots.some((snapshot) => snapshot.name === `migration:${step.id}:committed`));
  }
  assert.ok(output.some((line) => line.startsWith("ANNOUNCE [ADMISSION_CLOSED]")));
  assert.ok(output.some((line) => line.includes("enabledQueues=0; queued=0; provisioning=0; running=0")));
  assert.equal(output.at(-1), `COMPLETE migration ${deploymentId}`);
});

test("each step refuses when its stated precondition fails", async (suite) => {
  for (const failed of MIGRATION_STEPS.map((step) => step.id)) {
    await suite.test(failed, async () => {
      const stateObjects = new MemoryStateObjects();
      const steps = new FakeSteps({ failPrecondition: failed });

      await assert.rejects(
        migrateCluster(makeDeps(stateObjects, steps), {
          ...newRunOptions,
          deploymentId: `${deploymentId}-${failed.toLowerCase()}`,
        }),
        new MigrationRefusedError(`${failed} refused: ${failed} precondition failed`),
      );

      assert.equal(steps.checks.at(-1), failed);
      assert.ok(!steps.actions.includes(failed));
      const failedIndex = MIGRATION_STEPS.findIndex((step) => step.id === failed);
      assert.equal(steps.actions.length, Math.max(0, failedIndex - 1));
      if (failed === "PREFLIGHT_PASSED") {
        assert.equal(stateObjects.exists(), false);
      } else {
        assert.equal(stateObjects.record().status, "failed");
        assert.ok(!stateObjects.record().snapshots.some(
          (snapshot) => snapshot.name === `migration:${failed}:started`,
        ));
      }
    });
  }
});

test("a resumed run skips committed steps and reconciles the interrupted step", async () => {
  const stateObjects = new MemoryStateObjects();
  const firstSteps = new FakeSteps({ throwAt: "ECS_STAGED" });

  await assert.rejects(
    migrateCluster(makeDeps(stateObjects, firstSteps), newRunOptions),
    /simulated interruption at ECS_STAGED/,
  );
  const committedBeforeResume = stateObjects.record().snapshots
    .filter((snapshot) => snapshot.name.endsWith(":committed"))
    .map((snapshot) => snapshot.name);

  const resumedSteps = new FakeSteps();
  await migrateCluster(makeDeps(stateObjects, resumedSteps), {
    clusterName: newRunOptions.clusterName,
    awsRegion: newRunOptions.awsRegion,
    awsProfile: newRunOptions.awsProfile,
    stateBucket: newRunOptions.stateBucket,
    moduleSet: "ignored-on-resume",
    resume: deploymentId,
  });

  assert.deepEqual(resumedSteps.reconciliations, ["ECS_STAGED"]);
  assert.equal(resumedSteps.actions[0], "ECS_STAGED");
  assert.ok(!resumedSteps.actions.includes("ECS_CONFIGURATION_ACTIVE"));
  assert.ok(committedBeforeResume.length > 0);
  assert.equal(stateObjects.record().status, "completed");
});

test("scheduler closure refuses to proceed while any job remains", async () => {
  const stateObjects = new MemoryStateObjects();
  const steps = new FakeSteps({
    scheduler: {
      detail: "admission controls are closed but one running job remains",
      maintenanceEnabled: true,
      schedulingEnabled: false,
      enabledQueues: [],
      queuedJobs: 0,
      provisioningJobs: 0,
      runningJobs: 1,
    },
  });

  await assert.rejects(
    migrateCluster(makeDeps(stateObjects, steps), newRunOptions),
    /ADMISSION_CLOSED refused: the queue is not drained: 0 queued, 0 provisioning, 1 running/,
  );

  assert.deepEqual(steps.actions, ["OPERATION_STARTED", "ADMISSION_CLOSED"]);
  assert.ok(stateObjects.record().snapshots.some(
    (snapshot) => snapshot.name === "migration:ADMISSION_CLOSED:started",
  ));
  assert.ok(!stateObjects.record().snapshots.some(
    (snapshot) => snapshot.name === "migration:ADMISSION_CLOSED:committed",
  ));
  assert.ok(!steps.checks.includes("WORKLOAD_DRAINED"));
});

test("the command registration exposes migrate without executing it", () => {
  const program = new Command();
  const stateObjects = new MemoryStateObjects();
  registerMigrateCommands(program, makeDeps(stateObjects, new FakeSteps()));

  assert.ok(program.commands.some((command) => command.name() === "migrate"));
});

test("resume rechecks preflight before changing an existing journal", async () => {
  const stateObjects = new MemoryStateObjects();
  await assert.rejects(
    migrateCluster(makeDeps(stateObjects, new FakeSteps({ throwAt: "ECS_STAGED" })), newRunOptions),
    /simulated interruption/,
  );
  const before = await stateObjects.getObject();
  const steps = new FakeSteps({ failPrecondition: "PREFLIGHT_PASSED" });
  await assert.rejects(migrateCluster(makeDeps(stateObjects, steps), {
    clusterName: newRunOptions.clusterName,
    awsRegion: newRunOptions.awsRegion,
    stateBucket: newRunOptions.stateBucket,
    moduleSet: "default",
    resume: deploymentId,
  }), MigrationRefusedError);
  assert.deepEqual(await stateObjects.getObject(), before);
  assert.deepEqual(steps.actions, []);
  assert.deepEqual(steps.reconciliations, []);
});
