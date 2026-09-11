/**
 * Destructive cluster removal commands. Effects are injected so the command can be replayed
 * without credentials and each resource-discovery boundary remains explicit.
 */

import type { Command } from "commander";

import type { ClusterConfig, ModuleInfo } from "../../config/cluster-config.ts";

export const CLUSTER_NAME_TAG = "idea:ClusterName";
export const NODE_TYPE_TAG = "idea:NodeType";
/** Set by the platform on every instance an auto scaling group launches. */
export const AUTOSCALING_GROUP_TAG = "aws:autoscaling:groupName";
export const MODULE_ID_TAG = "idea:ModuleId";
const APP_NODE_TYPE = "app";
const INFRA_NODE_TYPE = "infra";

export interface DeleteClusterOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  deleteBootstrap?: boolean;
  deleteDatabases?: boolean;
  deleteBackups?: boolean;
  deleteCloudwatchLogs?: boolean;
  deleteAll?: boolean;
  force?: boolean;
}

export interface DeleteClusterInstance {
  instanceId: string;
  state: string;
  nodeType?: string;
  /** The auto scaling group that launched it, from the tag the platform sets on every member. */
  autoScalingGroupName?: string;
}

export interface DeleteClusterStack {
  stackName: string;
  stackStatus?: string;
  terminationProtection?: boolean;
}

export interface DeleteClusterUserPool {
  id: string;
  name: string;
}

export interface DescribedUserPool {
  deletionProtection?: string;
  tags?: Record<string, string>;
}

export interface RecoveryPoint {
  arn: string;
  status?: string;
}

export interface ProjectRecord {
  [key: string]: unknown;
}

export interface DeletionTagFilter {
  key: string;
  values: string[];
}

export interface DeletionInstanceFilter {
  name: string;
  values: string[];
}

/**
 * All deletion effects. The live CLI supplies adapters, while unit tests inject a recording
 * implementation. Discovery inputs deliberately use the native filter shapes.
 */
export interface DeleteClusterDeps {
  loadConfig(input: { clusterName: string; awsRegion: string; awsProfile?: string }): Promise<ClusterConfig | undefined>;
  findInstances(input: { filters: DeletionInstanceFilter[] }): Promise<DeleteClusterInstance[]>;
  instanceTerminationProtection(instanceId: string): Promise<boolean>;
  disableInstanceTerminationProtection(instanceId: string): Promise<void>;
  terminateInstance(input: { instanceId: string; force: boolean; skipOsShutdown: boolean }): Promise<void>;
  getTaggedStacks(input: {
    tagFilters: DeletionTagFilter[];
    resourceTypeFilters: string[];
    paginationToken?: string;
  }): Promise<{ stacks: string[]; paginationToken?: string }>;
  describeStack(stackName: string): Promise<DeleteClusterStack>;
  disableStackTerminationProtection(stackName: string): Promise<void>;
  /** `RetainResources` is passed only on a re-delete of a stack CloudFormation could not finish. */
  deleteStack(stackName: string, retainResources?: string[]): Promise<void>;
  /** Logical ids left in `DELETE_FAILED`. Optional: a replay implementation may not have them. */
  stackFailedResources?(stackName: string): Promise<string[]>;
  findAppInstance(input: { clusterName: string; moduleId: string }): Promise<DeleteClusterInstance | undefined>;
  sendAppCleanup(input: { instanceIds: string[]; deleteDatabases: boolean }): Promise<string>;
  appCleanupStatus(commandId: string): Promise<Array<{ status: string }>>;
  findBedrockProjects(clusterName: string): Promise<ProjectRecord[]>;
  deleteBedrockProjectResources(input: { clusterName: string; projects: ProjectRecord[] }): Promise<void>;
  listUserPools(nextToken?: string): Promise<{ pools: DeleteClusterUserPool[]; nextToken?: string }>;
  describeUserPool(userPoolId: string): Promise<DescribedUserPool>;
  disableUserPoolDeletionProtection(userPoolId: string): Promise<void>;
  describeLambdaNetworkInterfaces(input: { clusterName: string }): Promise<
    Array<{ networkInterfaceId: string; description?: string }>
  >;
  deleteNetworkInterface(networkInterfaceId: string): Promise<void>;
  describeBackupVault(backupVaultName: string): Promise<void>;
  listRecoveryPoints(backupVaultName: string): Promise<RecoveryPoint[]>;
  deleteRecoveryPoint(input: { backupVaultName: string; recoveryPointArn: string }): Promise<void>;
  listTables(nextTableName?: string): Promise<{ tableNames: string[]; nextTableName?: string }>;
  deleteTable(tableName: string): Promise<void>;
  listDynamoDbAlarms(clusterName: string): Promise<Array<{ name: string; namespace: string; tableName?: string }>>;
  deleteAlarms(alarmNames: string[]): Promise<void>;
  listLogGroups(prefix: string): Promise<Array<{ name: string; size: number }>>;
  deleteLogGroup(name: string): Promise<void>;
  accountId(): Promise<string>;
  bucketExists(name: string): Promise<boolean>;
  deleteAllBucketObjectVersions(name: string): Promise<void>;
  deleteBucket(name: string): Promise<void>;
  prompt(message: string): Promise<boolean | string>;
  sleep(milliseconds: number): Promise<void>;
  out(line: string): void;
  err(line: string): void;
}

