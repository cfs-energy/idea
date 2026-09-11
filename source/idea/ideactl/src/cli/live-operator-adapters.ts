/**
 * Live dependency factories for the operator command groups.
 *
 * Each factory receives the action options after parsing, so configuration
 * reads and every SDK client use the profile selected for that action.
 */

import { spawn } from "node:child_process";
import { basename, dirname, join } from "node:path";

import yaml from "js-yaml";

import { ClusterConfig, ClusterConfigError, GeneralException } from "../config/cluster-config.ts";
import { awsClientOptions } from "./aws-client-options.ts";
import type { Deps } from "./cdk-invoker.ts";
import {
  CLUSTER_NAME_TAG,
  MODULE_ID_TAG,
  AUTOSCALING_GROUP_TAG,
  NODE_TYPE_TAG,
  type DeleteClusterDeps,
  type DeleteClusterDepsFactory,
  type DeleteClusterInstance,
} from "./commands/delete-cluster.ts";
import type { RemainingOperatorCommandDeps } from "./commands/utils.ts";

interface AwsActionOptions {
  clusterName?: string;
  awsRegion?: string;
  awsProfile?: string;
  moduleSet?: string;
}

function requiredRegion(options: AwsActionOptions): string {
  if (options.awsRegion === undefined || options.awsRegion.trim() === "") {
    throw new GeneralException("This command needs --aws-region (for example us-east-2). Pass it on the command line.");
  }
  return options.awsRegion;
}

const clientOptions = (options: AwsActionOptions) =>
  awsClientOptions(requiredRegion(options), options.awsProfile);

async function loadConfig(deps: Deps, options: AwsActionOptions): Promise<ClusterConfig> {
  if (options.clusterName === undefined || options.clusterName.trim() === "") {
    throw new GeneralException("This command needs --cluster-name. Pass it on the command line.");
  }
  return ClusterConfig.fromDynamoDb(options.clusterName, requiredRegion(options), {
    moduleSet: options.moduleSet,
    scan: deps.scan,
  });
}

async function scanAll(
  deps: Deps,
  tableName: string,
): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let key: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: tableName, ExclusiveStartKey: key });
    rows.push(...(page.Items ?? []));
    key = page.LastEvaluatedKey;
  } while (key !== undefined);
  return rows;
}

async function archiveDirectory(directory: string): Promise<string> {
  const archive = `${directory}.tar.gz`;
  await new Promise<void>((resolve, reject) => {
    const process = spawn("tar", ["-czf", archive, "-C", dirname(directory), basename(directory)]);
    process.once("error", reject);
    process.once("close", (code) => {
      if (code === 0) resolve();
      else reject(new GeneralException(`could not create support archive: tar exited with ${code ?? 1}`));
    });
  });
  return archive;
}

/**
 * Live dependencies for `delete-cluster` and `delete-backups`.
 *
 * Every client is built on first use from the one options helper the other command groups use, so
 * the profile and region the action selected are the only credential path. The per-service
 * `lazy...` helpers exist because this command reaches nine services: they hold the dynamic import
 * and the client construction in one place instead of repeating both in thirty methods.
 */
