import assert from "node:assert/strict";
import { test } from "node:test";
import { CdkInvoker } from "../../src/cli/cdk-invoker.ts";
import { runDeploy } from "../../src/cli/commands/deploy.ts";
import { withDefaultUpgradeDeployment, type UpgradeDeps } from "../../src/cli/commands/upgrade.ts";
import { fakeDeps, moduleRow, withTempIdeaHome } from "./deploy-harness.ts";

export function fakeContainerHosts() {
  const calls: string[] = [];
  const state = { old: true, joined: false, reads: 0, joinAfter: 2, status: "Successful" };
  const api = {
    async hostGroup() { calls.push("group"); return { name: "hosts", minSize: 1, desiredCapacity: 1, instanceIds: ["i-host"] }; },
    async launchTemplateVersions() { calls.push("versions"); return { current: { id: "lt-host", version: "2" }, instances: [{ instanceId: "i-host", id: "lt-host", version: state.old ? "1" : "2" }] }; },
    async containerInstances() {
      calls.push("attributes");
      state.reads += 1;
      return [{ arn: "arn:host", instanceId: "i-host", status: "ACTIVE", runningTasks: 0,
        attributes: state.joined || state.reads >= state.joinAfter ? { "idea.directory": "joined" } : {} as Record<string, string> }];
    },
    async startInstanceRefresh(input: unknown) { calls.push(`refresh:${JSON.stringify(input)}`); return "refresh-id"; },
    async describeInstanceRefresh() { calls.push("progress"); if (state.status === "Successful") state.old = false; return { status: state.status, percentageComplete: 100 }; },
    async drain() {}, async activate() {}, async waitUntilEmpty() { return true; }, async release() {},
  };
  return { api, calls, state };
}

export function hostPoolDeploymentTests(command: "deploy" | "upgrade-cluster") {
  for (const optimized of [false, true]) {
    for (const scenario of ["refuse", "refresh", "current", "timeout", "no-directory", "failed", "cancelled", "refresh-timeout"] as const) {
      test(`${command}: host gate ${scenario}, optimized=${optimized}`, async (t) => {
        const home = withTempIdeaHome();
        t.after(home.restore);
        const hosts = fakeContainerHosts();
        hosts.state.joinAfter = 4; // the refresh check and the join gate each read the inventory once before polling
        hosts.state.old = !["current", "timeout"].includes(scenario);
        if (scenario === "timeout") hosts.state.joinAfter = Infinity;
        if (scenario === "failed") hosts.state.status = "Failed";
        if (scenario === "cancelled") hosts.state.status = "Cancelled";
        if (scenario === "refresh-timeout") hosts.state.status = "InProgress";
        const settings: Array<{ key: string; value: unknown }> = [{ key: "ecs.enabled", value: true }];
        const options = { clusterName: "fixture", awsRegion: "us-east-2", awsProfile: "fixture-profile", moduleSet: "default",
          upgrade: true, allModules: true, terminationProtection: true, forceBuildBootstrap: false, rollback: true,
          optimizeDeployment: optimized, refreshHosts: ["refresh", "failed", "cancelled", "refresh-timeout"].includes(scenario) };
        const tables = { "fixture.modules": [moduleRow("ecs", "ecs", "stack", "deployed"), moduleRow("bastion-host", "bastion-host", "stack", "deployed")], "fixture.cluster-settings": settings };
        const deps = Object.assign(fakeDeps({ tables }), { containerHosts: hosts.api, ecsAccountSettings: { async listAccountSettings() { return [{ name: "awsvpcTrunking", value: "enabled" }]; } } });
        const deployed: string[] = [];
        t.mock.method(CdkInvoker, "open", async (input: { moduleId: string }) => ({ invoke: async () => {
          deployed.push(input.moduleId);
          if (input.moduleId === "ecs") {
            settings.push(...[
              { key: "ecs.cluster_name", value: "fixture" }, { key: "ecs.capacity_provider", value: "capacity" },
              ...(scenario === "no-directory" ? [] : [{ key: "directoryservice.provider", value: "openldap" }]),
            ]);
          } else if (scenario !== "no-directory") assert.ok(hosts.calls.includes("attributes"));
        } }));
        const run = () => command === "deploy" ? runDeploy(deps, ["all"], { ...options, terminationProtection: "true" })
          : withDefaultUpgradeDeployment(deps as unknown as Omit<UpgradeDeps, "deploy">).deploy(options);
        if (scenario === "refuse") await assert.rejects(run, /i-host.*cannot join.*old launch template[\s\S]*--refresh-hosts[\s\S]*themselves/);
        else if (scenario === "timeout") await assert.rejects(run, /45 minutes.*i-host.*idea-directory-join.service/);
        else if (["failed", "cancelled"].includes(scenario)) await assert.rejects(run, new RegExp(hosts.state.status));
        else if (scenario === "refresh-timeout") await assert.rejects(run, /90 minutes/);
        else await run();
        assert.deepEqual(deployed, ["ecs", ...(["refresh", "current", "no-directory"].includes(scenario) ? ["bastion-host"] : [])]);
        const refreshes = hosts.calls.filter((call) => call.startsWith("refresh:"));
        assert.equal(refreshes.length, options.refreshHosts ? 1 : 0);
        if (options.refreshHosts) {
          const input = JSON.parse(refreshes[0].slice(8));
          assert.deepEqual(input.preferences, { MinHealthyPercentage: 100, MaxHealthyPercentage: 200, InstanceWarmup: 300, ScaleInProtectedInstances: "Refresh" });
          assert.equal(input.awsProfile, options.awsProfile);
        }
        if (scenario === "no-directory") assert.deepEqual(hosts.calls, []);
        if (["refresh", "current"].includes(scenario)) assert.ok(deps.sleeps.includes(30_000));
        if (scenario === "refresh-timeout") assert.equal(deps.sleeps.filter((ms) => ms === 60_000).length, 90);
      });
    }
  }
}
