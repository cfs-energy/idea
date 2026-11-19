/**
 * A read-only check of a cluster's deployed state: stacks, ECS services, load balancer targets, the
 * scheduler and bastion containers, and access denials from the cluster's own roles. The upgrade
 * runs it after its last deploy and `verify-cluster` runs it alone. It reads AWS APIs only, never
 * the cluster's endpoints, so it works when the VPN has dropped, and it changes nothing, so it is
 * safe to re-run.
 */

import type { Command } from "commander";

import { awsClientOptions } from "../aws-client-options.ts";
import { GeneralException } from "../../config/cluster-config.ts";
import { ExitWithCode, type Deps } from "../cdk-invoker.ts";
import { renderTable } from "./config.ts";

export interface EcsServiceState {
  name: string;
  running: number;
  desired: number;
  /** The primary deployment's rollout state. */
  rollout?: string;
  /** Every container image of the primary deployment's task definition. */
  images: string[];
}

export interface ContainerHealth {
  task: string;
  container: string;
  health: string;
}

export interface TargetGroupHealth {
  name: string;
  states: string[];
}

export interface AccessDeniedEvent {
  time: string;
  eventName: string;
  role: string;
  errorCode: string;
}

/** The reads behind the check. Each is a list or describe call; none writes. */
export interface ClusterHealthApi {
  services(input: { awsRegion: string; awsProfile?: string; cluster: string }): Promise<EcsServiceState[]>;
  /** The containers of the service's running tasks. */
  containerHealth(input: { awsRegion: string; awsProfile?: string; cluster: string; service: string }): Promise<ContainerHealth[]>;
  /** The target groups tagged with the cluster name, with each registered target's state. */
  targetGroups(input: { awsRegion: string; awsProfile?: string; clusterName: string }): Promise<TargetGroupHealth[]>;
  /**
   * Access denials since `since` by roles whose name starts with `rolePrefix`, reading at most
   * `maxEvents` management events, newest first. `complete` is false when the window held more.
   */
  accessDenied(input: { awsRegion: string; awsProfile?: string; since: Date; rolePrefix: string; maxEvents: number }): Promise<{
    events: AccessDeniedEvent[]; scanned: number; complete: boolean;
  }>;
}

export interface VerifyClusterDeps extends Pick<Deps, "scan" | "cfn" | "out" | "now"> {
  clusterHealth?: ClusterHealthApi;
}

export interface VerifyClusterOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  /** Start of the access-denial window, epoch milliseconds. */
  since?: number;
  /** A scoped upgrade's module ids: only their services are held to the release image. */
  modules?: readonly string[];
}

/** Events read from CloudTrail at most, 50 a page: ten pages at its two-calls-a-second limit. */
export const ACCESS_DENIED_MAX_EVENTS = 500;
const DEFAULT_WINDOW_MS = 60 * 60_000;
const STACK_OK = new Set(["CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"]);
const MODULE_TYPE_CONFIG = "config";
// a draining target is leaving after a rollout; an unused one sits behind a rule that is switched off
const TARGET_FAIL = new Set(["unhealthy", "initial"]);

type Result = "ok" | "FAIL" | "not checked";
type Row = [check: string, target: string, detail: string, result: Result];

async function scanRows(deps: Pick<Deps, "scan">, tableName: string): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await deps.scan({ TableName: tableName, ExclusiveStartKey: startKey, ConsistentRead: true });
    rows.push(...(page.Items ?? []));
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  return rows;
}

const message = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/** `repo:tag@sha256:...` and `repo:tag` both name `repo`; a registry port is not a tag. */
export function imageRepository(image: string): string {
  const name = image.split("@")[0] ?? image;
  const slash = name.lastIndexOf("/");
  const colon = name.lastIndexOf(":");
  return colon > slash ? name.slice(0, colon) : name;
}

function isAccessDeniedError(error: unknown): boolean {
  const name = (error as { name?: string }).name ?? "";
  return /AccessDenied|UnauthorizedOperation|NotAuthorized/i.test(name);
}

