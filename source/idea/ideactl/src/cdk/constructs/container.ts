/**
 * What the container services share.
 *
 * Each application service is created by the stack that publishes the settings the application
 * reads, so a task starts only after its own module has written `client_id` and the rest. That
 * puts six services in four stacks, and these are plain functions over an explicit input rather
 * than methods on a stack, so every one of them can use the same task, service and log shapes
 * without inheriting anything.
 *
 * Nothing here reads a module-specific setting. The shared capacity keys (`ecs.cluster_name`,
 * `ecs.capacity_provider`, `ecs.cpu_architecture`, `ecs.image`, `ecs.tasks.<role>.*`) are the
 * container stack's own published rows; everything else the stacks pass in.
 */

import { Aws, Duration, RemovalPolicy, type Stack } from "aws-cdk-lib";
import type { IConstruct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";

import type { IdeaContext } from "./base.ts";
import { addCommonTags, addNagSuppression } from "./base.ts";
import { Policy } from "./common.ts";
import { buildResourceName, buildTrimmedResourceName } from "../../util/names.ts";

/** Stream family preserved from the agent `application_{ip}` prefix, minus the address. */
export const STREAM_PREFIX_APPLICATION = "application";
/** Stream family for OpenPBS files that used `server_logs_`, `sched_logs_`, and `accounting_logs_`. */
export const STREAM_PREFIX_OPENPBS = "openpbs";
/** Stream family preserved from the agent `dcv-session-manager-broker_{ip}` prefix. */
export const STREAM_PREFIX_BROKER = "dcv-session-manager-broker";
/** Stream family preserved from the agent `dcv-connection-gateway_{ip}` prefix. */
export const STREAM_PREFIX_GATEWAY = "dcv-connection-gateway";

/** Where every module process writes `application.log`. */
export const APPLICATION_LOG_DIRECTORY = "/opt/idea/app/logs";

/**
 * Start allowances, used for the load balancer health check grace and for the container health
 * check that runs alongside it.
 *
 * The library default grace is 60 seconds, which is shorter than every role's start: the first
 * deployment would then fail its target health checks while the process being checked is still
 * starting, and the circuit breaker would roll it back. The two wait ceilings below are the ones
 * the image's role scripts impose on themselves, so they are facts about the start rather than
 * estimates: `roles/scheduler.sh` waits up to 30 attempts 5 seconds apart for the batch server to
 * answer, and `roles/broker.sh` waits up to 30 attempts 2 seconds apart for service discovery to
 * resolve. The application allowance is the time a role needs after its own wait to answer a health
 * request, and on a scheduler first start it also covers creating a new state directory and its
 * datastore.
 */
export const PBS_SERVER_WAIT_SECONDS = 150;
export const BROKER_DISCOVERY_WAIT_SECONDS = 60;
export const APPLICATION_START_SECONDS = 120;
/**
 * Registration allowance, including the network load balancer's distributed health checks.
 * Grace suppresses premature replacement; it does not delay a successful readiness check.
 */
export const TARGET_REGISTRATION_SECONDS = 150;

/** Container role, which is the `ecs.tasks.<role>` sizing key and the `IDEA_CONTAINER_ROLE` value. */
export type ContainerRole = "cluster-manager" | "vdc" | "scheduler" | "dcv-broker" | "dcv-gateway" | "bastion-host";

/** What every function here needs from the stack calling it. */
export interface ContainerScope {
  readonly ctx: IdeaContext;
  readonly stack: Stack;
  readonly vpc: ec2.IVpc;
  readonly privateSubnets: ec2.ISubnet[];
}

export interface RoleSizing {
  readonly cpu: number;
  readonly memory: number;
  readonly desired: number;
}

export interface StorageMount {
  readonly hostPath?: string;
  readonly fileSystemId?: string;
  readonly mountPath: string;
  readonly name: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A required string setting. */
export function requiredString(scope: ContainerScope, key: string): string {
  return scope.ctx.config.getString(key, undefined, { required: true }) as string;
}

/** A required integer setting. */
export function requiredInt(scope: ContainerScope, key: string): number {
  const value = scope.ctx.config.getInt(key);
  if (value === undefined) throw new Error(`${key} is required for ECS`);
  return value;
}

/** The container image every task and sidecar runs. */
export function containerImage(scope: ContainerScope): ecs.ContainerImage {
  return ecs.ContainerImage.fromRegistry(requiredString(scope, "ecs.image"));
}

/** `SocaBaseConstruct.build_resource_name`, trimmed only when IAM would refuse the length. */
export function taskRoleName(scope: ContainerScope, name: string): string {
  const region = scope.ctx.awsRegion;
  const fullName = buildResourceName(scope.ctx.clusterName, name, region);
  return fullName.length <= 64 ? fullName : buildTrimmedResourceName(scope.ctx.clusterName, name, region, 64);
}

/** The task service, trusted only from this account. */
export function ecsTasksPrincipal(scope: ContainerScope): iam.ServicePrincipal {
  return new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
    conditions: { StringEquals: { "aws:SourceAccount": scope.stack.account } },
  });
}