async function deleteClusterDeps(
  deps: Deps,
  options: AwsActionOptions,
): Promise<DeleteClusterDeps> {
  const lazyEc2 = async () => {
    const sdk = await import("@aws-sdk/client-ec2");
    return { sdk, client: new sdk.EC2Client(await clientOptions(options)) };
  };
  const lazyCfn = async () => {
    const sdk = await import("@aws-sdk/client-cloudformation");
    return { sdk, client: new sdk.CloudFormationClient(await clientOptions(options)) };
  };
  const lazySsm = async () => {
    const sdk = await import("@aws-sdk/client-ssm");
    return { sdk, client: new sdk.SSMClient(await clientOptions(options)) };
  };
  const lazyCognito = async () => {
    const sdk = await import("@aws-sdk/client-cognito-identity-provider");
    return { sdk, client: new sdk.CognitoIdentityProviderClient(await clientOptions(options)) };
  };
  const lazyDynamoDb = async () => {
    const sdk = await import("@aws-sdk/client-dynamodb");
    return { sdk, client: new sdk.DynamoDBClient(await clientOptions(options)) };
  };
  const lazyCloudWatch = async () => {
    const sdk = await import("@aws-sdk/client-cloudwatch");
    return { sdk, client: new sdk.CloudWatchClient(await clientOptions(options)) };
  };
  const lazyLogs = async () => {
    const sdk = await import("@aws-sdk/client-cloudwatch-logs");
    return { sdk, client: new sdk.CloudWatchLogsClient(await clientOptions(options)) };
  };
  const lazyS3 = async () => {
    const sdk = await import("@aws-sdk/client-s3");
    return { sdk, client: new sdk.S3Client(await clientOptions(options)) };
  };
  const lazyIam = async () => {
    const sdk = await import("@aws-sdk/client-iam");
    return { sdk, client: new sdk.IAMClient(await clientOptions(options)) };
  };

  /** `ResourceNotFoundException` for a table this cluster never created. */
  const isMissingTable = (error: unknown): boolean =>
    (error as { name?: string }).name === "ResourceNotFoundException";

  return {
    async loadConfig(input) {
      // A cluster whose settings tables are already gone is still deletable: the command falls
      // back to the generated names for every value it would have read.
      try {
        return await ClusterConfig.fromDynamoDb(input.clusterName, requiredRegion(options), {
          moduleSet: options.moduleSet,
          scan: deps.scan,
        });
      } catch (error) {
        // `ClusterConfigError` is what a cluster with no settings tables raises, which is the
        // normal state of a half-deleted cluster and the one this command has to survive.
        if (isMissingTable(error) || error instanceof ClusterConfigError || error instanceof GeneralException) {
          return undefined;
        }
        throw error;
      }
    },
    async findInstances(input) {
      const { sdk, client } = await lazyEc2();
      const found: DeleteClusterInstance[] = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(
          new sdk.DescribeInstancesCommand({
            Filters: input.filters.map((filter) => ({ Name: filter.name, Values: filter.values })),
            NextToken: nextToken,
          }),
        );
        nextToken = result.NextToken;
        for (const reservation of result.Reservations ?? []) {
          for (const instance of reservation.Instances ?? []) {
            if (instance.InstanceId === undefined) continue;
            found.push({
              instanceId: instance.InstanceId,
              state: instance.State?.Name ?? "unknown",
              nodeType: instance.Tags?.find((tag) => tag.Key === NODE_TYPE_TAG)?.Value,
              // Set by the platform on every instance an auto scaling group launches, and already
              // in this response, so telling a group member from a standalone instance costs no
              // extra call.
              autoScalingGroupName: instance.Tags?.find((tag) => tag.Key === AUTOSCALING_GROUP_TAG)?.Value,
            });
          }
        }
      } while (nextToken !== undefined && nextToken !== "");
      return found;
    },
    async instanceTerminationProtection(instanceId) {
      const { sdk, client } = await lazyEc2();
      const result = await client.send(
        new sdk.DescribeInstanceAttributeCommand({ InstanceId: instanceId, Attribute: "disableApiTermination" }),
      );
      return result.DisableApiTermination?.Value === true;
    },
    async disableInstanceTerminationProtection(instanceId) {
      const { sdk, client } = await lazyEc2();
      await client.send(
        new sdk.ModifyInstanceAttributeCommand({ InstanceId: instanceId, DisableApiTermination: { Value: false } }),
      );
    },
    async terminateInstance(input) {
      const { sdk, client } = await lazyEc2();
      await client.send(
        new sdk.TerminateInstancesCommand({
          InstanceIds: [input.instanceId],
          ...(input.force ? { Force: true, SkipOsShutdown: input.skipOsShutdown } : {}),
        }),
      );
    },
    async getTaggedStacks(input) {
      const sdk = await import("@aws-sdk/client-resource-groups-tagging-api");
      const client = new sdk.ResourceGroupsTaggingAPIClient(await clientOptions(options));
      const result = await client.send(
        new sdk.GetResourcesCommand({
          TagFilters: input.tagFilters.map((filter) => ({ Key: filter.key, Values: filter.values })),
          ResourceTypeFilters: input.resourceTypeFilters,
          PaginationToken: input.paginationToken,
        }),
      );
      return {
        stacks: (result.ResourceTagMappingList ?? []).flatMap((resource) =>
          resource.ResourceARN === undefined ? [] : [resource.ResourceARN],
        ),
        paginationToken: result.PaginationToken,
      };
    },
    async describeStack(stackName) {
      const { sdk, client } = await lazyCfn();
      const result = await client.send(new sdk.DescribeStacksCommand({ StackName: stackName }));
      const stack = result.Stacks?.[0];
      if (stack?.StackName === undefined) throw new GeneralException(`stack not found: ${stackName}`);
      return {
        stackName: stack.StackName,
        stackStatus: stack.StackStatus,
        terminationProtection: stack.EnableTerminationProtection,
      };
    },
    async stackFailedResources(stackName) {
      const { sdk, client } = await lazyCfn();
      const failed: string[] = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(
          new sdk.ListStackResourcesCommand({ StackName: stackName, NextToken: nextToken }),
        );
        nextToken = result.NextToken;
        for (const resource of result.StackResourceSummaries ?? []) {
          if (resource.ResourceStatus === "DELETE_FAILED" && resource.LogicalResourceId !== undefined) {
            failed.push(resource.LogicalResourceId);
          }
        }
      } while (nextToken !== undefined && nextToken !== "");
      return failed;
    },
    async disableStackTerminationProtection(stackName) {
      const { sdk, client } = await lazyCfn();
      await client.send(
        new sdk.UpdateTerminationProtectionCommand({ StackName: stackName, EnableTerminationProtection: false }),
      );
    },
    async deleteStack(stackName, retainResources) {
      const { sdk, client } = await lazyCfn();
      await client.send(
        new sdk.DeleteStackCommand({
          StackName: stackName,
          ...(retainResources === undefined || retainResources.length === 0
            ? {}
            : { RetainResources: retainResources }),
        }),
      );
    },
    async findAppInstance(input) {
      const { sdk, client } = await lazyEc2();
      const result = await client.send(
        new sdk.DescribeInstancesCommand({
          Filters: [
            { Name: "instance-state-name", Values: ["pending", "stopped", "running"] },
            { Name: `tag:${CLUSTER_NAME_TAG}`, Values: [input.clusterName] },
            { Name: `tag:${MODULE_ID_TAG}`, Values: [input.moduleId] },
            { Name: `tag:${NODE_TYPE_TAG}`, Values: ["app"] },
          ],
        }),
      );
      for (const reservation of result.Reservations ?? []) {
        for (const instance of reservation.Instances ?? []) {
          if (instance.InstanceId !== undefined && instance.State?.Name === "running") {
            return { instanceId: instance.InstanceId, state: "running" };
          }
        }
      }
      return undefined;
    },
    async sendAppCleanup(input) {
      // SendCommand rejects an empty target list, and a cluster whose application hosts are
      // already gone has nothing to clean up. The empty command id reads back as no invocations.
      if (input.instanceIds.length === 0) return "";
      const { sdk, client } = await lazySsm();
      const command = `sudo ideactl app-module-clean-up${input.deleteDatabases ? " --delete-databases" : ""}`;
      const result = await client.send(
        new sdk.SendCommandCommand({
          InstanceIds: input.instanceIds,
          DocumentName: "AWS-RunShellScript",
          Parameters: { commands: [command] },
        }),
      );
      const commandId = result.Command?.CommandId;
      if (commandId === undefined) throw new GeneralException("ssm:SendCommand returned no command id");
      return commandId;
    },
    async appCleanupStatus(commandId) {
      if (commandId === "") return [];
      const { sdk, client } = await lazySsm();
      const result = await client.send(
        new sdk.ListCommandInvocationsCommand({ CommandId: commandId, Details: false }),
      );
      return (result.CommandInvocations ?? []).map((invocation) => ({ status: invocation.Status ?? "Pending" }));
    },
    async findBedrockProjects(cluster) {
      const { DynamoDBDocumentClient, ScanCommand } = await import("@aws-sdk/lib-dynamodb");
      const { client } = await lazyDynamoDb();
      const document = DynamoDBDocumentClient.from(client);
      const projects: Array<Record<string, unknown>> = [];
      let startKey: Record<string, unknown> | undefined;
      try {
        do {
          const page = await document.send(
            new ScanCommand({ TableName: `${cluster}.projects`, ExclusiveStartKey: startKey }),
          );
          startKey = page.LastEvaluatedKey;
          for (const item of page.Items ?? []) {
            const bedrock = (item as { bedrock?: Record<string, unknown> }).bedrock ?? {};
            const hasRole = typeof bedrock["role_arn"] === "string" && bedrock["role_arn"] !== "";
            const hasProfile =
              typeof bedrock["instance_profile_arn"] === "string" && bedrock["instance_profile_arn"] !== "";
            const inference = bedrock["inference_profile_arns"];
            const hasInference =
              typeof inference === "object" && inference !== null && Object.keys(inference).length > 0;
            if (hasRole || hasProfile || hasInference) projects.push(item);
          }
        } while (startKey !== undefined);
      } catch (error) {
        if (isMissingTable(error)) return [];
        throw error;
      }
      return projects;
    },
    async deleteBedrockProjectResources(input) {
      const { sdk, client: iam } = await lazyIam();
      // Every step is best effort: the command reports a failure and carries on to the stack
      // delete, where the boundary-detaching custom resource clears what blocks a policy delete.
      const attempt = async (description: string, call: () => Promise<unknown>): Promise<void> => {
        try {
          await call();
          deps.out(description);
        } catch (error) {
          deps.err(`${description} failed: ${error instanceof Error ? error.message : String(error)}`);
        }
      };
      const inferenceProfileArns: string[] = [];
      for (const project of input.projects) {
        const bedrock = (project as { bedrock?: Record<string, unknown> }).bedrock ?? {};
        const roleArn = typeof bedrock["role_arn"] === "string" ? bedrock["role_arn"] : "";
        const instanceProfileArn =
          typeof bedrock["instance_profile_arn"] === "string" ? bedrock["instance_profile_arn"] : "";
        const roleName = roleArn === "" ? undefined : roleArn.split("/").pop();
        const instanceProfileName = instanceProfileArn === "" ? undefined : instanceProfileArn.split("/").pop();
        const inference = bedrock["inference_profile_arns"];
        if (typeof inference === "object" && inference !== null) {
          for (const value of Object.values(inference as Record<string, unknown>)) {
            if (typeof value === "string" && value !== "") inferenceProfileArns.push(value);
          }
        }

        if (roleName !== undefined && roleName !== "") {
          try {
            const attached = await iam.send(new sdk.ListAttachedRolePoliciesCommand({ RoleName: roleName }));
            for (const policy of attached.AttachedPolicies ?? []) {
              if (policy.PolicyArn === undefined) continue;
              await attempt(`detached ${policy.PolicyArn} from role ${roleName}`, () =>
                iam.send(new sdk.DetachRolePolicyCommand({ RoleName: roleName, PolicyArn: policy.PolicyArn })),
              );
            }
          } catch (error) {
            deps.err(`could not list policies of role ${roleName}: ${error instanceof Error ? error.message : String(error)}`);
          }
        }

        if (instanceProfileName !== undefined && instanceProfileName !== "") {
          try {
            const profile = await iam.send(
              new sdk.GetInstanceProfileCommand({ InstanceProfileName: instanceProfileName }),
            );
            for (const role of profile.InstanceProfile?.Roles ?? []) {
              if (role.RoleName === undefined) continue;
              await attempt(`removed role ${role.RoleName} from instance profile ${instanceProfileName}`, () =>
                iam.send(
                  new sdk.RemoveRoleFromInstanceProfileCommand({
                    InstanceProfileName: instanceProfileName,
                    RoleName: role.RoleName,
                  }),
                ),
              );
            }
          } catch (error) {
            deps.err(`could not read instance profile ${instanceProfileName}: ${error instanceof Error ? error.message : String(error)}`);
          }
          await attempt(`deleted instance profile ${instanceProfileName}`, () =>
            iam.send(new sdk.DeleteInstanceProfileCommand({ InstanceProfileName: instanceProfileName })),
          );
        }

        if (roleName !== undefined && roleName !== "") {
          await attempt(`deleted role ${roleName}`, () => iam.send(new sdk.DeleteRoleCommand({ RoleName: roleName })));
        }
      }

      // Every policy under the cluster's project path, including one whose role is already gone.
      let marker: string | undefined;
      do {
        const page = await iam.send(
          new sdk.ListPoliciesCommand({
            PathPrefix: `/idea/${input.clusterName}/projects/`,
            Scope: "Local",
            Marker: marker,
          }),
        );
        marker = page.IsTruncated === true ? page.Marker : undefined;
        for (const policy of page.Policies ?? []) {
          const policyArn = policy.Arn;
          if (policyArn === undefined) continue;
          try {
            const versions = await iam.send(new sdk.ListPolicyVersionsCommand({ PolicyArn: policyArn }));
            for (const version of versions.Versions ?? []) {
              if (version.IsDefaultVersion === true || version.VersionId === undefined) continue;
              await attempt(`deleted policy version ${version.VersionId} of ${policyArn}`, () =>
                iam.send(new sdk.DeletePolicyVersionCommand({ PolicyArn: policyArn, VersionId: version.VersionId })),
              );
            }
          } catch (error) {
            deps.err(`could not list versions of ${policyArn}: ${error instanceof Error ? error.message : String(error)}`);
          }
          await attempt(`deleted policy ${policyArn}`, () =>
            iam.send(new sdk.DeletePolicyCommand({ PolicyArn: policyArn })),
          );
        }
      } while (marker !== undefined && marker !== "");

      if (inferenceProfileArns.length > 0) {
        const bedrockSdk = await import("@aws-sdk/client-bedrock");
        const bedrock = new bedrockSdk.BedrockClient(await clientOptions(options));
        for (const profileArn of inferenceProfileArns) {
          await attempt(`deleted inference profile ${profileArn}`, () =>
            bedrock.send(
              new bedrockSdk.DeleteInferenceProfileCommand({ inferenceProfileIdentifier: profileArn.split("/").pop() }),
            ),
          );
        }
      }
    },
    async listUserPools(nextToken) {
      const { sdk, client } = await lazyCognito();
      const result = await client.send(new sdk.ListUserPoolsCommand({ MaxResults: 50, NextToken: nextToken }));
      return {
        pools: (result.UserPools ?? []).flatMap((pool) =>
          pool.Id === undefined ? [] : [{ id: pool.Id, name: pool.Name ?? "" }],
        ),
        nextToken: result.NextToken,
      };
    },
    async describeUserPool(userPoolId) {
      const { sdk, client } = await lazyCognito();
      const result = await client.send(new sdk.DescribeUserPoolCommand({ UserPoolId: userPoolId }));
      return {
        deletionProtection: result.UserPool?.DeletionProtection,
        tags: result.UserPool?.UserPoolTags,
      };
    },
    async disableUserPoolDeletionProtection(userPoolId) {
      const { sdk, client } = await lazyCognito();
      await client.send(new sdk.UpdateUserPoolCommand({ UserPoolId: userPoolId, DeletionProtection: "INACTIVE" }));
    },
    async describeLambdaNetworkInterfaces(input) {
      const { sdk, client } = await lazyEc2();
      const found: Array<{ networkInterfaceId: string; description?: string }> = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(
          new sdk.DescribeNetworkInterfacesCommand({
            Filters: [
              { Name: "description", Values: [`AWS Lambda VPC ENI-${input.clusterName}-*`] },
              { Name: "status", Values: ["available"] },
            ],
            NextToken: nextToken,
          }),
        );
        nextToken = result.NextToken;
        for (const networkInterface of result.NetworkInterfaces ?? []) {
          if (networkInterface.NetworkInterfaceId === undefined) continue;
          found.push({
            networkInterfaceId: networkInterface.NetworkInterfaceId,
            description: networkInterface.Description,
          });
        }
      } while (nextToken !== undefined && nextToken !== "");
      return found;
    },
    async deleteNetworkInterface(networkInterfaceId) {
      const { sdk, client } = await lazyEc2();
      await client.send(new sdk.DeleteNetworkInterfaceCommand({ NetworkInterfaceId: networkInterfaceId }));
    },
    async describeBackupVault(backupVaultName) {
      const { BackupClient, DescribeBackupVaultCommand } = await import("@aws-sdk/client-backup");
      await new BackupClient(await clientOptions(options)).send(new DescribeBackupVaultCommand({ BackupVaultName: backupVaultName }));
    },
    async listRecoveryPoints(backupVaultName) {
      const { BackupClient, ListRecoveryPointsByBackupVaultCommand } = await import("@aws-sdk/client-backup");
      const result = await new BackupClient(await clientOptions(options)).send(
        new ListRecoveryPointsByBackupVaultCommand({ BackupVaultName: backupVaultName }),
      );
      return (result.RecoveryPoints ?? []).flatMap((point) =>
        point.RecoveryPointArn === undefined ? [] : [{ arn: point.RecoveryPointArn, status: point.Status }],
      );
    },
    async deleteRecoveryPoint(input) {
      const { BackupClient, DeleteRecoveryPointCommand } = await import("@aws-sdk/client-backup");
      await new BackupClient(await clientOptions(options)).send(
        new DeleteRecoveryPointCommand({ BackupVaultName: input.backupVaultName, RecoveryPointArn: input.recoveryPointArn }),
      );
    },
    async listTables(nextTableName) {
      const { sdk, client } = await lazyDynamoDb();
      const result = await client.send(new sdk.ListTablesCommand({ ExclusiveStartTableName: nextTableName }));
      return { tableNames: result.TableNames ?? [], nextTableName: result.LastEvaluatedTableName };
    },
    async deleteTable(tableName) {
      const { sdk, client } = await lazyDynamoDb();
      await client.send(new sdk.DeleteTableCommand({ TableName: tableName }));
    },
    async listDynamoDbAlarms(cluster) {
      const { sdk, client } = await lazyCloudWatch();
      const alarms: Array<{ name: string; namespace: string; tableName?: string }> = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(
          new sdk.DescribeAlarmsCommand({
            AlarmNamePrefix: `TargetTracking-table/${cluster}`,
            NextToken: nextToken,
          }),
        );
        nextToken = result.NextToken;
        for (const alarm of result.MetricAlarms ?? []) {
          if (alarm.AlarmName === undefined) continue;
          alarms.push({
            name: alarm.AlarmName,
            namespace: alarm.Namespace ?? "unknown-namespace",
            tableName: alarm.Dimensions?.find((dimension) => dimension.Name === "TableName")?.Value,
          });
        }
      } while (nextToken !== undefined && nextToken !== "");
      return alarms;
    },
    async deleteAlarms(alarmNames) {
      if (alarmNames.length === 0) return;
      const { sdk, client } = await lazyCloudWatch();
      await client.send(new sdk.DeleteAlarmsCommand({ AlarmNames: alarmNames }));
    },
    async listLogGroups(prefix) {
      const { sdk, client } = await lazyLogs();
      const groups: Array<{ name: string; size: number }> = [];
      let nextToken: string | undefined;
      do {
        const result = await client.send(
          new sdk.DescribeLogGroupsCommand({ logGroupNamePrefix: prefix, nextToken }),
        );
        nextToken = result.nextToken;
        for (const group of result.logGroups ?? []) {
          if (group.logGroupName === undefined) continue;
          groups.push({ name: group.logGroupName, size: group.storedBytes ?? 0 });
        }
      } while (nextToken !== undefined && nextToken !== "");
      return groups;
    },
    async deleteLogGroup(name) {
      const { sdk, client } = await lazyLogs();
      await client.send(new sdk.DeleteLogGroupCommand({ logGroupName: name }));
    },
    accountId: deps.accountId,
    async bucketExists(name) {
      const { sdk, client } = await lazyS3();
      try {
        await client.send(new sdk.HeadBucketCommand({ Bucket: name }));
        return true;
      } catch (error) {
        const status = (error as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode;
        if (status === 404 || (error as { name?: string }).name === "NotFound") return false;
        throw error;
      }
    },
    async deleteAllBucketObjectVersions(name) {
      const { sdk, client } = await lazyS3();
      let keyMarker: string | undefined;
      let versionIdMarker: string | undefined;
      let deleted = 0;
      do {
        const page = await client.send(
          new sdk.ListObjectVersionsCommand({ Bucket: name, KeyMarker: keyMarker, VersionIdMarker: versionIdMarker }),
        );
        keyMarker = page.IsTruncated === true ? page.NextKeyMarker : undefined;
        versionIdMarker = page.IsTruncated === true ? page.NextVersionIdMarker : undefined;
        const objects = [...(page.Versions ?? []), ...(page.DeleteMarkers ?? [])].flatMap((entry) =>
          entry.Key === undefined ? [] : [{ Key: entry.Key, VersionId: entry.VersionId }],
        );
        // DeleteObjects takes at most 1000 keys per call.
        for (let index = 0; index < objects.length; index += 1000) {
          await client.send(
            new sdk.DeleteObjectsCommand({
              Bucket: name,
              Delete: { Objects: objects.slice(index, index + 1000), Quiet: true },
            }),
          );
        }
        deleted += objects.length;
      } while (keyMarker !== undefined || versionIdMarker !== undefined);
      deps.out(`deleted ${deleted} object version(s) from bucket: ${name}`);
    },
    async deleteBucket(name) {
      const { sdk, client } = await lazyS3();
      await client.send(new sdk.DeleteBucketCommand({ Bucket: name }));
    },
    prompt: async (message) => deps.prompt({ message, default: false }),
    sleep: deps.sleep,
    out: deps.out,
    err: deps.err,
  };
}