/** Builds dependencies for the profile selected by one deletion command. */
export type DeleteClusterDepsFactory = (
  options: DeleteClusterOptions,
  command: "delete-cluster" | "delete-backups",
) => Promise<DeleteClusterDeps>;

type DeleteClusterDepsSource = DeleteClusterDeps | DeleteClusterDepsFactory;

function isConfirmed(answer: boolean | string): boolean {
  return answer === true || answer === "Yes";
}

function shouldDelete(options: DeleteClusterOptions, name: "Bootstrap" | "Databases" | "Backups" | "CloudwatchLogs"): boolean {
  if (options.deleteAll === true) return true;
  if (name === "Bootstrap") return options.deleteBootstrap === true;
  if (name === "Databases") return options.deleteDatabases === true;
  if (name === "Backups") return options.deleteBackups === true;
  return options.deleteCloudwatchLogs === true;
}

function stackNameMatches(moduleName: string, stack: DeleteClusterStack, clusterName: string, modules: ModuleInfo[]): boolean {
  const configured = modules.find((module) => module.name === moduleName)?.stack_name;
  return stack.stackName === configured || stack.stackName === `${clusterName}-${moduleName}`;
}

/**
 * A stack that is not there. CloudFormation answers a missing stack with a `ValidationError` whose
 * message is `Stack with id <name> does not exist`, so the code has to be matched as well as the
 * message: matching the message alone classifies every disappeared stack as a hard failure.
 */
export function isMissingStackError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return /ValidationError|not found|does not exist/i.test(`${error.name}: ${error.message}`);
}

/**
 * Exact equivalent of the delete command's stack deletion helper. It discovers current stack
 * protection before clearing it, and treats a disappeared stack as already deleted.
 */
async function deleteStack(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  stackName: string,
  retainResources?: string[],
): Promise<void> {
  let stack: DeleteClusterStack;
  try {
    stack = await deps.describeStack(stackName);
  } catch (error) {
    if (isMissingStackError(error)) return;
    throw error;
  }

  if (stack.terminationProtection === true && options.force !== true) {
    const confirmed = await deps.prompt(`Termination protection is enabled for stack: ${stackName}. Disable and terminate?`);
    if (!isConfirmed(confirmed)) throw new DeleteClusterAbort("Cluster deletion cancelled. No stacks were deleted.");
  }
  if (stack.terminationProtection === true) {
    deps.out(`disabling termination protection for stack: ${stackName}`);
    await deps.disableStackTerminationProtection(stackName);
  }
  if (retainResources === undefined || retainResources.length === 0) {
    deps.out(`terminating CloudFormation stack: ${stackName}`);
    await deps.deleteStack(stackName);
    return;
  }
  deps.out(
    `terminating CloudFormation stack: ${stackName}, retaining ${retainResources.length} resource(s) ` +
      `CloudFormation could not delete: ${retainResources.join(", ")}. These are left in the account and ` +
      `have to be removed by hand.`,
  );
  await deps.deleteStack(stackName, retainResources);
}