// Secret injection happens before the application starts and uses the execution identity.
// Imported secrets do not carry their encryption key, so their grants cannot include decryption.
export function buildExecutionRole(scope: ContainerScope, constructId: string, name = constructId): iam.Role {
  const role = new iam.Role(scope.stack, constructId, {
    assumedBy: ecsTasksPrincipal(scope),
    roleName: taskRoleName(scope, name),
  });
  role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AmazonECSTaskExecutionRolePolicy"));
  addNagSuppression(
    role,
    [
      { rule_id: "AwsSolutions-IAM4", reason: "AmazonECSTaskExecutionRolePolicy is the documented role for pulling the image and writing task logs." },
      { rule_id: "AwsSolutions-IAM5", reason: "The default policy holds the CDK grants for the task's secrets and log streams, which are per-resource wildcards." },
    ],
    true,
  );
  return role;
}

// Imported secrets can use a key unrelated to the cluster default.
// Constrain decryption to the injected secret and its Secrets Manager service path.
export function grantInjectedSecret(scope: ContainerScope, role: iam.IRole, secretArn: string): void {
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    actions: ["kms:Decrypt"],
    resources: ["*"],
    conditions: { StringEquals: {
      "kms:ViaService": `secretsmanager.${scope.stack.region}.${scope.stack.urlSuffix}`,
      "kms:EncryptionContext:SecretARN": secretArn,
    } },
  }));
}

export interface TaskRoleInput {
  readonly constructId: string;
  /** Physical name before the cluster prefix and region suffix. */
  readonly name: string;
  readonly description: string;
  /** Instance managed policy ARNs, so the task holds what the module host role holds. */
  readonly managedPolicyArns: string[];
  readonly policyConstructId: string;
  readonly policyTemplateName: string;
  readonly policyModuleId?: string;
  /**
   * Template variables other than `role_arn`, which is always the task role itself. A function,
   * because the scheduler template also names the task role under its own variable.
   */
  readonly policyVars?: (taskRole: iam.Role) => Record<string, unknown>;
}

/**
 * The task identity for one service.
 *
 * The policy renders from the template the module's instance role renders from rather than being
 * written again by hand, so a fix to that template reaches the task. The trust is the
 * account-scoped task principal.
 */
export function buildTaskRole(scope: ContainerScope, input: TaskRoleInput): { role: iam.Role; policy: Policy } {
  const role = new iam.Role(scope.stack, input.constructId, {
    assumedBy: ecsTasksPrincipal(scope),
    description: input.description,
    managedPolicies: input.managedPolicyArns.map((policyArn, index) =>
      iam.ManagedPolicy.fromManagedPolicyArn(scope.stack, `${input.constructId}-managed-policy-${index}`, policyArn),
    ),
    roleName: taskRoleName(scope, input.name),
  });
  const policy = new Policy(scope.ctx, input.policyConstructId, scope.stack, {
    moduleId: input.policyModuleId,
    policyTemplateName: input.policyTemplateName,
    vars: { role_arn: role.roleArn, ...input.policyVars?.(role) },
  });
  role.attachInlinePolicy(policy);
  addCommonTags(scope.ctx, role, scope.ctx.moduleId);
  // The inline policy carries its own suppression; the default policy holds the CDK grants the
  // stack adds for the task's queues, streams and log groups.
  addNagSuppression(
    role,
    [{ rule_id: "AwsSolutions-IAM5", reason: "The default policy holds the CDK grants for the task's queues, streams and log groups, which are per-resource wildcards." }],
    true,
  );
  return { role, policy };
}

