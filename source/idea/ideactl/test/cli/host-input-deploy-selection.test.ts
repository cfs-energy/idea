/**
 * The routed intermediate is only real if the stacks that read its input actually get deployed.
 *
 * A module already recorded as deployed is skipped by the deploy path, so a run that changes a
 * setting a deployed stack branches on can change nothing and still report success. That was
 * observed on a fresh install: a module added to the module set after the initial install left the
 * cluster stack un-resynthesized, and the listeners it creates only for that module did not exist.
 *
 * Both container inputs branch inside stacks that are already deployed on every existing cluster, so
 * the same mechanism would make the routed intermediate unreachable. These tests pin the selection
 * rather than the intent.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { DeploymentHelper, deploymentOrder } from "../../src/cli/deployment-helper.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";
import type { ModuleInfo } from "../../src/config/cluster-config.ts";
import { createLiveUpgradeDeps, type UpgradeDeploymentOptions } from "../../src/cli/commands/upgrade.ts";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STACKS = join(PKG, "src", "cdk", "stacks");
const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

/** The two settings that change what a host-carrying stack emits. */
const CONTAINER_INPUTS = ["ecs.enabled", "ecs.retain_existing_hosts"] as const;

/** Stack file -> module name, for the files that branch on either input. */
const MODULE_NAME_BY_STACK_FILE: Record<string, string> = {
  "bastion-host.ts": "bastion-host",
  "cluster.ts": "cluster",
  "cluster-manager.ts": "cluster-manager",
  "scheduler.ts": "scheduler",
  "vdc.ts": "virtual-desktop-controller",
};

/** Every stack file whose source reads one of the container inputs. */
function readerStackFiles(): string[] {
  return readdirSync(STACKS)
    .filter((file) => file.endsWith(".ts"))
    .filter((file) => {
      const source = readFileSync(join(STACKS, file), "utf8");
      return CONTAINER_INPUTS.some((key) => source.includes(`"${key}"`) || source.includes(`'${key}'`));
    })
    .sort();
}

/** Module rows in the shape the table holds them, every one already deployed. */
function deployedModuleRows(): Array<Record<string, unknown>> {
  const names = [
    ["cluster", "cluster"],
    ["metrics", "metrics"],
    ["directoryservice", "directoryservice"],
    ["identity-provider", "identity-provider"],
    ["analytics", "analytics"],
    ["shared-storage", "shared-storage"],
    ["ecs", "ecs"],
    ["cluster-manager", "cluster-manager"],
    ["vdc", "virtual-desktop-controller"],
    ["scheduler", "scheduler"],
    ["bastion-host", "bastion-host"],
  ] as const;
  return names.map(([moduleId, name]) => ({
    module_id: moduleId,
    name,
    type: "app",
    status: "deployed",
    stack_name: `${CLUSTER}-${moduleId}`,
    version: "26.09.0",
  }));
}

function moduleInfos(): ModuleInfo[] {
  return deployedModuleRows() as unknown as ModuleInfo[];
}

/** Module ids of the stacks that read either input, from the discovered reader files. */
function readerModuleIds(): string[] {
  const rows = deployedModuleRows();
  return readerStackFiles()
    .map((file) => MODULE_NAME_BY_STACK_FILE[file])
    .map((name) => rows.find((row) => row.name === name)?.module_id)
    .filter((moduleId): moduleId is string => moduleId !== undefined)
    .sort();
}

test("every stack that reads a container input is a known one", () => {
  // A new reader that this file does not know about would not be covered by the selection tests
  // below, so discovering one takes this red rather than passing silently.
  const files = readerStackFiles();
  assert.deepEqual(files, ["bastion-host.ts", "cluster-manager.ts", "scheduler.ts", "vdc.ts"]);
  for (const file of files) {
    assert.ok(MODULE_NAME_BY_STACK_FILE[file] !== undefined, `${file} has no module name mapping`);
  }
});

test("an all-module upgrade selects every reader even though all of them are deployed", () => {
  const rows = moduleInfos();
  const allIds = rows.map((module) => module.module_id);
  const selected = deploymentOrder(rows, allIds, true);
  for (const moduleId of readerModuleIds()) {
    assert.ok(selected.includes(moduleId), `${moduleId} was not selected by an upgrade run`);
  }
  // The container module carries the target groups the readers point at, so it has to be there too.
  assert.ok(selected.includes("ecs"), "ecs was not selected by an upgrade run");
});

test("the deploy path skips every reader, which is why the cutover is an upgrade", () => {
  // Not an endorsement: this is the mechanism that made a fresh install refuse. It is pinned so the
  // reason the migration runs through the upgrade path stays visible, and so a change that makes
  // the deploy path safe shows up here. A refusal on this path would be better than a silent no-op.
  const rows = moduleInfos();
  const allIds = rows.map((module) => module.module_id);
  const selected = deploymentOrder(rows, allIds, false);
  assert.deepEqual(selected, [], "the deploy path selected a module recorded as deployed");
});

test("the upgrade command's deployment passes the flag that ignores recorded state", async () => {
  // The selection above only holds when the caller asks for an upgrade. This reads the value the
  // upgrade command actually passes, by capturing what it hands the helper.
  const captured: Array<Record<string, unknown>> = [];
  const open = DeploymentHelper.open;
  const base = { async spawn() { return 0; } } as unknown as Deps;
  const deployment: UpgradeDeploymentOptions = {
    clusterName: CLUSTER,
    awsRegion: REGION,
    terminationProtection: true,
    forceBuildBootstrap: false,
    rollback: true,
    optimizeDeployment: false,
    moduleSet: "default",
    allModules: true,
  };
  try {
    DeploymentHelper.open = async (options) => {
      captured.push(options as unknown as Record<string, unknown>);
      return { async invoke() {} } as unknown as DeploymentHelper;
    };
    await createLiveUpgradeDeps(base).deploy(deployment);
  } finally {
    DeploymentHelper.open = open;
  }

  assert.equal(captured.length, 1);
  assert.equal(captured[0]?.upgrade, true, "the upgrade command did not ask for an upgrade");
  assert.equal(captured[0]?.allModules, true);
  assert.equal(captured[0]?.moduleIds, undefined, "an all-module upgrade must not narrow the module ids");
});

test("the helper the upgrade command opens selects every reader from the cluster tables", async () => {
  // End to end through the real helper and the real table reader, with the scan replayed: the
  // selection a live all-module upgrade would make against a cluster whose every module is deployed.
  const settings = [
    { key: "cluster.aws.region", value: REGION },
    { key: "global-settings.module_sets.default.cluster.module_id", value: "cluster" },
    { key: "global-settings.module_sets.default.ecs.module_id", value: "ecs" },
  ];
  const rows: Record<string, Array<Record<string, unknown>>> = {
    [`${CLUSTER}.cluster-settings`]: settings,
    [`${CLUSTER}.modules`]: deployedModuleRows(),
  };
  const helper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    upgrade: true,
    allModules: true,
    deploymentId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    deps: {
      async scan(input: { TableName: string }) {
        return { Items: rows[input.TableName] ?? [] };
      },
      uuid() {
        return "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
      },
    } as unknown as Deps,
  });

  const order = helper.getDeploymentOrder();
  for (const moduleId of [...readerModuleIds(), "ecs"]) {
    assert.ok(order.includes(moduleId), `${moduleId} was not in the live upgrade order`);
  }
  // The container module publishes the target groups the readers consume, so it goes first.
  for (const moduleId of readerModuleIds()) {
    assert.ok(order.indexOf("ecs") < order.indexOf(moduleId), `ecs did not precede ${moduleId}`);
  }
});
