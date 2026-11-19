import assert from "node:assert/strict";
import { test } from "node:test";
import { returnBorrowedHosts, type UpgradeDeps } from "../../src/cli/commands/upgrade.ts";
import { refreshHostPool, refreshHostsCommand, waitForDirectoryJoin } from "../../src/cli/host-pool.ts";
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
  const released = t.mock.method(api, "release", async () => { });
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
  t.mock.method(api, "launchTemplateVersions", async () => ({
    current: { id: "lt-host", version: "2" }, instances: [
      { instanceId: "i-host", id: "lt-host", version: "2" }, { instanceId: "i-launching", id: "lt-host", version: "2" },
    ]
  }));
  await assert.rejects(waitForDirectoryJoin(deps, { ...options, refreshHosts: false }), /45 minutes.*i-launching.*idea-directory-join.service/);
});

test("rerun retains boolean termination protection as a valued option", () => {
  assert.match(refreshHostsCommand("upgrade-cluster", { terminationProtection: true }), /--termination-protection 'true' --refresh-hosts$/);
});

test("an application deploy refuses a host built before the current shared storage mounts", async (t) => {
  const { refuseHostsWithStaleMounts } = await import("../../src/cli/host-pool.ts");
  const { hostStorageDigest } = await import("../../src/cdk/stacks/ecs.ts");
  const { ClusterConfig } = await import("../../src/config/cluster-config.ts");
  const share = [
    { key: "shared-storage.projects.provider", value: "fsx_netapp_ontap" },
    { key: "shared-storage.projects.mount_dir", value: "/projects" },
    { key: "shared-storage.projects.scope", value: ["cluster"] },
    { key: "shared-storage.projects.fsx_netapp_ontap.svm.nfs_dns", value: "svm.example.invalid" },
    { key: "shared-storage.projects.fsx_netapp_ontap.volume.volume_path", value: "/projects" },
  ];
  const before = hostStorageDigest(new ClusterConfig([]));
  const after = hostStorageDigest(new ClusterConfig(share));
  assert.notEqual(before, after);
  const { hostStorageEntries } = await import("../../src/cdk/stacks/ecs.ts");
  const line = hostStorageEntries(new ClusterConfig(share)).entries[0]!.line;
  // [recorded digest, the fstab lines of the host's launch template version, refused]
  const cases: Array<[string | undefined, string[] | undefined, boolean, string]> = [
    [before, [], true, "a record from before the share"],
    [after, [], false, "a record of the current shares"],
    // a host built before 26.11.2 carries no record: its launch template version decides
    [undefined, [line.replace(",nodev", "")], false, "no record, same shares (options aside): proceeds as before"],
    [undefined, [], true, "no record, the share was added since the host was built"],
    [undefined, undefined, true, "no record, launch template unreadable"],
  ];
  for (const [recorded, lines, refused, label] of cases) {
    for (const refreshHosts of [false, true]) {
      const { deps, api, settings, state } = fixture();
      (settings as Array<{ key: string; value: unknown }>).push(...share);
      state.mountLines = lines;
      t.mock.method(api, "containerInstances", async () => [
        { instanceId: "i-host", arn: "arn:host", status: "ACTIVE", attributes: recorded === undefined ? {} : { "idea.storage": recorded } },
        // a draining host with an old mount set takes no task, so it does not stop the deploy
        { instanceId: "i-draining", arn: "arn:draining", status: "DRAINING", attributes: { "idea.storage": before } },
      ]);
      // --refresh-hosts no longer skips the check: it runs on the hosts running after the refresh step
      const run = refuseHostsWithStaleMounts(deps, { ...options, refreshHosts, hostPoolRerunCommand: undefined });
      if (refused) await assert.rejects(run, (error: Error) => /i-host were built before/.test(error.message) && !error.message.includes("i-draining"), `${label}, refreshHosts=${refreshHosts}`);
      else await run;
    }
  }
});

test("a container instance record that can take no task is named, not a stop; a connected host without a record still is", async (t) => {
  const { refuseHostsWithStaleMounts } = await import("../../src/cli/host-pool.ts");
  const { hostStorageDigest } = await import("../../src/cdk/stacks/ecs.ts");
  const { ClusterConfig } = await import("../../src/config/cluster-config.ts");
  const share = [
    { key: "shared-storage.projects.provider", value: "fsx_netapp_ontap" },
    { key: "shared-storage.projects.mount_dir", value: "/projects" },
    { key: "shared-storage.projects.scope", value: ["cluster"] },
    { key: "shared-storage.projects.fsx_netapp_ontap.svm.nfs_dns", value: "svm.example.invalid" },
    { key: "shared-storage.projects.fsx_netapp_ontap.volume.volume_path", value: "/projects" },
  ];
  const current = hostStorageDigest(new ClusterConfig(share));
  {
    // the connected host is current; a disconnected record and a record the group no longer has carry none
    const { deps, api, settings, state } = fixture();
    (settings as Array<{ key: string; value: unknown }>).push(...share);
    state.mountLines = [];
    t.mock.method(api, "containerInstances", async () => [
      { instanceId: "i-host", arn: "arn:host", status: "ACTIVE", agentConnected: true, attributes: { "idea.storage": current } },
      { instanceId: "i-gone", arn: "arn:gone", status: "ACTIVE", agentConnected: false, attributes: {} },
      { instanceId: "i-replaced", arn: "arn:replaced", status: "ACTIVE", agentConnected: true, attributes: {} },
    ]);
    await refuseHostsWithStaleMounts(deps, { ...options, refreshHosts: false });
    const out = deps.stdout.join("\n");
    for (const arn of ["arn:gone", "arn:replaced"]) {
      assert.match(out, new RegExp(`note: container instance i-\\w+ is registered but can take no task[^\\n]*deregister-container-instance --cluster fixture --container-instance ${arn} --force`));
    }
  }
  {
    // a connected host of the group with no record, built before the share was added
    const { deps, api, settings, state } = fixture();
    (settings as Array<{ key: string; value: unknown }>).push(...share);
    state.mountLines = [];
    t.mock.method(api, "containerInstances", async () => [
      { instanceId: "i-host", arn: "arn:host", status: "ACTIVE", agentConnected: true, attributes: {} },
    ]);
    await assert.rejects(refuseHostsWithStaleMounts(deps, { ...options, refreshHosts: false }), /i-host were built before/);
  }
});

test("the stale-mount refusal's re-run command replaces hosts: a scoped deploy gains the ecs module", () => {
  assert.match(refreshHostsCommand("deploy", { ...options, refreshHosts: false }, ["scheduler"]), /^ideactl deploy 'ecs' 'scheduler' .*--upgrade --refresh-hosts$/);
  assert.match(refreshHostsCommand("deploy", { ...options, refreshHosts: false }, ["ecs", "scheduler"]), /^ideactl deploy 'ecs' 'scheduler' /);
  assert.match(refreshHostsCommand("deploy", { ...options, refreshHosts: false }, ["all"]), /^ideactl deploy 'all' /);
});