/**
 * Creates an awsvpc task definition on the architecture the container stack resolved for its
 * hosts, with its revisions retained so a failed deployment has something to roll back to.
 */
export function buildTaskDefinition(
  scope: ContainerScope,
  constructId: string,
  input: { executionRole: iam.IRole; taskRole: iam.IRole },
): ecs.Ec2TaskDefinition {
  const taskDefinition = new ecs.Ec2TaskDefinition(scope.stack, constructId, {
    executionRole: input.executionRole,
    networkMode: ecs.NetworkMode.AWS_VPC,
    taskRole: input.taskRole,
  });
  const resource = taskDefinition.node.defaultChild;
  if (resource instanceof ecs.CfnTaskDefinition) {
    resource.runtimePlatform = {
      cpuArchitecture: requiredString(scope, "ecs.cpu_architecture"),
      operatingSystemFamily: "LINUX",
    };
  }
  taskDefinition.applyRemovalPolicy(RemovalPolicy.RETAIN);
  addNagSuppression(taskDefinition, [
    { rule_id: "AwsSolutions-ECS2", reason: "Environment variables carry non-secret cluster configuration; secrets reach the task through Secrets Manager references." },
  ]);
  return taskDefinition;
}

export { requireDigestPinnedImage } from "../../config/datadog-agent.ts";

/** Where the metrics agent daemon listens and every task sends: a socket on a host path both mount. */
export const DOGSTATSD_SOCKET = "/var/run/datadog/dsd.socket";

/** Common task environment values. */
export function commonEnvironment(
  scope: ContainerScope,
  input: { role: ContainerRole; moduleId: string; moduleName: string },
): Record<string, string> {
  return {
    AWS_DEFAULT_REGION: scope.ctx.awsRegion,
    DD_DOGSTATSD_URL: `unix://${DOGSTATSD_SOCKET}`,
    IDEA_CLUSTER_NAME: scope.ctx.clusterName,
    IDEA_CONTAINER_ROLE: input.role,
    IDEA_MODULE_ID: input.moduleId,
    IDEA_MODULE_NAME: input.moduleName,
    IDEA_MODULE_SET: scope.ctx.config.moduleSet,
  };
}

/** Labels consumed by the observability agent. */
export function dockerLabels(scope: ContainerScope, role: ContainerRole): Record<string, string> {
  return {
    "com.datadoghq.tags.env": scope.ctx.clusterName,
    "com.datadoghq.tags.service": role,
  };
}

/** Returns the configured CPU, memory and desired count for a role. */
export function roleSizing(scope: ContainerScope, role: ContainerRole): RoleSizing {
  return {
    cpu: requiredInt(scope, `ecs.tasks.${role}.cpu`),
    memory: requiredInt(scope, `ecs.tasks.${role}.memory`),
    desired: requiredInt(scope, `ecs.tasks.${role}.desired`),
  };
}

// Hosts serve all control plane modules, with no project or queue context.
// Apply the bootstrap's scope precedence so combined scopes retain their restrictions.
export function inControlPlaneScope(storage: Record<string, unknown>): boolean {
  const scope = Array.isArray(storage["scope"]) ? storage["scope"] : [];
  const modules = Array.isArray(storage["modules"]) ? storage["modules"] : [];
  if (scope.length === 0 || scope.includes("cluster")) return true;
  if (scope.includes("project")) return false;
  if (scope.includes("module")) {
    return modules.length === 0 || modules.some((name) =>
      ["cluster-manager", "scheduler", "virtual-desktop-controller", "bastion-host"].includes(String(name)));
  }
  return false;
}

export function storageMounts(config: IdeaContext["config"]): StorageMount[] {
  const mounts: StorageMount[] = [];
  const storageRoot = config.getConfig("shared-storage", {}) ?? {};
  for (const [name, storage] of Object.entries(storageRoot)) {
    if (!isRecord(storage) || typeof storage["mount_dir"] !== "string" || typeof storage["provider"] !== "string") {
      continue;
    }
    if (!inControlPlaneScope(storage)) continue;
    const mountPath = storage["mount_dir"];
    if (storage["provider"] === "efs") {
      const efs = storage["efs"];
      if (isRecord(efs) && typeof efs["file_system_id"] === "string") {
        mounts.push({ fileSystemId: efs["file_system_id"], mountPath, name });
      }
    }
    if (
      storage["provider"] === "fsx_lustre" ||
      storage["provider"] === "fsx_netapp_ontap" ||
      storage["provider"] === "fsx_openzfs"
    ) {
      mounts.push({ hostPath: mountPath, mountPath, name });
    }
  }
  return mounts;
}

