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
  const ordered = ["3.1.10", "25.06.1", "25.12.0", "26.08.0", "26.09.0"];
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
    { module_id: "cluster", type: "stack", status: "deployed", version: "26.09.0" },
    { module_id: "metrics", type: "stack", status: "not-deployed", version: null },
  ];
  assert.deepEqual(modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION), []);
});

test("deployed modules older than the floor, missing, or unparsable fail it", () => {
  const modules: FloorModule[] = [
    { module_id: "cluster", type: "stack", status: "deployed", version: "25.12.0" },
    { module_id: "scheduler", type: "app", status: "deployed", version: "26.09.0" },
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
    { module_id: "cluster", type: "stack", status: "deployed", version: "25.12.0" },
    { module_id: "scheduler", type: "app", status: "deployed", version: "25.12.0" },
  ];
  const message = upgradeFloorMessage("sample-cluster", "26.09.1", UPGRADE_FLOOR_VERSION, below);
  assert.match(message, /Cluster sample-cluster is below the supported upgrade floor/);
  assert.match(message, /already at 26\.09\.0 or newer/);
  assert.match(message, /cluster {3}25\.12\.0/);
  assert.match(message, /scheduler {3}25\.12\.0/);
  assert.match(message, /Upgrade this cluster to 26\.09\.0 using the 26\.09\.0 administrator/);
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
          { module_id: "cluster", name: "cluster", type: "stack", status: "deployed", version: "25.12.0" },
          { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed", version: "26.08.0" },
        ],
      };
    },
  } as unknown as Parameters<typeof upgradeCluster>[0];
  const options = { clusterName: "sample-cluster", awsRegion: "us-east-2" } as Parameters<typeof upgradeCluster>[1];
  await assert.rejects(upgradeCluster(deps, options), /below the supported upgrade floor[\s\S]*cluster {3}25\.12\.0/);
  assert.deepEqual(scanned, ["sample-cluster.modules"]);
});
