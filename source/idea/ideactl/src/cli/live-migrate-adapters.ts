/**
 * Live dependencies for the one-phase migration command.
 *
 * Cluster reads and writes reach the account through the shared command
 * dependencies and the upgrade command's live adapters, so the migration uses
 * one credential path with every other command. This file adds only what the
 * migration needs and no other command has: the durable operation record store,
 * the deployed-template and routing reads its before-state capture requires, and
 * the table that says which migration steps this release can execute.
 */

import { createHash } from "node:crypto";

import { ClusterConfig, GeneralException, type ModuleInfo } from "../config/cluster-config.ts";
import { compareUpgradeDrift, type UpgradeDriftInput } from "../config/upgrade-drift.ts";
import type {
  UpgradeStateObjectApi,
  VersionedUpgradeStateObject,
} from "../config/upgrade-state.ts";
import { ideaVersion } from "../version.ts";
import { awsClientOptions } from "./aws-client-options.ts";
import type { Deps } from "./cdk-invoker.ts";
import {
  MIGRATION_STEPS,
  MigrationRefusedError,
  type ExecutableMigrationStepId,
  type MigrateDeps,
  type MigrationContext,
  type MigrationObservation,
  type MigrationReconciliation,
  type MigrationStepExecutor,
  type MigrationStepId,
  type SchedulerClosureObservation,
} from "./commands/migrate.ts";
import { createLiveUpgradeDeps, prepareUpgradeDriftInput } from "./commands/upgrade.ts";
import {
  SchedulerStateUnreadableError,
  liveSsmReadChannel,
  readBatchServerState,
  renderBatchServerState,
  type BatchServerState,
} from "./scheduler-state-read.ts";
import {
  PreflightRegistry,
  createAwsvpcTrunkingCheck,
  createConfigurationDriftCheck,
  createTemplateComparisonCheck,
  renderPreflightReport,
  runPreflight,
  type PreflightContext,
} from "./preflight.ts";

// ---------------------------------------------------------------------------------------------
// what this release can execute
// ---------------------------------------------------------------------------------------------

/** An adapter or artifact a migration step needs and this release does not carry. */
export interface MigrationCapability {
  id: string;
  /** What the missing piece would do, in operator terms. */
  description: string;
  /** Where the change is written down for its owner. */
  request: string;
}

export const MIGRATION_CAPABILITIES: readonly MigrationCapability[] = [
  {
    id: "scheduler-state-read-container",
    description:
      "a read-only report of the batch server's own state once it runs as a task: job inventory, the scheduling flag and per-queue enablement. Before the cutover the same facts are read from the batch server host over the systems manager channel; after it, nothing reaches the server",
    request: "docs/port/requests/migrate-live.md section 1",
  },
  {
    id: "module-deployment",
    description:
      "a deployment hook on the executor, so a boundary can deploy one module in a fixed order and read its stack result back",
    request: "docs/port/requests/migrate-live.md section 6",
  },
  {
    id: "job-canary",
    description:
      "one isolated operator job, submitted and observed while user queues stay disabled, to prove a scheduler task replacement",
    request: "docs/port/requests/migrate-live.md section 7",
  },
  {
    id: "scheduler-state",
    description:
      "stop the legacy scheduler, capture its spool directory, verify the archive by reading it back, and restore it into the shared scheduler directory",
    request: "docs/port/requests/migrate-live.md section 2",
  },
  {
    id: "container-configuration",
    description:
      "generated container module configuration on the production path: the module row, the settings template and the module-set entry",
    request: "docs/port/requests/migrate-live.md section 4",
  },
  {
    id: "software-stack-plan",
    description:
      "the end-of-life desktop software-stack plan, which the upgrade command builds for its own run and does not export",
    request: "docs/port/requests/migrate-live.md section 5",
  },
];

const CAPABILITY_BY_ID = new Map(MIGRATION_CAPABILITIES.map((capability) => [capability.id, capability]));

/**
 * Capabilities each step needs beyond the shared adapters.
 *
 * A step is listed against every capability its stated precondition or action
 * requires, including the ones that only verify a result. A step with an empty
 * list is executed and verified by this release.
 */
export const MIGRATION_STEP_CAPABILITIES: Readonly<Record<MigrationStepId, readonly string[]>> = {
  PREFLIGHT_PASSED: [],
  OPERATION_STARTED: [],
  ADMISSION_CLOSED: [],
  WORKLOAD_DRAINED: [],
  LEGACY_SCHEDULER_CAPTURED: ["scheduler-state"],
  SOFTWARE_STACKS_RECONCILED: ["software-stack-plan"],
  CONFIGURATION_STAGED: ["container-configuration"],
  PROVIDERS_COMMITTED: ["module-deployment"],
  SCHEDULER_DNS_RETAINED: ["module-deployment"],
  ECS_CONFIGURATION_ACTIVE: ["container-configuration"],
  ECS_STAGED: ["container-configuration", "module-deployment"],
  PBS_STATE_SEEDED: ["scheduler-state"],
  ECS_SCHEDULER_READY: ["scheduler-state", "scheduler-state-read-container"],
  CLUSTER_MANAGER_ROUTED: ["module-deployment"],
  CLUSTER_MANAGER_LEGACY_REMOVED: ["module-deployment"],
  VDC_ROUTED: ["module-deployment"],
  VDC_LEGACY_REMOVED: ["module-deployment"],
  SCHEDULER_ROUTED: ["module-deployment"],
  SCHEDULER_LEGACY_REMOVED: ["module-deployment"],
  TARGET_PROVED: ["module-deployment", "job-canary"],
  ADMISSION_REOPENED: ["scheduler-state-read-container"],
  OPERATION_COMPLETED: ["scheduler-state-read-container"],
};

/**
 * Steps that do the part they own and then refuse on what they cannot finish.
 *
 * They are blocked, so they appear in the table above, but calling them is not a
 * no-op: each writes the configuration row its boundary is defined by, or, for
 * the admission boundary, announces maintenance and verifies what is observable.
 */
export const ACTING_MIGRATION_STEPS: ReadonlySet<MigrationStepId> = new Set<MigrationStepId>([
  "CONFIGURATION_STAGED",
  "SCHEDULER_DNS_RETAINED",
  "ECS_CONFIGURATION_ACTIVE",
  "CLUSTER_MANAGER_ROUTED",
  "CLUSTER_MANAGER_LEGACY_REMOVED",
  "VDC_ROUTED",
  "VDC_LEGACY_REMOVED",
  "SCHEDULER_ROUTED",
  "SCHEDULER_LEGACY_REMOVED",
]);

/** Capabilities one step needs, resolved to their descriptions. */
export function stepCapabilityGaps(step: MigrationStepId): MigrationCapability[] {
  return (MIGRATION_STEP_CAPABILITIES[step] ?? []).map((id) => {
    const capability = CAPABILITY_BY_ID.get(id);
    if (capability === undefined) throw new TypeError(`Unknown migration capability: ${id}`);
    return capability;
  });
}

/** Every step in the fixed order, with the capabilities it still needs. */
function stepsInOrder(): Array<{ step: MigrationStepId; capabilities: readonly string[] }> {
  return MIGRATION_STEPS.map((step) => ({
    step: step.id,
    capabilities: MIGRATION_STEP_CAPABILITIES[step.id] ?? [],
  }));
}

/** The first step of the fixed order this release cannot execute. */
export function firstUnsupportedStep(): MigrationStepId | undefined {
  return stepsInOrder().find((entry) => entry.capabilities.length > 0)?.step;
}