/** Adds shared storage and the optional observability host socket to an application task. */
export function addStorageMounts(
  scope: ContainerScope,
  taskDefinition: ecs.Ec2TaskDefinition,
  container: ecs.ContainerDefinition,
): void {
  for (const mount of storageMounts(scope.ctx.config)) {
    const volumeName = `storage-${mount.name}`;
    if (mount.fileSystemId !== undefined) {
      taskDefinition.addVolume({
        efsVolumeConfiguration: { fileSystemId: mount.fileSystemId },
        name: volumeName,
      });
    } else if (mount.hostPath !== undefined) {
      taskDefinition.addVolume({ host: { sourcePath: mount.hostPath }, name: volumeName });
    } else {
      continue;
    }
    container.addMountPoints({ containerPath: mount.mountPath, readOnly: false, sourceVolume: volumeName });
  }
  if (scope.ctx.config.getBool("ecs.datadog.enabled", false)) {
    taskDefinition.addVolume({ host: { sourcePath: "/var/run/datadog" }, name: "datadog" });
    container.addMountPoints({ containerPath: "/var/run/datadog", readOnly: true, sourceVolume: "datadog" });
  }
}

export function fileTailScript(directories: string[], application = false): string {
  // PBS date files need discovery, but rotated application archives must not be replayed.
  // Following only the active application path lets the follower handle rename and reopen.
  const pattern = application ? "application.log" : "*";
  const globList = directories.map((directory) => `'${directory.replaceAll("'", "'\\''")}'/${pattern}`).join(" ");
  return [
    "set -euo pipefail",
    "shopt -s nullglob dotglob",
    "followed=()",
    'from_line="0"',
    "while true; do",
    `  for file in ${globList}; do`,
    '    [[ -f "$file" ]] || continue',
    '    for existing in "${followed[@]:-}"; do',
    '      [[ "$existing" != "$file" ]] || continue 2',
    "    done",
    '    tail -n "$from_line" -F -- "$file" &',
    '    followed+=("$file")',
    "  done",
    '  from_line="+1"',
    "  sleep 30",
    "done",
  ].join("\n");
}

/**
 * Writes to a group the container stack created or adopted, without emitting an
 * `AWS::Logs::LogGroup`. Stream names become `{prefix}/{container}/{task-id}`.
 */
export function adoptedLogDriver(
  scope: ContainerScope,
  constructId: string,
  logGroupName: string,
  streamPrefix: string,
): ecs.LogDriver {
  const logGroup = logs.LogGroup.fromLogGroupName(scope.stack, constructId, logGroupName);
  return ecs.LogDrivers.awsLogs({ logGroup, streamPrefix });
}

/**
 * Sidecar that tails a role's log files into a preserved group.
 * The main container still has its own awslogs driver for stdout.
 */
export function addLogTailContainer(
  scope: ContainerScope,
  taskDefinition: ecs.Ec2TaskDefinition,
  input: {
    containerId: string;
    directories: string[];
    logGroupName: string;
    logGroupConstructId: string;
    streamPrefix: string;
    sourceVolume: string;
    containerPath: string;
    readOnly: boolean;
  },
): ecs.ContainerDefinition {
  const container = taskDefinition.addContainer(input.containerId, {
    command: [fileTailScript(input.directories, input.streamPrefix === STREAM_PREFIX_APPLICATION)],
    entryPoint: ["/bin/bash", "-lc"],
    essential: false,
    image: containerImage(scope),
    logging: adoptedLogDriver(scope, input.logGroupConstructId, input.logGroupName, input.streamPrefix),
    memoryReservationMiB: 32,
  });
  container.addMountPoints({
    containerPath: input.containerPath,
    readOnly: input.readOnly,
    sourceVolume: input.sourceVolume,
  });
  return container;
}

