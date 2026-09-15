/**
 * The teardown does not terminate a member of an auto scaling group.
 *
 * Terminating tagged instances before deleting stacks is a harmless head start for a standalone
 * instance, which is what every instance in a cluster used to be. It is wrong for a group member:
 * the group exists to notice a missing instance and replace it, so terminating the container hosts
 * while their group still wants that many has the group put them back, and the teardown is then
 * racing something built to win.
 *
 * It also compounds the protection defect. The group cannot be deleted while its members are
 * protected from scale-in, so the order that existed was: terminate, watch them come back, then
 * fail to delete the group anyway.
 *
 * The group tag is set by the platform on every member and is already in the response the
 * termination step reads, so telling one from the other costs no extra call: an instance in a
 * group carries `aws:autoscaling:groupName` in `DescribeInstances`.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { deleteCluster, type DeleteClusterDeps, type DeleteClusterInstance } from "../../src/cli/commands/delete-cluster.ts";

const CLUSTER = "idea-test1";
const REGION = "us-east-2";

interface Recorded {
  terminated: string[];
  protectionChecked: string[];
  out: string[];
}

/** Inert dependencies: only instance discovery and termination are interesting here. */
function deps(instances: DeleteClusterInstance[], recorded: Recorded): DeleteClusterDeps {
  return {
    loadConfig: async () => undefined,
    findInstances: async () => instances,
    instanceTerminationProtection: async (instanceId) => {
      recorded.protectionChecked.push(instanceId);
      return false;
    },
    disableInstanceTerminationProtection: async () => {},
    terminateInstance: async (input) => {
      recorded.terminated.push(input.instanceId);
    },
    getTaggedStacks: async () => ({ stacks: [] }),
    describeStack: async (stackName) => ({ stackName }),
    disableStackTerminationProtection: async () => {},
    deleteStack: async () => {},
    findAppInstance: async () => undefined,
    sendAppCleanup: async () => "command",
    appCleanupStatus: async () => [{ status: "Success" }],
    findBedrockProjects: async () => [],
    deleteBedrockProjectResources: async () => {},
    listUserPools: async () => ({ pools: [] }),
    describeUserPool: async () => ({}),
    disableUserPoolDeletionProtection: async () => {},
    describeLambdaNetworkInterfaces: async () => [],
    deleteNetworkInterface: async () => {},
    describeBackupVault: async () => {},
    listRecoveryPoints: async () => [],
    deleteRecoveryPoint: async () => {},
    listTables: async () => ({ tableNames: [] }),
    deleteTable: async () => {},
    listDynamoDbAlarms: async () => [],
    deleteAlarms: async () => {},
    listLogGroups: async () => [],
    deleteLogGroup: async () => {},
    accountId: async () => "123456789012",
    bucketExists: async () => false,
    deleteAllBucketObjectVersions: async () => {},
    deleteBucket: async () => {},
    prompt: async () => true,
    sleep: async () => {},
    out: (line) => recorded.out.push(line),
    err: () => {},
  };
}

async function tearDown(instances: DeleteClusterInstance[]): Promise<Recorded> {
  const recorded: Recorded = { terminated: [], protectionChecked: [], out: [] };
  await deleteCluster(deps(instances, recorded), {
    clusterName: CLUSTER,
    awsRegion: REGION,
    force: true,
  });
  return recorded;
}

describe("the teardown's instance termination", () => {
  it("leaves a group member to its group", async () => {
    const recorded = await tearDown([
      { instanceId: "i-host-a", state: "running", autoScalingGroupName: `${CLUSTER}-ecs-hosts` },
      { instanceId: "i-host-b", state: "running", autoScalingGroupName: `${CLUSTER}-ecs-hosts` },
    ]);
    assert.deepEqual(recorded.terminated, []);
    // Not even probed: a call per instance that is not ours to terminate is wasted either way.
    assert.deepEqual(recorded.protectionChecked, []);
    assert.ok(
      recorded.out.some((line) => line.includes("left to its auto scaling group") && line.includes("i-host-a")),
      recorded.out.join("\n"),
    );
  });

  it("still terminates a standalone instance", async () => {
    // The behaviour this is narrowing, not replacing. A jump host has nothing watching it.
    const recorded = await tearDown([
      { instanceId: "i-bastion", state: "running", nodeType: "infra-standalone" },
    ]);
    assert.deepEqual(recorded.terminated, ["i-bastion"]);
  });

  it("keeps both apart in one cluster", async () => {
    const recorded = await tearDown([
      { instanceId: "i-host", state: "running", autoScalingGroupName: `${CLUSTER}-ecs-hosts` },
      { instanceId: "i-bastion", state: "running", nodeType: "infra-standalone" },
      { instanceId: "i-gone", state: "terminated", autoScalingGroupName: `${CLUSTER}-ecs-hosts` },
    ]);
    assert.deepEqual(recorded.terminated, ["i-bastion"]);
  });
});