/** Where the run will stop, and every missing capability with the steps waiting on it. */
export function capabilityReport(): string | undefined {
  const all = stepsInOrder();
  const blocked = all.filter((entry) => entry.capabilities.length > 0);
  if (blocked.length === 0) return undefined;
  const acting = blocked.filter((entry) => ACTING_MIGRATION_STEPS.has(entry.step)).length;
  const lines = [
    `STOPS AT ${firstUnsupportedStep() ?? "an unknown step"}: ${all.length - blocked.length} of ${all.length} boundaries complete on their own and ${acting} more do the part they own before refusing. Each remaining step names what it waits on, so closing one does not wait on the others.`,
  ];
  for (const capability of MIGRATION_CAPABILITIES) {
    const steps = blocked
      .filter((entry) => entry.capabilities.includes(capability.id))
      .map((entry) => entry.step);
    if (steps.length === 0) continue;
    lines.push(
      `  ${capability.id}: ${capability.description}. Needed by ${steps.length} step(s): ${steps.join(", ")}. Change recorded in ${capability.request}.`,
    );
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------------------------
// durable record store
// ---------------------------------------------------------------------------------------------

/** The two response shapes the record store reads. */
interface StateGetResult {
  Body?: { transformToString(): Promise<string> };
  ETag?: string;
}

interface StatePutResult {
  ETag?: string;
}

/**
 * The S3 client surface the record store uses, as a structural interface so
 * tests drive the conditional-write branches without a network client.
 */
export interface MigrationStateStoreClient {
  send(command: unknown): Promise<unknown>;
}

/** True for the status codes S3 returns when a write condition does not hold. */
export function isWriteConditionFailure(error: unknown): boolean {
  const name = (error as { name?: string })?.name ?? "";
  const status = (error as { $metadata?: { httpStatusCode?: number } })?.$metadata?.httpStatusCode;
  return (
    name === "PreconditionFailed" ||
    name === "ConditionalRequestConflict" ||
    status === 409 ||
    status === 412
  );
}

/** True when the record has never been written. */
export function isMissingObject(error: unknown): boolean {
  const name = (error as { name?: string })?.name ?? "";
  const status = (error as { $metadata?: { httpStatusCode?: number } })?.$metadata?.httpStatusCode;
  return name === "NoSuchKey" || name === "NotFound" || status === 404;
}

/**
 * Conditional object store for the operation record.
 *
 * The revision is the object ETag: `IfNoneMatch` claims a record that does not
 * exist and `IfMatch` advances the one the caller read, so two runs cannot
 * advance the same record.
 */
export function createMigrationStateObjects(input: {
  client(): Promise<MigrationStateStoreClient>;
}): UpgradeStateObjectApi {
  return {
    async getObject(request): Promise<VersionedUpgradeStateObject | undefined> {
      const { GetObjectCommand } = await import("@aws-sdk/client-s3");
      const client = await input.client();
      try {
        const result = (await client.send(
          new GetObjectCommand({ Bucket: request.bucket, Key: request.key }),
        )) as StateGetResult;
        const body = await result.Body?.transformToString();
        if (body === undefined || result.ETag === undefined) {
          throw new GeneralException(
            `The operation record at s3://${request.bucket}/${request.key} returned no body or no revision. Retry, and check the bucket's versioning and replication settings.`,
          );
        }
        return { body, revision: result.ETag };
      } catch (error) {
        if (isMissingObject(error)) return undefined;
        throw error;
      }
    },
    async putObject(request): Promise<{ revision: string } | undefined> {
      const { PutObjectCommand } = await import("@aws-sdk/client-s3");
      const client = await input.client();
      const condition = request.condition.kind === "absent"
        ? { IfNoneMatch: "*" }
        : { IfMatch: request.condition.revision };
      try {
        const result = (await client.send(
          new PutObjectCommand({
            Bucket: request.bucket,
            Key: request.key,
            Body: request.body,
            ContentType: "application/json",
            ...condition,
          }),
        )) as StatePutResult;
        if (result.ETag === undefined) {
          throw new GeneralException(
            `The write to s3://${request.bucket}/${request.key} returned no revision, so the next conditional write cannot be built. Retry the command.`,
          );
        }
        return { revision: result.ETag };
      } catch (error) {
        if (isWriteConditionFailure(error)) return undefined;
        throw error;
      }
    },
  };
}

/** The live record store, bound to one region and profile. */
export function liveMigrationStateObjects(
  awsRegion: () => string,
  awsProfile: () => string | undefined,
): UpgradeStateObjectApi {
  return createMigrationStateObjects({
    async client() {
      const { S3Client } = await import("@aws-sdk/client-s3");
      return new S3Client(await awsClientOptions(awsRegion(), awsProfile()));
    },
  });
}

// ---------------------------------------------------------------------------------------------
// account reads the migration adds
// ---------------------------------------------------------------------------------------------

/** Where a read happens. Every migration read names its own region and profile. */
export interface MigrationReadTarget {
  awsRegion: string;
  awsProfile?: string;
}

export interface MigrationStackSummary {
  status: string;
  lastUpdated?: string;
  parameters: Record<string, string>;
}

export interface MigrationInstanceRecord {
  instanceId: string;
  state: string;
  moduleId?: string;
  nodeType?: string;
}

export interface MigrationListenerRecord {
  loadBalancerArn: string;
  listenerArn: string;
  port?: number;
  /** Target group ARNs the default action forwards to. */
  defaultTargetGroupArns: string[];
  rules: Array<{ ruleArn: string; priority?: string; targetGroupArns: string[] }>;
}

export interface MigrationTargetGroupRecord {
  targetGroupArn: string;
  targetGroupName?: string;
  healthy: number;
  unhealthy: number;
}

export interface MigrationRecordSetRecord {
  name: string;
  type: string;
  values: string[];
}

export interface MigrationObjectRecord {
  key: string;
  etag?: string;
  size?: number;
}

/** Account reads the shared dependencies do not already carry. */
export interface MigrationAccountReads {
  stackTemplate(input: MigrationReadTarget & { stackName: string }): Promise<string>;
  stackSummary(input: MigrationReadTarget & { stackName: string }): Promise<MigrationStackSummary>;
  clusterInstances(input: MigrationReadTarget & { clusterName: string }): Promise<MigrationInstanceRecord[]>;
  clusterListeners(input: MigrationReadTarget & { clusterName: string }): Promise<MigrationListenerRecord[]>;
  targetGroupHealth(input: MigrationReadTarget & { targetGroupArns: readonly string[] }): Promise<MigrationTargetGroupRecord[]>;
  recordSets(input: MigrationReadTarget & { hostedZoneId: string }): Promise<MigrationRecordSetRecord[]>;
  bucketObjects(input: MigrationReadTarget & { bucket: string; prefix: string }): Promise<MigrationObjectRecord[]>;
}

function tagValue(tags: Array<{ Key?: string; Value?: string }> | undefined, key: string): string | undefined {
  return tags?.find((tag) => tag.Key === key)?.Value;
}

/** The live implementation: one lazily imported client per service, bound to the caller's profile. */
export function liveMigrationAccountReads(): MigrationAccountReads {
  const cloudFormation = async (input: MigrationReadTarget) => {
    const { CloudFormationClient } = await import("@aws-sdk/client-cloudformation");
    return new CloudFormationClient(await awsClientOptions(input.awsRegion, input.awsProfile));
  };
  const elbv2 = async (input: MigrationReadTarget) => {
    const { ElasticLoadBalancingV2Client } = await import("@aws-sdk/client-elastic-load-balancing-v2");
    return new ElasticLoadBalancingV2Client(await awsClientOptions(input.awsRegion, input.awsProfile));
  };

  return {
    async stackTemplate(input) {
      const { GetTemplateCommand } = await import("@aws-sdk/client-cloudformation");
      const client = await cloudFormation(input);
      const result = await client.send(
        new GetTemplateCommand({ StackName: input.stackName, TemplateStage: "Original" }),
      );
      if (result.TemplateBody === undefined) {
        throw new GeneralException(`stack ${input.stackName} returned no template body`);
      }
      return result.TemplateBody;
    },
    async stackSummary(input) {
      const { DescribeStacksCommand } = await import("@aws-sdk/client-cloudformation");
      const client = await cloudFormation(input);
      const result = await client.send(new DescribeStacksCommand({ StackName: input.stackName }));
      const stack = result.Stacks?.[0];
      if (stack === undefined) throw new GeneralException(`stack not found: ${input.stackName}`);
      const parameters: Record<string, string> = {};
      for (const parameter of stack.Parameters ?? []) {
        if (parameter.ParameterKey !== undefined) {
          parameters[parameter.ParameterKey] = parameter.ParameterValue ?? "";
        }
      }
      return {
        status: stack.StackStatus ?? "UNKNOWN",
        lastUpdated: (stack.LastUpdatedTime ?? stack.CreationTime)?.toISOString(),
        parameters,
      };
    },
    async clusterInstances(input) {
      const { DescribeInstancesCommand, EC2Client } = await import("@aws-sdk/client-ec2");
      const client = new EC2Client(await awsClientOptions(input.awsRegion, input.awsProfile));
      const records: MigrationInstanceRecord[] = [];
      let token: string | undefined;
      do {
        const result = await client.send(
          new DescribeInstancesCommand({
            Filters: [
              { Name: "tag:idea:ClusterName", Values: [input.clusterName] },
              { Name: "instance-state-name", Values: ["pending", "running", "stopping", "stopped"] },
            ],
            NextToken: token,
          }),
        );
        for (const reservation of result.Reservations ?? []) {
          for (const instance of reservation.Instances ?? []) {
            if (instance.InstanceId === undefined) continue;
            records.push({
              instanceId: instance.InstanceId,
              state: instance.State?.Name ?? "unknown",
              moduleId: tagValue(instance.Tags, "idea:ModuleId"),
              nodeType: tagValue(instance.Tags, "idea:NodeType"),
            });
          }
        }
        token = result.NextToken;
      } while (token !== undefined);
      return records.sort((left, right) => left.instanceId.localeCompare(right.instanceId));
    },
    async clusterListeners(input) {
      const { DescribeListenersCommand, DescribeLoadBalancersCommand, DescribeRulesCommand } =
        await import("@aws-sdk/client-elastic-load-balancing-v2");
      const client = await elbv2(input);
      const balancers = await client.send(new DescribeLoadBalancersCommand({}));
      const owned = (balancers.LoadBalancers ?? []).filter((balancer) =>
        (balancer.LoadBalancerName ?? "").startsWith(`${input.clusterName}-`),
      );
      const records: MigrationListenerRecord[] = [];
      for (const balancer of owned) {
        if (balancer.LoadBalancerArn === undefined) continue;
        const listeners = await client.send(
          new DescribeListenersCommand({ LoadBalancerArn: balancer.LoadBalancerArn }),
        );
        for (const listener of listeners.Listeners ?? []) {
          if (listener.ListenerArn === undefined) continue;
          const rules = balancer.Type === "network"
            ? { Rules: [] }
            : await client.send(new DescribeRulesCommand({ ListenerArn: listener.ListenerArn }));
          records.push({
            loadBalancerArn: balancer.LoadBalancerArn,
            listenerArn: listener.ListenerArn,
            port: listener.Port,
            defaultTargetGroupArns: (listener.DefaultActions ?? []).flatMap((action) => [
              ...(action.TargetGroupArn === undefined ? [] : [action.TargetGroupArn]),
              ...(action.ForwardConfig?.TargetGroups ?? []).flatMap((target) =>
                target.TargetGroupArn === undefined ? [] : [target.TargetGroupArn],
              ),
            ]),
            rules: (rules.Rules ?? []).flatMap((rule) =>
              rule.RuleArn === undefined ? [] : [{
                ruleArn: rule.RuleArn,
                priority: rule.Priority,
                targetGroupArns: (rule.Actions ?? []).flatMap((action) => [
                  ...(action.TargetGroupArn === undefined ? [] : [action.TargetGroupArn]),
                  ...(action.ForwardConfig?.TargetGroups ?? []).flatMap((target) =>
                    target.TargetGroupArn === undefined ? [] : [target.TargetGroupArn],
                  ),
                ]),
              }],
            ),
          });
        }
      }
      return records;
    },
    async targetGroupHealth(input) {
      const { DescribeTargetGroupsCommand, DescribeTargetHealthCommand } =
        await import("@aws-sdk/client-elastic-load-balancing-v2");
      const client = await elbv2(input);
      const records: MigrationTargetGroupRecord[] = [];
      for (const targetGroupArn of input.targetGroupArns) {
        const [group, health] = await Promise.all([
          client.send(new DescribeTargetGroupsCommand({ TargetGroupArns: [targetGroupArn] })),
          client.send(new DescribeTargetHealthCommand({ TargetGroupArn: targetGroupArn })),
        ]);
        const states = (health.TargetHealthDescriptions ?? []).map(
          (description) => description.TargetHealth?.State ?? "unknown",
        );
        records.push({
          targetGroupArn,
          targetGroupName: group.TargetGroups?.[0]?.TargetGroupName,
          healthy: states.filter((state) => state === "healthy").length,
          unhealthy: states.filter((state) => state !== "healthy").length,
        });
      }
      return records;
    },
    async recordSets(input) {
      const { ListResourceRecordSetsCommand, Route53Client } = await import("@aws-sdk/client-route-53");
      const client = new Route53Client(await awsClientOptions(input.awsRegion, input.awsProfile));
      const records: MigrationRecordSetRecord[] = [];
      let startName: string | undefined;
      let startType: string | undefined;
      for (;;) {
        const result = await client.send(
          new ListResourceRecordSetsCommand({
            HostedZoneId: input.hostedZoneId,
            StartRecordName: startName,
            StartRecordType: startType as never,
          }),
        );
        for (const record of result.ResourceRecordSets ?? []) {
          records.push({
            name: record.Name ?? "",
            type: record.Type?.toString() ?? "",
            values: [
              ...(record.ResourceRecords ?? []).flatMap((value) =>
                value.Value === undefined ? [] : [value.Value],
              ),
              ...(record.AliasTarget?.DNSName === undefined ? [] : [record.AliasTarget.DNSName]),
            ],
          });
        }
        if (result.IsTruncated !== true) return records;
        startName = result.NextRecordName;
        startType = result.NextRecordType?.toString();
      }
    },
    async bucketObjects(input) {
      const { ListObjectsV2Command, S3Client } = await import("@aws-sdk/client-s3");
      const client = new S3Client(await awsClientOptions(input.awsRegion, input.awsProfile));
      const records: MigrationObjectRecord[] = [];
      let token: string | undefined;
      do {
        const result = await client.send(
          new ListObjectsV2Command({ Bucket: input.bucket, Prefix: input.prefix, ContinuationToken: token }),
        );
        for (const object of result.Contents ?? []) {
          if (object.Key === undefined) continue;
          records.push({ key: object.Key, etag: object.ETag, size: object.Size });
        }
        token = result.IsTruncated === true ? result.NextContinuationToken : undefined;
      } while (token !== undefined);
      return records;
    },
  };
}

// ---------------------------------------------------------------------------------------------
// fingerprints
// ---------------------------------------------------------------------------------------------

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/** Order-independent digest of one template, so formatting cannot mask a change. */
export function templateDigest(templateBody: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(templateBody);
  } catch {
    return sha256(templateBody);
  }
  return sha256(canonicalJson(parsed));
}

/** JSON with object keys in sorted order. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, member]) => member !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, member]) => `${JSON.stringify(key)}:${canonicalJson(member)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/**
 * The value an operator accepts with `--accept-template-comparison`.
 *
 * It binds the acceptance to this release and to the exact templates deployed
 * now, so a comparison that was green against an older template cannot be
 * replayed against a cluster that has changed since.
 */
export function templateComparisonFingerprint(
  release: string,
  stacks: ReadonlyArray<{ stackName: string; digest: string }>,
): string {
  const lines = [...stacks]
    .map((stack) => `${stack.stackName}=${stack.digest}`)
    .sort((left, right) => left.localeCompare(right));
  return sha256([`release=${release}`, ...lines].join("\n"));
}

/** The value an operator accepts with `--accept-drift`. */
export function driftReportFingerprint(release: string, lossKeys: readonly string[]): string {
  return sha256([`release=${release}`, ...[...lossKeys].sort((left, right) => left.localeCompare(right))].join("\n"));
}

/** Module rows that own a deployed stack, in deployment order. */
export function deployableStacks(modules: readonly ModuleInfo[], clusterName: string): Array<{ moduleId: string; stackName: string }> {
  return modules
    .filter((module) => module.type === "stack" || module.type === "app")
    .flatMap((module) => {
      const stackName = typeof module.stack_name === "string" && module.stack_name.startsWith(`${clusterName}-`)
        ? module.stack_name
        : undefined;
      return stackName === undefined ? [] : [{ moduleId: module.module_id, stackName }];
    })
    .sort((left, right) => left.stackName.localeCompare(right.stackName));
}

// ---------------------------------------------------------------------------------------------
// the live step executor
// ---------------------------------------------------------------------------------------------

/** Everything the executor reads or writes, injected so tests drive every branch. */
export interface LiveMigrationStepsInput {
  deps: Deps;
  reads: MigrationAccountReads;
  /** Live drift preview input, as the upgrade command prepares it. */
  driftInput(context: Readonly<MigrationContext>): Promise<UpgradeDriftInput>;
  /** Effective account setting for task network interface trunking. */
  trunkingEnabled(context: Readonly<MigrationContext>): Promise<boolean>;
  clusterConfig(context: Readonly<MigrationContext>): Promise<ClusterConfig>;
  /**
   * The batch server's own state, read-only, or a throw.
   *
   * It never answers with a default, because the values it would default to are
   * the ones that let the admission boundary pass.
   */
  schedulerState(context: Readonly<MigrationContext>): Promise<BatchServerState>;
  release?(): string;
}

/** The banner text the portal shows and the scheduler returns while admission is closed. */
const MAINTENANCE_MESSAGE = "This cluster is undergoing maintenance. Job submission is closed.";

function blockedObservation(step: MigrationStepId): MigrationObservation {
  const gaps = stepCapabilityGaps(step);
  return {
    ok: false,
    detail: [
      `${step} cannot be executed or verified by this release.`,
      ...gaps.map((gap) => `  ${gap.id}: ${gap.description}. Change recorded in ${gap.request}.`),
    ].join("\n"),
  };
}

/**
 * The production step executor.
 *
 * Steps this release can execute do their work through the shared adapters and
 * are verified from a read after the write. Every other step refuses at its
 * precondition, which is checked before the driver writes a started marker, so
 * a run that cannot finish changes nothing.
 */
export class LiveMigrationSteps implements MigrationStepExecutor {
  readonly #input: LiveMigrationStepsInput;

  constructor(input: LiveMigrationStepsInput) {
    this.#input = input;
  }

  async checkPrecondition(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    if (step === "PREFLIGHT_PASSED") return this.#preflight(context);
    if (step === "ADMISSION_CLOSED") return this.#admissionPathsReachable(context);
    if (step === "WORKLOAD_DRAINED") return this.#drainPrecondition(context);
    if (step === "CONFIGURATION_STAGED") return this.#stagingPrecondition(context);
    if (step === "SCHEDULER_DNS_RETAINED") return this.#schedulerStackStable(context);
    if (step === "ECS_CONFIGURATION_ACTIVE") return this.#containerModuleRegistered(context);
    if (ROUTING_STEPS[step] !== undefined) return this.#routingPrecondition(step, context);
    if (stepCapabilityGaps(step).length > 0) return blockedObservation(step);
    if (step === "OPERATION_STARTED") return this.#operationStartedPrecondition(context);
    return {
      ok: false,
      detail: `${step} has no precondition implementation in this release, and a step is never executed on an unchecked precondition`,
    };
  }

  async execute(
    step: ExecutableMigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    if (step === "OPERATION_STARTED") return this.#captureBeforeState(context);
    if (step === "WORKLOAD_DRAINED") return this.#confirmDrained(context);
    if (step === "CONFIGURATION_STAGED") return this.#stageConfiguration(context);
    if (step === "SCHEDULER_DNS_RETAINED") return this.#retainSchedulerDns(context);
    if (step === "ECS_CONFIGURATION_ACTIVE") return this.#activateContainerConfiguration(context);
    if (ROUTING_STEPS[step] !== undefined) return this.#routeOrRemove(step, context);
    const blocked = blockedObservation(step);
    throw new MigrationRefusedError(blocked.detail);
  }

  /**
   * Announce maintenance, then verify. The tool never closes the batch server.
   *
   * It writes the maintenance rows, which is settings state it already owns, reads
   * them back, reads every queue profile, reads the batch server's own state, and
   * returns what it observed. A read that fails throws rather than returning a
   * zero: an unobservable scheduler is not a closed scheduler.
   *
   * The driver decides on the returned values, and it refuses while anything still
   * admits work, so this method prints the exact commands a person runs before it
   * hands them back.
   */
  async closeScheduler(context: Readonly<MigrationContext>): Promise<SchedulerClosureObservation> {
    const { deps } = this.#input;
    const config = await this.#input.clusterConfig(context);
    const maintenanceKey = `${config.moduleId("cluster-manager")}.maintenance`;

    const writer = await deps.configWriter({
      clusterName: context.clusterName,
      awsRegion: context.awsRegion,
      awsProfile: context.awsProfile,
    });
    await writer.setConfigEntry(`${maintenanceKey}.message`, MAINTENANCE_MESSAGE);
    await writer.setConfigEntry(`${maintenanceKey}.enabled`, true);

    const after = await this.#input.clusterConfig(context);
    const maintenanceEnabled = after.getBool(`${maintenanceKey}.enabled`, false);
    if (!maintenanceEnabled) {
      throw new MigrationRefusedError(
        `ADMISSION_CLOSED refused: ${maintenanceKey}.enabled read back as false after it was written. Check the settings table and the writer's permissions, then rerun.`,
      );
    }

    const profiles = await this.#queueProfiles(context, config);
    const enabledProfiles = profiles.filter((profile) => profile.enabled);
    const server = await this.#input.schedulerState(context);
    const serverEnabledQueues = server.queues.filter((queue) => queue.enabled).map((queue) => queue.name);

    // Both admission paths are named, because closing one leaves the other open: the profile
    // rows gate the portal and the API, and the server's own queues gate a direct submission.
    const enabledQueues = [
      ...serverEnabledQueues,
      ...enabledProfiles.map((profile) => `profile:${profile.name}`),
    ];
    if (server.scheduling || enabledQueues.length > 0) {
      deps.out(`OBSERVED [ADMISSION_CLOSED] ${renderBatchServerState(server)}`);
      deps.out("OBSERVED [ADMISSION_CLOSED] close the remaining admission controls by hand, then rerun:");
      // Only what is still open is printed, so a rerun does not ask for a command already run.
      if (server.scheduling) {
        deps.out('  on the batch server: /opt/pbs/bin/qmgr -c "set server scheduling = False"');
      }
      for (const queue of serverEnabledQueues) {
        deps.out(`  on the batch server: /opt/pbs/bin/qmgr -c "set queue ${queue} enabled = False"`);
      }
      for (const profile of enabledProfiles) {
        deps.out(`  queue profile ${profile.name} still admits work through the portal and the API`);
      }
    }

    return {
      detail: [
        `maintenance is announced (${maintenanceKey}.enabled=true)`,
        `${profiles.length - enabledProfiles.length} of ${profiles.length} queue profiles are disabled`,
        renderBatchServerState(server),
      ].join("; "),
      maintenanceEnabled,
      schedulingEnabled: server.scheduling,
      enabledQueues,
      queuedJobs: server.queuedJobs,
      provisioningJobs: server.provisioningJobs,
      runningJobs: server.runningJobs,
    };
  }

  /** Queue profiles, with the batch-server queues each one admits work to. */
  async #queueProfiles(
    context: Readonly<MigrationContext>,
    config: ClusterConfig,
  ): Promise<Array<{ name: string; enabled: boolean; queues: string[] }>> {
    const table = `${context.clusterName}.${config.moduleId("scheduler")}.queue-profiles`;
    const rows = await scanTable(this.#input.deps, table);
    return rows.map((row) => ({
      name: typeof row["name"] === "string" ? row["name"] : "unnamed",
      enabled: row["enabled"] === true,
      queues: Array.isArray(row["queues"]) ? row["queues"].filter((queue): queue is string => typeof queue === "string") : [],
    }));
  }

  /**
   * Every admission path must be readable before anything is announced.
   *
   * A missing table, an unwritable settings store or a batch server that does not
   * answer is a refusal here, where no maintenance row has been written yet.
   */
  async #admissionPathsReachable(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    let profiles: Array<{ name: string; enabled: boolean; queues: string[] }>;
    try {
      profiles = await this.#queueProfiles(context, config);
    } catch (error) {
      return {
        ok: false,
        detail: `the queue profile table for ${context.clusterName} could not be read (${error instanceof Error ? error.message : String(error)}), and an unobservable scheduler is not a closed scheduler`,
      };
    }
    let server: BatchServerState;
    try {
      server = await this.#input.schedulerState(context);
    } catch (error) {
      return {
        ok: false,
        detail: `the batch server's own state could not be read (${error instanceof Error ? error.message : String(error)}), and an unobservable scheduler is not a closed scheduler`,
      };
    }
    const enabled = profiles.filter((profile) => profile.enabled).length;
    return {
      ok: true,
      detail: `the maintenance rows under ${config.moduleId("cluster-manager")}.maintenance are writable, ${profiles.length} queue profiles are readable with ${enabled} enabled, and the batch server answers: ${renderBatchServerState(server)}`,
    };
  }

  /**
   * Nothing may admit work before the drain is confirmed.
   *
   * Every value is read again here rather than carried from the closure, because a
   * resumed run can reach this boundary long after admission was closed and a queue
   * that was reopened in between must stop the run.
   */
  async #drainPrecondition(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    const maintenanceKey = `${config.moduleId("cluster-manager")}.maintenance.enabled`;
    let server: BatchServerState;
    let profiles: Array<{ name: string; enabled: boolean; queues: string[] }>;
    try {
      profiles = await this.#queueProfiles(context, config);
      server = await this.#input.schedulerState(context);
    } catch (error) {
      return {
        ok: false,
        detail: `admission state could not be read (${error instanceof Error ? error.message : String(error)}), and an unobservable scheduler is not a closed scheduler`,
      };
    }
    const open = [
      ...(config.getBool(maintenanceKey, false) ? [] : [`${maintenanceKey} is not true`]),
      ...(server.scheduling ? ["the batch server is still scheduling"] : []),
      ...server.queues.filter((queue) => queue.enabled).map((queue) => `batch queue ${queue.name} is enabled`),
      ...profiles.filter((profile) => profile.enabled).map((profile) => `queue profile ${profile.name} is enabled`),
    ];
    if (open.length > 0) {
      return { ok: false, detail: `these admission controls are still open: ${open.join(", ")}` };
    }
    return {
      ok: true,
      detail: `maintenance is enabled, no queue profile admits work, and ${renderBatchServerState(server)}`,
    };
  }

  /**
   * Confirm the inventories are empty and name any compute node still carrying the
   * legacy scheduler address.
   *
   * Retiring a node is a user-visible action, so this boundary refuses and names
   * them rather than terminating them. With admission closed no new node is
   * created, so the list can only shrink.
   */
  async #confirmDrained(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const server = await this.#input.schedulerState(context);
    const active = server.queuedJobs + server.provisioningJobs + server.runningJobs;
    if (active > 0) {
      return {
        ok: false,
        detail: `the batch server still holds ${active} jobs (${renderBatchServerState(server)}). Wait for them to finish; do not cancel them here.`,
      };
    }
    const instances = await this.#input.reads.clusterInstances({
      awsRegion: context.awsRegion,
      awsProfile: context.awsProfile,
      clusterName: context.clusterName,
    });
    const computeNodes = instances.filter(
      (instance) => instance.nodeType === COMPUTE_NODE_TYPE && instance.state !== "terminated",
    );
    if (computeNodes.length > 0) {
      return {
        ok: false,
        detail: `${computeNodes.length} compute nodes still carry the legacy scheduler address and must be retired before the server moves: ${computeNodes.map((node) => `${node.instanceId}=${node.state}`).join(", ")}`,
      };
    }
    return {
      ok: true,
      detail: `the batch server holds no queued, provisioning or running jobs and no compute node carries the legacy scheduler address (${renderBatchServerState(server)})`,
    };
  }

  async reconcile(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationReconciliation> {
    if (step !== "OPERATION_STARTED") {
      return {
        state: "retryable",
        detail: `${step} never ran, because this release refuses it before its started marker is written`,
      };
    }
    const key = captureKey(context);
    const bucket = await this.#captureBucket(context);
    const objects = await this.#input.reads.bucketObjects({
      awsRegion: context.awsRegion,
      awsProfile: context.awsProfile,
      bucket,
      prefix: key,
    });
    const stored = objects.find((object) => object.key === key);
    if (stored === undefined) {
      return { state: "retryable", detail: `no before-state capture exists at s3://${bucket}/${key}` };
    }
    return {
      state: "committed",
      detail: `the before-state capture at s3://${bucket}/${key} exists, ${stored.size ?? 0} bytes`,
    };
  }

  // -------------------------------------------------------------------------------------------
  // step 0
  // -------------------------------------------------------------------------------------------

  async #preflight(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const identity = await this.#identity(context);
    const preflightContext: PreflightContext = {
      command: "migrate",
      account: identity.account,
      region: context.awsRegion,
      cluster: context.clusterName,
      ...(context.awsProfile === undefined ? {} : { profile: context.awsProfile }),
    };

    const registry = new PreflightRegistry()
      .register(createAwsvpcTrunkingCheck(async () => this.#input.trunkingEnabled(context)))
      .register(createTemplateComparisonCheck(async () => this.#templateComparisonEvidence(context)))
      .register(createConfigurationDriftCheck(async () => this.#driftEvidence(context)));

    const report = await runPreflight(registry, preflightContext);
    const notChecked = [
      "NOT CHECKED: that the control-plane image digest is present in this account's registry. No registry client is declared, so the account prerequisite is verified by hand.",
    ];
    // The capability report says where the run stops; it is not a pre-flight failure. The run is
    // meant to reach the boundary it cannot pass and refuse there, because that boundary is where
    // a person is in the loop, and because every mutating step refuses before its started marker.
    const detail = [
      renderPreflightReport(report),
      ...notChecked,
      ...(capabilityReport() === undefined ? [] : [capabilityReport() as string]),
    ].join("\n");
    return { ok: report.passed, detail };
  }

  async #identity(context: Readonly<MigrationContext>): Promise<{ account: string; arn: string }> {
    const { deps } = this.#input;
    if (deps.callerIdentity !== undefined) {
      return deps.callerIdentity({ awsRegion: context.awsRegion, awsProfile: context.awsProfile });
    }
    return { account: await deps.accountId(), arn: "unknown" };
  }

  async #deployedStacks(context: Readonly<MigrationContext>): Promise<Array<{ moduleId: string; stackName: string; digest: string }>> {
    const config = await this.#input.clusterConfig(context);
    const stacks = deployableStacks(config.modules(), context.clusterName);
    if (stacks.length === 0) {
      throw new GeneralException(
        `No deployed module stacks were found for cluster ${context.clusterName}. Check --cluster-name, --aws-region and the module table.`,
      );
    }
    const digests: Array<{ moduleId: string; stackName: string; digest: string }> = [];
    for (const stack of stacks) {
      const template = await this.#input.reads.stackTemplate({
        awsRegion: context.awsRegion,
        awsProfile: context.awsProfile,
        stackName: stack.stackName,
      });
      digests.push({ ...stack, digest: templateDigest(template) });
    }
    return digests;
  }

  async #templateComparisonEvidence(
    context: Readonly<MigrationContext>,
  ): Promise<{ matches: boolean; remedyCommand: string }> {
    const stacks = await this.#deployedStacks(context);
    const expected = templateComparisonFingerprint(this.#release(), stacks);
    const remedyCommand =
      `the target-template comparison for all ${stacks.length} module stacks and accept its result with --accept-template-comparison ${expected}`;
    return { matches: context.acceptTemplateComparison === expected, remedyCommand };
  }

  async #driftEvidence(
    context: Readonly<MigrationContext>,
  ): Promise<{ reportHash: string; lossKeys: string[]; acceptedReportHash?: string }> {
    const report = compareUpgradeDrift(await this.#input.driftInput(context));
    const lossKeys = [...report.changedRowsDifferingFromGenerated];
    return {
      reportHash: driftReportFingerprint(this.#release(), lossKeys),
      lossKeys,
      ...(context.acceptDrift === undefined ? {} : { acceptedReportHash: context.acceptDrift }),
    };
  }

  #release(): string {
    return (this.#input.release ?? ideaVersion)();
  }

  // -------------------------------------------------------------------------------------------
  // step 1
  // -------------------------------------------------------------------------------------------

  /**
   * The fingerprints the comparison was accepted against must still hold, and
   * no stack may be mid-operation. Both are read from the account.
   */
  async #operationStartedPrecondition(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const stacks = await this.#deployedStacks(context);
    const fingerprint = templateComparisonFingerprint(this.#release(), stacks);
    if (context.acceptTemplateComparison !== undefined && context.acceptTemplateComparison !== fingerprint) {
      return {
        ok: false,
        detail: `a deployed template changed after the accepted comparison: the fingerprint is now ${fingerprint}. Repeat the comparison and accept the new value.`,
      };
    }
    const unstable: string[] = [];
    for (const stack of stacks) {
      const summary = await this.#input.reads.stackSummary({
        awsRegion: context.awsRegion,
        awsProfile: context.awsProfile,
        stackName: stack.stackName,
      });
      if (!summary.status.endsWith("_COMPLETE") || summary.status.startsWith("DELETE")) {
        unstable.push(`${stack.stackName}=${summary.status}`);
      }
    }
    if (unstable.length > 0) {
      return {
        ok: false,
        detail: `these stacks are not in a stable complete state: ${unstable.join(", ")}. Let the current operation finish, then rerun.`,
      };
    }
    return {
      ok: true,
      detail: `${stacks.length} module stacks are complete and the deployed-template fingerprint is ${fingerprint}`,
    };
  }

  // -------------------------------------------------------------------------------------------
  // configuration boundaries
  // -------------------------------------------------------------------------------------------

  /** Write rows through the shared writer and read every one of them back. */
  async #writeRows(
    context: Readonly<MigrationContext>,
    rows: ReadonlyArray<{ key: string; value: unknown }>,
  ): Promise<string[]> {
    const writer = await this.#input.deps.configWriter({
      clusterName: context.clusterName,
      awsRegion: context.awsRegion,
      awsProfile: context.awsProfile,
    });
    for (const row of rows) await writer.setConfigEntry(row.key, row.value);

    const after = await this.#input.clusterConfig(context);
    const wrong = rows.flatMap((row) => {
      const actual = after.get<unknown>(row.key, undefined);
      // The DynamoDB type matters as much as the value: a boolean stored as a string reads as true.
      return actual === row.value && typeof actual === typeof row.value
        ? []
        : [`${row.key}=${JSON.stringify(actual)} (wanted ${JSON.stringify(row.value)})`];
    });
    if (wrong.length > 0) {
      throw new MigrationRefusedError(
        `these rows did not read back as written: ${wrong.join(", ")}. Check the settings table and the writer's permissions, then rerun.`,
      );
    }
    return rows.map((row) => `${row.key}=${JSON.stringify(row.value)}`);
  }

  /**
   * Container routing must still be off when configuration is staged.
   *
   * The flag turns on three steps later. With it on here, any module synthesis
   * takes its hostless branch before the container target groups exist.
   */
  async #stagingPrecondition(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    if (config.getBool(CONTAINER_ENABLED_KEY, false)) {
      return {
        ok: false,
        detail: `${CONTAINER_ENABLED_KEY} is already true, which is the ECS_CONFIGURATION_ACTIVE boundary, not this one. Stage the values file with the container key false, then rerun.`,
      };
    }
    return { ok: true, detail: `${CONTAINER_ENABLED_KEY} is off, so staging cannot select a hostless branch` };
  }

  /** Seed the container scheduler at zero. The later add-only sync keeps the seed. */
  async #stageConfiguration(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const written = await this.#writeRows(context, [{ key: SCHEDULER_DESIRED_KEY, value: 0 }]);
    throw new MigrationRefusedError(
      [
        `CONFIGURATION_STAGED refused: seeded ${written.join(", ")}, which is the row this step owns, but the generated target configuration cannot be produced by this release.`,
        ...stepCapabilityGaps("CONFIGURATION_STAGED").map(
          (gap) => `  waiting on ${gap.id}: ${gap.description}. Change recorded in ${gap.request}.`,
        ),
      ].join("\n"),
    );
  }

  /** The retain-only scheduler update needs a stable stack to update. */
  async #schedulerStackStable(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    const stackName = deployableStacks(config.modules(), context.clusterName)
      .find((stack) => stack.moduleId === config.moduleId("scheduler"))?.stackName;
    if (stackName === undefined) {
      return { ok: false, detail: `no deployed scheduler stack was found for ${context.clusterName}` };
    }
    const summary = await this.#input.reads.stackSummary({
      awsRegion: context.awsRegion,
      awsProfile: context.awsProfile,
      stackName,
    });
    if (!summary.status.endsWith("_COMPLETE") || summary.status.startsWith("DELETE")) {
      return { ok: false, detail: `${stackName} is ${summary.status}, so a retain-only update cannot be the only change` };
    }
    return { ok: true, detail: `${stackName} is ${summary.status}` };
  }

  /**
   * Set the retain flag on the existing scheduler DNS record.
   *
   * The scheduler stack already expresses the retain shape behind this row. Without
   * it the target template stops managing the record and the name execution hosts
   * resolve is deleted with the resource.
   */
  async #retainSchedulerDns(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const written = await this.#writeRows(context, [{ key: SCHEDULER_RETAIN_DNS_KEY, value: true }]);
    throw new MigrationRefusedError(
      [
        `SCHEDULER_DNS_RETAINED refused: set ${written.join(", ")}, so the next scheduler synthesis keeps the record with a retain policy, but this release cannot deploy that one module and read the committed template back.`,
        ...stepCapabilityGaps("SCHEDULER_DNS_RETAINED").map(
          (gap) => `  waiting on ${gap.id}: ${gap.description}. Change recorded in ${gap.request}.`,
        ),
      ].join("\n"),
    );
  }

  /** The container module must be registered before its flag turns on. */
  async #containerModuleRegistered(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    const registered = config.modules().some((module) => module.name === "ecs" || module.module_id === "ecs");
    if (!registered) {
      return {
        ok: false,
        detail: [
          `the container module has no row in ${context.clusterName}.modules, so turning ${CONTAINER_ENABLED_KEY} on would make every later module synthesis select a hostless branch with no module to deploy.`,
          ...stepCapabilityGaps("ECS_CONFIGURATION_ACTIVE").map(
            (gap) => `  waiting on ${gap.id}: ${gap.description}. Change recorded in ${gap.request}.`,
          ),
        ].join("\n"),
      };
    }
    return { ok: true, detail: "the container module is registered in the module table" };
  }

  /** A route or removal boundary only exists under container routing. */
  async #routingPrecondition(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    const config = await this.#input.clusterConfig(context);
    if (!config.getBool(CONTAINER_ENABLED_KEY, false)) {
      return {
        ok: false,
        detail: `${CONTAINER_ENABLED_KEY} is off, so there is nothing to route to. This boundary runs after ECS_CONFIGURATION_ACTIVE.`,
      };
    }
    const plan = ROUTING_STEPS[step];
    return {
      ok: true,
      detail: `container routing is on and ${plan?.moduleName ?? "the module"} ${plan?.retainHosts === true ? "keeps" : "loses"} its legacy hosts at this boundary`,
    };
  }

  /**
   * Drive the route and removal split with the two inputs the stacks read.
   *
   * `ecs.retain_existing_hosts` is what makes a routed step a route-only change:
   * the endpoints move to the container targets and every legacy host stays, so
   * the new target can be proved before the old one is gone. The removal step
   * clears the same row, and only the module named at that boundary is deployed.
   */
  async #routeOrRemove(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation> {
    const plan = ROUTING_STEPS[step];
    if (plan === undefined) throw new TypeError(`${step} is not a routing boundary`);
    const written = await this.#writeRows(context, [{ key: RETAIN_HOSTS_KEY, value: plan.retainHosts }]);
    throw new MigrationRefusedError(
      [
        `${step} refused: set ${written.join(", ")}, so a deploy of ${plan.moduleName} now ${plan.retainHosts ? "routes its endpoints to the container targets and keeps every legacy host" : "removes only its legacy host resources"}, but this release cannot deploy that one module and prove its production path.`,
        ...stepCapabilityGaps(step).map(
          (gap) => `  waiting on ${gap.id}: ${gap.description}. Change recorded in ${gap.request}.`,
        ),
      ].join("\n"),
    );
  }

  /** Turn container routing on, with the stable scheduler name and the task at zero. */
  async #activateContainerConfiguration(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const written = await this.#writeRows(context, [
      { key: CONTAINER_IMAGE_KEY, value: context.imageDigest },
      { key: SCHEDULER_STABLE_NAME_KEY, value: true },
      { key: SCHEDULER_DESIRED_KEY, value: 0 },
      { key: CONTAINER_ENABLED_KEY, value: true },
    ]);
    return { ok: true, detail: `container configuration active: ${written.join("; ")}` };
  }

  async #captureBucket(context: Readonly<MigrationContext>): Promise<string> {
    const config = await this.#input.clusterConfig(context);
    const bucket = config.getString("cluster.cluster_s3_bucket", "");
    if (bucket === "") {
      throw new GeneralException(
        `cluster.cluster_s3_bucket is not set for ${context.clusterName}, so the before-state capture has nowhere to go.`,
      );
    }
    return bucket;
  }

  /**
   * Capture the exact before-state the record refers to.
   *
   * The capture is an object in the cluster bucket, and the observation names
   * its key and digest, because the record holds references rather than copies.
   */
  async #captureBeforeState(context: Readonly<MigrationContext>): Promise<MigrationObservation> {
    const { deps, reads } = this.#input;
    const target = { awsRegion: context.awsRegion, awsProfile: context.awsProfile };
    const config = await this.#input.clusterConfig(context);
    const bucket = await this.#captureBucket(context);

    const settings = await scanTable(deps, `${context.clusterName}.cluster-settings`);
    const modules = await scanTable(deps, `${context.clusterName}.modules`);
    // The prior enabled value of every queue profile: the reopen boundary restores these, and they
    // live in their own table rather than in cluster settings.
    const queueProfiles = await this.#queueProfiles(context, config);
    const stacks = await this.#deployedStacks(context);
    const stackSummaries: Record<string, MigrationStackSummary & { digest: string }> = {};
    for (const stack of stacks) {
      stackSummaries[stack.stackName] = {
        ...(await reads.stackSummary({ ...target, stackName: stack.stackName })),
        digest: stack.digest,
      };
    }

    const instances = await reads.clusterInstances({ ...target, clusterName: context.clusterName });
    const listeners = await reads.clusterListeners({ ...target, clusterName: context.clusterName });
    const targetGroupArns = [
      ...new Set(
        listeners.flatMap((listener) => [
          ...listener.defaultTargetGroupArns,
          ...listener.rules.flatMap((rule) => rule.targetGroupArns),
        ]),
      ),
    ];
    const targetGroups = await reads.targetGroupHealth({ ...target, targetGroupArns });

    const hostedZoneId = config.getString("cluster.route53.private_hosted_zone_id", "");
    const recordSets = hostedZoneId === ""
      ? []
      : await reads.recordSets({ ...target, hostedZoneId });

    const packages = await reads.bucketObjects({ ...target, bucket, prefix: "idea/bootstrap/" });
    let valuesFileSha256 = "absent";
    try {
      valuesFileSha256 = sha256(await deps.s3.getObject({ Bucket: bucket, Key: "values/values.yml" }));
    } catch (error) {
      // A cluster installed before the bucket copy existed has no stored values file. Record
      // that, because a restore needs to know which values file the run started from.
      deps.out(`OBSERVED [OPERATION_STARTED] values/values.yml is not readable in the cluster bucket: ${error instanceof Error ? error.message : String(error)}`);
    }
    // The same accessor the status command uses, so a cluster with a generated load balancer name
    // and one with a custom name are both reached.
    let health: number | undefined;
    try {
      health = await deps.httpStatus(config.getClusterExternalEndpoint());
    } catch (error) {
      deps.out(`OBSERVED [OPERATION_STARTED] the cluster external endpoint is not resolvable: ${error instanceof Error ? error.message : String(error)}`);
    }

    const capture = {
      release: this.#release(),
      deploymentId: context.deploymentId,
      clusterName: context.clusterName,
      awsRegion: context.awsRegion,
      moduleSet: context.moduleSet,
      selectedModules: [...context.selectedModules],
      targetBaseOs: context.targetBaseOs,
      imageDigest: context.imageDigest,
      capturedAt: new Date(deps.now()).toISOString(),
      settings,
      modules,
      queueProfiles,
      stacks: stackSummaries,
      instances,
      listeners,
      targetGroups,
      recordSets,
      packages,
      valuesFileSha256,
      preRunHealthStatus: health,
    };
    const body = JSON.stringify(capture);
    const key = captureKey(context);
    await deps.s3.putObject({ Bucket: bucket, Key: key, Body: body });

    const stored = (await reads.bucketObjects({ ...target, bucket, prefix: key }))
      .find((object) => object.key === key);
    if (stored === undefined) {
      return {
        ok: false,
        detail: `the before-state capture was written to s3://${bucket}/${key} but a read back did not find it`,
      };
    }
    return {
      ok: true,
      detail: [
        `captured s3://${bucket}/${key}`,
        `sha256=${sha256(body)}`,
        `settings=${settings.length}`,
        `modules=${modules.length}`,
        `queueProfiles=${queueProfiles.length}/${queueProfiles.filter((profile) => profile.enabled).length} enabled`,
        `stacks=${stacks.length}`,
        `instances=${instances.length}`,
        `running=${instances.filter((instance) => instance.state === "running").length}`,
        `listeners=${listeners.length}`,
        `targetGroups=${targetGroups.length}`,
        `recordSets=${recordSets.length}`,
        `packages=${packages.length}`,
        `valuesFile=${valuesFileSha256 === "absent" ? "absent" : `sha256:${valuesFileSha256.slice(0, 16)}`}`,
        `preRunHealth=${health ?? "not checked"}`,
      ].join("; "),
    };
  }
}

