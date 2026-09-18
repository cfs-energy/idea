import assert from "node:assert/strict";
import test from "node:test";
import { Command } from "commander";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import {
  CLUSTER_NAME_TAG,
  deleteBackupsCommand,
  deleteCluster,
  registerDeleteClusterCommands,
  type DeleteClusterDeps,
  type DeleteClusterOptions,
  type DeleteClusterStack,
} from "../../src/cli/commands/delete-cluster.ts";

const target = "sample-cluster";
const region = "us-east-2";

function indexOf(trace: string[], value: string): number {
  const index = trace.indexOf(value);
  assert.notEqual(index, -1, `missing trace entry: ${value}`);
  return index;
}

function assertBefore(trace: string[], first: string, second: string): void {
  assert.ok(indexOf(trace, first) < indexOf(trace, second), `${first} must precede ${second}`);
}

function makeDeps(overrides: Partial<DeleteClusterDeps> = {}): { deps: DeleteClusterDeps; trace: string[] } {
  const trace: string[] = [];
  const deletedStacks = new Set<string>();
  const stack = (stackName: string): DeleteClusterStack => ({
    stackName,
    stackStatus: deletedStacks.has(stackName) ? "DELETE_COMPLETE" : "CREATE_COMPLETE",
    terminationProtection: false,
  });
  const defaults: DeleteClusterDeps = {
    async loadConfig() {
      trace.push("load-config");
      return new ClusterConfig(
        [
          { key: "cluster.cluster_s3_bucket", value: "fixture-bucket" },
          { key: "cluster.route53.private_hosted_zone_id", value: "Z-fixture" },
        ],
        [
          { module_id: "cluster", name: "cluster", type: "stack", stack_name: `${target}-cluster` },
          { module_id: "identity", name: "identity-provider", type: "stack", stack_name: `${target}-identity-provider` },
          { module_id: "analytics", name: "analytics", type: "stack", stack_name: `${target}-analytics` },
          { module_id: "portal", name: "cluster-manager", type: "app" },
        ],
      );
    },
    async findInstances(input) {
      trace.push(`find-instances:${input.filters[0]?.name}:${input.filters[0]?.values[0]}`);
      return [
        { instanceId: "i-unmanaged", state: "running", nodeType: "compute" },
        { instanceId: "i-app", state: "running", nodeType: "app" },
      ];
    },
    async instanceTerminationProtection(instanceId) {
      trace.push(`instance-protection:${instanceId}`);
      return instanceId === "i-unmanaged";
    },
    async disableInstanceTerminationProtection(instanceId) {
      trace.push(`disable-instance-protection:${instanceId}`);
    },
    async terminateInstance(input) {
      trace.push(`terminate-instance:${input.instanceId}:${input.force}:${input.skipOsShutdown}`);
    },
    async getTaggedStacks(input) {
      trace.push(`find-stacks:${input.tagFilters[0]?.key}:${input.tagFilters[0]?.values[0]}`);
      return {
        stacks: [`${target}-analytics`, `${target}-identity-provider`, `${target}-cluster`, `${target}-bootstrap`],
      };
    },
    async describeStack(stackName) {
      trace.push(`describe-stack:${stackName}`);
      return stack(stackName);
    },
    async disableStackTerminationProtection(stackName) {
      trace.push(`disable-stack-protection:${stackName}`);
    },
    async deleteStack(stackName) {
      deletedStacks.add(stackName);
      trace.push(`delete-stack:${stackName}`);
    },
    async findAppInstance(input) {
      trace.push(`find-app:${input.clusterName}:${input.moduleId}`);
      return { instanceId: "i-portal", state: "running" };
    },
    async sendAppCleanup(input) {
      trace.push(`app-cleanup:${input.instanceIds.join(",")}:${input.deleteDatabases}`);
      return "command-1";
    },
    async appCleanupStatus(commandId) {
      trace.push(`app-cleanup-status:${commandId}`);
      return [{ status: "Success" }];
    },
    async findBedrockProjects(clusterName) {
      trace.push(`find-projects:${clusterName}`);
      return [{ id: "project-1" }];
    },
    async deleteBedrockProjectResources(input) {
      trace.push(`delete-projects:${input.clusterName}`);
    },
    async listUserPools(nextToken) {
      trace.push(`list-pools:${nextToken ?? ""}`);
      return { pools: [{ id: "pool-target", name: `${target}-user-pool` }] };
    },
    async describeUserPool(userPoolId) {
      trace.push(`describe-pool:${userPoolId}`);
      return { deletionProtection: "ACTIVE", tags: { [CLUSTER_NAME_TAG]: target } };
    },
    async disableUserPoolDeletionProtection(userPoolId) {
      trace.push(`disable-pool-protection:${userPoolId}`);
    },
    async describeLambdaNetworkInterfaces(input) {
      trace.push(`find-enis:${input.clusterName}`);
      return [];
    },
    async deleteNetworkInterface(networkInterfaceId) {
      trace.push(`delete-eni:${networkInterfaceId}`);
    },
    async describeBackupVault(name) {
      trace.push(`describe-vault:${name}`);
    },
    async listRecoveryPoints(name) {
      trace.push(`list-recovery-points:${name}`);
      return [
        { arn: "recovery-complete", status: "COMPLETED" },
        { arn: "recovery-active", status: "CREATING" },
        { arn: "recovery-expired", status: "EXPIRED" },
      ];
    },
    async deleteRecoveryPoint(input) {
      trace.push(`delete-recovery-point:${input.recoveryPointArn}`);
    },
    async listTables(nextTableName) {
      trace.push(`list-tables:${nextTableName ?? ""}`);
      return { tableNames: [`${target}.cluster-settings`, "idea-test1.cluster-settings"] };
    },
    async deleteTable(name) {
      trace.push(`delete-table:${name}`);
    },
    async listDynamoDbAlarms(clusterName) {
      trace.push(`list-alarms:${clusterName}`);
      return [
        { name: "target-alarm", namespace: "AWS/DynamoDB", tableName: `${target}.cluster-settings` },
        { name: "other-alarm", namespace: "AWS/DynamoDB", tableName: "idea-test1.cluster-settings" },
      ];
    },
    async deleteAlarms(names) {
      trace.push(`delete-alarms:${names.join(",")}`);
    },
    async listHostedZoneRecords(hostedZoneId) {
      trace.push(`list-zone-records:${hostedZoneId}`);
      return [
        { Name: `${target}.us-east-2.local.`, Type: "NS" },
        { Name: `${target}.us-east-2.local.`, Type: "SOA" },
        { Name: `scheduler.${target}.us-east-2.local.`, Type: "A", TTL: 60, ResourceRecords: [{ Value: "10.0.0.5" }] },
      ];
    },
    async deleteHostedZoneRecords(hostedZoneId, records) {
      trace.push(`delete-zone-records:${hostedZoneId}:${records.map((record) => `${record.Name} ${record.Type}`).join(",")}`);
    },
    async listLogGroups(prefix) {
      trace.push(`list-logs:${prefix}`);
      return prefix === target ? [{ name: `${target}-log`, size: 10 }] : [];
    },
    async deleteLogGroup(name) {
      trace.push(`delete-log:${name}`);
    },
    async accountId() {
      trace.push("account-id");
      return "123456789012";
    },
    async bucketExists(name) {
      trace.push(`bucket-exists:${name}`);
      return true;
    },
    async deleteAllBucketObjectVersions(name) {
      trace.push(`empty-bucket:${name}`);
    },
    async deleteBucket(name) {
      trace.push(`delete-bucket:${name}`);
    },
    async prompt(message) {
      trace.push(`prompt:${message}`);
      return true;
    },
    async sleep(milliseconds) {
      trace.push(`sleep:${milliseconds}`);
    },
    out(line) {
      trace.push(`out:${line}`);
    },
    err(line) {
      trace.push(`err:${line}`);
    },
  };
  return { deps: { ...defaults, ...overrides }, trace };
}

