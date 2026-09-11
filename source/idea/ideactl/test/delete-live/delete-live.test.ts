/**
 * The teardown behaviours a fresh-cluster delete depends on:
 * error classification against the shapes the services really return, the interface sweep's
 * position relative to every stack group, the retained re-delete of a stack CloudFormation cannot
 * finish, and the two adapter calls that must not reach AWS at all.
 *
 * The error strings here are the ones observed from the services, not invented: a missing stack
 * answers `ValidationError` / `Stack with id <name> does not exist`, and a backup vault that does
 * not exist answers `AccessDeniedException` / `Insufficient privileges to perform this action.`
 * rather than a not-found.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import {
  CLUSTER_NAME_TAG,
  deleteBackupsCommand,
  deleteCluster,
  isMissingStackError,
  type DeleteClusterDeps,
  type DeleteClusterOptions,
} from "../../src/cli/commands/delete-cluster.ts";
import { createLiveDeleteClusterDeps } from "../../src/cli/live-operator-adapters.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";

const target = "idea-test1";
const region = "us-east-2";

/** An SDK service exception: the code is on `name`, and the message does not repeat it. */
function serviceError(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}

const MISSING_STACK = () => serviceError("ValidationError", `Stack with id ${target}-metrics does not exist`);
const MISSING_VAULT = () => serviceError("AccessDeniedException", "Insufficient privileges to perform this action.");

interface Harness {
  deps: DeleteClusterDeps;
  trace: string[];
}

/**
 * A cluster with one module stack, one identity-provider stack and the cluster stack, so the order
 * of the three stack groups is observable.
 */
function makeDeps(overrides: Partial<DeleteClusterDeps> = {}): Harness {
  const trace: string[] = [];
  const deleted = new Set<string>();
  const defaults: DeleteClusterDeps = {
    async loadConfig() {
      return new ClusterConfig(
        [{ key: "cluster.cluster_s3_bucket", value: "sample-bucket" }],
        [
          { module_id: "cluster", name: "cluster", type: "stack", stack_name: `${target}-cluster` },
          { module_id: "analytics", name: "analytics", type: "stack", stack_name: `${target}-analytics` },
          {
            module_id: "identity-provider",
            name: "identity-provider",
            type: "stack",
            stack_name: `${target}-identity-provider`,
          },
        ],
      );
    },
    async findInstances() {
      return [];
    },
    async instanceTerminationProtection() {
      return false;
    },
    async disableInstanceTerminationProtection() {},
    async terminateInstance() {},
    async getTaggedStacks() {
      return { stacks: [`${target}-analytics`, `${target}-identity-provider`, `${target}-cluster`] };
    },
    async describeStack(stackName) {
      return {
        stackName,
        stackStatus: deleted.has(stackName) ? "DELETE_COMPLETE" : "CREATE_COMPLETE",
        terminationProtection: false,
      };
    },
    async stackFailedResources(stackName) {
      trace.push(`failed-resources:${stackName}`);
      return ["phantomCustomResource"];
    },
    async disableStackTerminationProtection() {},
    async deleteStack(stackName, retainResources) {
      deleted.add(stackName);
      trace.push(`delete-stack:${stackName}:${(retainResources ?? []).join("|")}`);
    },
    async findAppInstance() {
      return undefined;
    },
    async sendAppCleanup() {
      return "";
    },
    async appCleanupStatus() {
      return [];
    },
    async findBedrockProjects() {
      return [];
    },
    async deleteBedrockProjectResources() {},
    async listUserPools() {
      return { pools: [] };
    },
    async describeUserPool() {
      return {};
    },
    async disableUserPoolDeletionProtection() {},
    async describeLambdaNetworkInterfaces() {
      trace.push("find-enis");
      return [{ networkInterfaceId: "eni-held", description: `AWS Lambda VPC ENI-${target}-analytics-sink-lambda` }];
    },
    async deleteNetworkInterface(networkInterfaceId) {
      trace.push(`delete-eni:${networkInterfaceId}`);
    },
    async describeBackupVault(name) {
      trace.push(`describe-vault:${name}`);
    },
    async listRecoveryPoints() {
      return [{ arn: "recovery-1", status: "COMPLETED" }];
    },
    async deleteRecoveryPoint(input) {
      trace.push(`delete-recovery-point:${input.recoveryPointArn}`);
    },
    async listTables() {
      return { tableNames: [] };
    },
    async deleteTable() {},
    async listDynamoDbAlarms() {
      return [];
    },
    async deleteAlarms() {},
    async listLogGroups() {
      return [];
    },
    async deleteLogGroup() {},
    async accountId() {
      return "123456789012";
    },
    async bucketExists() {
      return false;
    },
    async deleteAllBucketObjectVersions() {},
    async deleteBucket() {},
    async prompt() {
      return true;
    },
    async sleep() {},
    out(line) {
      trace.push(`out:${line}`);
    },
    err(line) {
      trace.push(`err:${line}`);
    },
  };
  return { deps: { ...defaults, ...overrides }, trace };
}