/** Configuration rows the migration owns, by the step that writes them. */
const CONTAINER_ENABLED_KEY = "ecs.enabled";
const CONTAINER_IMAGE_KEY = "ecs.image";
const SCHEDULER_DESIRED_KEY = "ecs.tasks.scheduler.desired";
const SCHEDULER_STABLE_NAME_KEY = "scheduler.use_stable_server_name";
const SCHEDULER_RETAIN_DNS_KEY = "scheduler.retain_dns_record";
const RETAIN_HOSTS_KEY = "ecs.retain_existing_hosts";

/** The tag value a scheduler compute node carries. */
const COMPUTE_NODE_TYPE = "compute-node";

/** The endpoint-routing boundaries, and whether each one keeps the legacy hosts. */
const ROUTING_STEPS: Readonly<Partial<Record<MigrationStepId, { moduleName: string; retainHosts: boolean }>>> = {
  CLUSTER_MANAGER_ROUTED: { moduleName: "cluster-manager", retainHosts: true },
  CLUSTER_MANAGER_LEGACY_REMOVED: { moduleName: "cluster-manager", retainHosts: false },
  VDC_ROUTED: { moduleName: "virtual-desktop-controller", retainHosts: true },
  VDC_LEGACY_REMOVED: { moduleName: "virtual-desktop-controller", retainHosts: false },
  SCHEDULER_ROUTED: { moduleName: "scheduler", retainHosts: true },
  SCHEDULER_LEGACY_REMOVED: { moduleName: "scheduler", retainHosts: false },
};

