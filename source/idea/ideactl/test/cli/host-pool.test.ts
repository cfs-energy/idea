import assert from "node:assert/strict";
import { test } from "node:test";
import { refreshHostPool, waitForDirectoryJoin, refreshHostsCommand } from "../../src/cli/host-pool.ts";
import { returnBorrowedHosts, type UpgradeDeps } from "../../src/cli/commands/upgrade.ts";
import { fakeDeps } from "../support/deploy-harness.ts";
import { fakeContainerHosts } from "../support/host-pool-harness.ts";

const options = { clusterName: "fixture", awsRegion: "us-east-2", awsProfile: "fixture-profile", moduleSet: "default", refreshHosts: true };
function fixture() {
  const hosts = fakeContainerHosts();
  const settings = [{ key: "ecs.enabled", value: true }, { key: "directoryservice.provider", value: "openldap" }, { key: "ecs.cluster_name", value: "fixture" }, { key: "ecs.capacity_provider", value: "capacity" }];
  const deps = Object.assign(fakeDeps({ tables: { "fixture.cluster-settings": settings } }), { containerHosts: hosts.api });
  return { ...hosts, deps, settings };
}

test("refresh fallback removes only rejected maximum health preference and reports it", async (t) => {
  const { deps, api } = fixture();
  let calls = 0;
  t.mock.method(api, "startInstanceRefresh", async (input: { preferences: object }) => {
    calls += 1;
    if (calls === 1) throw Object.assign(new Error("MaxHealthyPercentage is not supported"), { name: "ValidationError" });
    assert.deepEqual(input.preferences, { MinHealthyPercentage: 100, InstanceWarmup: 300, ScaleInProtectedInstances: "Refresh" });
    return "refresh-id";
  });
  await refreshHostPool(deps, options);
  assert.equal(calls, 2);
  assert.match(deps.stdout.join("\n"), /rejected MaxHealthyPercentage.*MinHealthyPercentage: 100/);
});

test("refresh never retries unrelated API failures", async (t) => {
  const { deps, api } = fixture();
  const failure = new Error("Access denied");
  const start = t.mock.method(api, "startInstanceRefresh", async () => { throw failure; });
  await assert.rejects(refreshHostPool(deps, options), (error) => error === failure);
  assert.equal(start.mock.callCount(), 1);
});

for (const old of [false, true]) test(`joined hosts do not need replacement without opt-in, old=${old}`, async () => {
  const { deps, state, calls } = fixture();
  state.old = old;
  state.joined = true;
  await refreshHostPool(deps, { ...options, refreshHosts: false });
  assert.equal(calls.some((call) => call.startsWith("refresh:")), false);
});

test("refresh opt-in leaves matching templates alone", async () => {
  const { deps, state, calls } = fixture();
  state.old = false;
  await refreshHostPool(deps, options);
  assert.equal(calls.some((call) => call.startsWith("refresh:")), false);
});

test("fresh hosts must register before an empty inventory can pass", async (t) => {
  const { deps, api, state } = fixture();
  state.old = false;
  t.mock.method(api, "containerInstances", async () => []);
  await assert.rejects(waitForDirectoryJoin(deps, options), /45 minutes.*i-host.*idea-directory-join.service/);
  assert.equal(deps.sleeps.length, 90);
  assert.equal(deps.stdout.filter((line) => line.startsWith("Waiting")).length, 23);
});

test("borrowed host return reads the replacement inventory after refresh", async (t) => {
  const { deps, api, settings } = fixture();
  await refreshHostPool(deps, options);
  await waitForDirectoryJoin(deps, options);
  t.mock.method(api, "hostGroup", async () => ({ name: "hosts", minSize: 1, desiredCapacity: 2, instanceIds: ["i-host", "i-replacement"] }));
  t.mock.method(api, "containerInstances", async () => [
    { arn: "arn:replacement", instanceId: "i-replacement", status: "ACTIVE", runningTasks: 1, registeredAt: "2026-09-20" },
    { arn: "arn:host", instanceId: "i-host", status: "ACTIVE", runningTasks: 0, registeredAt: "2026-09-01" },
  ]);
  const released = t.mock.method(api, "release", async () => {});
  await returnBorrowedHosts(deps as unknown as UpgradeDeps, options, settings);
  assert.deepEqual(released.mock.calls[0].arguments, [{ awsRegion: options.awsRegion, awsProfile: options.awsProfile, name: "hosts", instanceId: "i-replacement", desiredCapacity: 1 }]);
});

test("rerun preserves module selection and options and forces a deployed ECS stack to run again", () => {
  const command = refreshHostsCommand("deploy", { ...options, refreshHosts: false, rollback: false, allowReplacement: ["Resource"] }, ["ecs", "bastion-host"]);
  assert.match(command, /^ideactl deploy 'ecs' 'bastion-host'/);
  assert.match(command, /--aws-profile 'fixture-profile'/);
  assert.match(command, /--no-rollback --allow-replacement 'Resource' --upgrade --refresh-hosts$/);
});

test("a host still launching cannot be omitted from the join gate", async (t) => {
  const { deps, api, state } = fixture();
  state.old = false;
  state.joined = true;
  t.mock.method(api, "launchTemplateVersions", async () => ({ current: { id: "lt-host", version: "2" }, instances: [
    { instanceId: "i-host", id: "lt-host", version: "2" }, { instanceId: "i-launching", id: "lt-host", version: "2" },
  ] }));
  await assert.rejects(waitForDirectoryJoin(deps, { ...options, refreshHosts: false }), /45 minutes.*i-launching.*idea-directory-join.service/);
});

test("rerun retains boolean termination protection as a valued option", () => {
  assert.match(refreshHostsCommand("upgrade-cluster", { terminationProtection: true }), /--termination-protection 'true' --refresh-hosts$/);
});

test("a host-era cluster with an EC2 bastion has no host pool to gate", async () => {
  const { deps, calls, settings } = fixture();
  settings.splice(0, 1);
  await refreshHostPool(deps, options);
  await waitForDirectoryJoin(deps, options);
  assert.deepEqual(calls, []);
});