/** Shared volume plus sidecar so `application.log` reaches the preserved group. */
export function attachApplicationFileLogs(
  scope: ContainerScope,
  input: {
    idPrefix: string;
    logGroupName: string;
    taskDefinition: ecs.Ec2TaskDefinition;
    container: ecs.ContainerDefinition;
  },
): void {
  input.taskDefinition.addVolume({ name: "application-logs" });
  input.container.addMountPoints({
    containerPath: APPLICATION_LOG_DIRECTORY,
    readOnly: false,
    sourceVolume: "application-logs",
  });
  addLogTailContainer(scope, input.taskDefinition, {
    containerId: `${input.idPrefix}-application-logs`,
    directories: [APPLICATION_LOG_DIRECTORY],
    logGroupName: input.logGroupName,
    logGroupConstructId: `${input.idPrefix}-application-logs-group`,
    streamPrefix: STREAM_PREFIX_APPLICATION,
    sourceVolume: "application-logs",
    containerPath: APPLICATION_LOG_DIRECTORY,
    readOnly: true,
  });
}

/** Creates an HTTPS application target group for an awsvpc service. */
export function applicationTargetGroup(
  scope: ContainerScope,
  input: {
    constructId: string;
    targetGroupName: string;
    port: number;
    healthCheckPath: string;
    deregistrationDelaySeconds?: number;
  },
): elbv2.ApplicationTargetGroup {
  return new elbv2.ApplicationTargetGroup(scope.stack, input.constructId, {
    deregistrationDelay: Duration.seconds(input.deregistrationDelaySeconds ?? 300),
    healthCheck: {
      path: input.healthCheckPath, protocol: elbv2.Protocol.HTTPS,
      interval: Duration.seconds(5), timeout: Duration.seconds(4), healthyThresholdCount: 2,
    },
    port: input.port,
    protocol: elbv2.ApplicationProtocol.HTTPS,
    targetGroupName: input.targetGroupName,
    targetType: elbv2.TargetType.IP,
    vpc: scope.vpc,
  });
}

/**
 * How long a role's container takes to answer a health request.
 *
 * A role waits only for what its own start does: the batch server wait for the scheduler, the
 * service discovery wait for the broker, and the application allowance for every role.
 */
export function taskStartAllowance(role: ContainerRole): Duration {
  if (role === "scheduler") return Duration.seconds(PBS_SERVER_WAIT_SECONDS + APPLICATION_START_SECONDS);
  if (role === "dcv-broker") return Duration.seconds(BROKER_DISCOVERY_WAIT_SECONDS + APPLICATION_START_SECONDS);
  return Duration.seconds(APPLICATION_START_SECONDS);
}

/** The task start plus the time its target needs to register as healthy behind a load balancer. */
export function healthCheckGrace(role: ContainerRole): Duration {
  return Duration.seconds(taskStartAllowance(role).toSeconds() + TARGET_REGISTRATION_SECONDS);
}

/** Readiness and shutdown controls for every application container. */
export function applicationContainerSettings(role: ContainerRole, brokerPorts = [8444, 8445, 8446]): Pick<ecs.ContainerDefinitionOptions, "healthCheck" | "stopTimeout"> {
  const http = (url: string) => `curl --fail --silent --show-error --insecure --max-time 4 ${url}`;
  const command = role === "dcv-broker"
    ? brokerPorts.map((port) => http(`https://localhost:${port}/health`)).join(" && ")
    : role === "dcv-gateway"
      // The gateway exposes a dedicated TCP readiness listener, not an HTTP endpoint.
      ? "timeout 4 bash -c '</dev/tcp/127.0.0.1/8989'"
      : role === "bastion-host"
        ? "timeout 4 ssh-keyscan -p 22 127.0.0.1 >/dev/null 2>&1"
        : http("https://localhost:8443/healthcheck");
  return {
    healthCheck: {
      command: ["CMD-SHELL", command], interval: Duration.seconds(5),
      timeout: Duration.seconds(5), retries: 3, startPeriod: taskStartAllowance(role),
    },
    // ECS deregisters the task and drains its target groups before entering STOPPING.
    // This is the separate allowance between SIGTERM and SIGKILL, not a drain timer.
    stopTimeout: Duration.seconds(30),
  };
}