/**
 * Waits out one stack group, printing every status it reads so a slow delete is distinguishable
 * from a wedged one.
 *
 * A stack in `DELETE_FAILED` is re-issued. The first re-issue is a plain delete, after the Lambda
 * interface sweep, because an interface holding a security group is the common and recoverable
 * cause. A stack that fails again is re-issued retaining the resources CloudFormation could not
 * delete, which is the only way past a resource whose deletion never succeeds; the retained ids are
 * printed because they stay in the account. The retry counter is shared by the whole invocation.
 */
async function waitForStackDeletion(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  stackNames: string[],
  retryState: { attempts: number },
): Promise<boolean> {
  let failed = 0;
  const pending = [...stackNames];
  const maxAttempts = 3;
  const failures = new Map<string, number>();
  while (pending.length > 0) {
    const deleted: string[] = [];
    for (const stackName of pending) {
      try {
        const stack = await deps.describeStack(stackName);
        if (stack.stackStatus === "DELETE_COMPLETE") {
          deps.out(`stack: ${stackName}, status: ${stack.stackStatus}`);
          deleted.push(stackName);
        } else if (stack.stackStatus === "DELETE_FAILED") {
          const previousFailures = (failures.get(stackName) ?? 0) + 1;
          failures.set(stackName, previousFailures);
          if (retryState.attempts < maxAttempts) {
            deps.err(
              `stack: ${stackName}, status: ${stack.stackStatus}, submitting a new delete request. ` +
                `[Loop ${retryState.attempts}/${maxAttempts}]`,
            );
            await deleteLambdaNetworkInterfaces(deps, options.clusterName);
            const retain = previousFailures > 1 ? await failedResources(deps, stackName) : [];
            await deleteStack(deps, options, stackName, retain);
            retryState.attempts += 1;
          } else {
            deps.err(`stack: ${stackName}, status: ${stack.stackStatus}`);
            deleted.push(stackName);
            failed += 1;
          }
        } else {
          deps.out(`stack: ${stackName}, status: ${stack.stackStatus ?? "unknown"}`);
          // Sweep on every poll of a stack that is still deleting, not only the search stack: a
          // function's interfaces become available part way through its own stack's delete, and the
          // group that owns the network is the one that then waits out a timeout for them.
          await deleteLambdaNetworkInterfaces(deps, options.clusterName);
        }
      } catch (error) {
        if (isMissingStackError(error)) {
          deps.out(`stack: ${stackName}, status: DELETE_COMPLETE`);
          deleted.push(stackName);
        } else {
          throw error;
        }
      }
    }
    for (const stackName of deleted) pending.splice(pending.indexOf(stackName), 1);
    if (pending.length > 0) {
      deps.out(`waiting for ${pending.length} stack(s) to be deleted: ${pending.join(", ")} ...`);
      await deps.sleep(15_000);
    }
  }
  return failed === 0;
}

/** Logical ids CloudFormation reports as `DELETE_FAILED`, empty when the read is unavailable. */
async function failedResources(deps: DeleteClusterDeps, stackName: string): Promise<string[]> {
  if (deps.stackFailedResources === undefined) return [];
  try {
    return await deps.stackFailedResources(stackName);
  } catch (error) {
    deps.err(`could not read failed resources of ${stackName}: ${error instanceof Error ? error.message : String(error)}`);
    return [];
  }
}

/**
 * Removes the cluster's available Lambda network interfaces.
 *
 * A function in the cluster's network leaves its interfaces behind after the function is gone, and
 * each one holds the security group it was launched with. CloudFormation cannot delete a group an
 * interface still references, so it waits out its own timeout and then reports DELETE_FAILED. On a
 * measured run that timeout was 57 minutes, and the same delete finished in 31 seconds once the
 * interfaces were gone.
 */
async function deleteLambdaNetworkInterfaces(deps: DeleteClusterDeps, clusterName: string): Promise<void> {
  const interfaces = await deps.describeLambdaNetworkInterfaces({ clusterName });
  if (interfaces.length === 0) return;
  deps.out(`found ${interfaces.length} available Lambda network interface(s) for the cluster. deleting ...`);
  for (const networkInterface of interfaces) {
    deps.out(
      `deleting Lambda network interface: ${networkInterface.networkInterfaceId}` +
        (networkInterface.description === undefined ? "" : `, description: ${networkInterface.description}`),
    );
    await deps.deleteNetworkInterface(networkInterface.networkInterfaceId);
  }
}