/** Prints the table and returns whether every checked line is ok. */
export async function verifyCluster(deps: VerifyClusterDeps, options: VerifyClusterOptions): Promise<boolean> {
  const rows: Row[] = [];
  const settings = new Map((await scanRows(deps, `${options.clusterName}.cluster-settings`)).map((row) => [String(row["key"]), row["value"]]));
  const modules = (await scanRows(deps, `${options.clusterName}.modules`))
    .filter((row) => row["type"] !== MODULE_TYPE_CONFIG && row["status"] === "deployed");
  const context = { awsRegion: options.awsRegion, awsProfile: options.awsProfile };

  for (const module of modules) {
    const stackName = typeof module["stack_name"] === "string" && module["stack_name"] !== ""
      ? module["stack_name"] : `${options.clusterName}-${String(module["module_id"])}`;
    try {
      const stack = await deps.cfn.describeStack(stackName);
      const status = stack.StackStatus ?? "unknown";
      rows.push(["stack", stackName, status, STACK_OK.has(status) ? "ok" : "FAIL"]);
    } catch (error) {
      rows.push(["stack", stackName, message(error), "FAIL"]);
    }
  }

  const api = deps.clusterHealth;
  const cluster = settings.get("ecs.cluster_name");
  const image = settings.get("ecs.image");
  if (api === undefined || typeof cluster !== "string" || cluster === "") {
    rows.push(["ecs services", "-", api === undefined ? "no ECS reader in this build" : "ecs.cluster_name is not set", "not checked"]);
  } else {
    try {
      const repository = typeof image === "string" ? imageRepository(image) : undefined;
      // services are named <cluster>-<module id>[-<component>]; the longest matching id owns one
      const owner = (name: string): string | undefined => modules.map((row) => String(row["module_id"]))
        .filter((id) => name === `${options.clusterName}-${id}` || name.startsWith(`${options.clusterName}-${id}-`))
        .sort((a, b) => b.length - a.length)[0];
      for (const service of await api.services({ ...context, cluster })) {
        const ours = repository === undefined ? [] : service.images.filter((candidate) => imageRepository(candidate) === repository);
        const selected = !options.modules?.length || options.modules.includes(owner(service.name) ?? "");
        const imageOk = ours.every((candidate) => candidate === image);
        const counts = `${service.running}/${service.desired} running, rollout ${service.rollout ?? "unknown"}`;
        const imageText = ours.length === 0 ? "no release image" : imageOk ? "release image"
          : `image ${ours.find((candidate) => candidate !== image)}${selected ? "" : ", not in this upgrade"}`;
        const ok = service.running === service.desired && service.rollout === "COMPLETED" && (imageOk || !selected);
        rows.push(["ecs service", service.name, `${counts}, ${imageText}`, ok ? "ok" : "FAIL"]);
      }
    } catch (error) {
      rows.push(["ecs services", cluster, message(error), "FAIL"]);
    }
    for (const module of modules.filter((row) => row["name"] === "scheduler" || row["name"] === "bastion-host")) {
      const service = `${options.clusterName}-${String(module["module_id"])}`;
      try {
        const containers = await api.containerHealth({ ...context, cluster, service });
        // UNKNOWN is a container with no health check (the log sidecars); each task still needs one HEALTHY
        const healthy = containers.filter((container) => container.health === "HEALTHY");
        const blind = new Set(containers.map((container) => container.task).filter((task) => !healthy.some((container) => container.task === task)));
        const sidecar = (container: ContainerHealth): boolean => container.health === "UNKNOWN" && !blind.has(container.task);
        const unhealthy = containers.filter((container) => container.health !== "HEALTHY" && !sidecar(container));
        const unchecked = [...new Set(containers.filter(sidecar).map((container) => container.container))];
        const ok = containers.length > 0 && unhealthy.length === 0 && blind.size === 0;
        const detail = containers.length === 0 ? "no running task" : [
          ...unhealthy.map((container) => `${container.container} ${container.health}`),
          ...(blind.size > 0 ? [`no HEALTHY container in ${blind.size} task(s)`] : []),
          ...(ok ? [`${healthy.length} container(s) HEALTHY`] : []),
          ...(unchecked.length > 0 ? [`unchecked (no health check): ${unchecked.join(" ")}`] : []),
        ].join(", ");
        rows.push(["container health", service, detail, ok ? "ok" : "FAIL"]);
      } catch (error) {
        rows.push(["container health", service, message(error), "FAIL"]);
      }
    }
  }

  if (api === undefined) {
    rows.push(["target groups", "-", "no load balancer reader in this build", "not checked"]);
  } else {
    try {
      const groups = await api.targetGroups({ ...context, clusterName: options.clusterName });
      if (groups.length === 0) rows.push(["target groups", options.clusterName, "none tagged with the cluster name", "not checked"]);
      for (const group of groups) {
        const counts = new Map<string, number>();
        for (const state of group.states) counts.set(state, (counts.get(state) ?? 0) + 1);
        const detail = counts.size === 0 ? "no targets" : [...counts].map(([state, count]) => `${state} ${count}`).join(", ");
        rows.push(["target group", group.name, detail, group.states.some((state) => TARGET_FAIL.has(state)) ? "FAIL" : "ok"]);
      }
    } catch (error) {
      rows.push(["target groups", options.clusterName, message(error), "FAIL"]);
    }
  }

  // The lookup is evidence, not a gate on its own: no permission or no answer reads "not checked".
  const since = new Date(options.since ?? deps.now() - DEFAULT_WINDOW_MS);
  const rolePrefix = `${options.clusterName}-`;
  if (api === undefined) {
    rows.push(["access denied", `${rolePrefix}* roles`, "no CloudTrail reader in this build", "not checked"]);
  } else {
    try {
      const found = await api.accessDenied({ ...context, since, rolePrefix, maxEvents: ACCESS_DENIED_MAX_EVENTS });
      const window = `${found.scanned} event(s) since ${since.toISOString()}${found.complete ? "" : `, the newest ${ACCESS_DENIED_MAX_EVENTS} only`}`;
      const detail = found.events.length === 0 ? `none in ${window}`
        : `${found.events.length} in ${window}: ${found.events.slice(0, 3).map((event) => `${event.role} ${event.eventName} ${event.errorCode} at ${event.time}`).join("; ")}`;
      rows.push(["access denied", `${rolePrefix}* roles`, detail, found.events.length === 0 ? "ok" : "FAIL"]);
    } catch (error) {
      const why = isAccessDeniedError(error) ? "no permission for cloudtrail:LookupEvents" : message(error);
      rows.push(["access denied", `${rolePrefix}* roles`, why, "not checked"]);
    }
  }

  const failed = rows.filter((row) => row[3] === "FAIL").length;
  deps.out(`Cluster verification for ${options.clusterName}\n${renderTable(["check", "target", "detail", "result"], rows)}`);
  deps.out(failed === 0 ? "Cluster verification: every checked line is ok" : `Cluster verification: ${failed} line(s) FAIL`);
  return failed === 0;
}

