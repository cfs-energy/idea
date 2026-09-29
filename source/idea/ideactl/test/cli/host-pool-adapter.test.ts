import assert from "node:assert/strict";
import { test } from "node:test";
import { AutoScalingClient } from "@aws-sdk/client-auto-scaling";
import { EC2Client } from "@aws-sdk/client-ec2";
import { ECSClient } from "@aws-sdk/client-ecs";
import { liveContainerHosts } from "../../src/cli/live-operator-adapters.ts";
import { fakeContainerHosts } from "../support/host-pool-harness.ts";

const context = { awsRegion: "us-east-2", awsProfile: "fixture-profile" };

test("host adapter paginates attributes, batches descriptions and includes draining hosts", async (t) => {
  const lists: Record<string, unknown>[] = [];
  const batches: number[] = [];
  t.mock.method(ECSClient.prototype, "send", async function (this: ECSClient, command: { constructor: { name: string }; input: Record<string, unknown> }) {
    assert.equal(typeof this.config.credentials, "function");
    if (command.constructor.name === "ListContainerInstancesCommand") {
      lists.push(command.input);
      if (command.input.status === "DRAINING") return { containerInstanceArns: ["draining"] };
      return command.input.nextToken ? { containerInstanceArns: ["last"] } : { containerInstanceArns: Array.from({ length: 101 }, (_, n) => `host-${n}`), nextToken: "page-2" };
    }
    const arns = command.input.containerInstances as string[];
    batches.push(arns.length);
    return { containerInstances: arns.map((arn) => ({ containerInstanceArn: arn, ec2InstanceId: arn, status: "ACTIVE", attributes: [{ name: "idea.directory", value: "joined" }] })) };
  });
  const hosts = await liveContainerHosts().containerInstances({ ...context, cluster: "fixture" });
  assert.equal(hosts.length, 103);
  assert.deepEqual(batches, [100, 3]);
  assert.equal(lists.length, 3);
  assert.equal(hosts[102].attributes?.["idea.directory"], "joined");
});

test("host adapter resolves the group template version and reads running templates", async (t) => {
  t.mock.method(AutoScalingClient.prototype, "send", async () => ({ AutoScalingGroups: [{ LaunchTemplate: { LaunchTemplateId: "lt-host", Version: "$Latest" }, Instances: [{ InstanceId: "i-host", LaunchTemplate: { LaunchTemplateId: "lt-host", Version: "1" } }] }] }));
  t.mock.method(EC2Client.prototype, "send", async (command: { input: unknown }) => {
    assert.deepEqual(command.input, { LaunchTemplateId: "lt-host", LaunchTemplateName: undefined, Versions: ["$Latest"] });
    return { LaunchTemplateVersions: [{ LaunchTemplateId: "lt-host", VersionNumber: 2 }] };
  });
  assert.deepEqual(await liveContainerHosts().launchTemplateVersions({ ...context, name: "hosts" }), {
    current: { id: "lt-host", version: "2" }, instances: [{ instanceId: "i-host", id: "lt-host", version: "1" }],
  });
});

test("host adapter starts and describes only the requested refresh", async (t) => {
  const calls: unknown[] = [];
  t.mock.method(AutoScalingClient.prototype, "send", async (command: { constructor: { name: string }; input: unknown }) => {
    calls.push(command.input);
    return command.constructor.name === "StartInstanceRefreshCommand" ? { InstanceRefreshId: "refresh-id" }
      : { InstanceRefreshes: [{ Status: "Failed", PercentageComplete: 25, StatusReason: "capacity unavailable" }] };
  });
  const preferences = { MinHealthyPercentage: 100, MaxHealthyPercentage: 200, InstanceWarmup: 300, ScaleInProtectedInstances: "Refresh" as const };
  const api = liveContainerHosts();
  assert.equal(await api.startInstanceRefresh({ ...context, name: "hosts", preferences }), "refresh-id");
  assert.deepEqual(await api.describeInstanceRefresh({ ...context, name: "hosts", id: "refresh-id" }), { status: "Failed", percentageComplete: 25, reason: "capacity unavailable" });
  assert.deepEqual(calls, [{ AutoScalingGroupName: "hosts", Preferences: preferences }, { AutoScalingGroupName: "hosts", InstanceRefreshIds: ["refresh-id"] }]);
});

test("fake host adapter replaces stale hosts and exposes delayed directory attributes", async () => {
  const { api, state } = fakeContainerHosts();
  assert.equal((await api.launchTemplateVersions()).instances[0].version, "1");
  assert.deepEqual((await api.containerInstances())[0].attributes, {});
  await api.startInstanceRefresh({});
  await api.describeInstanceRefresh();
  assert.equal(state.old, false);
  assert.equal((await api.containerInstances())[0].attributes["idea.directory"], "joined");
});

test("host adapter fails closed when an ECS description is incomplete", async (t) => {
  t.mock.method(ECSClient.prototype, "send", async (command: { constructor: { name: string } }) =>
    command.constructor.name === "ListContainerInstancesCommand" ? { containerInstanceArns: ["host"] }
      : { failures: [{ arn: "host", reason: "MISSING" }] });
  await assert.rejects(liveContainerHosts().containerInstances({ ...context, cluster: "fixture" }), /host.*MISSING/);
});