function allOptions(): DeleteClusterOptions {
  return {
    clusterName: target,
    awsRegion: region,
    deleteBootstrap: true,
    deleteDatabases: true,
    deleteBackups: true,
    deleteCloudwatchLogs: true,
    force: true,
  };
}

test("runs the 13 deletion phases in order", async () => {
  const { deps, trace } = makeDeps();
  await deleteCluster(deps, allOptions());

  assertBefore(trace, `find-instances:tag:${CLUSTER_NAME_TAG}:${target}`, `find-stacks:${CLUSTER_NAME_TAG}:${target}`);
  assertBefore(trace, `find-stacks:${CLUSTER_NAME_TAG}:${target}`, "app-cleanup:i-portal:true");
  assertBefore(trace, "app-cleanup:i-portal:true", "disable-instance-protection:i-unmanaged");
  assertBefore(trace, "disable-instance-protection:i-unmanaged", "terminate-instance:i-unmanaged:true:true");
  assertBefore(trace, "terminate-instance:i-unmanaged:true:true", `delete-projects:${target}`);
  assertBefore(trace, `delete-projects:${target}`, `delete-stack:${target}-analytics`);
  assertBefore(trace, `delete-stack:${target}-analytics`, "disable-pool-protection:pool-target");
  assertBefore(trace, "disable-pool-protection:pool-target", `delete-stack:${target}-identity-provider`);
  assertBefore(trace, `delete-stack:${target}-identity-provider`, "delete-recovery-point:recovery-complete");
  assertBefore(trace, "delete-recovery-point:recovery-expired", `delete-stack:${target}-cluster`);
  assertBefore(trace, `delete-stack:${target}-cluster`, `delete-stack:${target}-bootstrap`);
  assertBefore(trace, `delete-stack:${target}-bootstrap`, "empty-bucket:fixture-bucket");
  assertBefore(trace, "delete-bucket:fixture-bucket", `delete-table:${target}.cluster-settings`);
  assertBefore(trace, `delete-table:${target}.cluster-settings`, "delete-alarms:target-alarm");
  assertBefore(trace, "delete-alarms:target-alarm", `delete-log:${target}-log`);
});