function options(extra: Partial<DeleteClusterOptions> = {}): DeleteClusterOptions {
  return { clusterName: target, awsRegion: region, force: true, ...extra };
}

function indexOf(trace: string[], value: string): number {
  const index = trace.indexOf(value);
  assert.notEqual(index, -1, `missing trace entry: ${value}`);
  return index;
}

test("a missing stack is classified from the service error code, not only its message", () => {
  // The message alone carries no code, so a matcher that reads only the message calls this a hard
  // failure and aborts a teardown over a stack that is already gone.
  assert.equal(isMissingStackError(MISSING_STACK()), true);
  assert.equal(/ValidationError|not found/i.test(MISSING_STACK().message), false);
  // A real failure stays a real failure.
  assert.equal(isMissingStackError(serviceError("AccessDeniedException", "not authorized to perform: cloudformation:DescribeStacks")), false);
  assert.equal(isMissingStackError(serviceError("ThrottlingException", "Rate exceeded")), false);
  assert.equal(isMissingStackError("not an error"), false);
});

test("a stack that disappears during discovery does not stop the teardown", async () => {
  const gone = new Set<string>();
  const { deps, trace } = makeDeps({
    async getTaggedStacks() {
      return { stacks: [`${target}-metrics`, `${target}-analytics`, `${target}-cluster`] };
    },
    async describeStack(stackName) {
      if (stackName === `${target}-metrics`) throw MISSING_STACK();
      return {
        stackName,
        stackStatus: gone.has(stackName) ? "DELETE_COMPLETE" : "CREATE_COMPLETE",
        terminationProtection: false,
      };
    },
    async deleteStack(stackName, retainResources) {
      gone.add(stackName);
      trace.push(`delete-stack:${stackName}:${(retainResources ?? []).join("|")}`);
    },
  });
  // Every other stack still has to be deleted: the vanished one is skipped, not fatal.
  await deleteCluster(deps, options());
  assert.ok(trace.includes(`delete-stack:${target}-analytics:`));
  assert.ok(trace.includes(`delete-stack:${target}-cluster:`));
});

test("the interface sweep runs before the first delete of every stack group", async () => {
  const { deps, trace } = makeDeps();
  await deleteCluster(deps, options());
  const sweeps = trace.reduce<number[]>((positions, entry, index) => {
    if (entry === "delete-eni:eni-held") positions.push(index);
    return positions;
  }, []);
  // Three groups are deleted, so three sweeps precede three first deletes.
  for (const stackName of ["analytics", "identity-provider", "cluster"]) {
    const firstDelete = indexOf(trace, `delete-stack:${target}-${stackName}:`);
    assert.ok(
      sweeps.some((position) => position < firstDelete),
      `no interface sweep precedes the delete of ${stackName}`,
    );
  }
  assert.equal(sweeps.length >= 3, true, `expected a sweep per group, saw ${sweeps.length}`);
});

test("a stack that is still deleting is swept on every poll", async () => {
  // A function's interfaces become available part way through its own stack's delete. Sweeping only
  // once, before the delete is issued, leaves the network stack to wait out a CloudFormation
  // timeout for interfaces that appeared after that sweep.
  let polls = 0;
  const { deps, trace } = makeDeps({
    async getTaggedStacks() {
      return { stacks: [`${target}-cluster`] };
    },
    async describeStack(stackName) {
      if (!trace.some((entry) => entry.startsWith("delete-stack:"))) {
        return { stackName, stackStatus: "CREATE_COMPLETE", terminationProtection: false };
      }
      polls += 1;
      return { stackName, stackStatus: polls < 3 ? "DELETE_IN_PROGRESS" : "DELETE_COMPLETE" };
    },
  });
  await deleteCluster(deps, options());
  const deleteIndex = indexOf(trace, `delete-stack:${target}-cluster:`);
  const sweepsAfterDelete = trace.filter((entry, index) => entry === "delete-eni:eni-held" && index > deleteIndex);
  assert.ok(
    sweepsAfterDelete.length >= 2,
    `expected a sweep per in-progress poll, saw ${sweepsAfterDelete.length}`,
  );
});

test("a stack that fails to delete twice is re-issued retaining the resources CloudFormation could not delete", async () => {
  // The stack refuses two deletes and accepts the third. State is driven by the number of deletes
  // issued rather than by a read counter, because the delete helper reads the stack itself.
  let deletesIssued = 0;
  const { deps, trace } = makeDeps({
    async getTaggedStacks() {
      return { stacks: [`${target}-analytics`] };
    },
    async describeStack(stackName) {
      if (deletesIssued === 0) return { stackName, stackStatus: "CREATE_COMPLETE", terminationProtection: false };
      return { stackName, stackStatus: deletesIssued < 3 ? "DELETE_FAILED" : "DELETE_COMPLETE" };
    },
    async deleteStack(stackName, retainResources) {
      deletesIssued += 1;
      trace.push(`delete-stack:${stackName}:${(retainResources ?? []).join("|")}`);
    },
  });
  await deleteCluster(deps, options());
  const deletes = trace.filter((entry) => entry.startsWith(`delete-stack:${target}-analytics`));
  assert.deepEqual(deletes, [
    // first attempt: plain
    `delete-stack:${target}-analytics:`,
    // first failure: plain re-delete, after the sweep, because a held interface is recoverable
    `delete-stack:${target}-analytics:`,
    // second failure: retain what will never delete, so the stack can go
    `delete-stack:${target}-analytics:phantomCustomResource`,
  ]);
  assert.ok(trace.includes(`failed-resources:${target}-analytics`));
  // The retained ids are printed, because they stay in the account.
  assert.ok(trace.some((entry) => entry.startsWith("out:") && entry.includes("phantomCustomResource")));
});