/**
 * Deletes one group of stacks and waits for the group to finish.
 *
 * The interface sweep runs before the first delete is issued, not only on a failure: interfaces
 * left available by an earlier deploy already hold the groups these stacks own, so a sweep after
 * the fact costs the operator the CloudFormation timeout first.
 */
async function deleteStackGroup(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  stacks: DeleteClusterStack[],
  retryState: { attempts: number },
): Promise<void> {
  const stackNames = stacks.map((stack) => stack.stackName);
  if (stackNames.length === 0) return;
  await deleteLambdaNetworkInterfaces(deps, options.clusterName);
  for (const stackName of stackNames) await deleteStack(deps, options, stackName);
  const successful = await waitForStackDeletion(deps, options, stackNames, retryState);
  if (successful) return;
  throw new DeleteClusterAbort(
    `CloudFormation stacks for cluster ${options.clusterName} did not all delete. Later delete steps were not run. Check the stack events, then re-run ideactl delete-cluster --cluster-name ${options.clusterName} --aws-region ${options.awsRegion}.`,
  );
}

async function cleanUpAppModules(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  modules: ModuleInfo[],
): Promise<void> {
  const instanceIds: string[] = [];
  for (const module of modules) {
    if (module.type !== "app") continue;
    const instance = await deps.findAppInstance({ clusterName: options.clusterName, moduleId: module.module_id });
    if (instance?.state === "running") instanceIds.push(instance.instanceId);
  }
  const commandId = await deps.sendAppCleanup({
    instanceIds,
    deleteDatabases: shouldDelete(options, "Databases"),
  });
  for (;;) {
    const statuses = await deps.appCleanupStatus(commandId);
    const complete = statuses.filter((entry) => ["Success", "TimedOut", "Cancelled", "Failed"].includes(entry.status));
    if (complete.length === statuses.length) return;
    await deps.sleep(10_000);
  }
}

async function deleteIdentityProviderStacks(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  identityStacks: DeleteClusterStack[],
  retryState: { attempts: number },
): Promise<void> {
  const userPoolIds: string[] = [];
  let nextToken: string | undefined;
  do {
    const page = await deps.listUserPools(nextToken);
    nextToken = page.nextToken;
    if (options.force !== true) {
      const confirmed = await deps.prompt(
        `Are you sure you want to delete the User Pools associated with the cluster: ${options.clusterName}? This action is not reversible.`,
      );
      if (!isConfirmed(confirmed)) throw new DeleteClusterAbort("Cluster deletion cancelled. No stacks were deleted.");
    }
    for (const pool of page.pools) {
      if (pool.name === `${options.clusterName}-user-pool`) userPoolIds.push(pool.id);
    }
    for (const userPoolId of userPoolIds) {
      const pool = await deps.describeUserPool(userPoolId);
      if (pool.deletionProtection?.toUpperCase() !== "ACTIVE") continue;
      const tags = pool.tags ?? {};
      if (Object.keys(tags).length === 0 || tags[CLUSTER_NAME_TAG] === options.clusterName) {
        await deps.disableUserPoolDeletionProtection(userPoolId);
        await deps.sleep(500);
      }
    }
  } while (nextToken !== undefined && nextToken !== "");

  await deleteStackGroup(deps, options, identityStacks, retryState);
}

/**
 * Purges the cluster's backup vault.
 *
 * The existence probe is allowed to fail for any reason and the purge is then skipped: a cluster
 * installed without backups has no vault, and the service answers a vault that does not exist with
 * an access denial rather than a not-found, so the two are indistinguishable from here and neither
 * is a reason to stop a teardown.
 *
 * A failure once the vault is known to exist is raised. A recovery point that is not deleted keeps
 * the vault, and the vault keeps its stack, so swallowing that error turns a loud failure into a
 * stack delete that fails later for a reason nothing printed.
 */