/** Where one operation's before-state capture lives. */
export function captureKey(context: Readonly<MigrationContext>): string {
  return `values/migration/${context.deploymentId}/before-state.json`;
}

/** Page one cluster table in full. */
async function scanTable(deps: Deps, tableName: string): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: tableName, ExclusiveStartKey: startKey });
    rows.push(...(page.Items ?? []));
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  return rows;
}

// ---------------------------------------------------------------------------------------------
// factory
// ---------------------------------------------------------------------------------------------

/**
 * Build the live migration dependencies.
 *
 * `awsRegion` resolves the region the command selected, because the durable
 * record store is created once and every other read takes its region from the
 * migration context.
 */
export function createLiveMigrateDeps(
  deps: Deps,
  awsRegion: () => string,
  awsProfile: () => string | undefined = () => process.env.AWS_PROFILE,
): MigrateDeps {
  const upgrade = createLiveUpgradeDeps(deps);
  const reads = liveMigrationAccountReads();

  return {
    stateObjects: liveMigrationStateObjects(awsRegion, awsProfile),
    steps: new LiveMigrationSteps({
      deps,
      reads,
      async clusterConfig(context) {
        return ClusterConfig.fromDynamoDb(context.clusterName, context.awsRegion, {
          moduleSet: context.moduleSet,
          scan: deps.scan,
        });
      },
      async driftInput(context) {
        return prepareUpgradeDriftInput(upgrade, {
          clusterName: context.clusterName,
          awsRegion: context.awsRegion,
          awsProfile: context.awsProfile,
          baseOs: context.targetBaseOs,
          modules: [...context.selectedModules],
        });
      },
      /**
       * The batch server's own state, from the host it runs on.
       *
       * It refuses rather than answers once the server is a task: a host reply at
       * that point would describe an instance that no longer serves the cluster,
       * and a stale answer to this question is worse than no answer. The steps
       * after the cutover wait on the container-side interface for that reason.
       */
      async schedulerState(context) {
        const config = await ClusterConfig.fromDynamoDb(context.clusterName, context.awsRegion, {
          moduleSet: context.moduleSet,
          scan: deps.scan,
        });
        if (config.getBool("ecs.enabled", false)) {
          throw new SchedulerStateUnreadableError(
            "the batch server runs as a task on this cluster, so its state is not readable from a host. This boundary waits on the container-side read interface recorded in docs/port/requests/migrate-live.md section 1.",
          );
        }
        const instanceId = config.getString(`${config.moduleId("scheduler")}.instance_id`, "");
        if (instanceId === "") {
          throw new SchedulerStateUnreadableError(
            `no scheduler instance is recorded for ${context.clusterName}, so the batch server's state cannot be read`,
          );
        }
        return readBatchServerState(
          liveSsmReadChannel({
            awsRegion: context.awsRegion,
            awsProfile: context.awsProfile,
            sleep: deps.sleep,
            clientOptions: awsClientOptions,
          }),
          instanceId,
        );
      },
      async trunkingEnabled(context) {
        const accountSettings = upgrade.ecsAccountSettings;
        if (accountSettings === undefined) {
          throw new GeneralException(
            "the container account-setting reader is not wired, so task network interface trunking cannot be checked",
          );
        }
        const settings = await accountSettings.listAccountSettings({
          awsRegion: context.awsRegion,
          effectiveSettings: true,
          name: "awsvpcTrunking",
        });
        return settings.some((setting) => setting.name === "awsvpcTrunking" && setting.value === "enabled");
      },
    }),
    uuid: deps.uuid,
    targetVersion: () => ideaVersion(),
    out: deps.out,
    now: deps.now,
  };
}