export function registerVerifyClusterCommand(program: Command, deps: VerifyClusterDeps): void {
  program
    .command("verify-cluster")
    .description("read-only check of a cluster's stacks, services, targets and access denials")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .option("--since-minutes <minutes>", "Read access denials from this many minutes back. Default: 60", "60")
    .action(async (options: VerifyClusterOptions & { sinceMinutes: string }) => {
      const minutes = Number(options.sinceMinutes);
      if (!Number.isFinite(minutes) || minutes <= 0) throw new GeneralException(`--since-minutes must be a positive number, not ${options.sinceMinutes}`);
      const healthy = await verifyCluster(deps, { ...options, since: deps.now() - minutes * 60_000 });
      if (!healthy) throw new ExitWithCode(1);
    });
}

/** The live reads, each loaded only when the check reaches it. */
export function liveClusterHealth(): ClusterHealthApi {
  return {
    async services(input) {
      const ecs = await import("@aws-sdk/client-ecs");
      const client = new ecs.ECSClient(await awsClientOptions(input.awsRegion, input.awsProfile));
      const arns: string[] = [];
      let nextToken: string | undefined;
      do {
        const page = await client.send(new ecs.ListServicesCommand({ cluster: input.cluster, nextToken }));
        arns.push(...(page.serviceArns ?? []));
        nextToken = page.nextToken;
      } while (nextToken);
      const images = new Map<string, string[]>();
      const result: EcsServiceState[] = [];
      for (let index = 0; index < arns.length; index += 10) {
        const page = await client.send(new ecs.DescribeServicesCommand({ cluster: input.cluster, services: arns.slice(index, index + 10) }));
        for (const service of page.services ?? []) {
          const primary = service.deployments?.find((deployment) => deployment.status === "PRIMARY");
          const taskDefinition = primary?.taskDefinition ?? service.taskDefinition;
          if (taskDefinition !== undefined && !images.has(taskDefinition)) {
            const described = await client.send(new ecs.DescribeTaskDefinitionCommand({ taskDefinition }));
            images.set(taskDefinition, (described.taskDefinition?.containerDefinitions ?? []).flatMap((container) => container.image ?? []));
          }
          result.push({
            name: service.serviceName ?? "unknown",
            running: service.runningCount ?? 0,
            desired: service.desiredCount ?? 0,
            rollout: primary?.rolloutState,
            images: taskDefinition === undefined ? [] : images.get(taskDefinition) ?? [],
          });
        }
      }
      return result;
    },
    async containerHealth(input) {
      const ecs = await import("@aws-sdk/client-ecs");
      const client = new ecs.ECSClient(await awsClientOptions(input.awsRegion, input.awsProfile));
      const tasks = (await client.send(new ecs.ListTasksCommand({ cluster: input.cluster, serviceName: input.service, desiredStatus: "RUNNING" }))).taskArns ?? [];
      if (tasks.length === 0) return [];
      const described = await client.send(new ecs.DescribeTasksCommand({ cluster: input.cluster, tasks: tasks.slice(0, 100) }));
      return (described.tasks ?? []).flatMap((task) => (task.containers ?? []).map((container) => ({
        task: task.taskArn ?? "unknown", container: container.name ?? "unknown", health: container.healthStatus ?? "UNKNOWN",
      })));
    },
    async targetGroups(input) {
      const tagging = await import("@aws-sdk/client-resource-groups-tagging-api");
      const elb = await import("@aws-sdk/client-elastic-load-balancing-v2");
      const options = await awsClientOptions(input.awsRegion, input.awsProfile);
      const tags = new tagging.ResourceGroupsTaggingAPIClient(options);
      const client = new elb.ElasticLoadBalancingV2Client(options);
      const arns: string[] = [];
      let token: string | undefined;
      do {
        const page = await tags.send(new tagging.GetResourcesCommand({
          ResourceTypeFilters: ["elasticloadbalancing:targetgroup"],
          TagFilters: [{ Key: "idea:ClusterName", Values: [input.clusterName] }],
          PaginationToken: token,
        }));
        arns.push(...(page.ResourceTagMappingList ?? []).flatMap((resource) => resource.ResourceARN ?? []));
        token = page.PaginationToken || undefined;
      } while (token);
      const result: TargetGroupHealth[] = [];
      for (const arn of arns) {
        const health = await client.send(new elb.DescribeTargetHealthCommand({ TargetGroupArn: arn }));
        result.push({
          name: arn.split("/").slice(-2, -1)[0] ?? arn,
          states: (health.TargetHealthDescriptions ?? []).map((target) => target.TargetHealth?.State ?? "unknown"),
        });
      }
      return result;
    },
    async accessDenied(input) {
      const trail = await import("@aws-sdk/client-cloudtrail");
      const client = new trail.CloudTrailClient(await awsClientOptions(input.awsRegion, input.awsProfile));
      const events: AccessDeniedEvent[] = [];
      let scanned = 0;
      let nextToken: string | undefined;
      // a NextToken is only valid with the parameters of the first page
      const endTime = new Date();
      do {
        const page = await client.send(new trail.LookupEventsCommand({ StartTime: input.since, EndTime: endTime, MaxResults: 50, NextToken: nextToken }));
        for (const event of page.Events ?? []) {
          scanned += 1;
          const record = JSON.parse(event.CloudTrailEvent ?? "{}") as {
            errorCode?: string; eventName?: string; eventTime?: string;
            userIdentity?: { sessionContext?: { sessionIssuer?: { userName?: string } } };
          };
          const role = record.userIdentity?.sessionContext?.sessionIssuer?.userName ?? "";
          if (!role.startsWith(input.rolePrefix) || !/AccessDenied|UnauthorizedOperation/i.test(record.errorCode ?? "")) continue;
          events.push({ time: record.eventTime ?? "", eventName: record.eventName ?? "", role, errorCode: record.errorCode ?? "" });
        }
        nextToken = page.NextToken;
      } while (nextToken && scanned < input.maxEvents);
      return { events, scanned, complete: nextToken === undefined };
    },
  };
}
