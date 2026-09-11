/**
 * A control-plane module that runs as container tasks publishes no host packages.
 *
 * The per-module release archives a host downloads are no longer produced, so a deploy that still
 * demanded one would stop every new cluster at its first control-plane module. The skip is
 * conditional on the same setting pair the module stacks build their host resources from, so a run
 * that deliberately retains hosts still publishes what those hosts read: that direction is
 * asserted here too, because a skip that always fires would pass the first check on its own.
 */

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

import { CdkInvoker } from "../../src/cli/cdk-invoker.ts";
import { fakeDeps, moduleRow, withTempIdeaHome } from "../w20a/harness.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

const home = withTempIdeaHome();
const previousCdkBin = process.env.IDEA_CDK_BIN;

before(() => {
  process.env.IDEA_CDK_BIN = "/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk";
});
after(() => {
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  home.restore();
});

function tables(settings: ReadonlyArray<{ key: string; value: unknown }>): Record<string, unknown[]> {
  return {
    [`${CLUSTER}.modules`]: [
      moduleRow("cluster", "cluster", "stack", "deployed"),
      moduleRow("scheduler", "scheduler", "app", "not-deployed"),
      moduleRow("ecs", "ecs", "stack", "deployed"),
    ],
    [`${CLUSTER}.cluster-settings`]: [
      { key: "cluster.cluster_s3_bucket", value: `${CLUSTER}-cluster-${REGION}-123456789012` },
      { key: "global-settings.module_sets.default.cluster.module_id", value: "cluster" },
      ...settings,
    ],
  };
}

function open(settings: ReadonlyArray<{ key: string; value: unknown }>): Promise<{
  invoker: CdkInvoker;
  deps: ReturnType<typeof fakeDeps>;
}> {
  const deps = fakeDeps({ tables: tables(settings) as never });
  return CdkInvoker.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleId: "scheduler",
    moduleSet: "default",
    deps,
  }).then((invoker) => ({ invoker, deps }));
}

describe("a module that runs as container tasks", () => {
  it("deploys without a release archive on disk", async () => {
    const { invoker, deps } = await open([{ key: "ecs.enabled", value: true }]);
    await invoker.invoke();
    assert.deepEqual([...deps.puts.keys()], []);
    assert.equal(deps.spawns.length, 1);
  });

  it("still publishes them when the run keeps the hosts", async () => {
    const { invoker, deps } = await open([
      { key: "ecs.enabled", value: true },
      { key: "ecs.retain_existing_hosts", value: true },
    ]);
    await assert.rejects(() => invoker.invoke(), /package not found: .*idea-scheduler-/);
    assert.deepEqual(deps.spawns, []);
  });

  it("still publishes them when the cluster has no container control plane", async () => {
    const { invoker, deps } = await open([]);
    await assert.rejects(() => invoker.invoke(), /package not found: .*idea-scheduler-/);
    assert.deepEqual(deps.spawns, []);
  });
});
