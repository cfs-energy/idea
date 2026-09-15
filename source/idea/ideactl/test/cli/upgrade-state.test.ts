/**
 * Durable upgrade-state tests. The object API below models conditional writes
 * in memory, so the suite performs no network calls.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  UPGRADE_BOUNDARIES,
  UpgradeStateConflictError,
  UpgradeStateError,
  UpgradeStateJournal,
  UpgradeStateNotFoundError,
  parseUpgradeOperationRecord,
  reportUpgradeState,
  type UpgradeBoundary,
  type UpgradePlan,
  type UpgradeStateObjectApi,
  type UpgradeStateWriteCondition,
  type VersionedUpgradeStateObject,
} from "../../src/config/upgrade-state.ts";

const location = { bucket: "sample-cluster-bucket" };
const startTime = Date.UTC(2026, 8, 10, 12, 0, 0);

const plan: UpgradePlan = {
  clusterName: "sample-cluster",
  awsRegion: "us-east-2",
  targetVersion: "26.09.0",
  targetBaseOs: "amazonlinux2023",
  moduleSet: "default",
  selectedModules: ["cluster", "scheduler"],
  deploymentId: "00000000-0000-0000-0000-000000000001",
};

/** Atomic in-memory implementation of the shared object contract. */
class MemoryObjectApi implements UpgradeStateObjectApi {
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

  clear(): void {
    this.object = undefined;
  }

  body(): string {
    if (this.object === undefined) throw new Error("The test object does not exist");
    return this.object.body;
  }
}

interface RunOptions {
  failAt?: UpgradeBoundary;
  events?: string[];
}

/**
 * Exercise every boundary through the same calls the upgrade command will
 * make. Completed callbacks are skipped automatically on resume.
 */
async function runUpgrade(journal: UpgradeStateJournal, options: RunOptions = {}): Promise<void> {
  for (const boundary of UPGRADE_BOUNDARIES) {
    await journal.runBoundary(boundary, async () => {
      options.events?.push(boundary);

      if (boundary === "protection-sweep") {
        await journal.recordProtectionBaseline([
          { stackName: "sample-cluster-scheduler", instanceId: "instance-protected", originallyProtected: true },
          { stackName: "sample-cluster-cluster", instanceId: "instance-unprotected", originallyProtected: false },
        ]);
        await journal.markProtectionCleared("instance-protected");
      }

      if (boundary === "module-deployments") {
        for (const moduleId of plan.selectedModules) {
          await journal.runModule(moduleId, async () => {
            options.events?.push(`module:${moduleId}`);
          });
        }
      }

      if (boundary === "finalization") {
        for (const instance of journal.protectionToRestore()) {
          await journal.markProtectionRestore(instance.instanceId, "restored");
        }
      }

      if (options.failAt === boundary) throw new Error(`simulated failure at ${boundary}`);
    });
  }
}

function session(holderId: string, now = startTime): {
  holderId: string;
  now: () => number;
  leaseMs: number;
} {
  return { holderId, now: () => now, leaseMs: 60_000 };
}

test("a clean run records every boundary, module, and protection restore", async () => {
  const api = new MemoryObjectApi();
  const journal = await UpgradeStateJournal.start(api, location, plan, session("run-clean"));

  await journal.recordSnapshot({
    name: "cluster-settings-before",
    source: "sample-cluster.cluster-settings",
    body: JSON.stringify([{ key: { S: "scheduler.base_os" }, value: { S: "amazonlinux2023" } }]),
  });
  await journal.recordPackageKey("idea/releases/sample-package.tar.gz");
  await runUpgrade(journal);
  await journal.complete();

  const report = journal.report();
  assert.equal(report.status, "completed");
  assert.deepEqual(report.remainingBoundaries, []);
  assert.deepEqual(report.remainingModules, []);
  assert.deepEqual(report.protectionToRestore, []);
  assert.equal(parseUpgradeOperationRecord(api.body()).snapshots.length, 1);
  assert.deepEqual(parseUpgradeOperationRecord(api.body()).packageKeys, ["idea/releases/sample-package.tar.gz"]);
});

