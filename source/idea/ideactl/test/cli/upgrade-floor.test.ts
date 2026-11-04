import assert from "node:assert/strict";
import { test } from "node:test";

import { upgradeCluster } from "../../src/cli/commands/upgrade.ts";
import {
  UPGRADE_FLOOR_VERSION,
  compareIdeaRelease,
  modulesBelowFloor,
  upgradeFloorMessage,
  type FloorModule,
} from "../../src/cli/upgrade-floor.ts";

test("calendar and legacy three-part releases compare numerically", () => {
  const ordered = ["3.1.10", "25.06.1", "25.10.0", "26.08.0", "26.09.0"];
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const older = ordered[index] as string;
    const newer = ordered[index + 1] as string;
    const compared = compareIdeaRelease(older, newer);
    assert.notEqual(compared, undefined);
    assert.ok((compared ?? 0) < 0, `${older} must rank below ${newer}`);
  }
  assert.equal(compareIdeaRelease("26.08.0", "26.08.0"), 0);
  assert.ok((compareIdeaRelease("26.09.0", "26.08.0") ?? 0) > 0);
  assert.equal(compareIdeaRelease("26.09.0-dev", UPGRADE_FLOOR_VERSION), undefined);
});

test("the floor ignores config rows and undeployed rows", () => {
  const modules: FloorModule[] = [
    { module_id: "global-settings", type: "config", status: "deployed", version: null },
    { module_id: "cluster", type: "stack", status: "deployed", version: "26.10.9" },
    { module_id: "metrics", type: "stack", status: "not-deployed", version: null },
  ];
  assert.deepEqual(modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION), []);
});

test("deployed modules older than the floor, missing, or unparsable fail it", () => {
  const modules: FloorModule[] = [
    { module_id: "cluster", type: "stack", status: "deployed", version: "25.10.0" },
    { module_id: "scheduler", type: "app", status: "deployed", version: "26.10.9" },
    { module_id: "vdc", type: "app", status: "deployed", version: null },
    { module_id: "analytics", type: "stack", status: "deployed", version: "not-a-version" },
  ];
  assert.deepEqual(
    modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION).map((module) => module.module_id),
    ["cluster", "vdc", "analytics"],
  );
});

test("the refusal names the floor, the below-floor modules, and the operator path", () => {
  const below: FloorModule[] = [
    { module_id: "cluster", type: "stack", status: "deployed", version: "25.10.0" },
    { module_id: "scheduler", type: "app", status: "deployed", version: "25.10.0" },
  ];
  const message = upgradeFloorMessage("sample-cluster", "26.09.1", UPGRADE_FLOOR_VERSION, below);
  assert.match(message, /Cluster sample-cluster is below the supported upgrade floor/);
  assert.match(message, /already at 26\.10\.0 or newer/);
  assert.match(message, /cluster {3}25\.10\.0/);
  assert.match(message, /scheduler {3}25\.10\.0/);
  assert.match(message, /Upgrade this cluster to 26\.10\.9 using the 26\.10\.9 administrator/);
  assert.match(message, /IDEA_REVISION=v26\.10\.9 \.\/idea-admin\.sh upgrade-cluster --cluster-name <name> --aws-region <region>/);
  assert.match(message, /Clusters on 25\.11\.0 or newer upgrade to 26\.10\.9 directly/);
  assert.match(message, /Only a cluster older than 25\.11\.0 first upgrades one published release at a time/);
  assert.doesNotMatch(message, /--force/);
});

test("upgrade-cluster refuses a below-floor cluster before touching anything else", async () => {
  // Only the modules-table scan is provided. Anything the command reached past the floor check
  // would fail on a missing dependency with a different message, which is what makes this
  // assertion about the check's position rather than about the message alone.
  const scanned: string[] = [];
  const deps = {
    scan: async (input: { TableName: string }) => {
      scanned.push(input.TableName);
      return {
        Items: [
          { module_id: "cluster", name: "cluster", type: "stack", status: "deployed", version: "25.10.0" },
          { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed", version: "26.08.0" },
        ],
      };
    },
  } as unknown as Parameters<typeof upgradeCluster>[0];
  const options = { clusterName: "sample-cluster", awsRegion: "us-east-2" } as Parameters<typeof upgradeCluster>[1];
  await assert.rejects(upgradeCluster(deps, options), /below the supported upgrade floor[\s\S]*cluster {3}25\.10\.0/);
  assert.deepEqual(scanned, ["sample-cluster.modules"]);
});

test("26.10 and later deployed releases clear the direct upgrade floor", () => {
  assert.equal(UPGRADE_FLOOR_VERSION, "26.10.0");
  for (const version of ["26.10.0", "26.10.9", "26.11.1"]) {
    assert.deepEqual(modulesBelowFloor([{ module_id: "scheduler", type: "app", status: "deployed", version }], UPGRADE_FLOOR_VERSION), []);
  }
});

for (const settings of [[], [{ key: 'ecs.enabled', value: false }], [{ key: 'ecs.enabled', value: true }, { key: 'ecs.retain_existing_hosts', value: true }]]) {
  test(`refuses an unsupported host mode before writes: ${JSON.stringify(settings)}`, async () => {
    const deps = {
      scan: async ({ TableName }: { TableName: string }) => ({
        Items: TableName.endsWith('.modules')
          ? [{ module_id: 'cluster', name: 'cluster', type: 'stack', status: 'deployed', version: '26.10.0' }]
          : settings
      }),
    } as unknown as Parameters<typeof upgradeCluster>[0];
    await assert.rejects(upgradeCluster(deps, { clusterName: 'sample-cluster', awsRegion: 'us-east-2', moduleSet: 'default' }), /Complete the ECS migration with the 26.10.9 administrator/);
  });
}

for (const marker of [
  { key: 'cluster.upgrade_historical_pending', value: true },
  { key: 'cluster-manager.maintenance.upgrade_baseline', value: { enabled: false, message: '' } },
  { key: 'custom-manager.maintenance.upgrade_baseline', value: { enabled: true } },
]) {
  test(`A-3: refuses unfinished migration marker ${marker.key} before any other effects`, async () => {
    const deps = {
      scan: async ({ TableName }: { TableName: string }) => ({ Items: TableName.endsWith('.modules')
        ? [{ module_id: 'cluster', name: 'cluster', type: 'stack', status: 'deployed', version: '26.10.9' }]
        : [{ key: 'ecs.enabled', value: true }, marker]
      }),
    } as unknown as Parameters<typeof upgradeCluster>[0];
    await assert.rejects(upgradeCluster(deps, { clusterName: 'sample-cluster', awsRegion: 'us-east-2', moduleSet: 'default' }), (error: Error) => {
      assert.match(error.message, /Incomplete migration.*26\.10\.9 administrator/);
      assert.ok(error.message.includes(marker.key));
      assert.match(error.message, /IDEA_REVISION=v26\.10\.9/);
      return true;
    });
  });
}