test("refuses cluster deletion without confirmation before any destructive operation", async () => {
  const { deps, trace } = makeDeps({
    async prompt() {
      return false;
    },
  });
  await deleteCluster(deps, { clusterName: target, awsRegion: region });
  assert.equal(trace.some((entry) => entry.startsWith("delete-")), false);
  assert.equal(trace.some((entry) => entry.startsWith("app-cleanup:")), false);
});

test("uses the named cluster tag for discovery and never receives a foreign resource", async () => {
  const { deps, trace } = makeDeps({
    async getTaggedStacks(input) {
      assert.deepEqual(input, {
        tagFilters: [{ key: CLUSTER_NAME_TAG, values: [target] }],
        resourceTypeFilters: ["cloudformation"],
        paginationToken: undefined,
      });
      return { stacks: [`${target}-cluster`] };
    },
    async findInstances(input) {
      assert.deepEqual(input.filters, [{ name: `tag:${CLUSTER_NAME_TAG}`, values: [target] }]);
      return [];
    },
  });
  await deleteCluster(deps, { clusterName: target, awsRegion: region, force: true });
  assert.equal(trace.some((entry) => entry.includes("idea-test1")), false);
  assert.equal(trace.some((entry) => entry.startsWith("delete-stack:") && !entry.includes(target)), false);
});