test("a failure at every boundary is durable and reports the stopping point", async () => {
  for (const failedBoundary of UPGRADE_BOUNDARIES) {
    const api = new MemoryObjectApi();
    const journal = await UpgradeStateJournal.start(
      api,
      location,
      { ...plan, deploymentId: `${plan.deploymentId}-${failedBoundary}` },
      session(`run-${failedBoundary}`),
    );

    await assert.rejects(runUpgrade(journal, { failAt: failedBoundary }), new RegExp(`simulated failure at ${failedBoundary}`));

    const stored = parseUpgradeOperationRecord(api.body());
    const report = reportUpgradeState(stored);
    assert.equal(stored.status, "failed");
    assert.equal(report.stoppedAt, failedBoundary);
    assert.equal(stored.boundaries.find((entry) => entry.name === failedBoundary)?.status, "failed");
  }
});

test("resume skips completed boundaries and completed modules", async () => {
  const api = new MemoryObjectApi();
  const first = await UpgradeStateJournal.start(api, location, plan, session("run-first"));
  const firstEvents: string[] = [];

  await assert.rejects(
    runUpgrade(first, { failAt: "module-deployments", events: firstEvents }),
    /simulated failure at module-deployments/,
  );
  assert.deepEqual(first.record().completedModules, ["cluster", "scheduler"]);

  const resumed = await UpgradeStateJournal.resume(
    api,
    location,
    {
      clusterName: plan.clusterName,
      awsRegion: plan.awsRegion,
      deploymentId: plan.deploymentId,
    },
    session("run-resumed", startTime + 1_000),
  );
  const resumedEvents: string[] = [];
  await runUpgrade(resumed, { events: resumedEvents });
  await resumed.complete();

  assert.deepEqual(resumedEvents, ["module-deployments", "finalization"]);
  assert.ok(!resumedEvents.some((entry) => entry.startsWith("module:")));
});

test("one optimized priority group records concurrent module completions", async () => {
  const api = new MemoryObjectApi();
  const journal = await UpgradeStateJournal.start(api, location, plan, session("run-optimized"));

  for (const boundary of UPGRADE_BOUNDARIES.slice(0, 7)) {
    await journal.runBoundary(boundary, async () => {
      if (boundary === "protection-sweep") await journal.recordProtectionBaseline([]);
    });
  }
  await journal.runBoundary("module-deployments", async () => {
    await Promise.all(plan.selectedModules.map(async (moduleId) => {
      await journal.runModule(moduleId, async () => Promise.resolve());
    }));
  });

  assert.deepEqual(journal.record().completedModules.sort(), ["cluster", "scheduler"]);
});

test("a completed module is reopened when live validation disagrees", async () => {
  const api = new MemoryObjectApi();
  const journal = await UpgradeStateJournal.start(api, location, plan, session("run-reconcile"));

  for (const boundary of UPGRADE_BOUNDARIES.slice(0, 7)) {
    await journal.runBoundary(boundary, async () => {
      if (boundary === "protection-sweep") await journal.recordProtectionBaseline([]);
    });
  }
  await journal.runBoundary("module-deployments", async () => {
    await journal.runModule("cluster", async () => Promise.resolve());
    await journal.reopenModule("cluster", "the stack operation id did not match");
    await journal.runModule("cluster", async () => Promise.resolve());
    await journal.runModule("scheduler", async () => Promise.resolve());
  });

  assert.deepEqual(journal.record().completedModules, ["cluster", "scheduler"]);
  assert.match(journal.record().warnings[0] ?? "", /operation id did not match/);
});