/** The cluster the container stack created, imported once per stack. */
export function importedEcsCluster(scope: ContainerScope): ecs.ICluster {
  const existing = scope.stack.node.tryFindChild("ecs-cluster");
  if (existing !== undefined) return existing as ecs.ICluster;
  return ecs.Cluster.fromClusterAttributes(scope.stack, "ecs-cluster", {
    clusterName: requiredString(scope, "ecs.cluster_name"),
    vpc: scope.vpc,
  });
}

/**
 * The service ARN, spelled out rather than referenced.
 *
 * The module's settings resource publishes this, and the service has to start after that resource
 * because the application reads `client_id` and the rest from it. Referencing the service here
 * would make the settings depend on the service and the service depend on the settings, which
 * CloudFormation refuses, so the identity is built from the name the service is given.
 */
export function serviceArn(scope: ContainerScope, serviceName: string): string {
  const cluster = requiredString(scope, "ecs.cluster_name");
  return `arn:${Aws.PARTITION}:ecs:${Aws.REGION}:${Aws.ACCOUNT_ID}:service/${cluster}/${serviceName}`;
}

export interface ServiceInput {
  readonly constructId: string;
  /** Explicit, so the settings resource can publish the identity without referencing the service. */
  readonly serviceName: string;
  readonly taskDefinition: ecs.Ec2TaskDefinition;
  readonly desiredCount: number;
  readonly securityGroups: ec2.ISecurityGroup[];
  /** Only the single PBS writer stops before its replacement starts. */
  readonly singleWriter?: boolean;
  readonly healthCheckGracePeriod?: Duration;
  readonly cloudMapOptions?: ecs.CloudMapOptions;
  /** False only for the scheduler, whose single task must not be spread. */
  readonly distinctInstances?: boolean;
  /**
   * The task role, its inline policy, the execution role, and every endpoint custom resource that
   * attaches one of this service's target groups to a listener. A service may not name a target
   * group with no load balancer, and a task must not start before its role carries its policies.
   */
  readonly dependencies: IConstruct[];
}

/** Creates an EC2 service on the container stack's shared capacity. */
export function buildEc2Service(scope: ContainerScope, input: ServiceInput): ecs.Ec2Service {
  if (!Number.isInteger(input.desiredCount) || input.desiredCount < 1) throw new Error("A service needs at least one desired task");
  if (input.singleWriter === true && input.desiredCount !== 1) throw new Error("The scheduler requires exactly one task");
  const service = new ecs.Ec2Service(scope.stack, input.constructId, {
    capacityProviderStrategies: [
      { capacityProvider: requiredString(scope, "ecs.capacity_provider"), weight: 1 },
    ],
    circuitBreaker: { rollback: true },
    cloudMapOptions: input.cloudMapOptions,
    cluster: importedEcsCluster(scope),
    desiredCount: input.desiredCount,
    // An operator's diagnosis path into a running control-plane task, per task and per session
    // and logged to the cluster's exec group. The permission it needs is already on every task
    // role through the instance managed policies, so this uses a standing grant rather than
    // adding one. It stays out of this tool's own identity: the tool observes and refuses.
    enableExecuteCommand: true,
    healthCheckGracePeriod: input.healthCheckGracePeriod,
    maxHealthyPercent: input.singleWriter === true ? 100 : Math.ceil(100 * (input.desiredCount + 1) / input.desiredCount),
    // Surge by one task when a distinct host has room; otherwise ECS may stop one replica first.
    // At a floor of 100 a two-task distinct-instance service needs a third host with the task's
    // memory free, and a rollout that finds none waits until the stack times out.
    minHealthyPercent: input.singleWriter === true ? 0 : Math.floor(100 * (input.desiredCount - 1) / input.desiredCount),
    placementConstraints:
      input.distinctInstances === false ? undefined : [ecs.PlacementConstraint.distinctInstances()],
    // Spread replicas across zones, then pack by memory: random placement fragments the hosts so a
    // large task finds no hole and the host group cannot shrink back to its minimum.
    placementStrategies: [
      ecs.PlacementStrategy.spreadAcross(ecs.BuiltInAttributes.AVAILABILITY_ZONE),
      ecs.PlacementStrategy.packedByMemory(),
    ],
    securityGroups: input.securityGroups,
    serviceName: input.serviceName,
    taskDefinition: input.taskDefinition,
    vpcSubnets: { subnets: scope.privateSubnets },
  });
  for (const dependency of input.dependencies) service.node.addDependency(dependency);
  return service;
}