async function deleteBackups(deps: DeleteClusterDeps, clusterName: string): Promise<void> {
  const vaultName = `${clusterName}-cluster-backup-vault`;
  try {
    await deps.describeBackupVault(vaultName);
  } catch (error) {
    deps.out(
      `backup vault ${vaultName} is not readable (${error instanceof Error ? error.message : String(error)}). skip.`,
    );
    return;
  }
  const recoveryPoints = await deps.listRecoveryPoints(vaultName);
  deps.out(`${recoveryPoints.length} recovery point(s) in vault ${vaultName}`);
  for (const point of recoveryPoints) {
    const status = (point.status ?? "UNKNOWN").toUpperCase();
    if (status !== "COMPLETED" && status !== "EXPIRED") {
      deps.err(`cannot delete recovery point ${point.arn}, status: ${status}. this may block the stack delete.`);
      continue;
    }
    deps.out(`deleting recovery point: ${point.arn} ...`);
    await deps.deleteRecoveryPoint({ backupVaultName: vaultName, recoveryPointArn: point.arn });
    await deps.sleep(100);
  }
}

async function deleteBootstrapAndBucket(
  deps: DeleteClusterDeps,
  options: DeleteClusterOptions,
  config: ClusterConfig | undefined,
): Promise<void> {
  await deleteStack(deps, options, `${options.clusterName}-bootstrap`);
  const configuredBucket = config?.getString("cluster.cluster_s3_bucket", undefined, { moduleId: "cluster" });
  const bucketName = configuredBucket ?? `${options.clusterName}-cluster-${options.awsRegion}-${await deps.accountId()}`;
  if (!(await deps.bucketExists(bucketName))) {
    deps.out(`cluster bucket not found: ${bucketName}. skip.`);
    return;
  }
  deps.out(`deleting S3 bucket: ${bucketName} ...`);
  await deps.deleteAllBucketObjectVersions(bucketName);
  await deps.sleep(5_000);
  await deps.deleteBucket(bucketName);
}

async function deleteDatabases(deps: DeleteClusterDeps, options: DeleteClusterOptions): Promise<void> {
  const tables: string[] = [];
  let nextTableName: string | undefined;
  do {
    const page = await deps.listTables(nextTableName);
    nextTableName = page.nextTableName;
    tables.push(...page.tableNames.filter((name) => name.startsWith(`${options.clusterName}.`)));
  } while (nextTableName !== undefined && nextTableName !== "");
  if (tables.length === 0) return;

  if (options.force !== true) {
    const confirmed = await deps.prompt(`Are you sure you want to delete all dynamodb tables associated with the cluster: ${options.clusterName}?`);
    if (!isConfirmed(confirmed)) return;
  }
  for (const tableName of tables) {
    deps.out(`deleting table: ${tableName} ...`);
    await deps.deleteTable(tableName);
  }

  const alarmNames = (await deps.listDynamoDbAlarms(options.clusterName))
    .filter((alarm) => alarm.namespace === "AWS/DynamoDB" && alarm.tableName !== undefined && tables.includes(alarm.tableName))
    .map((alarm) => alarm.name);
  if (alarmNames.length > 0) deps.out(`deleting ${alarmNames.length} cloudwatch alarm(s) ...`);
  for (let index = 0; index < alarmNames.length; index += 100) {
    await deps.deleteAlarms(alarmNames.slice(index, index + 100));
  }
}

async function deleteCloudwatchLogs(deps: DeleteClusterDeps, options: DeleteClusterOptions): Promise<void> {
  const logs: Array<{ name: string; size: number }> = [];
  for (const prefix of [options.clusterName, `/${options.clusterName}`, `/aws/lambda/${options.clusterName}`]) {
    logs.push(...(await deps.listLogGroups(prefix)));
  }
  if (logs.length === 0) return;
  if (options.force !== true) {
    const confirmed = await deps.prompt(`Are you sure you want to delete all cloudwatch logs associated with the cluster: ${options.clusterName}?`);
    if (!isConfirmed(confirmed)) return;
  }
  for (const log of logs) {
    deps.out(`deleting cloudwatch log group: ${log.name} ...`);
    await deps.deleteLogGroup(log.name);
    await deps.sleep(100);
  }
}

export class DeleteClusterAbort extends Error {}

/**
 * Runs the documented 13-step removal sequence. Every discovered stack is scoped by the cluster
 * tag, and the retained bootstrap bucket is reached only in the explicit bootstrap branch.
 */