test("clears only the target identity pool protection before its stack is deleted", async () => {
  const { deps, trace } = makeDeps({
    async listUserPools() {
      return {
        pools: [
          { id: "pool-target", name: `${target}-user-pool` },
          { id: "pool-foreign", name: `${target}-user-pool` },
        ],
      };
    },
    async describeUserPool(userPoolId) {
      if (userPoolId === "pool-foreign") {
        return { deletionProtection: "ACTIVE", tags: { [CLUSTER_NAME_TAG]: "idea-test1" } };
      }
      return { deletionProtection: "ACTIVE", tags: { [CLUSTER_NAME_TAG]: target } };
    },
  });
  await deleteCluster(deps, { clusterName: target, awsRegion: region, force: true });
  assert.ok(trace.includes("disable-pool-protection:pool-target"));
  assert.equal(trace.includes("disable-pool-protection:pool-foreign"), false);
  assertBefore(trace, "disable-pool-protection:pool-target", `delete-stack:${target}-identity-provider`);
});

test("retries a failed analytics stack after deleting its available Lambda ENIs", async () => {
  let deleteRequested = false;
  let statusReads = 0;
  const { deps, trace } = makeDeps({
    async getTaggedStacks() {
      return { stacks: [`${target}-analytics`] };
    },
    async deleteStack(stackName) {
      deleteRequested = true;
      trace.push(`delete-stack:${stackName}`);
    },
    async describeStack(stackName) {
      trace.push(`describe-stack:${stackName}`);
      if (!deleteRequested) return { stackName, stackStatus: "CREATE_COMPLETE" };
      statusReads += 1;
      return { stackName, stackStatus: statusReads === 1 ? "DELETE_FAILED" : "DELETE_COMPLETE" };
    },
    async describeLambdaNetworkInterfaces() {
      return [{ networkInterfaceId: "eni-target" }];
    },
  });
  await deleteCluster(deps, { clusterName: target, awsRegion: region, force: true });
  const initialDelete = indexOf(trace, `delete-stack:${target}-analytics`);
  const firstEniDelete = indexOf(trace, "delete-eni:eni-target");
  const retryDelete = trace.lastIndexOf(`delete-stack:${target}-analytics`);
  const lastEniDelete = trace.lastIndexOf("delete-eni:eni-target");
  // The sweep has to run before the first delete: an interface left available by an earlier deploy
  // already holds the security group, so a sweep only on failure costs the CloudFormation timeout.
  assert.ok(firstEniDelete < initialDelete, "the interface sweep must precede the first stack delete");
  assert.ok(initialDelete < retryDelete, "the failed stack must be re-issued");
  assert.ok(
    lastEniDelete > initialDelete && lastEniDelete < retryDelete,
    "the retry must be preceded by its own interface sweep",
  );
});

test("keeps the retained bootstrap bucket unless its explicit flag is present", async () => {
  const { deps, trace } = makeDeps();
  await deleteCluster(deps, { clusterName: target, awsRegion: region, force: true });
  assert.equal(trace.some((entry) => entry.startsWith("empty-bucket:")), false);
  assert.equal(trace.some((entry) => entry.startsWith("delete-bucket:")), false);
});

test("delete-backups requires confirmation and deletes completed or expired points only", async () => {
  const declined = makeDeps({ async prompt() { return false; } });
  await deleteBackupsCommand(declined.deps, { clusterName: target, force: false });
  assert.equal(declined.trace.some((entry) => entry.startsWith("delete-recovery-point:")), false);

  const confirmed = makeDeps();
  await deleteBackupsCommand(confirmed.deps, { clusterName: target, force: true });
  assert.ok(confirmed.trace.includes("delete-recovery-point:recovery-complete"));
  assert.ok(confirmed.trace.includes("delete-recovery-point:recovery-expired"));
  assert.equal(confirmed.trace.includes("delete-recovery-point:recovery-active"), false);
});

test("a failure clearing user-pool deletion protection aborts the delete", async () => {
  const { deps } = makeDeps({
    async disableUserPoolDeletionProtection() {
      throw new Error("required-user-pool-protection");
    },
  });
  await assert.rejects(
    deleteCluster(deps, { clusterName: target, awsRegion: region, force: true }),
    /required-user-pool-protection/,
  );
});