test("an unreadable backup vault is skipped and a failed recovery-point delete is not", async () => {
  const skipped = makeDeps({
    async describeBackupVault() {
      throw MISSING_VAULT();
    },
  });
  await deleteBackupsCommand(skipped.deps, { clusterName: target, force: true });
  assert.equal(skipped.trace.some((entry) => entry.startsWith("delete-recovery-point:")), false);
  assert.ok(skipped.trace.some((entry) => entry.startsWith("out:backup vault")));

  const failing = makeDeps({
    async deleteRecoveryPoint() {
      throw serviceError("InvalidRequestException", "recovery point is in use");
    },
  });
  // A recovery point that stays keeps the vault, and the vault keeps its stack, so this must be
  // loud rather than swallowed.
  await assert.rejects(
    deleteBackupsCommand(failing.deps, { clusterName: target, force: true }),
    /recovery point is in use/,
  );
});

test("the application clean-up makes no call when the cluster has no running application host", async () => {
  // No credentials are configured for this test, so a call that reached SSM would fail rather than
  // return. The empty command id is the contract the status read understands.
  const live = await createLiveDeleteClusterDeps({
    async accountId() {
      return "123456789012";
    },
    out() {},
    err() {},
  } as unknown as Deps)({ clusterName: target, awsRegion: region }, "delete-cluster");
  assert.equal(await live.sendAppCleanup({ instanceIds: [], deleteDatabases: false }), "");
  assert.deepEqual(await live.appCleanupStatus(""), []);
});

test("a cluster whose settings tables are already gone still loads", async () => {
  // The teardown has to run on a half-deleted cluster. `ClusterConfig` raises its own error type
  // for absent tables, so a catch that only knows the SDK's would abort at the first step.
  const live = await createLiveDeleteClusterDeps({
    async scan() {
      throw Object.assign(new Error("Requested resource not found"), { name: "ResourceNotFoundException" });
    },
    async accountId() {
      return "123456789012";
    },
    out() {},
    err() {},
  } as unknown as Deps)({ clusterName: target, awsRegion: region }, "delete-cluster");
  assert.equal(await live.loadConfig({ clusterName: target, awsRegion: region }), undefined);
});

test("the live factory builds a complete set of adapters for both deletion commands", async () => {
  const factory = createLiveDeleteClusterDeps({
    async accountId() {
      return "123456789012";
    },
    async scan() {
      return { Items: [] };
    },
    async sleep() {},
    out() {},
    err() {},
    async prompt() {
      return false;
    },
  } as unknown as Deps);
  const members = [
    "loadConfig", "findInstances", "instanceTerminationProtection", "disableInstanceTerminationProtection",
    "terminateInstance", "getTaggedStacks", "describeStack", "stackFailedResources",
    "disableStackTerminationProtection", "deleteStack", "findAppInstance", "sendAppCleanup",
    "appCleanupStatus", "findBedrockProjects", "deleteBedrockProjectResources", "listUserPools",
    "describeUserPool", "disableUserPoolDeletionProtection", "describeLambdaNetworkInterfaces",
    "deleteNetworkInterface", "describeBackupVault", "listRecoveryPoints", "deleteRecoveryPoint",
    "listTables", "deleteTable", "listDynamoDbAlarms", "deleteAlarms", "listLogGroups",
    "deleteLogGroup", "accountId", "bucketExists", "deleteAllBucketObjectVersions", "deleteBucket",
  ];
  for (const command of ["delete-cluster", "delete-backups"] as const) {
    // Both commands must build the full adapter set, so resolving at all is the check.
    const built = (await factory({ clusterName: target, awsRegion: region }, command)) as unknown as Record<string, unknown>;
    for (const member of members) {
      assert.equal(typeof built[member], "function", `${command} is missing ${member}`);
    }
  }
});

test("the tag filters the discovery calls send name the cluster and nothing else", async () => {
  const seen: string[] = [];
  const { deps } = makeDeps({
    async findInstances(input) {
      seen.push(JSON.stringify(input.filters));
      return [];
    },
    async getTaggedStacks(input) {
      seen.push(JSON.stringify(input.tagFilters));
      return { stacks: [] };
    },
  });
  await deleteCluster(deps, options());
  assert.deepEqual(seen, [
    JSON.stringify([{ name: `tag:${CLUSTER_NAME_TAG}`, values: [target] }]),
    JSON.stringify([{ key: CLUSTER_NAME_TAG, values: [target] }]),
  ]);
});