export async function deleteCluster(deps: DeleteClusterDeps, options: DeleteClusterOptions): Promise<void> {
  deps.out(`deleting cluster: ${options.clusterName}, region: ${options.awsRegion}`);
  const config = await deps.loadConfig(options);
  const modules = config?.modules() ?? [];
  if (config === undefined) deps.out("no cluster settings tables found. using generated resource names.");
  deps.out(`searching for EC2 instances tagged ${CLUSTER_NAME_TAG}=${options.clusterName} ...`);
  const instances = await deps.findInstances({
    filters: [{ name: `tag:${CLUSTER_NAME_TAG}`, values: [options.clusterName] }],
  });
  const ec2Instances: DeleteClusterInstance[] = [];
  const protectedInstances: DeleteClusterInstance[] = [];
  const groupInstances: DeleteClusterInstance[] = [];
  for (const instance of instances) {
    if (instance.state === "terminated") continue;
    // A member of an auto scaling group is not ours to terminate. Terminating one while its group
    // still wants that many members has the group replace it, which is the group doing its job
    // and this command losing a race it started. The stack delete removes the group, and the
    // group removes its members, once the container stack's release has cleared their scale-in
    // protection. Terminating early is a harmless head start only for a standalone instance,
    // which is what every instance in a cluster used to be.
    if (instance.autoScalingGroupName !== undefined) {
      groupInstances.push(instance);
      continue;
    }
    if (await deps.instanceTerminationProtection(instance.instanceId)) protectedInstances.push(instance);
    await deps.sleep(100);
    if (instance.nodeType !== APP_NODE_TYPE && instance.nodeType !== INFRA_NODE_TYPE) ec2Instances.push(instance);
  }
  for (const instance of groupInstances) {
    deps.out(
      `instance left to its auto scaling group: ${instance.instanceId}, group: ${instance.autoScalingGroupName ?? "unknown"}`,
    );
  }

  for (const instance of ec2Instances) deps.out(`instance to terminate: ${instance.instanceId}, state: ${instance.state}`);
  for (const instance of protectedInstances) deps.out(`instance with termination protection: ${instance.instanceId}`);

  deps.out(`searching for CloudFormation stacks tagged ${CLUSTER_NAME_TAG}=${options.clusterName} ...`);
  const regularStacks: DeleteClusterStack[] = [];
  const clusterStacks: DeleteClusterStack[] = [];
  const identityStacks: DeleteClusterStack[] = [];
  let paginationToken: string | undefined;
  do {
    const page = await deps.getTaggedStacks({
      tagFilters: [{ key: CLUSTER_NAME_TAG, values: [options.clusterName] }],
      resourceTypeFilters: ["cloudformation"],
      paginationToken,
    });
    paginationToken = page.paginationToken;
    for (const stackId of page.stacks) {
      try {
        const stack = await deps.describeStack(stackId);
        if (stack.stackName === `${options.clusterName}-bootstrap`) continue;
        if (stackNameMatches("cluster", stack, options.clusterName, modules)) {
          clusterStacks.push(stack);
        } else if (stackNameMatches("identity-provider", stack, options.clusterName, modules)) {
          identityStacks.push(stack);
        } else {
          regularStacks.push(stack);
        }
        await deps.sleep(500);
      } catch (error) {
        if (!isMissingStackError(error)) throw error;
      }
    }
  } while (paginationToken !== undefined && paginationToken !== "");
  for (const stack of [...regularStacks, ...identityStacks, ...clusterStacks]) {
    deps.out(
      `stack to delete: ${stack.stackName}, status: ${stack.stackStatus ?? "unknown"}, ` +
        `termination protection: ${stack.terminationProtection === true}`,
    );
  }
  deps.out(`${regularStacks.length + identityStacks.length + clusterStacks.length} stack(s) will be terminated.`);

  if (options.force !== true) {
    const confirmed = await deps.prompt(`Are you sure you want to delete cluster: ${options.clusterName}, region: ${options.awsRegion} ?`);
    if (!isConfirmed(confirmed)) return;
  }

  deps.out("running the application module clean-up ...");
  await cleanUpAppModules(deps, options, modules);
  if (protectedInstances.length > 0 && options.force !== true) {
    const confirmed = await deps.prompt("Are you sure you want to disable termination protection for above instances ?");
    if (!isConfirmed(confirmed)) return;
  }
  for (const instance of protectedInstances) {
    deps.out(`disabling termination protection for EC2 instance: ${instance.instanceId} ...`);
    await deps.disableInstanceTerminationProtection(instance.instanceId);
    await deps.sleep(1_000);
  }
  for (const instance of ec2Instances) {
    deps.out(`terminating EC2 instance: ${instance.instanceId}`);
    await deps.terminateInstance({
      instanceId: instance.instanceId,
      force: options.force === true,
      skipOsShutdown: options.force === true,
    });
    await deps.sleep(1_000);
  }

  deps.out("searching for project resources CloudFormation does not own ...");
  const projects = await deps.findBedrockProjects(options.clusterName);
  try {
    if (projects.length > 0) await deps.deleteBedrockProjectResources({ clusterName: options.clusterName, projects });
  } catch (error) {
    deps.err(`failed to delete bedrock project resources: ${error instanceof Error ? error.message : String(error)}`);
  }

  const retryState = { attempts: 0 };
  deps.out(`deleting ${regularStacks.length} module stack(s) ...`);
  await deleteStackGroup(deps, options, regularStacks, retryState);

  deps.out(`deleting ${identityStacks.length} identity-provider stack(s) ...`);
  await deleteIdentityProviderStacks(deps, options, identityStacks, retryState);

  if (shouldDelete(options, "Backups")) {
    if (options.force === true || isConfirmed(await deps.prompt(`Are you sure you want to delete all the backup recovery points associated with the cluster: ${options.clusterName}?`))) {
      await deleteBackups(deps, options.clusterName);
    }
  }

  deps.out(`deleting ${clusterStacks.length} cluster stack(s) ...`);
  await deleteStackGroup(deps, options, clusterStacks, retryState);

  if (shouldDelete(options, "Bootstrap")) {
    if (options.force === true || isConfirmed(await deps.prompt(`Are you sure you want to delete the bootstrap stack and S3 Bucket associated with the cluster: ${options.clusterName}? This action is not reversible.`))) {
      await deleteBootstrapAndBucket(deps, options, config);
    }
  }
  if (shouldDelete(options, "Databases")) await deleteDatabases(deps, options);
  if (shouldDelete(options, "CloudwatchLogs")) await deleteCloudwatchLogs(deps, options);
}