test("registers both destructive command surfaces", () => {
  const { deps } = makeDeps();
  const program = new Command();
  registerDeleteClusterCommands(program, deps);
  assert.deepEqual(program.commands.map((command) => command.name()), ["delete-cluster", "delete-backups"]);
});

// The container capacity stack owns the ECS cluster and the Cloud Map namespace the module
// services live on. Deleted alongside the module stacks it fails on both ("namespace has
// associated services"), which is what idea-ctr2 did on 2026-09-15; after them it deletes clean.
test("the container capacity stack is deleted after the module stacks and before identity-provider", async () => {
  const { deps, trace } = makeDeps();
  const base = deps.loadConfig;
  deps.loadConfig = async (input) => {
    const config = await base(input);
    if (config === undefined) throw new Error("the fixture always loads a config");
    config.modules().push({ module_id: "ecs", name: "ecs", type: "stack", stack_name: `${target}-ecs` });
    return config;
  };
  const stacksBase = deps.getTaggedStacks;
  deps.getTaggedStacks = async (input) => {
    const page = await stacksBase(input);
    return { ...page, stacks: [`${target}-ecs`, ...page.stacks] };
  };
  await deleteCluster(deps, allOptions());
  assertBefore(trace, `delete-stack:${target}-analytics`, `delete-stack:${target}-ecs`);
  assertBefore(trace, `delete-stack:${target}-ecs`, `delete-stack:${target}-identity-provider`);
});

// The container scheduler upserts its own record and its stack retains it, so once the stacks are
// gone the zone still holds it and Route 53 refuses to delete the zone with the cluster stack:
// idea-ctr2's teardown stopped there on 2026-09-15. Only the records a service left go; NS and SOA stay.
test("the private hosted zone is emptied of service records right before the cluster stack is deleted", async () => {
  const { deps, trace } = makeDeps();
  await deleteCluster(deps, allOptions());
  const cleared = `delete-zone-records:Z-fixture:scheduler.${target}.us-east-2.local. A`;
  assert.ok(trace.includes(cleared), trace.join("\n"));
  assertBefore(trace, `delete-stack:${target}-identity-provider`, cleared);
  assertBefore(trace, cleared, `delete-stack:${target}-cluster`);
});

test("a zone with nothing but its own NS and SOA is left alone", async () => {
  const { deps, trace } = makeDeps();
  deps.listHostedZoneRecords = async () => [{ Name: "x.local.", Type: "NS" }, { Name: "x.local.", Type: "SOA" }];
  await deleteCluster(deps, allOptions());
  assert.ok(!trace.some((event) => event.startsWith("delete-zone-records:")));
});

test('the bastion service stack is deleted before its capacity with no bastion instance', async () => {
  const { deps, trace } = makeDeps();
  const load = deps.loadConfig;
  deps.loadConfig = async (input) => {
    const config = await load(input);
    assert.ok(config);
    config.modules().push(
      { module_id: 'bastion-host', name: 'bastion-host', type: 'stack', stack_name: `${target}-bastion-host` },
      { module_id: 'ecs', name: 'ecs', type: 'stack', stack_name: `${target}-ecs` },
    );
    return config;
  };
  const stacks = deps.getTaggedStacks;
  deps.getTaggedStacks = async (input) => {
    const page = await stacks(input);
    return { ...page, stacks: [`${target}-ecs`, `${target}-bastion-host`, ...page.stacks] };
  };
  deps.findInstances = async () => [];
  await deleteCluster(deps, allOptions());
  assertBefore(trace, `delete-stack:${target}-bastion-host`, `delete-stack:${target}-ecs`);
  assertBefore(trace, `delete-stack:${target}-ecs`, `delete-stack:${target}-cluster`);
  assert.ok(!trace.some((entry) => entry.startsWith('disable-instance-protection:')));
});