test("a missing record can be rebuilt from verified evidence and resumed", async () => {
  const api = new MemoryObjectApi();
  const first = await UpgradeStateJournal.start(api, location, plan, session("run-before-loss"));
  await assert.rejects(runUpgrade(first, { failAt: "global-settings" }), /simulated failure at global-settings/);
  api.clear();

  await assert.rejects(
    UpgradeStateJournal.resume(
      api,
      location,
      {
        clusterName: plan.clusterName,
        awsRegion: plan.awsRegion,
        deploymentId: plan.deploymentId,
      },
      session("run-missing"),
    ),
    UpgradeStateNotFoundError,
  );

  const recovered = await UpgradeStateJournal.recover(
    api,
    location,
    plan,
    {
      completedBoundaries: ["preflight", "eol-software-stacks", "values-file"],
      snapshots: [{
        name: "cluster-settings-before",
        source: "sample-cluster.cluster-settings",
        body: "[]",
      }],
    },
    session("run-recovered", startTime + 2_000),
  );
  const events: string[] = [];
  await runUpgrade(recovered, { events });
  await recovered.complete();

  assert.equal(recovered.record().recovered, true);
  assert.deepEqual(events.slice(0, 2), ["global-settings", "full-configuration"]);
  assert.equal(recovered.report().status, "completed");
});

test("recovery after an unrecorded protection sweep fails closed", async () => {
  const api = new MemoryObjectApi();
  const recovered = await UpgradeStateJournal.recover(
    api,
    location,
    plan,
    {
      completedBoundaries: UPGRADE_BOUNDARIES.slice(0, 7),
    },
    session("run-unknown-protection"),
  );

  assert.equal(recovered.report().protectionBaseline, "unknown");
  assert.throws(() => recovered.protectionToRestore(), UpgradeStateError);
});

test("conditional creation and leases permit only one concurrent run", async () => {
  const api = new MemoryObjectApi();
  const results = await Promise.allSettled([
    UpgradeStateJournal.start(api, location, plan, session("run-one")),
    UpgradeStateJournal.start(api, location, plan, session("run-two")),
  ]);

  assert.equal(results.filter((result) => result.status === "fulfilled").length, 1);
  const rejected = results.find((result) => result.status === "rejected");
  assert.ok(rejected?.status === "rejected");
  assert.ok(rejected.reason instanceof UpgradeStateConflictError);

  await assert.rejects(
    UpgradeStateJournal.resume(
      api,
      location,
      {
        clusterName: plan.clusterName,
        awsRegion: plan.awsRegion,
        deploymentId: plan.deploymentId,
      },
      session("run-three", startTime + 30_000),
    ),
    UpgradeStateConflictError,
  );
});

test("an expired lease is taken by one resumer and fences the old holder", async () => {
  const api = new MemoryObjectApi();
  const old = await UpgradeStateJournal.start(api, location, plan, session("run-old"));
  const resumed = await UpgradeStateJournal.resume(
    api,
    location,
    {
      clusterName: plan.clusterName,
      awsRegion: plan.awsRegion,
      deploymentId: plan.deploymentId,
    },
    session("run-new", startTime + 60_001),
  );

  await resumed.heartbeat();
  await assert.rejects(old.heartbeat(), UpgradeStateConflictError);
});

test("evidence includes durable contents at three simulated failure boundaries", async () => {
  for (const failedBoundary of ["eol-software-stacks", "host-settings", "module-deployments"] as const) {
    const api = new MemoryObjectApi();
    const journal = await UpgradeStateJournal.start(
      api,
      location,
      { ...plan, deploymentId: `${plan.deploymentId}-${failedBoundary}` },
      session(`run-evidence-${failedBoundary}`),
    );
    await assert.rejects(runUpgrade(journal, { failAt: failedBoundary }), /simulated failure/);
    const stored = parseUpgradeOperationRecord(api.body());
    console.log(`RECORD_CONTENT ${JSON.stringify({
      deploymentId: stored.operation.deploymentId,
      status: stored.status,
      boundaries: stored.boundaries.map((entry) => ({ name: entry.name, status: entry.status })),
      completedModules: stored.completedModules,
      protection: stored.protection,
    })}`);
  }
});