/** `delete-backups` is the same vault purge, preceded by its own confirmation. */
export async function deleteBackupsCommand(
  deps: DeleteClusterDeps,
  options: Pick<DeleteClusterOptions, "clusterName" | "force">,
): Promise<void> {
  if (options.force !== true) {
    const confirmed = await deps.prompt(`Are you sure you want to delete all the backup recovery points for cluster ${options.clusterName} ?`);
    if (!isConfirmed(confirmed)) return;
  }
  await deleteBackups(deps, options.clusterName);
}

/** Registers the two destructive commands on the command program supplied by CLI core. */
export function registerDeleteClusterCommands(program: Command, deps: DeleteClusterDepsSource): void {
  const resolveDeps = async (
    options: DeleteClusterOptions,
    command: "delete-cluster" | "delete-backups",
  ): Promise<DeleteClusterDeps> => typeof deps === "function" ? deps(options, command) : deps;
  program
    .command("delete-cluster")
    .description("delete cluster")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .option("--delete-bootstrap", "Delete Bootstrap and S3 bucket")
    .option("--delete-databases", "Delete Databases")
    .option("--delete-backups", "Delete Backups")
    .option("--delete-cloudwatch-logs", "Delete CloudWatch Logs")
    .option("--delete-all", "Delete all")
    .option("--force", "Skip confirmation prompts")
    .action(async (options: DeleteClusterOptions) => {
      await deleteCluster(await resolveDeps(options, "delete-cluster"), options);
    });

  program
    .command("delete-backups")
    .description("delete all recovery points in the cluster's backup vault")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .option("--force", "Skip confirmation prompts")
    .action(async (options: DeleteClusterOptions) => {
      await deleteBackupsCommand(await resolveDeps(options, "delete-backups"), options);
    });
}