export function createLiveDeleteClusterDeps(deps: Deps): DeleteClusterDepsFactory {
  return async (options) => deleteClusterDeps(deps, options);
}

/** Create all runnable operator adapters with a profile-local AWS credential provider. */
export function createLiveRemainingOperatorDeps(deps: Deps): RemainingOperatorCommandDeps {
  return {
    sso: async (options) => {
      const config = await loadConfig(deps, options);
      return {
        config,
        cognito: {
          async getIdentityProviderByIdentifier(input) {
            const { CognitoIdentityProviderClient, GetIdentityProviderByIdentifierCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            const result = await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new GetIdentityProviderByIdentifierCommand(input as never),
            );
            return result.IdentityProvider === undefined ? {} : { IdentityProvider: {} };
          },
          async createIdentityProvider(input) {
            const { CognitoIdentityProviderClient, CreateIdentityProviderCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new CreateIdentityProviderCommand(input as never),
            );
          },
          async updateIdentityProvider(input) {
            const { CognitoIdentityProviderClient, UpdateIdentityProviderCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new UpdateIdentityProviderCommand(input as never),
            );
          },
          async createUserPoolClient(input) {
            const { CognitoIdentityProviderClient, CreateUserPoolClientCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            const result = await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new CreateUserPoolClientCommand(input as never),
            );
            return {
              UserPoolClient: result.UserPoolClient === undefined ? undefined : {
                ClientId: result.UserPoolClient.ClientId,
                ClientSecret: result.UserPoolClient.ClientSecret,
              },
            };
          },
          async updateUserPoolClient(input) {
            const { CognitoIdentityProviderClient, UpdateUserPoolClientCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            const result = await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new UpdateUserPoolClientCommand(input as never),
            );
            return {
              UserPoolClient: result.UserPoolClient === undefined ? undefined : {
                ClientId: result.UserPoolClient.ClientId,
                ClientSecret: result.UserPoolClient.ClientSecret,
              },
            };
          },
          async listUsers(input) {
            const { CognitoIdentityProviderClient, ListUsersCommand } =
              await import("@aws-sdk/client-cognito-identity-provider");
            const result = await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new ListUsersCommand(input),
            );
            return {
              Users: (result.Users ?? []).map((user) => ({
                Username: user.Username,
                UserStatus: user.UserStatus,
                Attributes: user.Attributes?.map((attribute) => ({ Name: attribute.Name, Value: attribute.Value })),
              })),
              PaginationToken: result.PaginationToken,
            };
          },
          async adminLinkProviderForUser(input) {
            const { AdminLinkProviderForUserCommand, CognitoIdentityProviderClient } =
              await import("@aws-sdk/client-cognito-identity-provider");
            await new CognitoIdentityProviderClient(await clientOptions(options)).send(
              new AdminLinkProviderForUserCommand(input as never),
            );
          },
        },
        secrets: {
          async describeSecret(input) {
            const { DescribeSecretCommand, SecretsManagerClient } = await import("@aws-sdk/client-secrets-manager");
            const result = await new SecretsManagerClient(await clientOptions(options)).send(
              new DescribeSecretCommand(input),
            );
            return { ARN: result.ARN };
          },
          async createSecret(input) {
            const { CreateSecretCommand, SecretsManagerClient } = await import("@aws-sdk/client-secrets-manager");
            const result = await new SecretsManagerClient(await clientOptions(options)).send(
              new CreateSecretCommand(input as never),
            );
            return { ARN: result.ARN };
          },
          async updateSecret(input) {
            const { SecretsManagerClient, UpdateSecretCommand } = await import("@aws-sdk/client-secrets-manager");
            const result = await new SecretsManagerClient(await clientOptions(options)).send(
              new UpdateSecretCommand(input as never),
            );
            return { ARN: result.ARN };
          },
        },
        async setConfigEntry(key, value) {
          const writer = await deps.configWriter({
            clusterName: options.clusterName,
            awsRegion: requiredRegion(options),
            awsProfile: options.awsProfile,
          });
          await writer.setConfigEntry(key, value);
        },
        sleep: deps.sleep,
        out: deps.out,
      };
    },
    directoryService: async (options) => ({
      secrets: {
        async createSecret(input) {
          const { CreateSecretCommand, SecretsManagerClient } = await import("@aws-sdk/client-secrets-manager");
          const result = await new SecretsManagerClient(await clientOptions(options)).send(
            new CreateSecretCommand(input as never),
          );
          return { ARN: result.ARN };
        },
      },
      out: deps.out,
    }),
    sharedStorage: async () => {
      throw new GeneralException(
        "shared-storage requires the EFS and FSx client adapters, which are not ported",
      );
    },
    utils: async (options) => {
      const config = options.clusterName === undefined ? new ClusterConfig([]) : await loadConfig(deps, options);
      return {
        config,
        api: {
          async getParametersByPath(input) {
            const { GetParametersByPathCommand, SSMClient } = await import("@aws-sdk/client-ssm");
            const result = await new SSMClient(await clientOptions(options)).send(
              new GetParametersByPathCommand({ ...input, Recursive: false }),
            );
            return {
              Parameters: result.Parameters?.map((parameter) => ({ Value: parameter.Value })),
              NextToken: result.NextToken,
            };
          },
          async describeVpcEndpointServices(input) {
            const { DescribeVpcEndpointServicesCommand, EC2Client } = await import("@aws-sdk/client-ec2");
            const result = await new EC2Client(await clientOptions(options)).send(
              new DescribeVpcEndpointServicesCommand(input as never),
            );
            return {
              ServiceDetails: result.ServiceDetails?.map((detail) => ({
                ServiceName: detail.ServiceName,
                ServiceType: detail.ServiceType?.map((type) => ({ ServiceType: type.ServiceType })),
                AvailabilityZones: detail.AvailabilityZones,
              })),
            };
          },
          async getManagedPrefixListEntries(input) {
            const { EC2Client, GetManagedPrefixListEntriesCommand } = await import("@aws-sdk/client-ec2");
            const result = await new EC2Client(await clientOptions(options)).send(
              new GetManagedPrefixListEntriesCommand(input),
            );
            return {
              Entries: result.Entries?.map((entry) => ({ Cidr: entry.Cidr, Description: entry.Description })),
              NextToken: result.NextToken,
            };
          },
          async describeManagedPrefixLists(input) {
            const { DescribeManagedPrefixListsCommand, EC2Client } = await import("@aws-sdk/client-ec2");
            const result = await new EC2Client(await clientOptions(options)).send(
              new DescribeManagedPrefixListsCommand(input),
            );
            return { PrefixLists: result.PrefixLists?.map((list) => ({ Version: list.Version })) };
          },
          async modifyManagedPrefixList(input) {
            const { EC2Client, ModifyManagedPrefixListCommand } = await import("@aws-sdk/client-ec2");
            await new EC2Client(await clientOptions(options)).send(new ModifyManagedPrefixListCommand(input));
          },
        },
        async dnsSuffix() {
          return requiredRegion(options).startsWith("cn-") ? "amazonaws.com.cn" : "amazonaws.com";
        },
        async syncGlobalSettings(input) {
          const clusterName = options.clusterName;
          if (clusterName === undefined || clusterName.trim() === "") {
            throw new GeneralException("cluster name is required for global settings update");
          }
          const writer = await deps.configWriter({
            clusterName,
            awsRegion: requiredRegion(options),
            awsProfile: options.awsProfile,
          });
          await writer.deleteConfigEntries(input.deletePrefix);
          await writer.syncClusterSettingsInDb(input.entries, true);
        },
        async exportConfig(input) {
          const [settings, modules] = await Promise.all([
            scanAll(deps, `${input.clusterName}.cluster-settings`),
            scanAll(deps, `${input.clusterName}.modules`),
          ]);
          const config = yaml.dump(settings, { noRefs: true, sortKeys: false });
          const moduleConfig = yaml.dump(modules, { noRefs: true, sortKeys: false });
          await import("node:fs/promises").then(({ mkdir, writeFile }) =>
            mkdir(input.configDir, { recursive: true }).then(async () => {
              await writeFile(join(input.configDir, "config.yml"), config);
              await writeFile(join(input.configDir, "modules.yml"), moduleConfig);
            }),
          );
        },
        now: () => new Date(),
        prompt: async (message) => (await deps.prompt({ message, default: false })) === true,
        out: deps.out,
      };
    },
    support: async (options) => ({
      async databaseConfig() {
        const clusterName = options.clusterName;
        if (clusterName === undefined) throw new GeneralException("cluster name is required for support database export");
        const [settings, modules] = await Promise.all([
          scanAll(deps, `${clusterName}.cluster-settings`),
          scanAll(deps, `${clusterName}.modules`),
        ]);
        return {
          configYaml: yaml.dump(settings, { noRefs: true, sortKeys: false }),
          modulesYaml: yaml.dump(modules, { noRefs: true, sortKeys: false }),
        };
      },
      now: () => new Date(),
      archive: archiveDirectory,
      out: deps.out,
    }),
    integrationTests: async (options) => ({
      config: await loadConfig(deps, options),
      casesForModule() {
        throw new GeneralException("integration test case registry is not ported");
      },
      out: deps.out,
      err: deps.err,
    }),
  };
}
